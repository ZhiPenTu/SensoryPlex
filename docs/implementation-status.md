# 0.1.0 工程底座与后续阶段

依据根目录 ADR 的第 1 周目标建立工程，保留原需求文档作为设计依据。
目标平台包含 `macos-aarch64`（Apple Silicon，含 Mac mini 端侧部署）与 `linux-x86_64`（NVIDIA 性能主线），
见 ADR-008 与蓝图 §1.3。

2026-09-24 并行状态：**M8 整体仍在研发中**；Timeline 核心在独立分支
`codex/timeline-fusion`（`bdb00ef`），尚未合并。真实媒体端到端联调与语义冲突识别
仍是明确未完成项，见 `docs/TODO.md` 的“当前并行工作与明确未完成项”。
素材查询与回看体验单独在 `codex/material-review` 推进；它沿用现有入库事实，
不意味着媒体准入、自动来源映射或模型到素材链路已经完成。

| 范围 | 当前实现 | 下一步 |
| --- | --- | --- |
| Rust Core | 6 crate workspace、Proto、配置校验、有界队列、descriptor 校验 | Pipeline 生命周期、调度、进程与 lease 实际管理 |
| Runtime 服务 | gRPC Health + DescribeCapabilities（平台、宿主内存、允许的 memory kind、每个不可用后端的原因） | NATS JetStream 指令与任务分发 |
| Python SDK | Proto 绑定、输入校验、deadline、取消 token、并发限制、结构化错误；worker 侧生命周期 gRPC 服务、`LeaseBufferReader` 读字节与 lease 归还（已在 vlm-moondream 插件落地，见 ADR-012） | 持久幂等、崩溃后的 lease 回收、沙箱与外发策略执行 |
| Timeline | Material/Observation 校验 | ASR/OCR/VLM 实际融合、冲突判定 |
| PostgreSQL | 显式迁移、不可变素材与模型版本、来源校验、事务 outbox | 保留与归档策略、outbox 消费与补偿 |
| Gateway | Bearer 认证、owner 过滤、素材详情、历史版本、关键词/标签/时间查询 | 外部鉴权、语义检索、短期媒体授权 URL |
| Console / Platform API | 独立 React / TS / Vite 工程、统一模块化 API、会话/CSRF/RBAC、真实上传与 Range 回看、插件配置版本、方案/任务草稿、作用域凭据、账户/角色管理与审计；运行手册见 `docs/runbooks/console.md` | Runtime 媒体准入、安装与生命周期、方案发布、任务执行及素材来源映射；当前不是完整业务 Golden Path |
| 存储/硬件 | Rust adapter traits，模型与配置 hash 契约；向量落库与检索走 `services/index-worker` 的 Milvus（本机 **Lite 文件形态**，写后回读确认） | NAS/MinIO、服务端 Milvus 拓扑（本机 Docker Hub 不可达，未验收）、ONNX/TensorRT 实现 |
| 直播接入基础设施 | 本机 MediaMTX 1.21.1（独立 Compose，仅回环端口）；SRT 直推（GStreamer `srtsink` 与用户自有 OBS）与 Runtime `ingest` 已打通：稳定窗口、断流恢复、无源失败、实时数据面交接、VideoToolbox 视频五个场景通过，OBS 真实直推亦实测（无 timing 码流的视频时长按 PTS 差分补齐），见 `docs/verification.md` | Mac mini / 跨机部署、SRT 加密与带凭据 publish、`linux-x86_64` 侧验收；服务器上有流不等于语义链路可用 |
| 媒体与模型 | Pipeline 配置、真实媒体 probe 工具、ffprobe 锚点回放，GStreamer 真实解码 → arena → `BufferDescriptor` → lease 签发/校验/释放 → 音频 5 秒切段，视频自适应抽帧（keep/skip 全部带原因，7 个真实样本通过），跨进程数据面：Runtime 保留字节、独立进程按 lease 读取（3 个样本 × 2 个场景通过，具备有界容量与稳定拒绝码），SRT 实时接入 `ingest`（`make live-check` 五个场景通过），背压与队列可观察：三条有界队列的深度/峰值/容量、按原因与按种类的丢弃、lease 等待时间（`verify_backpressure.py` 4 场景 + OBS 直播实测，见 `docs/verification.md`），以及第一个**端侧模型插件**：本机 ollama `moondream:v2`（VLM），插件经 `LeaseBufferReader` 读真实视频帧产出带锚点/来源/版本/显式置信度语义的 observation，`tools/ai_worker.py` 只发现与调用不读字节，`make model-check` 四进程通过（见 `docs/verification.md` 的"M8"一节与 ADR-012），以及**媒体格式准入与显式拒绝**：承诺矩阵写成数据、源格式按 stream ID 关联、被拒轨道带稳定拒绝码进报告（`make capability-check` **19 场景**通过：6 个公开授权正样本 + 13 条拒绝路径，见 `docs/verification.md` 的"M9"一节与 ADR-009），以及第二个**端侧模型插件**（ASR）：本机 MLX Whisper 经 `LeaseBufferReader` 读真实音频段产出带锚点/来源/显式置信度语义的转写 observation，音频样本布局（`sample_format`）与音频段描述符进保留表一并落成契约，`make asr-check` 四进程通过（见 `docs/verification.md` 的"M10"一节与 ADR-014），以及第三个（OCR）与第四个（BGE 文本向量）端侧模型插件：OCR 以随包携带的 PP-OCR 组合权重的**组合摘要**为身份、产出带帧像素坐标的文字块；BGE **不接数据面**（`acceptsMemoryKinds: []`），消费上游 OCR 事实产出**维度版本化**的 L2 归一化向量，`make ocr-check` / `make embed-check` 均多进程通过，以及**向量落库与检索闭环**：`services/index-worker`（`sensoryplex-index`）把 BGE 向量写进 Milvus（本机 **Lite 文件形态**）并**读回来确认**才置 `embedding_record.state='ready'`，检索命中必须回查 PostgreSQL 的 `ready` + material 存在 + `source.owner` 才允许返回（被丢弃的命中单独计数），`make index-check` **11 个场景**通过（见 `docs/verification.md` 与 ADR-020），以及**宿主加速器能力上报**：`DescribeCapabilities` 新增 `host_accelerators`，与执行后端分成两张表、三态不得互相塌陷，`make accelerator-check` 四路对账通过（本机 `coreml=available(3520.5.1)`、`metal=available(metal4)` 与宿主直读逐字一致；`LANG=zh_CN.UTF-8` 判定不变；`PATH=/nonexistent` 落 `unknown` 而**不是**"不存在"；见 `docs/verification.md` 的"M8 剩余：宿主加速器能力探测与上报（ADR-022）"与 ADR-022） | Rust 侧仍没有任何 in-process `ExecutionBackend`（`model_inference` 恒在 `unavailable_capabilities`；宿主加速器探测只在开发机 `macos-aarch64` 实测，`cuda` 分支与 Mac mini 均未验收）；常驻 index-worker 消费与网关语义检索接线（`mode=semantic` 仍 501，RRF/混合检索未做）；服务端 Milvus 形态（本机 Docker Hub 不可达，未验收）；ASR 的 Linux 后端（`mlx` 是 Apple Silicon 专属）；插件**未签名**（`local_native` 形态，签名/SBOM 只有结构预检）；旋转的采集与应用（v1 未实现） |
| 工程 | uv/Cargo 锁文件、Docker、检查命令、CI（`check`/`check-console` + **Apple Silicon** `check-apple-silicon`，远端 `macos-15-arm64` 已真实通过）、macOS `launchd` 常驻形态与统一内存分级（`tools/macos_resident.py`，见 ADR-015） | 真视频 Golden Path、Linux NVIDIA 侧 CI、压测、监控仪表盘 |

下一里程碑：**本地文件 → GStreamer → PTS 正确的 frame/audio descriptor**，先完成
真实样本回放、lease 生命周期和断流测试，再引入实际模型。真实媒体、lease 生命周期、断流测试
与第一个端侧模型（VLM，M8）都已完成；但**语义质量**仍未验收——模型输出不稳定，
2–5 秒语义可见性与任何 GPU 吞吐目标都**未**验证。

该里程碑需在 `macos-aarch64` 与 `linux-x86_64` 上分别验收：macOS 侧以原生进程运行
runtime/media-worker（容器无法访问 Metal/CoreML），NVIDIA 侧沿用容器与 CUDA/TensorRT 路径。

解码路径已在真实样本（`video/1.mp4`）上通过：918 视频帧被观测、26 帧按抽帧策略保留 + 1321 音频帧写入
arena，1354 个 `BufferDescriptor` 全部通过校验（0 失败），1354 个 lease 签发并全部释放，音频切成 6 个完整段
+ 1 个尾部 partial 段。跨进程数据面也已落地：`replay --handoff-listen` 把样本留在共享内存里，
独立进程经 `BufferHandoffService` 领窗口、校验摘要并显式释放，三个样本（`video/1.mp4`、`sasebo-basketball`、
`officehours-panel`）两个场景全部通过，容量与 expired 计数可对账；安全边界见 ADR-010。

SRT 实时接入也已落地：`ingest` 在有限窗口内从 SRT 拉流，测量断流与恢复（重连归解码元素
`srtsrc auto-reconnect`，本进程只测量），并把实时样本交给同一条 arena/descriptor/lease/交接链路；
`make live-check` 的五个场景（稳定窗口、断流恢复、无源失败、实时数据面交接、VideoToolbox 视频）全部通过，
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

模型链路（M8）已接入四个端侧模型插件：本机 ollama 的 `moondream:v2`（VLM，读视频帧）、
MLX Whisper（ASR，读音频段）、PP-OCR（OCR，读视频帧）与 BGE（文本向量，**消费上游 OCR 观测而不是
字节**，worker 用 `--input-observations` 走 observation 输入路径）；`tools/ai_worker.py` 只做发现与
调用（不读字节），四者的验收脚本都是真跑多进程。边界见
[ADR-012](adr/ADR-012-模型插件与端侧推理边界.md)。仍未实现的是**运行时（Rust）侧对加速后端的能力
上报**、**常驻 index-worker 消费与网关侧语义检索**、以及插件签名验证；`metal` 在 ONNX 路径上没有独立执行后端。
因此 `golden_path_verified` 恒为 false，不得把本节读作 Golden Path 已完成；
接入的 VLM 只保证链路语义正确，**不保证描述可用**（模型输出不稳定）。
抽帧的覆盖率目前只到帧数口径，语义覆盖仍未用模型输出度量。
共享内存数据面只在本机有意义（且同 UID 进程之间没有逐 buffer 隔离），不是分布式数据面。

ASR 链路（M10）也已落地：第二个模型插件 `plugins/python/processors/asr-whisper-mlx` 消费数据面里的
**真实音频段**，用本机 **MLX Whisper**（`mlx-whisper 0.4.3` + `mlx-community/whisper-large-v3-turbo`）
产出转写 observation，`make asr-check MEDIA=video/samples/screencast-video2commons.480p.vp9.webm`
四进程通过：段锚点等于源段半开区间（`[0,5015)`、`[5015,10015)`）、`contentHash` 等于段 lease 窗口摘要、
`modelArtifactDigest` 等于脚本**独立复算**的权重摘要（1,613,977,612 B，`sha256:951ed3fc…`）、
`confidence` 显式缺省并写原因、子段 = 窗口起点 + 模型相对时间（越窗的子段时间戳**不夹取、不丢弃**：逐子段标记 `timing_outside_window` 并计数）。为此把**音频样本布局**写进契约
（`BufferFormat.sample_format` / `AudioSegment.sample_format`，空串只表示未知，解码链只承认 `F32LE`，
其它布局显式记账丢弃）并修掉一个真实产品缺陷：**音频段描述符此前从未进跨进程保留表**
（`emit_segment()` 只做了进程内 `hand_off()`），插件因此永远读不到段。决策与 A/B 证据见
[ADR-014](adr/ADR-014-ASR插件与音频样本布局契约.md) 与 `docs/verification.md` 的"M10"一节。
**仍未验证的仍然不得声称可用**：转写**质量**未验收（无 WER/CER，实测到一次重复退化，
只能靠 `compression_ratio` 等诊断量筛）；段是固定 5 秒切分（无静音切分、无说话人对齐）；
只在本机 `macos-aarch64` 验收，`mlx` 为 Apple Silicon 专属，Linux/Mac mini 路径需另选后端；
段受单一种类上限约束（默认 32 条表 → 16 段 = 80 秒音频）。

OCR 链路也已落地（第三个模型插件 `plugins/python/processors/ocr-rapidocr`）：消费数据面里的真实
视频帧，用**随包携带**的 PP-OCR 组合权重（det/cls/rec 三份 ONNX，不联网下权重）产出带
**帧像素坐标 + 归一化坐标**、`timing_source=media_pts`、`contentHash` 等于帧 lease 窗口摘要、
`modelArtifactDigest` 等于脚本独立复算的**三份权重组合摘要**、`confidence` 显式缺省并写原因的
文字块 observation；`make ocr-check MEDIA=video/samples/screencast-video2commons.480p.vp9.webm`
（6 块/帧）与 `make ocr-check MEDIA=video/1.mp4 EXPECT=empty`（0 块 + `empty_reason=model_found_no_text`）
四进程通过。`provider=coreml` 时插件**断言三个会话的首选 provider 必须逐字等于请求值**，
不一致即 `execution_provider_not_selected` 失败（不静默退回 CPU）；但实测 CoreML 对 PP-OCR
**不加速**——动态 shape/NMS 子图无法编译，ORT 分区回退到 CPU，同帧 216–228 ms vs 1163 ms，
故本版本只交付"可选择的执行后端"，不声称加速。决策与实测证据见
[ADR-016](adr/ADR-016-OCR与ONNX执行后端.md) 与 `docs/verification.md` 的"M8 OCR"一节。
**仍未验证的仍然不得声称可用**：识别**质量**未验收（无准确率/召回基准，中文界面样本未覆盖）；
CoreML 收益未取得；`metal`（ONNX 路径）、`linux-x86_64`、Mac mini 与跨机未验证；
"绝不联网"只有 manifest 声明，没有 DNS/egress 强制执行。

文本向量链路也已落地（第四个模型插件 `plugins/python/processors/embed-bge-onnx`）：这是第一条
**不接数据面**的链路——输入是上游 OCR 观测里的文字（`observation.ocr_blocks`），不是字节，
因此 manifest 声明 `acceptsMemoryKinds: []`，喂 buffer 会以 `buffer_reader_not_attached` 明确拒绝。
本机 BGE 权重（`onnx/model_quantized.onnx` 24 010 842 B + `tokenizer.json` + `config.json` 三份文件的
**组合摘要**，由验收脚本独立复算）产出**维度版本化**的 L2 归一化向量：`dimension=512` 取自
`config.json` 并经 Start 前向探针实测，`dimension_source`、`pooling=cls`、`normalize=l2`、
`vector_index_key=material_text_bge_small_zh_v1_5_d512_v1` 都写进结果；`content_hash` 是**实际被编码
文本**的摘要（验收脚本按同一规则独立重拼），上游身份另写 `input.*`；`confidence` 显式缺省并写原因。
`make embed-check MEDIA=...` 先跑一遍真实 OCR 链路产出文字块、再让 BGE 消费它（provider cpu/coreml
都通过）；observation 路径的对账显式写 `drain.leases=0`，且报告里**没有**数据面统计。决策与实测见
[ADR-017](adr/ADR-017-BGE文本向量与维度版本化.md) 与 `docs/verification.md` 的"M8 BGE"一节。
**仍未验证的仍然不得声称可用**：向量**质量**未验收（无召回/排序基准，中文长文本与领域文本未覆盖）；
插件本身仍**不落向量库**（`storage=inline_payload`、`vector_ref=null`）——写入由独立进程 `services/index-worker` 承担（ADR-020，Milvus **Lite 文件形态**，11 场景通过）；服务端 Milvus 拓扑因本机 Docker Hub 不可达**未验收**，换模型或换维度按 ADR-020 走新 collection（不原地迁移）；CoreML 实测**更慢**（短文本 0.78 ms vs 3.16 ms），不声称加速；
`linux-x86_64`、Mac mini 与跨机未验证。

向量落库与检索闭环（M8 剩余项）也已落地：`services/index-worker`（`sensoryplex-index`）是 BGE
之后的 sink——它把插件产出的向量写进向量库、**读回来确认**之后才把 `embedding_record.state` 置成
`ready`（迁移 `0003_embedding_index.sql` 把这条顺序写成行不变式：`ready` 必须有 `vector_ref`+`indexed_at`、
`failed` 必须有 `error_code`）。collection 名就是 `vector_index_key`（换模型/换维度=新 collection，
不原地迁移），索引 FLAT + COSINE，`vector_ref` 只是逻辑引用（`milvus://<collection>/<id>`，不含主机
路径与端口）。**Milvus 不是事实源**：它只回答"哪条最近"，命中必须回查 PostgreSQL 的 `ready` +
material 存在 + `source.owner` 才允许返回，被丢弃的命中单独计数（非 owner 与被标 `failed` 的记录
即使还在向量库里也不返回）。`make index-check EMBEDDINGS=<ai-worker.json>` 用真实 BGE 向量 +
真实 PostgreSQL + 真实 Milvus Lite 跑 **11 个场景**（写入确认、跨进程持久、检索回查、非 owner 丢弃、
failed 不返回、幂等、维度篡改、库不可达、collection 契约漂移、不外泄、数据目录被别的进程锁住）全过。
边界：本机 Milvus **Lite 是进程独占的**（数据目录 flock，被占用即 `vector_store_locked`，不重试、
不换路径），因此 edge 形态是单写进程；服务端拓扑见
`deploy/compose/docker-compose.vector.yml`，但本机 Docker Hub 不可达
（`milvusdb/milvus` 拉取 EOF），**standalone 形态未经写入与检索验收**；常驻消费（NATS/outbox）
与网关 `mode=semantic` 都未接线，向量质量（recall/MRR）未验收。决策见
[ADR-020](adr/ADR-020-向量索引落库与检索闭环.md)，实测见 `docs/verification.md` 的
"M8 剩余：向量索引落库与检索闭环（ADR-020）"一节。

媒体格式准入（M9）已按 [ADR-009](adr/ADR-009-媒体格式支持矩阵与拒绝语义.md) 落地：承诺矩阵写成
数据（容器 → 编码 → 位深 → 色彩 → 采样格式 → 声道），判定输入是**解码前采集的源格式上下文**加上
解码后格式，矩阵之外的组合进 `DecodedDataPlane.rejected_tracks`（稳定拒绝码 + 观测值 + 容器 +
解码器元素），命令行打 `rejected=`。`make capability-check` **19 场景**通过：**6 个公开授权正样本**
（HEVC/AAC MP4、VP9/Opus WebM、H.264/AAC MP4、VP8/Vorbis WebM、文件形态 H.264/AAC MPEG-TS、
H.264 + 容器内 `pcm_s16le` MOV）全部 `rejected=0`，13 条拒绝路径逐字命中命名表（10-bit ×2、4:2:2、
5.1、MP3、AVI ×2、裸 ES、字幕 pad、双视频轨、OGG、AC-3、WAV）；`make live-check` 5 场景通过
（MPEG-TS over SRT 未被误拒）。样本出处/许可/SHA-256 登记在
`tests/fixtures/media/OPEN-SAMPLES.md`。验收同时修掉 5 个真实缺陷（`typefind ! decodebin` 直连时
容器证据全失效、"源里没有这种轨道"被误报成 `decode_stalled`、被拒 pad 悬空叠加 `vtdec_hw` 的
GLMemory 协商导致多视频轨竞态、容器内 PCM 的源编码采集不到、WAV/FLAC 被误读成裸 ES），
细节与仍未验证范围见 `docs/verification.md` 的"M9"一节与 ADR-009 §10。
**仍未验证的仍然不得声称可用**：VP8 的位深/采样格式是矩阵推导值；文件形态 MPEG-TS 样本是本机
重编码产物，不代表设备直出；E-AC-3 / DTS / TrueHD 与真实 HDR 素材无样本；旋转（几何）未采集
也未应用；容器级 VFR 与设备直出无样本；`golden_path_verified` 恒为 false。

常驻形态（M5）与 CI（M7）也已落地：`tools/macos_resident.py` 把原生 `sensoryplex-runtime serve`
交给**用户级 launchd** 常驻，按宿主统一内存分级（`small`/`medium`/`large`/`xlarge`，`medium` 锚定
今天的默认上限）渲染 plist 与 `resident.env`，档位之外**不吸附**、探测来源（`sysctl`/`env`/`unavailable`）
显式；`install`/`status --verify-endpoint`/`uninstall` 与 `sensoryplex-media-run` 分级注入上限
都在本机真机验收（崩溃重启、登录自启、`caffeinate -ims` assertion、分级上限在 `handoff_stats` 中实测）。
决策见 [ADR-015](adr/ADR-015-macOS常驻形态与统一内存分级.md)，操作见
[运行手册](runbooks/macos-resident.md)，证据见 `docs/verification.md` 的"M5"一节。
**仍未验证的仍然不得声称可用**：`small` 档与 Mac mini 各档位未实跑；高帧率下的背压样本与
`retry_exhausted` 的真实插件路径未跑；断电重启、休眠唤醒、小时级长稳未验证。
（分级上限的两半都已收口：队列上限由 `replay`/`ingest` 按分级对声明值与真实保留窗口做准入，越界即失败
（[ADR-019](adr/ADR-019-运行时消费分级队列上限.md)）；模型并发由 `tools/ai_worker.py` 按
`SENSORYPLEX_MODEL_PARALLELISM` / 运行时转述的分级上限做准入与在飞调用限流，坏值与越界 exit 2
（[ADR-021](adr/ADR-021-模型worker按分级并发上限限流.md)）。）
远端 CI 的 `check-apple-silicon` 早已真实通过（run 35850290513 / 35860979204，`macos-15-arm64`），
本次新增 `workflow_dispatch`、`uname -m` 硬断言与解码路径单测步骤，并已在远端跑通
（run 35863597690：`check`/`check-console`/`check-apple-silicon` 三个 job 全绿，runner
`macos-15-arm64`、`uname -m` 实测 `arm64`、解码路径单测 **105 passed**，见 `docs/verification.md` 的"M7"一节）。
**默认的容器模式此前其实没跑通过**：集成测试前缀把 `-e` 写在 SERVICE 之后（容器模式 127 退出），
而 `services/api/Dockerfile` 也从未把 OCR / BGE 两个插件注入容器 venv（契约测试 collect error）；
两处都已修复，`make check EXEC_MODE=container` 现在全绿（契约 154 + 集成 24 + cargo 全过），
并记下"Docker Desktop 文件共享缓存可能给出过期构建上下文、需先核对镜像内源码 md5"这条教训，
详见 `docs/verification.md` 的"M7 补记二"。同一批提交在远端也跑了两轮 `engineering-checks`、
三个 job 全绿（run 35896034916 分支 / 35896037193 `master`）；`master` 那次是**外部**快进、
没有经过 PR，需要维护者确认来源（见 `docs/verification.md` 的"M8 BGE"一节末）。
