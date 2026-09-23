# 0.1.0 工程底座与后续阶段

依据根目录 ADR 的第 1 周目标建立工程，保留原需求文档作为设计依据。
目标平台包含 `macos-aarch64`（Apple Silicon，含 Mac mini 端侧部署）与 `linux-x86_64`（NVIDIA 性能主线），
见 ADR-008 与蓝图 §1.3。

| 范围 | 当前实现 | 下一步 |
| --- | --- | --- |
| Rust Core | 6 crate workspace、Proto、配置校验、有界队列、descriptor 校验 | Pipeline 生命周期、调度、进程与 lease 实际管理 |
| Runtime 服务 | gRPC Health + DescribeCapabilities（平台、宿主内存、允许的 memory kind、每个不可用后端的原因） | NATS JetStream 指令与任务分发 |
| Python SDK | Proto 绑定、输入校验、deadline、取消 token、并发限制、结构化错误 | worker lifecycle gRPC server、持久幂等与 lease 释放 |
| Timeline | Material/Observation 校验 | ASR/OCR/VLM 实际融合、冲突判定 |
| PostgreSQL | 显式迁移、不可变素材与模型版本、来源校验、事务 outbox | 保留与归档策略、outbox 消费与补偿 |
| Gateway | Bearer 认证、owner 过滤、素材详情、历史版本、关键词/标签/时间查询 | 外部鉴权、语义检索、短期媒体授权 URL |
| 存储/硬件 | Rust adapter traits，模型与配置 hash 契约 | NAS/MinIO/Milvus、ONNX/TensorRT 实现 |
| 直播接入基础设施 | 本机 MediaMTX 1.21.1（独立 Compose，仅回环端口）；SRT 直推（GStreamer `srtsink` 与用户自有 OBS）与 Runtime `ingest` 已打通：稳定窗口、断流恢复、无源失败、实时数据面交接四个场景通过，OBS 真实直推亦实测（无 timing 码流的视频时长按 PTS 差分补齐），见 `docs/verification.md` | Mac mini / 跨机部署、SRT 加密与带凭据 publish、`linux-x86_64` 侧验收；服务器上有流不等于模型链路可用 |
| 媒体与模型 | Pipeline 配置、真实媒体 probe 工具、ffprobe 锚点回放，GStreamer 真实解码 → arena → `BufferDescriptor` → lease 签发/校验/释放 → 音频 5 秒切段，视频自适应抽帧（keep/skip 全部带原因，7 个真实样本通过），跨进程数据面：Runtime 保留字节、独立进程按 lease 读取（3 个样本 × 2 个场景通过，具备有界容量与稳定拒绝码），SRT 实时接入 `ingest`（`make live-check` 四个场景通过），以及背压与队列可观察：三条有界队列的深度/峰值/容量、按原因与按种类的丢弃、lease 等待时间（`verify_backpressure.py` 4 场景 + OBS 直播实测，见 `docs/verification.md`） | **模型 worker**（当前消费方是验收脚本，不是推理进程）、媒体格式准入（ADR-009）；ASR/OCR/VLM/BGE 插件 |
| 工程 | uv/Cargo 锁文件、Docker、检查命令、CI（ubuntu） | 真视频 Golden Path、macOS CI 与 `launchd` 常驻形态、压测、监控仪表盘 |

下一里程碑：**本地文件 → GStreamer → PTS 正确的 frame/audio descriptor**，先完成
真实样本回放、lease 生命周期和断流测试，再引入实际模型。未具备真实媒体和推理链路前，
不声明 2–5 秒语义可见性或任何 GPU 吞吐目标已达成。

该里程碑需在 `macos-aarch64` 与 `linux-x86_64` 上分别验收：macOS 侧以原生进程运行
runtime/media-worker（容器无法访问 Metal/CoreML），NVIDIA 侧沿用容器与 CUDA/TensorRT 路径。

解码路径已在真实样本（`video/1.mp4`）上通过：918 视频帧被观测、26 帧按抽帧策略保留 + 1321 音频帧写入
arena，1354 个 `BufferDescriptor` 全部通过校验（0 失败），1354 个 lease 签发并全部释放，音频切成 6 个完整段
+ 1 个尾部 partial 段。跨进程数据面也已落地：`replay --handoff-listen` 把样本留在共享内存里，
独立进程经 `BufferHandoffService` 领窗口、校验摘要并显式释放，三个样本（`video/1.mp4`、`sasebo-basketball`、
`officehours-panel`）两个场景全部通过，容量与 expired 计数可对账；安全边界见 ADR-010。

SRT 实时接入也已落地：`ingest` 在有限窗口内从 SRT 拉流，测量断流与恢复（重连归解码元素
`srtsrc auto-reconnect`，本进程只测量），并把实时样本交给同一条 arena/descriptor/lease/交接链路；
`make live-check` 的四个场景（稳定窗口、断流恢复、无源失败、实时数据面交接）全部通过，
细节与未验证范围见 `docs/verification.md` 的"M4"一节。注意直播**没有 anchor 区间**（没有已知时长）：
`duration_ms` 恒为 0，`replay` 读 SRT 仍显式拒绝（`srt_source_requires_ingest_command`）。
用户自有 OBS 的 SRT 直推随后也实测通过（同一次接入就暴露并修掉了"编码器不带 timing 时整条视频轨
被丢弃"的缺陷），实测数据与仍未验证范围见同一文件的"M4+"一节。

背压与队列可观察（M2）也已落地：`BackpressureReport` 给出保留表/按种类/arena 三条有界队列的
深度与峰值、按原因与按种类的丢弃、lease 等待时间与超时，以及降级抑制的 keep 数；处理顺序是
**先降级、再拒绝**。落地过程中用真实 OBS 直播发现并修掉一个缺陷——保留表是各类共用的 FIFO，
音频入队频率远高于视频，曾把 32 条窗口全占满、视频一帧也交不出去；现在单一种类最多占一半
（[ADR-011](adr/ADR-011-保留窗口按种类分配.md)），消费者实测拿到视频帧。证据见
`docs/verification.md` 的"M2"一节。

仍未实现：真正消费字节的**模型** worker（当前消费方只是验收脚本）、媒体格式准入。
因此 `golden_path_verified` 恒为 false，不得把本节读作 Golden Path 已完成。
抽帧的覆盖率目前只到帧数口径，语义覆盖要等模型接入后才能验证。
共享内存数据面只在本机有意义（且同 UID 进程之间没有逐 buffer 隔离），不是分布式数据面。

媒体格式准入同样未实现（[ADR-009](adr/ADR-009-媒体格式支持矩阵与拒绝语义.md)）：现在没有矩阵判据，
10-bit HEVC 会被静默降成 8-bit 仍算成功，非音视频 pad 只写日志，5.1 音频原样透传。实测记录见
`docs/verification.md` 的"媒体格式准入"一节；补齐拒绝语义排在新增任何格式之前（`docs/TODO.md` M9）。
