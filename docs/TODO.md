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
      （证据：`video/1.mp4` 与 `video/samples/` 6 个公开许可样本，见 `tests/fixtures/media/OPEN-SAMPLES.md`）
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
- 仍未验证（不要当成已完成）：覆盖率是帧数口径，**语义**覆盖要等 M8 接入模型才能验证；
  样本仍全是 CFR，VFR 下的抽帧语义没有样本。
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
  单一种类流只能用一半窗口是显式接受的代价。模型 worker（M8）仍未接入。

### M3 lease 消费方（跨进程数据面）

- 状态：**已完成**（证据见 `docs/verification.md` "M3 跨进程数据面：lease 消费方与真实交接"）。
- 结果：`sensoryplex-runtime replay --handoff-listen` 把样本留在 POSIX 共享内存里，独立进程
  `tools/handoff_worker.py` 经 `BufferHandoffService` 领窗口、读字节、校验摘要、显式释放；
  `lease_consumer_not_implemented` 已从 `blockers` 移除（`verify_replay.py` 的 `EXPECTED_BLOCKERS` 收紧为空集）。
  三个样本（`video/1.mp4`、`sasebo-basketball`、`officehours-panel`）× 两个场景全部通过：
  越界/非法窗口/重复领取/迟到释放/过期 TTL 各得到稳定拒绝码，
  `offered == retained_total + retain_rejections`、`retained_total == retained + released + expired`、
  `arena_live_slabs == 0`，且 `offered` 与 `ReplayReport` 的交接样本数一致。
- 仍未验证（不要当成已完成）：消费方是**验收脚本不是模型 worker**，语义链路（M8）没有进展；
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

### M5 macOS 常驻形态（Mac mini）

- 现状：只在文档中描述了 `launchd` + `pmset`/`caffeinate` 策略，没有可执行产物。
- 验收：plist 模板 + 安装/卸载脚本；重启自启、崩溃重启、按统一内存设置队列上限各验证一次。
- 待输入：Mac mini 目标机型统一内存档位（16/24/32GB），决定默认队列上限与模型量化档。

### M6 linux-x86_64 侧验收

- 现状：所有媒体验收都在 `macos-aarch64` 完成。
- 验收：同一命令序列（`make check`、`make media-replay`）在 NVIDIA 主线机器上跑通并记录平台标识。

### M7 CI 远端首次执行

- 现状：`.github/workflows/ci.yml` 已包含 `check-apple-silicon`（含 `make media-check`），但从未在远端跑过。
- 验收：macOS job 在 GitHub Actions 上真实通过一次；未通过前不得声称 macOS CI 可用。

### M8 模型插件（ASR/OCR/VLM/BGE）

- 现状：完全未接入，CoreML/Metal 报 `execution_backend_not_implemented`。
- 前置已满足：M1（抽帧）与 M3（跨进程数据面）都已完成；本项仍未开始，没有任何模型被接入。
- 验收：至少一个模型在真实样本上产出带时间锚点、来源、版本与置信度语义的 observation。

### M9 格式准入与显式拒绝（ADR-009，新增格式之前必须先做）

- 现状：没有准入判据，实测存在三类静默降级——10-bit HEVC 被降成 8-bit 仍算成功、
  非音视频 pad 只写日志不进报告、5.1 音频原样透传（证据见 `docs/verification.md`
  "媒体格式准入"一节）。这三条都违反 AGENTS.md。
- 目标：按 ADR-009 §4 在 typefind / demux / parser / autoplug 阶段采集源格式，按轨道标识关联；
  `crates/media/src/capability.rs`（矩阵 + `classify_format`）结合源上下文与解码后格式完成准入，
  不得仅凭 `pad-added` 的 raw caps 推断源编码或位深；信息缺失须有界等待后显式拒绝，CAPS 变化须重判。
- 契约：`proto/common/v1/common.proto` 的 `BufferFormat` 补几何（`display_rotation_deg`、SAR）
  与源位深/色彩（`bit_depth`、primaries、transfer、matrix）字段；`proto/media/v1/media.proto`
  补源格式、轨道关联、应用的旋转角度、时间基与帧率模式（`CONSTANT|VARIABLE|UNKNOWN`），
  以及实际解码器元素（`vtdec_hw`、`avdec_h264`）的报告证据；随后运行 `make proto`。
- 验收：10-bit、多声道、未知 pad 三条拒绝路径各跑出稳定拒绝码（负样本可用 FFmpeg 合成，
  但只能标注为拒绝路径验证样本）；补充源信息缺失、多轨关联、相同 raw caps 来自不同源格式及
  CAPS 变化的准入检查；已通过的正样本回放结论不得回退。
- 注意：本项与 M1–M8 相互独立，但**必须先于**任何"新增支持格式"的动作。
- 许可检查项：发布产物的 `ffmpeg -version` 不得含 `--enable-gpl` / libx264 / libx265 等 GPL 组件；
  `gst-libav` 受其底层 `libav*` 构建约束（本机为 GPL 构建，见 ADR-009 §5）。

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

## 3. 优化项（基座与模块完成后再做）

- [ ] 解码热路径去 memcpy：按平台用零拷贝（Apple `unified_memory` / DMA buffer）替代当前拷贝进 arena。
- [ ] 摘要与校验：当前每个 descriptor 一次 SHA-256 + 逐字节比对，可改分块哈希 + 抽样校验。
- [ ] 音频段与 ASR 窗口对齐、静音切分，替代固定 5 秒切段。
- [ ] 抽帧策略调参（覆盖率/成本曲线），依据 M1 的覆盖率报告。
- [ ] 模型量化档位与并发上限按统一内存自适应（依赖 M5 的机型档位）。

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
