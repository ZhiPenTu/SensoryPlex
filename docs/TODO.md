# 待办与未完成范围

本文件是 0.1.0 之后的工作队列。规则：

- 一项只有**真实执行并通过对应验收**后才标记完成，并把证据写进 `docs/verification.md`；
  健康检查成功、跳过的测试、合成素材都不算证据（AGENTS.md）。
- 完成一项时同时更新 `docs/implementation-status.md`，避免"文档说完成、代码没实现"。
- 未实现的能力必须继续出现在 `ReplayReport.blockers` 与 `DescribeCapabilities.unavailable_reason` 中。

媒体格式的"支持 / 不支持"以 **[ADR-009：媒体格式支持矩阵与拒绝语义](adr/ADR-009-媒体格式支持矩阵与拒绝语义.md)**
为准：矩阵之外的格式一律显式拒绝，新增一个格式 = 矩阵一行 + 1 正样本 + 1 拒绝样本 + 一条验证记录。

状态：`未开始` / `进行中` / `已完成（证据见 …）`

## 0. 当前基座（已完成，作为其他模块的起点）

- [x] 工程底座、Proto 契约、迁移、Gateway 鉴权与查询（证据：`docs/verification.md` 顶部表）
- [x] Runtime 能力上报与 Apple Silicon 平台口径（ADR-008）
- [x] 媒体锚点路径：ffprobe 半开区间、重排计数、显式丢弃原因
- [x] 媒体解码路径：GStreamer 解码 → 有界 arena → `BufferDescriptor` → lease 签发/校验/释放 → 音频 5 秒切段
      （证据：`video/1.mp4` 与 `video/samples/` 的公开许可样本——该轮 6 个 VP9/Opus，现共 10 个，
      见 `tests/fixtures/media/OPEN-SAMPLES.md`）
- [x] 视频自适应抽帧（M1）：进入 arena 前判定，keep/skip 全部带原因，保留率随内容自适应
      （证据：`docs/verification.md` "M1 自适应抽帧：接线与真实样本覆盖率"）
- [x] 跨进程数据面（M3）：Runtime 保留字节 + 独立进程按 lease 读取/校验/释放，容量与 lease 生命周期有上限
      （证据：`docs/verification.md` "M3 跨进程数据面：lease 消费方与真实交接"；契约见 ADR-010）
- [x] SRT 实时接入（M4）：`ingest` 在有限窗口内拉流、解码、测量断流与恢复，重连归解码元素
      （证据：`docs/verification.md` "M4 SRT 实时接入：真实直推、断流恢复与实时数据面"）
- [x] 背压与队列可观察（M2）：保留表/arena 深度、丢弃原因与种类、lease 等待时间以计数 + 原因暴露；
      降级先于拒绝，且单一种类不能独占保留窗口
      （证据：`docs/verification.md` "M2 背压与队列可观察：直播实测、两条恒等式与按种类分配"；决策见 ADR-011）

## 1. 其他模块（先做这些，再做优化）

### M1 抽帧（adaptive sampler）

- 状态：**已完成**（证据见 `docs/verification.md` "M1 自适应抽帧：接线与真实样本覆盖率"）。
- 结果：视频帧在进入 arena 前判定，声明 keep（首帧/内容变化/静止心跳）或带原因的 skip；
  `adaptive_sampling_not_implemented` 已从 `blockers` 移除；7 个真实样本（含 `video/1.mp4`）全部通过，
  静止类保留率 0.35%、翻页类 0.87–0.96%、运动类 1.78%。
- 仍未验证（不要当成已完成）：覆盖率是**帧数**口径；M8 已接入 VLM，但抽帧的**语义**覆盖
  （被跳过的帧是否漏掉语义变化）仍没有用模型输出度量；样本仍全是 CFR，VFR 下的抽帧语义没有样本。
- 注意（保持有效）：不得把"静止段跳过"实现成静默丢帧——跳过必须可计数、可解释。

### M2 背压与队列可观察

- 状态：**已完成**（证据见 `docs/verification.md` "M2 背压与队列可观察：直播实测、两条恒等式与按种类分配"；
  设计决策见 [ADR-011](adr/ADR-011-保留窗口按种类分配.md)）。
- 结果：`BackpressureReport`（契约 `media/v1/media.proto`）装配在 `DecodedDataPlane.backpressure`，
  给出三条有界队列 `handoff_retained_table`/`handoff_retained_kind`/`handoff_arena_bytes` 的
  深度/峰值/容量、`state`（`ok|degraded|saturated`）、`degraded_entries`/`saturated_entries`、
  按原因与按种类的丢弃（`dropped_total == Σ drop_reasons == Σ drop_kinds`）、lease 等待时间
  （`timeouts_total`/`residency_*`）以及降级抑制的 keep 数（`sampling_throttled_samples`）。
  处理顺序是**先降级、再拒绝**；保留表分两层上限，单一种类最多占一半（`retained_kind_limit`）。
  `tools/verify_backpressure.py` 4 场景（运动饱和 / 静止填表 / 消费方测等待 / 无保留控制）全部通过；
  用户 OBS 直播实测（10–20 秒窗口）得到非零指标，且消费者真拿到视频帧（`video_buffers=3`）。
- 仍未验证（不要当成已完成）：GStreamer `queue` 与 `appsink max_buffers` **没有计数出口**，
  不在报告内；只在本机回环与 `macos-aarch64` 验收；未验证小时级长直播与唯一 kind 长期贴住配额的尾延迟；
  单一种类流只能用一半窗口是显式接受的代价。模型 worker 已在 M8 接入 VLM 插件（见下方 §M8），
  但本节结论只覆盖保留表与 arena，不因此改变。

### M3 lease 消费方（跨进程数据面）

- 状态：**已完成**（证据见 `docs/verification.md` "M3 跨进程数据面：lease 消费方与真实交接"）。
- 结果：`sensoryplex-runtime replay --handoff-listen` 把样本留在 POSIX 共享内存里，独立进程
  `tools/handoff_worker.py` 经 `BufferHandoffService` 领窗口、读字节、校验摘要、显式释放；
  `lease_consumer_not_implemented` 已从 `blockers` 移除（`verify_replay.py` 的 `EXPECTED_BLOCKERS` 收紧为空集）。
  三个样本（`video/1.mp4`、`sasebo-basketball`、`officehours-panel`）× 两个场景全部通过：
  越界/非法窗口/重复领取/迟到释放/过期 TTL 各得到稳定拒绝码，
  `offered == retained_total + retain_rejections`、`retained_total == retained + released + expired`、
  `arena_live_slabs == 0`，且 `offered` 与 `ReplayReport` 的交接样本数一致。
- 仍未验证（不要当成已完成）：本节的消费方仍是**验收脚本** `handoff_worker.py`；模型消费方见 §M8
  （`tools/ai_worker.py` + 插件），但那只证明 lease 路径可承载模型，不改变本节结论；
  同 UID 进程间没有逐 buffer 内存隔离（lease 不是隔离，见 ADR-010）；只在本机回环验证过，
  跨主机不适用；`macos-aarch64` 之外未验收；未验证长时间运行的段清理与强杀后的段残留。
- 注意（保持有效）：一次没有 `--handoff-listen` 的 replay 必须继续报 `handoff_state=not_exercised`，
  不得被读成"数据面已验证"。

### M4 SRT 接入与断流重连

- 状态：**已完成**（证据见 `docs/verification.md` "M4 SRT 实时接入：真实直推、断流恢复与实时数据面"）。
- 结果：新增契约 `media/v1/live.proto`（`StreamStall` / `LiveStreamStats` / `LiveIngestReport`）与
  `sensoryplex-runtime ingest <pipeline.yaml> --report <report.pb>`；URI 只从 pipeline 的
  `uri_secret_ref` 派生出的环境变量读，命令行/日志/报告里只有引用名。
  `make live-check` 自己用 GStreamer `srtsink` 直推授权样本（不经 RTMP 转封装、不占用 OBS），
  四个场景全部通过：稳定窗口（samples=963、stalls=0、blockers 为空）、断流恢复
  （stalls=1、stalled_ms=4723、recovered=true）、无源（exit 1，`live_window_produced_no_samples`）、
  实时数据面交接（独立进程 63 项检查通过、账目对得上）。
- 重连归属：重连由解码元素负责（`srtsrc auto-reconnect=true`）；本进程只**测量**断流与恢复，
  报告里 `reconnect_owner` 如实写 `srtsrc auto-reconnect`，不声称自己控制重连。
- 用户自有采集端：OBS 直推 SRT 已实测通过（见 `docs/verification.md` "M4+"），并因此修掉一个
  真实缺陷——OBS 的 Apple VT H.264 不带 timing，旧实现会把整条视频轨按 `duration_unavailable`
  丢掉；现在用同一轨下一个样本的 PTS 差分补时长并计入 `duration_derived_samples`。
  新增的 `videotoolbox_video` 场景已随五场景 `make live-check` 通过（该发布端同样不带 timing，
  `duration_derived_samples=3` 三帧全靠差分定时）。仍未验证：Mac mini / 跨机部署、OBS 之外的采集端。
- 仍未验证（不要当成已完成）：SRT 加密与带凭据的 publish；只在本机回环与 `macos-aarch64` 上验收；
  小时级长直播、连续多次断流、VFR/设备直出/720p 屏幕文字的直播样本都没有；
  真实流里出现过 1 次 `duration_delta_nonpositive`（PTS 重复/非单调），B 帧重排序没有专门验证。
- 注意（保持有效）：没有样本的窗口必须以 `live_window_produced_no_samples` 失败，不得报成
  "成功但为空"；`replay` 读 SRT 仍必须显式拒绝（`srt_source_requires_ingest_command`）；
  直播没有 anchor 区间，因此 M1 的覆盖率结论不适用于直播。

### M5 macOS 常驻形态（Mac mini）—— 2026-09-23 落地（ADR-015）

- 现状：已有可执行产物 `tools/macos_resident.py`（`probe`/`render`/`install`/`status`/`uninstall`）、
  `deploy/macos/launchd/*.plist.template`、`deploy/macos/bin/sensoryplex-media-run`，以及
  `tests/contracts/test_macos_resident_contract.py`（11 项）。分级表、模板与验收证据见
  [ADR-015](adr/ADR-015-macOS常驻形态与统一内存分级.md)，操作步骤见
  [运行手册](runbooks/macos-resident.md)。
- 档位已定（不再"待输入 Mac mini 机型"）：`small` 16–24 GiB / `medium` 24–32 GiB /
  `large` 32–64 GiB / `xlarge` ≥64 GiB。`medium` **锚定**今天的默认上限（`retained 32` /
  `arena 64 MiB`），即"不改现有行为"；16 GiB 的 `small` 档模型并发为 1（ADR-008 禁止并行 ASR+OCR+VLM）。
  档位之外**不吸附**（返回 `unsupported` 并给出原因），探测来源（`sysctl`/`env`/`unavailable`）显式写入 `resident.env`。
- 验收（本机 macOS 26.5.2 / arm64 / M2 Max / 32 GiB）：
  - [x] plist 模板 + 安装/卸载脚本：`install` / `uninstall --purge-logs` 真机通过，残留检查干净。
  - [x] 崩溃重启：`kill -9` 后 `KeepAlive.SuccessfulExit=false` 在 3 秒内拉起新 pid。
  - [x] 登录/引导自启：`bootout` + `bootstrap`（不 kickstart）后 `RunAtLoad` 自动 running。
  - [x] 分级上限真实生效：`sensoryplex-media-run` 注入 `retained_limit=64` / `arena=128 MiB`，
    无消费者场景 `handoff_stats` 实测 `retained_limit=64 retained_kind_limit=32`、backpressure `saturated`。
  - [x] 防休眠：`caffeinate -ims` 持有 `PreventUserIdleSystemSleep` + `PreventSystemSleep`。
  - [ ] **未验证**：队列上限按分级生效——`queue_capacity` 当前只被校验、未被运行时消费（ADR-015 §5）；
    `small` 档（16 GiB）与 Mac mini 各档位未实跑；模型并发只有配置事实，无并发执行样本；
    断电重启、休眠唤醒、小时级长稳未验证。这些不得当成已完成。

### M6 linux-x86_64 侧验收

- 现状：所有媒体验收都在 `macos-aarch64` 完成。
- 验收：同一命令序列（`make check`、`make media-replay`）在 NVIDIA 主线机器上跑通并记录平台标识。

### M7 CI 远端执行 —— 2026-09-23 已验收并加固

- 现状（**原记录有误**）：`.github/workflows/ci.yml` 的 `check-apple-silicon` **早已在远端真实通过过**，
  不是"从未在远端跑过"。M9 的 push（run 35850290513，job 107146096696）11 个 step 全绿；
  M10 的 push（run 35860979204）三个 job 全绿：`check` 2m50s、`check-console` 58s、
  `check-apple-silicon` 2m5s（job 107180840473）。远端 runner 实测为 **`macos-15-arm64`**
  （Image Version 20260907.0337.1、macOS 15.7.9），是真 Apple Silicon，不是 x86 交叉。
- 本次加固：
  - [x] `on:` 增加 `workflow_dispatch`，允许在不制造空提交的前提下重跑。
  - [x] `check-apple-silicon` 增加硬断言 step：`test "$(uname -m)" = "arm64"` 并打印 `hw.memsize`。
        若镜像哪天变成 x86，"macOS CI 通过"必须先红，而不是悄悄退化成"在 Intel 上通过"。
  - [x] `make media-check` 之后增加 `make media-test`（解码路径单测，含保留表 A/B 回归；带 feature 才存在）。
- [x] 加固后已在远端验证：push 触发 run **35863597690**，三个 job 全绿——
  `check` 2m52s（job 107189530260）、`check-console` 1m0s（job 107189529919）、
  `check-apple-silicon` 3m46s（job 107189530242，13 个 step 全绿；含新增的 `uname -m` 硬断言
  与 `cargo test -p sensoryplex-media --features gstreamer` → **105 passed**）。
  runner 为 `macos-15-arm64`（Image 20260907.0337），`uname -m` 实测 `arm64`。
- 仍未验证：Windows 与 Linux NVIDIA 侧没有 CI job；CI runner 只有 7 GiB 内存，
  低于 `small` 档下限，所以 `launchd` 常驻形态（M5）**不能**在 CI 里验收，只能真机跑。

### M8 模型插件（ASR/OCR/VLM/BGE）

- 状态：**进行中**——两个真实端侧模型（**VLM**、**ASR**）已接入并通过验收；OCR/BGE 与 CoreML/Metal 仍未做。
  证据见 `docs/verification.md` 的 "M8 模型插件：真实 VLM 端侧接入与观察语义" 与
  "M10 模型插件：真实 ASR 端侧接入与音频样本布局契约"；设计决策见
  [ADR-012](adr/ADR-012-模型插件与端侧推理边界.md) 与 [ADR-014](adr/ADR-014-ASR插件与音频样本布局契约.md)。
- 已完成（VLM）：`plugins/python/processors/vlm-moondream` 消费 Runtime 数据面里的真实视频帧
  （经 `LeaseBufferReader` 读字节，非文件名），调用**本机** ollama 的 `moondream:v2` 产出
  `observation.vision.scene_description`：锚点等于源帧半开区间（`timing_source=media_pts`）、
  `content_hash` 等于该帧 lease 窗口摘要、`modelArtifactDigest` 等于模型服务实测摘要、
  `confidence` 显式缺省并写 `model_does_not_report_calibrated_confidence`。
  `make model-check MEDIA=video/1.mp4` 四进程（编排/生产者/插件/worker）通过：2 帧真实推理，
  单帧端到端 0.6–2.2 s，账目 `released_total=24 retained=0 arena_live_slabs=0`。
- 验收暴露并修掉 4 个真实缺陷（GET/POST 误用、空闲超时过短、消费者未归还非视频条目、验收脚本键名），详见 ADR-012 §7。
- 仍未验证（不要当成已完成）：只有 VLM 一个模型，ASR/OCR/BGE 未接入；CoreML/Metal 仍
  `execution_backend_not_implemented`；`local_native` 插件**未签名**（只在 manifest 写明原因），
  签名/SBOM 只有结构预检；未做 worker 的 durable 幂等、lease 崩溃回收、沙箱与无外网策略的强制执行；
  `golden_path_verified` 恒为 false；只在本机回环 `macos-aarch64` 验收，`linux-x86_64` 与 Mac mini / 跨机未验证。
- 仍未验证（模型质量）：`moondream:v2` 输出**不稳定**，同一帧两次推理可能不同，本轮实测到一次退化输出。
  本项只保证**链路语义**正确，不保证**描述可用**。
- 已完成（ASR，2026-09-23）：第二个模型插件 `plugins/python/processors/asr-whisper-mlx` 消费数据面里的
  真实音频段，用本机 MLX Whisper 产出带锚点/来源/显式置信度语义的转写 observation；
  `make asr-check MEDIA=video/samples/screencast-video2commons.480p.vp9.webm` 四进程通过。
  本轮为此把**音频样本布局**（`sample_format`）与**段描述符进保留表**落成契约，
  暴露并修掉 5 个真实缺陷（含一个产品缺陷：段从未进跨进程数据面；以及"子段必定落在窗口内"这个
  错误假设——Whisper 退化会给出越窗时间戳）。详见 §M10 与
  [ADR-014](adr/ADR-014-ASR插件与音频样本布局契约.md)。
- 剩余子项：OCR、BGE（向量），以及按机型档位选择模型（依赖 M5）。

### M9 格式准入与显式拒绝（ADR-009，新增格式之前必须先做）

- 状态：**准入与拒绝已完成并实测**（`make capability-check` 19/19），且 ADR-009 §2 承诺矩阵里
  原先缺样本的 5 行（H.264、VP8、Vorbis、容器内 PCM、文件形态 MPEG-TS）已于 2026-09-23 补上
  **真实公开授权回放样本**。剩余工作是矩阵里其余行、旋转与容器级 VFR。
  证据见 `docs/verification.md` "M9 媒体格式准入"；决策见
  [ADR-009](adr/ADR-009-媒体格式支持矩阵与拒绝语义.md)；样本见
  `tests/fixtures/media/OPEN-SAMPLES.md`。
- 已完成（准入与拒绝）：
  - `crates/media/src/capability.rs`：把承诺矩阵写成数据，判定顺序为
    容器 → 编码 → 位深 → 色彩 → 采样格式 → 声道；判定输入是**解码前采集的源格式上下文**
    加上解码后格式，绝不从归一化后的 `pixel_format` 反推源位深（实测 A 的修法）。
  - 源格式采集与关联：容器来自 typefind（demuxer sink caps 作为兜底），轨道源格式来自
    demuxer/parser/decoder/capsfilter 的 sink caps，一律按 GStreamer **stream ID** 关联；
    禁止按 pad 出现顺序或只按 video/audio 类型配对。
  - 拒绝上报：准入探针挂在链头 queue 的 sink pad，被判拒的样本在该链上**丢弃**（不是 unlink），
    拒绝码进 `DecodedDataPlane.rejected_tracks`（`track_kind` + 稳定码 + 观测值 + 容器 +
    解码器元素），命令行打 `rejected=`；被拒轨道不产生 descriptor，也不产生 track stat。
  - Proto：`BufferFormat` 补几何与源位深/色彩；`media.proto` 补源格式上下文、`RejectedTrack`、
    解码器元素、帧率模式（`CONSTANT|VARIABLE|UNKNOWN`）与声明的帧率；`make proto` 已重跑。
- 已完成（验收）：`make capability-check` **19/19**——6 个登记正样本不得被误拒（并给出源编码、
  解码器元素、源位深、采样格式、帧率模式或显式 UNKNOWN），13 条拒绝路径逐字命中 ADR-009 §3
  命名表（10-bit ×2、4:2:2、5.1、MP3、AVI ×2、裸 ES、字幕 pad、双视频轨、OGG、AC-3、WAV）；
  `make live-check` **5/5**（MPEG-TS over SRT 未被误拒）。负样本全部由 FFmpeg 现场合成/重封装，
  **只证明拒绝路径**，不是任何格式的可用性证据。
- 已完成（补样本）：5 行矩阵缺口按 ADR-009 §8 的四件事补齐——
  `sintel-trailer.480p.h264.mp4`（H.264/AAC，Blender CC BY 3.0）、
  `editing-basics-sandboxes.vp8.webm`（VP8/Vorbis，Commons CC BY-SA 3.0，容器未声明帧率）、
  `mpegts-h264-aac.live-recording.ts`（文件形态 MPEG-TS，本机 SRT 直推现场录制）、
  `conger-conger.h264-pcm.mov`（容器内 `pcm_s16le`，Zenodo CC BY 4.0）。
  出处、许可、SHA-256 与实测特性登记在 `tests/fixtures/media/OPEN-SAMPLES.md`。
- 验收暴露并修掉 5 个真实缺陷（细节与 A/B 数据见 `docs/verification.md`）：① `typefind ! decodebin`
  直连时 `have-type` 从不触发、demuxer sink caps 也未协商，容器结论永远缺失（等于把一切都拒了）；
  ② 没接上 pad 的空链路被当成活跃轨道，源里只有音频时把"有结论的没有轨道"报成 `decode_stalled`；
  ③ 被拒 pad 悬空（`not-linked`）+ `vtdec_hw` 偶发 GLMemory 协商 → 双视频轨随机失败；
  ④ 容器里直存 raw 采样（MOV 的 PCM）的源编码采集不到 → 误报 `unknown_source_codec`；
  ⑤ `audio/x-wav` / `audio/x-flac` 被误读成裸 ES。
- 仍未验证（不要当成已完成）：
  - 只在 `macos-aarch64`（GStreamer 1.28.7）验收；`linux-x86_64` 与 Mac mini 未验证，
    `vtdechw0` / `avdec_aac0` 等解码器元素不得外推。
  - 旋转：v1 既未采集也未应用，`display_rotation_deg` / `applied_rotation_deg` 保持缺省；
    手机竖屏素材的几何正确性未验证。
  - AV1 仍按**实验项**拒绝（不进 Golden Path）；PQ/HLG 等 HDR 只有合成 10-bit 相邻证据，
    没有真实 HDR 素材；E-AC-3 / DTS / TrueHD 无实测样本（AC-3 已在 MPEG-TS 里实测）。
  - **VP8 的位深/采样格式是矩阵推导值**（`video/x-vp8` caps 不含 profile/位深/采样格式），
    证据强度低于 H.264/HEVC；**文件形态 MPEG-TS 样本是本机重编码产物**，不代表设备直出。
  - CAPS 变化重判、"相同 raw caps 来自不同源格式"目前只有单元测试级证据，没有端到端样本。
  - `golden_path_verified` 恒为 false；`ReplayReport.blockers` 不列准入项（拒绝是流级事实）。
- 剩余子项：矩阵里其余行按 ADR-009 §8 补正样本；旋转的采集与归一化阶段落地；
  CAPS 变化重判的端到端样本；容器级 VFR 与设备直出样本（见 §2）。
- 许可检查项：发布产物的 `ffmpeg -version` 不得含 `--enable-gpl` / libx264 / libx265 等 GPL 组件；
  `gst-libav` 受其底层 `libav*` 构建约束（本机为 GPL 构建，见 ADR-009 §5）。

### M10 ASR 插件与音频样本布局契约（ADR-014，2026-09-23 落地）

- 状态：**ASR 链路已完成并实测**；转写**质量**与 Linux/Mac mini 路径未验收。
  证据见 `docs/verification.md` 的"M10"一节；决策见
  [ADR-014](adr/ADR-014-ASR插件与音频样本布局契约.md)；契约见
  [契约文档](contracts/README.md) 的"模型插件契约"。
- 已完成：
  - 契约：`common/v1/common.proto` 的 `BufferFormat` 与 `media/v1/media.proto` 的 `AudioSegment`
    各补 `string sample_format`（空串 = 未知，读者不得假设宽度或字节序）。
  - Rust：`crates/media/src/segment.rs` 的 `AUDIO_SAMPLE_FORMAT` 与链上 capsfilter 同源；
    未知布局显式记账丢弃（`audio_unsupported_sample_format`），不猜宽度；`emit_segment()` 补齐
    `retain_or_reject()`，**音频段描述符从此进跨进程保留表**（此前只在报告里存在）。
  - 插件：`plugins/python/processors/asr-whisper-mlx/`——本机 `mlx-whisper` 0.4.3 +
    `mlx-community/whisper-large-v3-turbo`；模型身份 = 实际加载的**权重文件** SHA-256（现场复算 +
    容器头探测 + `config.json` 维度校验）；`confidence` 显式缺省并写原因，`avg_logprob` /
    `no_speech_prob` / `compression_ratio` / `temperature` 原样带出且**不**冒充置信度；
    子段 = 窗口起点 + 模型相对时间，换算方式写进 payload；越窗时间戳**不夹取、不丢弃**，
    逐子段标记 `timing_outside_window` 并给 `segments_outside_window` 计数。
  - 工具：`tools/verify_asr.py`（四进程验收 + 独立复算权重摘要）、`tools/ai_worker.py`
    （`--input-kind` / `--plugin-config`）、`make asr-check`；`tools/verify_handoff.py` 的会计基准
    改为 `descriptors_built == Σtrack.samples + audio_segments.segments`。
- 仍未验证（不要当成已完成）：
  - 只在本机 `macos-aarch64` 验收；`linux-x86_64` 与 Mac mini / 跨机未验证——`mlx` 是 Apple Silicon
    专属，Linux 侧需要另选后端与另一轮验收。
  - 转写质量未验收（无 WER/CER；实测到一次重复退化，只能靠 `compression_ratio` 等诊断量筛）。
  - 段是固定 5 秒切分，没有静音切分与说话人对齐（多人对话样本见 §2）；窗口边界会切在词中间。
  - 段受 ADR-011 的单一种类上限约束（默认 32 条表 → 单类 16 段 = 80 秒音频），长直播必须提高
    `retained_limit` 或持续领取，否则表现为 `handoff_kind_quota_full`。
  - 没有取消 / 超时 / 崩溃后 lease 回收的端到端样本；插件仍 `local_native` 未签名。
- 剩余子项：OCR、BGE；ASR 的 Linux 后端与质量度量（WER/CER + 静音切分）。

## 2. 待补样本（用户后续提供，先按现有样本推进）

- [x] 断流重连样本（最小覆盖）：登记在册的 552 秒授权长样本经 GStreamer `srtsink` 直推 SRT，
      主动断流后重启发布端（`make live-check` 的 `stall_recovery` 场景）。
- [x] 用户自有采集端的 SRT 直推样本（OBS）：本机 OBS 自定义服务直推
      `srt://127.0.0.1:8890?streamid=publish:live/obs`（密钥留空），MediaMTX 报 `state="ready"`，
      Runtime `ingest` 20 秒窗口 samples=1604 / descriptors=1013（0 失败）；同轮修掉"无 timing 码流
      被整轨丢弃"的缺陷（证据：`docs/verification.md` "M4+"）。Mac mini 与跨机部署仍待补。
- [ ] SRT 加密（`passphrase`）与带凭据 publish 的样本。
- [ ] 容器级 VFR 长间隙样本：现有 6 个样本都是 CFR，"长间隙"只由内容静止段近似（最长 57.8s）。
- [ ] 720p/原始分辨率屏幕文字样本：现有 480p 转码下 OCR 可辨识度有限，不能据此下 OCR 结论。
- [ ] 设备直出样本：现有样本均为 FFmpeg/Commons 转码产物（`encoder=Lavf58.20.100` 或 vp9 转码），
      不代表采集端直出行为。
- [ ] 多人对话专用样本：用于 ASR 说话人分离；现有 `officehours-panel` 只是通用会议录制。
- [x] ASR 语音样本（最小覆盖）：已登记样本里含语音的那几个（`screencast-video2commons`、
      `officehours-panel` 等）已作为 M10 验收的真实音频段来源，见 `make asr-check`。
- [ ] ASR 质量度量样本：需要带**参考文本**的授权样本（目前没有），否则只能验收链路语义，
      给不出 WER/CER；重复退化段目前只能靠 `compression_ratio` 之类诊断量筛掉。

## 3. 优化项（基座与模块完成后再做）

- [ ] 解码热路径去 memcpy：按平台用零拷贝（Apple `unified_memory` / DMA buffer）替代当前拷贝进 arena。
- [ ] 摘要与校验：当前每个 descriptor 一次 SHA-256 + 逐字节比对，可改分块哈希 + 抽样校验。
- [ ] 音频段与 ASR 窗口对齐、静音切分，替代固定 5 秒切段。
- [ ] 抽帧策略调参（覆盖率/成本曲线），依据 M1 的覆盖率报告。
- [ ] 模型量化档位与并发上限按统一内存自适应：**分级表已落地**（M5 / ADR-015），但运行时仍不消费 `MODEL_PARALLELISM`/`queue_capacity`，需要 worker 侧按变量限流后再验收。

## 4. 已知差异与取舍记录

- 容器 edit list 会让 GStreamer 保留媒体时间戳、ffprobe 应用 edit list；已通过 segment event 呈现原点
  修正（样例偏差 166 ms → 0，`timeline_offset_ms` 记录偏移）。
- Opus `initial_padding=312`（6.5 ms pre-skip）导致两侧首帧位置相差一个 pre-skip：ffprobe 12 ms、
  GStreamer 6 ms。校验脚本按"音频起点 ≤ 25 ms、视频 ≤ 1 ms"判定并把实测偏移打印为 `start_offsets`；
  这是子帧级差异，不是缺陷，但真实错位（如 166 ms）仍会失败。
- 毫秒粒度下同一毫秒内的多个点按 `collapsed_interval` 显式丢弃，不制造区间。
- 审计中发现 `check_anchors` 依赖"锚点严格递增"，因此同毫秒点必然表现为 drop；同一现象在解码路径记为
  `overlapping_samples`，两处计数不可相加。
- [x] **arena 峰值口径（已核对，2026-09-23）。** `officehours-panel` 峰值 1489376 = 单帧；`sasebo-basketball`
      峰值 3564864 = 单帧 1639680 + 双声道 5 s 段 1925184。原因是 `peak_bytes` 统计**已提交容量**，
      bump 分配 + 空闲链复用：mono 段（960000 B）能复用单帧释放的区域，stereo 段（1920000 B）不能，
      于是新增提交。行为正确，语义已写入 `proto/media/v1/media.proto` 与 `docs/contracts/README.md`。
      已覆盖（2026-09-23）：提交量接近容量时由 `BackpressureReport` 的 `handoff_arena_bytes` 队列与
      `state=degraded|saturated` 显式告警（M2，见本文件 §M2 与 ADR-011），不再是待办。
