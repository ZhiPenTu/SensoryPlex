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

## 1. 其他模块（先做这些，再做优化）

### M1 抽帧（adaptive sampler）

- 现状：Pipeline 里只有 `adaptive_sampler` 声明，无实现，blocker `adaptive_sampling_not_implemented`。
- 目标：在 descriptor 之上做**有界**抽帧（静止段降采样、跳变处补采），输出受控引用与显式丢弃原因。
- 验收：用 `tests/fixtures/media/OPEN-SAMPLES.md` 的静止/翻页/运动三类样本产出**抽帧覆盖率报告**
      （抽到的帧数、相对锚点的覆盖比例、漏采原因）；移除对应 blocker。
- 注意：不得把"静止段跳过"实现成静默丢帧——跳过必须可计数、可解释。

### M2 背压与队列可观察

- 现状：队列有上限（pipeline `queue_capacity=32`、sink `max_buffers=8`），但没有等待/丢弃/超时指标。
- 目标：把等待时间、队列峰值、丢弃与超时以计数 + 原因暴露（报告或指标端点）。
- 验收：在 `officehours-panel`（最长静止段 57.8s）与 `sasebo-basketball`（持续运动）上跑出非零指标。

### M3 lease 消费方（模型 worker 基座）

- 现状：lease 在签发进程内立即释放，等于没有真实交接；blocker `lease_consumer_not_implemented`。
- 目标：独立 Python worker 经 gRPC 取 descriptor 引用 + lease、读 arena、显式释放；超时/失败可观察。
- 验收：正常路径 + lease 过期路径 + arena 越界路径各有一条真实执行记录；移除对应 blocker。

### M4 SRT 接入与断流重连

- 现状：`UnavailableSource`，调用即报 `gstreamer_srt_ingest_not_implemented`。
- 待输入：SRT 端点（host:port、streamid、加密/密码引用方式），或允许用 ffmpeg 从授权文件向本地
  `srtsink` 推流做回放。
- 验收：真实 SRT 源回放 + 主动断流后重连，锚点/descriptor 计数与丢弃原因可解释。

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
- 前置：M1（抽帧）与 M3（lease 消费方）。
- 验收：至少一个模型在真实样本上产出带时间锚点、来源、版本与置信度语义的 observation。

### M9 格式准入与显式拒绝（ADR-009，新增格式之前必须先做）

- 现状：没有准入判据，实测存在三类静默降级——10-bit HEVC 被降成 8-bit 仍算成功、
  非音视频 pad 只写日志不进报告、5.1 音频原样透传（证据见 `docs/verification.md`
  "媒体格式准入"一节）。这三条都违反 AGENTS.md。
- 目标：`crates/media/src/capability.rs`（矩阵 + `classify_caps`）在 caps 协商阶段判定；
  `media.proto` 补几何（`display_rotation_deg`、SAR）与位深/色彩（`bit_depth`、primaries、
  transfer、matrix）字段，以及时间基与帧率模式（`CONSTANT|VARIABLE|UNKNOWN`）；
  实际解码器元素（`vtdec_hw`、`avdec_h264`）进入报告证据。
- 验收：10-bit、多声道、未知 pad 三条拒绝路径各跑出稳定拒绝码（负样本可用 FFmpeg 合成，
  但只能标注为拒绝路径验证样本）；已通过的正样本回放结论不得回退。
- 注意：本项与 M1–M8 相互独立，但**必须先于**任何"新增支持格式"的动作。
- 许可检查项：发布产物的 `ffmpeg -version` 不得含 `--enable-gpl` / libx264 / libx265 等 GPL 组件；
  `gst-libav` 受其底层 `libav*` 构建约束（本机为 GPL 构建，见 ADR-009 §5）。

## 2. 待补样本（用户后续提供，先按现有样本推进）

- [ ] 断流重连样本：必须来自 SRT，文件样本无法覆盖（依赖 M4）。
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
      剩余待办：当提交量接近容量时给出显式告警（属 M2 背压可观察范畴）。
