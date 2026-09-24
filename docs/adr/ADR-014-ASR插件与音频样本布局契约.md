# ADR-014：ASR 插件、音频样本布局与段描述符的跨进程交接

**状态：** Accepted（2026-09-23）
**上游决策：** ADR-008（Apple Silicon 一等目标）、ADR-009（媒体格式矩阵与显式拒绝）、ADR-010（跨进程数据面安全边界）、ADR-011（保留窗口按种类分配）、ADR-012（模型插件与端侧推理边界）
**相关文档：** [实现状态](../implementation-status.md)、[验证记录](../verification.md)、[契约](../contracts/README.md)、[待办](../TODO.md)

---

## 1. 背景与问题

ADR-012 用 VLM 回答"插件怎么拿到视频帧、怎么被信任"。ASR（音频段 → 文本）**不能照抄**这个答案，
因为音频与视频在三处不同：

1. **字节怎么解释**取决于样本布局。视频帧有 `pixel_format`（`RGBA`），音频此前只有 `sample_rate`
   与 `channels`——**没有宽度**。少了布局，"这段字节是 F32LE 还是 S16LE"就只能猜，而猜错会
   **静默改变模型看到的内容**（同样的字节被读成另一条波形）。
2. **插件消费的不是解码样本，而是切段后的段**。M1/M2 之后 `audio_segment` 只是报告里的一个条目：
   它从未进过跨进程保留表。也就是说"段"在报告里存在，但在数据面上**不存在**。
3. **端侧 ASR 的模型身份来自一个权重文件**，不是"模型服务报告的 digest"。文件摘要必须现场复算，
   并且要拒绝那些"摘要能算出来、但加载器读不了"的权重。

本决策用 **Apple Silicon 原生的 `mlx-whisper` + `mlx-community/whisper-large-v3-turbo`**
作为第二个真实端侧模型接入（`plugins/python/processors/asr-whisper-mlx`），把上述三条落成规则。
选它的理由：ADR-008 把 Apple Silicon（含 Mac mini 家庭工作站）定为一等目标，`mlx` 有 macOS arm64
轮子，本机实测单段 5 秒音频推理 0.4–0.8 s。**这一条只承诺"接真实 ASR 模型"，
不承诺"转写可用"**——见 §5 与 §7。

---

## 2. 决策一：音频样本布局进契约，未知布局显式丢弃而不是猜宽度

- `common/v1/common.proto` 的 `BufferFormat` 与 `media/v1/media.proto` 的 `AudioSegment`
  各补一个 `string sample_format`：**空串只表示"未知"**，读者不得假设任何宽度或字节序。
- 解码链只承认一种布局：`crates/media/src/segment.rs` 的
  `AUDIO_SAMPLE_FORMAT = "F32LE"`，且它同时是链上 capsfilter（`audioconvert ! audio/x-raw,
  format=F32LE`）的取值——**契约里的常量与归一化链上的取值是同一个标识符**，不允许两处各写一份字面量。
- `AudioSegmenter::push()` 收到非 `F32LE` 的布局时**不解释字节**：`dropped_samples += 1`、
  `drop_reasons.insert("audio_unsupported_sample_format")`、返回 `Ok(None)`。既没有"默认宽度"，
  也没有"按 4 字节/样本先读读看"。
- 段描述符写出的 `sample_format` 是**实测值**——能走到 `emit_segment()` 的段已经在 segmenter 里
  被核实为 `F32LE`，不是"填个默认值"。
- 插件侧对应 `plugins/python/processors/asr-whisper-mlx/src/.../audio.py`：`frames_from_f32le()`
  只认 `F32LE`，否则以稳定原因串失败（`unsupported_sample_format:<值|unset>`）；空载荷
  （`empty_audio_payload`）、非整帧对齐（`audio_payload_not_frame_aligned`）、声道数未知
  （`audio_channel_count_unknown`）同样是**显式失败**，不是"安静"。

多声道与重采样也是契约的一部分，因为它们会改变模型看到的波形：多声道按**算术平均**下混成单声道
（确定性的取值，而不是"取第一路"这种随实现而变的选择）；采样率用多相重采样
（`scipy.signal.resample_poly`）换算到 Whisper 的 16 kHz 入口，不做零阶保持那种会引入镜像频谱的取巧。
两者都写成**可对账的输入事实**回传（`input_samples` / `whisper_samples` 等），而不是只留在日志里。

## 3. 决策二：音频段描述符必须和逐样本 buffer 一样进跨进程保留表

这是本轮**暴露出来的真实缺陷**：`crates/media/src/decode.rs` 的 `emit_segment()` 只调用了
`hand_off()`——它做的是"分配 arena → 签发 lease → 校验 descriptor → 释放 lease"，全部**在本进程内**
完成，只够自证；跨进程交接靠的是 `BufferHandoff::retain_or_reject()`（ADR-010/011 的保留表）。
每一条 `audio_pcm` 都调了两者，而段只调了前者。后果是：

- `retained_by_kind` 里永远没有 `audio_segment`，插件端读不到任何一段音频，
  worker 只会报 `no_audio_segment_buffer_to_process`；
- 报告里 `segments=4`、`descriptors=1012` 一切正常——**报告正常并不证明数据面有这条输入**。

修法：`emit_segment()` 对段描述符补上 `retain_or_reject()`，与 `process_sample()` 同构。
这条缺陷有一个可复现的 A/B 单测（`audio_segments_reach_the_cross_process_retained_table`：
注释掉 `retain_or_reject()` 必红）。

**下游会计口径随之修正**：`tools/verify_handoff.py` 原先断言"数据面 offer 的 buffer 数 ==
解码样本数"，段进表后不再成立（`video/1.mp4` 实测 `offered=1354` vs `samples=1347`，差 7 就是段数）。
现在的基准是报告亲手交接过的 descriptor 数，并要求
`decoded.descriptors_built == Σtrack.samples + audio_segments.segments`——
**这个差必须被报告解释，不能当成误差抹掉**。

**代价（必须知道）**：段的保留槽位受 ADR-011 的**单一种类上限**约束
（`retained_kind_limit = max(1, retained_limit/2)`，默认 32 条表 → 单类 16 条）。
按默认 5 秒切段，**单类上限 = 80 秒音频**；更长的直播要么提高 `retained_limit`（上限 4096），
要么由消费者持续领取。这条限制不写清楚，就会在长直播上表现为"音频段莫名丢失"
（实际是 `handoff_kind_quota_full`，已计入背压报告）。

## 4. 决策三：本地权重文件的模型身份，必须现场复算并验证可加载性

ADR-012 §2 的规则是"身份来自模型服务实测"。本地权重文件没有服务可问，因此规则改为：

- 摘要来自**即将加载的那个权重文件**：按 `weights.safetensors` → `weights.npz` 的顺序取
  （与 `mlx_whisper.load_model` 的取值顺序一致），逐块算 SHA-256；
- **真读一次容器头**（`probe_weight_container()`）：safetensors 校验长度前缀 + JSON 头，
  npz 校验 zip 目录；损坏、截断、空头都**在 Start 就拒绝**，而不是等第一次推理才炸；
- **校验 `config.json` 的维度字段**能对上加载器的 `ModelDimensions`（排除 `model_type` /
  `quantization` 这类非维度键），不一致报 `model_config_not_loadable`——摘要正确但加载器读不了的
  权重同样不是可用模型；
- `releaseId` 用 `mlx-whisper:<model>@<权重摘要前 12 位>`，`executionBackend` 来自实际导入的
  `mlx` 版本（`mlx-0.32.2`）；`language=null` 表示"让模型自己判"，**不是**默认英语。

验收脚本独立用 `huggingface_hub.snapshot_download` + 自己算一遍摘要与
`provenance.modelArtifactDigest` 比对——**不调用插件代码**，否则就是自己验证自己。

## 5. 决策四：解码诊断量不是校准置信度，必须显式区分

Whisper 会给 `avg_logprob` / `no_speech_prob` / `compression_ratio` / `temperature`。
它们**不是**校准置信度（没有校准过程，也不满足"同分布测试集上的可靠概率"这一条件），
因此：

- `confidence` **留空**，`confidence_unavailable_reason = model_does_not_report_calibrated_confidence`；
- 四个诊断量**原样**进 `payload.segments[]`（不换算、不归一化、不截断），缺哪个写 `null`；
- 明确写下它们**不是**置信度，避免下游把它们当概率用。

这条规则的价值在验收里立刻显现：同一素材的两个 5 秒窗口，一个给出正常句子
（`compression_ratio=0.949`、`temperature=0.0`），另一个给出**重复退化**
（`compression_ratio=22.2`、`temperature=1.0`）。**没有置信度字段可读，但诊断量足以让消费者
筛掉退化段**——这就是"显式未知 + 原始诊断"的用途。

## 6. 决策五：子段时间 = 窗口起点 + 模型相对时间，换算方式写在结果里

- observation 的 `time_range` 就是**源音频段自己的半开区间**（`timing_source=media_pts`），
  不按模型耗时或墙钟重算；
- 模型给的子段 `start`/`end` 是**窗口内相对时间**，写进时间轴时加窗口起点，并在 payload 里写明
  `segment_timing=media_pts_window_relative_plus_window_start`——换算方式属于结果的一部分，
  不能留给读者猜；
- **子段可能越窗，不能假设模型守规矩**：Whisper 退化时会给出越出窗口的时间戳（实测 5 秒窗口上
  `[940, 29880]`）。越窗是**观察结果**而不是要消掉的异常：原值保留、`timing_outside_window=true`
  逐个标记、`segments_outside_window` 计数；**不夹取**（夹取会让越界的时序看起来像测得值）、
  **不丢弃**（文本是模型真实输出）。observation 的锚点仍然是**源段区间**——越窗的是子段时序，
  不是这段字节的来源。注意"越窗意味着段落到了别的段的字节上"这个直觉在这里**不成立**：
  它只说明时间戳不可信，字节来源仍由 lease 窗口决定；
- 空转写**不是失败**（静音段上这是正确结果），但必须说清是哪一种空：
  `empty_transcript_reason = model_returned_no_segments | model_returned_empty_text`；
  而超出上限的文本（`MAX_TRANSCRIPT_CHARS`）按失败上报，**不静默截断**成一个看起来正常的转写。

---

## 7. 后果

### 收益

- 第二个真实端侧模型接入：**四个进程**（编排 / 生产者 runtime / 插件 / worker），
  `make asr-check MEDIA=video/samples/screencast-video2commons.480p.vp9.webm` 在授权样本上通过
  （修复越窗语义后重跑一轮仍通过，74.5 s）。

  | 观察项 | 实测值（2 段） |
  | --- | --- |
  | 段锚点 | `[0,5015)`、`[5015,10015)`，`timing_source=media_pts` |
  | 子段越窗 | `timing_outside_window=false` ×2、`segments_outside_window=0`（越窗路径由契约测试覆盖，本轮真实运行未触发） |
  | `content_hash` | = 源段 lease 窗口摘要（逐位相等） |
  | `modelArtifactDigest` | `sha256:951ed3fc…`（= 独立复算的权重摘要，1,613,977,612 B） |
  | `executionBackend` | `mlx-0.32.2` |
  | 输入事实 | `sample_format=F32LE`、48 kHz 立体声 → 16 kHz 单声道（240648 → 80216 样本） |
  | `confidence` | 缺省 + `model_does_not_report_calibrated_confidence` |
  | 单段端到端耗时 | 1321.1 ms（首次）/ 783.6 ms |
  | 账目 | `during: retained_by_kind={audio_pcm:16, audio_segment:4, video_frame:8}`；`after: released_total=28 retained=0 expired=0 retained_total=28`、`arena_live_slabs=0` |
  | 消费者归还 | `drain.discarded=26 failures=[]` |

- `make handoff-check` 的会计口径随之修正（见 §3），M8 的 `make model-check` 不受影响。

### 验收暴露的真实缺陷（本轮修掉，不是"一次就过"）

| # | 现象 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | worker 报 `no_audio_segment_buffer_to_process`；`retained_by_kind` 里没有段 | `emit_segment()` 只做进程内 `hand_off()`，从未 `retain_or_reject()` | 段描述符补上保留表交接；A/B 单测锁死 |
| 2 | 数据面 offer 数与解码样本数对不上（1354 vs 1347） | 会计口径仍按"样本数"，段进表后不再等价 | 基准改为 `descriptors_built`，并要求它与样本数 + 段数一致 |
| 3 | `AssertionError: runtime exited before 'handoff_stats'` | 验收脚本先 kill 生产者，再去读它退出时才打印的对账行 | 照 M8 顺序：先摘插件 → `producer.finish()` → 读 `handoff_stats` |
| 4 | 泄漏检查报"worker 携带了段名"（恰好 2 次） | 该检查把单条 observation 的 payload 当成了 worker 报告 | 对 worker 的整份报告查一次（`segment_name_hint` 是 worker 写的字段） |
| 5 | 验收断言 `sub-segment [940, 29880] leaves its own window [0, 5015]` | 脚本把"子段落在窗口内"写成了硬断言，等于假设模型守规矩；实测到 Whisper 幻觉时的越窗时间戳 | 语义改为不夹取不丢弃：`timing_outside_window` + `segments_outside_window`（见 §6），验收改判"越窗必须被标记且计数一致"，契约测试锁死（含窗口内**不得**被标记的反向断言） |

### 代价与已知限制

- **转写质量未验收**：本 ADR 只保证链路语义正确，不保证转写可用；没有 WER/CER 度量，
  验收里已实测到一次重复退化（见 §5），且同一素材换一轮跑文本会变（见 §6 的越窗轮）。
- 段是**固定 5 秒切分**，没有静音切分、没有说话人对齐，一个段可能横跨多个说话人；
  窗口边界会切在词中间。
- 单段串行（`maxConcurrency=maxBatchSize=1`），吞吐受单段延迟限制；模型权重约 1.6 GB，
  首次冷缓存需要下载（插件把 `~/.cache/huggingface` 列为唯一可写路径，其余只读）。
- 后端是 **Apple Silicon 专属**（`mlx`）；Linux/NVIDIA 侧需要另选后端（例如 faster-whisper /
  TensorRT），本节不承诺该路径可用。
- `local_native` 插件**未签名**（manifest 只写 `signatureUnavailableReason`）。

**最小实现清单（当前状态）**

- [x] `plugins/python/processors/asr-whisper-mlx/`：`plugin.py`（权重身份 / 显式置信度 / 锚点与子段换算）、
      `audio.py`（布局准入、下混、重采样）、`artifact.py`、`server.py`、`plugin.yaml`、`config.schema.json`
- [x] `crates/media/src/segment.rs`（`AUDIO_SAMPLE_FORMAT` + 未知布局显式丢弃）、
      `crates/media/src/decode.rs`（段描述符 `retain_or_reject()` + `sample_format` 上报）
- [x] `proto/common/v1/common.proto`、`proto/media/v1/media.proto`（`sample_format`）与重跑 `make proto`
- [x] `tools/verify_asr.py`（四进程验收，独立复算权重摘要）、`tools/verify_handoff.py`（会计口径）、
      `tools/ai_worker.py`（`--input-kind` / `--plugin-config`）、`Makefile` 的 `asr-check`
- [x] `tests/contracts/test_asr_plugin_contract.py`（20 项）、
      `tests/contracts/test_media_contract.py`（`sample_format` 显式未知）

**未验证范围（不得当作已完成）**

- 只在本机 `macos-aarch64` 上验收；`linux-x86_64`、Mac mini / 跨机未验证；
  `mlx` 的 Apple Silicon 定位意味着 ASR 的 Linux 路径需要另一套后端与另一轮验收。
- OCR / BGE 未接入；CoreML / Metal 仍 `execution_backend_not_implemented`（该条与
  [ADR-022](ADR-022-宿主加速器能力探测与上报.md) 的 `host_accelerators` 是两张表：前者说
  "本进程不执行推理"，后者说"这台宿主有没有"）。
- 没有取消（中途打断推理）、超时、崩溃后 lease 回收的端到端样本。
- `golden_path_verified` 恒为 false。
