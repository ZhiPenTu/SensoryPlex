# 0.1.0 工程底座与后续阶段

2026-09-29 逐秒素材修复：文件 Timeline 写侧依据可信 duration 建立完整 `[0,duration)`
1 秒来源切片（尾部不足 1 秒保留，上限 7200 片），模型尚未返回时仅有来源引用与待补充状态，
不合成 Observation。VLM 队列按完整媒体时长登记可重新解码的时间锚点，不再依赖 OCR 成功保留
到内存里的帧；新方案默认每秒摘要，旧方案通过显式 `:segment-seconds` 补齐并审计，原 Revision
和既有素材历史不改写。Console 执行页展示完整秒索引，详情按 100 片分页，10 秒刷新进度。

宿主 `tools/vlm_task_worker.py` 用已安装的不可变 VLM 包逐条消费（并发 1），按时间位置即时
解码并在推理期间续租；macOS 内存准入包含可回收 inactive 页，避免缓存使队列永远等待。
macOS 常驻入口是 `deploy/up-vlm-worker.sh <release_dir> <config.json>`，日志位于
`.data/vlm-worker/`。文件快路径新增无丢弃背压：超时显式失败，音频边解码边消费；直播原有
有界降级策略保持原语义。长媒体全量 OCR/ASR 重新推理尚未验收，不把已有覆盖账本视作模型成功。

实机已为 717007 ms 授权视频保存 718 个连续切片和 718 个异步摘要任务，已有真实 VLM 结果
越过原先约 95 秒截断位置并持续入库。全片摘要仍在后台补充；这不是完整 Golden Path 验收。

依据根目录 ADR 的第 1 周目标建立工程，保留原需求文档作为设计依据。
目标平台包含 `macos-aarch64`（Apple Silicon，含 Mac mini 端侧部署）与 `linux-x86_64`（NVIDIA 性能主线），
见 ADR-008 与蓝图 §1.4。

产品当前以 Web Console 交付，不做桌面客户端。控制面采用一个可调度的主节点和按节点管理的局域网
插件 worker 子节点拓扑（ADR-026）：子节点可与主节点同机，也可安装在局域网内的异构硬件主机；已实现
node agent、节点注册/心跳、Web 安装位置选择、5 项硬性预检、数据本地性强制约束（共享内存仅限同机数据面）、
远程文本观测插件部署以及全生命周期审计与回滚，详见
[ADR-026](adr/ADR-026-Web主节点与局域网插件worker拓扑.md) 与 `make node-check` 验收证据。

2026-09-24 并行状态：**M8 整体仍在研发中**；Timeline 核心已通过 `ffa9b56` 合并主线。
真实媒体端到端联调与语义冲突识别
仍是明确未完成项，见 `docs/TODO.md` 的“当前并行工作与明确未完成项”。
素材查询与回看核心实现已通过 `c05ba8d` 合并主线，筛选、历史版本、观测定位与授权原片播放
已通过浏览器验收，见 [专项记录](verification-material-review.md)。它沿用现有入库事实，
不意味着媒体准入、自动来源映射或模型到素材链路已经完成。

| 范围 | 当前实现 | 下一步 |
| --- | --- | --- |
| Rust Core | 6 crate workspace、Proto、配置校验、有界队列、descriptor 校验 | Pipeline 生命周期、调度、进程与 lease 实际管理 |
| Runtime 服务 | gRPC Health + DescribeCapabilities（平台、宿主内存、允许的 memory kind、每个不可用后端的原因） | NATS JetStream 指令与任务分发 |
| 可编排插件执行核心 | P0 已实现：`orchestration/v1` Proto、受限 DAG 编译、modality/placement 校验、确定性 Run/Task 状态机、`orchestration-check`；已有数据面 lease、节点预检与部署意图 | P1：持久任务、outbox 分发和单节点真实插件调用；当前**没有** Runtime Scheduler、持久 PipelineRun 或可执行 PipelineRun |
| Python SDK | Proto 绑定、输入校验、deadline、取消 token、并发限制、结构化错误；worker 侧生命周期 gRPC 服务、`LeaseBufferReader` 读字节与 lease 归还（已在 vlm-moondream 插件落地，见 ADR-012） | 持久幂等、崩溃后的 lease 回收、沙箱与外发策略执行 |
| Timeline | Material/Observation 校验 | ASR/OCR/VLM 实际融合、冲突判定 |
| PostgreSQL | 显式迁移、不可变素材与模型版本、来源校验、事务 outbox | 保留与归档策略、outbox 消费与补偿 |
| Gateway | Bearer 认证、owner 过滤、素材详情、历史版本、关键词/标签/时间查询 | 外部鉴权、语义检索、短期媒体授权 URL |
| Console / Platform API | 独立 React / TS / Vite 工程、统一模块化 API、会话/CSRF/RBAC、真实上传与 Range 回看、插件配置版本、方案/任务草稿、作用域凭据、账户/角色管理与审计；运行手册见 `docs/runbooks/console.md` | Runtime 媒体准入、安装与生命周期、方案发布、任务执行及素材来源映射；当前不是完整业务 Golden Path |
| 局域网插件 worker 拓扑（必做） | 已实现（ADR-026）：主节点作为唯一控制面权威、受控子节点 Agent（`tools/node_agent.py`）、节点 Registry 与认证心跳、5 项硬性预检与数据本地性约束（共享内存强制同机数据面）、不可变制品部署意图与审计回滚，Web 节点拓扑与安装位置选择完成闭环；6 大验收场景通过（`make node-check`）。边界：其中“目录创建/删除”这一路只是**部署意图通路**（控制面创建槽位 + 下发意图 + 状态回执），**不含**真实制品下载、离线安装、进程托管与候选验证 | 生产级跨机 mTLS 证书自动轮换与高可用主节点选主（后续阶段） |
| 插件热部署执行器（ADR-030，local_native 首期） | 已实现并在 macOS 实机验收：不可变 `plugin_release`（`artifact_digest` = 可执行代码身份、`bundle_digest` = 整包传输内容摘要）、受控平台定向 bundle + Agent 离线安装（解包前拒绝路径穿越/符号链接/超限成员与任一摘要不符，wheelhouse 离线装依赖，部署期不联网）、逻辑槽位 `console_plugin_instance` + `plugin_runtime_instance` / `plugin_deployment_operation` 台账（同槽位旧版 active 与新版 candidate 并存）、状态机 `accepted → staging → starting → validating → candidate_ready → cutting_over → draining_old → succeeded`、事务 + generation CAS 切换 active 指针、Drain → 受限 grace → Stop → 卸载 unit（旧 bundle 留作已验证回滚版本）、显式回滚 = 反向部署操作、Agent 重启只报 `reconciliation_required`；macOS 平台适配器用用户级 LaunchAgent，候选进程 `--port 0` 启动且端口只从插件原子写出的 loopback endpoint 文件读取。验收命令 `make plugin-deploy-check-api`（容器内 92 项）与 `make plugin-deploy-check-native`（宿主真实平台服务与真实首方插件 embed-bge-onnx 的蓝绿/排空/回滚/对账），Console 插件中心「热部署（ADR-030）」页签已通过**浏览器验收**（UI 真实发起升级并轮询到已完成；「当前 active」读槽位实时指针、等于本次候选端点，「本次操作替换」等于契约 `from_runtime_instance_id`），证据见 [验证记录](verification.md) 的“插件热部署执行器”一节 | Linux systemd user unit 适配器**尚未在 Linux 节点验收**（因此不得宣称支持 Linux）；容器插件、第三方未签名插件、意图内任意 URL、跨机共享内存、业务 `Process` 验证与自动 CPU fallback 首期不做；通过上述验收**不等于**整体 Golden Path 完成（`golden_path_verified=false` 不变） |
| 存储/硬件 | Rust adapter traits，模型与配置 hash 契约；向量落库与检索走 `services/index-worker` 的 Milvus（本机 **Lite 文件形态**，写后回读确认） | NAS/MinIO、服务端 Milvus 拓扑（本机 Docker Hub 不可达，未验收）、ONNX/TensorRT 实现 |
| 直播接入基础设施 | 本机 MediaMTX 1.21.1（独立 Compose，仅回环端口）；SRT 直推（GStreamer `srtsink` 与用户自有 OBS）与 Runtime `ingest` 已打通：稳定窗口、断流恢复、无源失败、实时数据面交接、VideoToolbox 视频五个场景通过，OBS 真实直推亦实测（无 timing 码流的视频时长按 PTS 差分补齐），见 `docs/verification.md` | Mac mini / 跨机部署、SRT 加密与带凭据 publish、`linux-x86_64` 侧验收；服务器上有流不等于语义链路可用 |
| 媒体与模型 | Pipeline 配置、真实媒体 probe 工具、ffprobe 锚点回放，GStreamer 真实解码 → arena → `BufferDescriptor` → lease 签发/校验/释放 → 音频 5 秒切段，视频自适应抽帧（keep/skip 全部带原因，7 个真实样本通过），跨进程数据面：Runtime 保留字节、独立进程按 lease 读取（3 个样本 × 2 个场景通过，具备有界容量与稳定拒绝码），SRT 实时接入 `ingest`（`make live-check` 五个场景通过），背压与队列可观察：三条有界队列的深度/峰值/容量、按原因与按种类的丢弃、lease 等待时间（`verify_backpressure.py` 4 场景 + OBS 直播实测，见 `docs/verification.md`），以及第一个**端侧模型插件**：本机 ollama `moondream:v2`（VLM），插件经 `LeaseBufferReader` 读真实视频帧产出带锚点/来源/版本/显式置信度语义的 observation，`tools/ai_worker.py` 只发现与调用不读字节，`make model-check` 四进程通过（见 `docs/verification.md` 的"M8"一节与 ADR-012），以及**媒体格式准入与显式拒绝**：承诺矩阵写成数据、源格式按 stream ID 关联、被拒轨道带稳定拒绝码进报告（`make capability-check` **19 场景**通过：6 个公开授权正样本 + 13 条拒绝路径，见 `docs/verification.md` 的"M9"一节与 ADR-009），以及第二个**端侧模型插件**（ASR）：本机 MLX Whisper 经 `LeaseBufferReader` 读真实音频段产出带锚点/来源/显式置信度语义的转写 observation，音频样本布局（`sample_format`）与音频段描述符进保留表一并落成契约，`make asr-check` 四进程通过（见 `docs/verification.md` 的"M10"一节与 ADR-014），以及第三个（OCR）与第四个（BGE 文本向量）端侧模型插件：OCR 以随包携带的 PP-OCR 组合权重的**组合摘要**为身份、产出带帧像素坐标的文字块；BGE **不接数据面**（`acceptsMemoryKinds: []`），消费上游 OCR 事实产出**维度版本化**的 L2 归一化向量，`make ocr-check` / `make embed-check` 均多进程通过，以及**向量落库与检索闭环**：`services/index-worker`（`sensoryplex-index`）把 BGE 向量写进 Milvus（本机 **Lite 文件形态**）并**读回来确认**才置 `embedding_record.state='ready'`，检索命中必须回查 PostgreSQL 的 `ready` + material 存在 + `source.owner` 才允许返回（被丢弃的命中单独计数），`make index-check` **11 个场景**通过（见 `docs/verification.md` 与 ADR-020），以及**宿主加速器能力上报**：`DescribeCapabilities` 新增 `host_accelerators`，与执行后端分成两张表、三态不得互相塌陷，`make accelerator-check` 四路对账通过（本机 `coreml=available(3520.5.1)`、`metal=available(metal4)` 与宿主直读逐字一致；`LANG=zh_CN.UTF-8` 判定不变；`PATH=/nonexistent` 落 `unknown` 而**不是**"不存在"；见 `docs/verification.md` 的"M8 剩余：宿主加速器能力探测与上报（ADR-022）"与 ADR-022），以及**outbox 分发接线与消费去重边界**：新增 `services/outbox-relay`，把事务性 outbox 的事件
**确认发到** NATS JetStream（`published_at` 只在确认之后写、`Nats-Msg-Id = event_id` 由 duplicate
window 吸收重发、stream 漂移只报不改、NATS 不可达显式失败而不是静默挂着），sink 侧去重原语
`is_consumed` / `record_consumed` 的键是 `(event_id, consumer_name)`，`make outbox-check`
**9 个场景**在真 PostgreSQL + 真 JetStream 上通过（见 `docs/verification.md` 的
"outbox 分发与消费去重边界（ADR-024）"与 ADR-024），以及**网关语义检索接线**：`mode=semantic` 从 501 变成真实检索——常驻检索面（`sensoryplex-index serve`）是持有向量库的唯一进程，API 只转发查询 + 按 `(material_unit_id, revision)` 水合事实，查询向量用 BGE 插件自己的 `Start` 编码并按 `model_release_id` 做 collection 级同源守卫（异源或混装整请求拒绝），未配置/不可达/令牌不符与"检索面答了但不是本契约"按失败发生位置分 503/502，`retryable` 是独立标记（503 也可能是不可重试的配置错误），`make semantic-check` **13 个场景**通过（见 `docs/verification.md` 的"网关语义检索接线（ADR-023）"与 ADR-023） | Rust 侧仍没有任何 in-process `ExecutionBackend`（`model_inference` 恒在 `unavailable_capabilities`；宿主加速器探测只在开发机 `macos-aarch64` 实测，`cuda` 分支与 Mac mini 均未验收）；**常驻消费循环（当时）未写**——outbox → JetStream 的**发布**已接，但当时判断上游 observation 没有事件、
`material.upserted` 也带不了可编码文本——该判断已由 ADR-025 改写）；RRF/混合检索与相关性校准未做；服务端 Milvus 形态（本机 Docker Hub 不可达，未验收）；ASR 的 Linux 后端（`mlx` 是 Apple Silicon 专属）；插件**未签名**（`local_native` 形态，签名/SBOM 只有结构预检）；旋转的采集与应用（v1 未实现） |
| 工程 | uv/Cargo 锁文件、Docker、检查命令、CI（`check`/`check-console` + **Apple Silicon** `check-apple-silicon`，远端 `macos-15-arm64` 已真实通过）、macOS `launchd` 常驻形态与统一内存分级（`tools/macos_resident.py`，见 ADR-015） | 真视频 Golden Path、Linux NVIDIA 侧 CI、压测、监控仪表盘 |
| 开源使用文档站 | `apps/docs`：VitePress 1.6.4 静态站，英文默认在 `/`、简体中文在 `/zh/`，两棵语言树逐页对等（23 页 × 2）；能力描述只带三态标签（已验证 / 未验收 / 未实现）与证据命令，`golden_path_verified=false` 等边界原样保留；`make docs-install`（唯一联网步骤）/ `docs-build` / `docs-check`（语言树对等 + 产物内部链接校验）/ `docs-dev` / `docs-serve` 全部在 `docs` 容器内执行，由 `docs` 服务（`127.0.0.1:5174`，纯静态、不反代、不持凭据）托管；`DOCS_BASE` / `DOCS_SITE_URL` 是构建期开关，未设置时表示"尚未发布"（不产出 sitemap、不编造域名）。验收证据见 [验证记录](verification.md) 的"开源使用文档站"一节 | 更多语言（日/韩：加语言目录 + 一份 `locales` 配置）；公网发布与子路径托管实测；远端 CI 接入 `make docs-check` |

## 可编排插件执行核心（ADR-029）：P0 编译内核、P1 持久执行与 P2 受控多节点集群编排已闭环落地

不可变 Pipeline revision → DAG 编译 → `PipelineRun` / `PipelineTask` 状态机 → 基于数据本地性与资源上限的
多节点集群调度 → 跨节点任务认领与可审计故障转移（Failover）已在 P1/P2 完整闭环落地：

1. **不可变 Revision 与持久化模型**：
   - 追加数据库迁移 `0008_orchestration_run_task.sql`，落 `pipeline_definition`、`pipeline_revision`、`pipeline_run`、`pipeline_task`、`pipeline_task_edge` 与 `scheduler_assignment`；
   - `pipeline_revision` 挂载 `deny_fact_update` 触发器，发布后禁止原地修改或删除；
   - `pipeline_run` 建立基于 `(pipeline_id, revision, input_ref, idempotency_key)` 的部分唯一索引，实现活跃状态严格幂等。
2. **状态机与调度硬约束**：
   - 调度器基于数据本地性与节点角色派发就绪任务；`same_item` 关联的 `data_plane_local` 任务若被指派到远程节点，显式以 `data_locality_violation` 记录拒绝；
   - 任务结果对账严格校验 `(run_id, task_id, attempt, assignment_id)`，过期或非活跃结果拒绝为 `stale_task_result`；
   - 必需上游成功后递归级联解锁下游就绪任务；必需上游失败时递归级联阻断下游任务（`state='blocked'`，`reason_code='upstream_failed'`）；
   - 取消优先原则：Run 取消后级联未完成任务，迟到结果安全审计丢弃，绝不反向改写为成功，绝不解锁下游；
   - 有界重试机制：可重试错误进入 `retry_wait` 状态，退避后刷新 deadline 重派，超过 `max_attempts` 显式落 `retry_exhausted:<reason>`；
   - 崩溃恢复器：原子扫描过期租约，安全收敛已写入事实或重置回 `ready` 重新派发，尝试耗尽则标记失败，彻底杜绝孤儿任务与重复计算。
3. **接口与验收**：
   - API 提供 `/v1/orchestration/pipelines[:validate]`、`/v1/orchestration/runs[/:id/cancel]`、`/v1/orchestration/scheduler:step`、`/v1/orchestration/tasks/:id:result`，受 `pipelines:manage`、`jobs:write`、`jobs:read` 保护；
   - 验收命令 `make orchestration-p1-check`（7 大核心场景）与 `pytest tests/integration/test_orchestration_api.py` 全部在真实 PostgreSQL 上通过。

P2 已闭环交付：
1. **多节点算力感知与数据本地性硬过滤**：同机节点（`is_co_located=True`）独占消费 raw BufferDescriptor；远程 GPU / Edge 节点仅处理 observation / object reference；跨机 raw buffer 边在解析期与调度期均被双重硬拦截（`data_locality_violation`）；
2. **节点排空与离线安全阻断**：节点处于 draining / offline 状态时，预检与任务认领均被 409 拒绝，严禁静默降级或任意改派；
3. **可审计故障转移（Failover）**：当节点失联或租约超时，恢复器原子检测并回收任务，重新派发至备用算力节点，在持久账目中完整保留第一任与第二任两次 assignment 历史记录；
4. **多节点验收工具**：`make orchestration-p2-check` 全量通过 6 大核心分布式编排场景。

P3（场景产品包、Console/API 运维与真实媒体业务闭环）现为下一阶段门禁。

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
[ADR-012](adr/ADR-012-模型插件与端侧推理边界.md)。这一节当时列的"仍未实现"里，
**运行时侧加速后端能力上报**（[ADR-022](adr/ADR-022-宿主加速器能力探测与上报.md)）、
**常驻 index-worker 消费**（[ADR-025](adr/ADR-025-常驻消费循环与sink接线.md)；2026-09-25 起
"本事件**应产出**的向量"由**文本契约**决定——观测给不出可编码文本就按原因计数跳过、
不再让整条素材陪着重投耗尽，状态行随之多两个计数字段，见其 §5/§8 的修订）与
**网关侧语义检索**（[ADR-023](adr/ADR-023-网关语义检索接线与索引检索面.md)）都已经落地，
该归因已过期；**仍未实现的是插件签名验证**，且 `metal` 在 ONNX 路径上没有独立执行后端。
截至 2026-09-25，`golden_path_verified` 恒为 false 的原因是**素材链路尚未整体闭环**：
Runtime → Timeline → 追加已验收（[ADR-028](adr/ADR-028-Runtime到Timeline接线与授权追加.md)），
"融合出的素材经**常驻** relay/index 写成向量、再被 api 语义检索命中"也已验收
（`make timeline-resident-check`，2026-09-25），**剩下的**是"经 HTTP 查询与**回看**"、"同一素材的
**revision 前进**"与"**向量 GC**"这三段没走通。
不得把本节读作 Golden Path 已完成；
接入的 VLM 只保证链路语义正确，**不保证描述可用**（模型输出不稳定）。
抽帧口径已升级为**全帧判别 + 有界证据窗口**（见下节“多模态文件方案执行闭环”第 5 项）：
**输入完整性**（每一个被解码的视频帧都有明确结论）与**语义覆盖**（静态画面相邻语义输入不超过
`evidence_max_gap_ms`）都已用真实授权样本核对；但语义覆盖量的是**输入**，不等于**模型输出质量**
——“每个窗口/锚点都拿到了可用的模型描述”仍未验收。
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
**不接数据面**的链路——输入是上游 OCR 文字块（`observation.ocr_blocks`）或受控 VLM 场景描述
（`observation.vision.scene_description.text`），不是字节，
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
已由 [ADR-025](adr/ADR-025-常驻消费循环与sink接线.md) 接上（消费与检索面同进程），向量质量（recall/MRR）未验收。决策见
[ADR-020](adr/ADR-020-向量索引落库与检索闭环.md)，实测见 `docs/verification.md` 的
"M8 剩余：向量索引落库与检索闭环（ADR-020）"一节。

网关语义检索接线（M8 剩余项）也已落地：`mode=semantic` 不再是 501。**Milvus Lite 是进程独占的**
（ADR-020 §7），所以"网关接上检索"不等于"网关自己检索"——索引持有者 `services/index-worker` 新增
常驻形态 `sensoryplex-index serve`（gRPC 检索面：共享令牌 + 常量时间比较，默认只绑回环
`127.0.0.1:50077`，无令牌或令牌不足 32 字符拒绝启动），调用方只提交**查询文本**：编码查询向量、
向量库近邻、PostgreSQL 事实回查都在这一侧完成。API（`infrastructure/semantic.py`）只做转发 + 按
`(material_unit_id, revision)` 重新水合素材事实——**命中不是事实源、也不是鉴权依据**，水合不出来的
命中计入 `unresolved_hits`、检索面丢弃的命中计入 `unindexed_hits`，结果集不静默变小。查询向量用 BGE
插件自己的 `Start` 装配（不复制第二套模型），并按 `model_release_id` 做 **collection 级同源守卫**：
另一个 release 或同库混装 → 整请求拒绝（`query_model_release_mismatch` /
`vector_index_model_release_mixed`）。HTTP 状态码只表示**失败落在哪一环**（没走到检索面 → 503；
检索面答了但不是本契约 → 502），`retryable` 是**独立**标记——`semantic_index_unauthenticated` 与
`semantic_search_unavailable` 都是 503 却不可重试。本切片**只做纯语义**：`mode=semantic` 只接受
`query` + `limit`，其余筛选条件显式 422 `semantic_filters_not_supported`（静默忽略筛选会给出
"像是筛过"的结果）；keyword 不排名，`hits` 为空而不是补一串 0 相似度；RRF/混合检索与相关性校准未做，
`distance` 是 COSINE **相似度**（越大越近、按降序返回；字段名是历史遗留，值不是距离）、不是置信度。`make semantic-check` 用真实 BGE → 真实 Milvus Lite →
常驻检索面 → 真实 API 走 HTTP **13 个场景**全过（含同源守卫两种形态、启动即拒契约漂移、目录锁、
令牌不符与不可达的状态码/`retryable` 分野、线上不外泄）。决策见
[ADR-023](adr/ADR-023-网关语义检索接线与索引检索面.md)，实测见 `docs/verification.md` 的
"网关语义检索接线（ADR-023）"一节。

**outbox 分发也已接线**（[ADR-024](adr/ADR-024-outbox分发接线与消费去重边界.md)）：新增
`services/outbox-relay`（`sensoryplex-relay`），把 `append_material` 在同一事务里写的
`material.upserted` 事件**确认发到** NATS JetStream。四条写死的语义——`published_at` 只在
JetStream 确认之后写（发布失败时保持 `NULL`，"压根没发出去"绝不会被读成"已经发过"）；
`Nats-Msg-Id = event_id`，崩溃在"已发布、未提交"之间的重发由 duplicate window 吸收；
认领用 `FOR UPDATE SKIP LOCKED` 且**立刻提交**（不跨网络持锁，重复发布由上一句吸收，
比"慢 NATS 拖住行锁"便宜）；stream 由 relay 建有界契约（`max_msgs` / `max_bytes` / `max_age`
/ `duplicate_window` 都写死上限），**漂移只报不改**（静默改小保留策略会丢掉还没被消费的事件）。
`make outbox-check` 在真实 PostgreSQL + 真实 JetStream 上 **9 个场景**通过（含逐字节对账、
重放去重、漂移不被修好、NATS 不可达时一行都不写），`make outbox-run` 是常驻形态。
**边界（当时）**：这一项只是"发布这一跳"。当时判断消费循环"不只是接线问题"——上游 observation
连 `event_id` 都没有，`material.upserted` 也携带不了 BGE 需要的 `ocr_blocks` 文本；
sink 侧的消费去重原语（`is_consumed` / `record_consumed`，键 `(event_id, consumer_name)`）
已就绪并有测试，但当时**没有任何常驻消费者在用它**。

**消费那一跳也已接上**（[ADR-025](adr/ADR-025-常驻消费循环与sink接线.md)）：
`sensoryplex-index serve --consume` 把常驻消费挂在**持有向量库的那个进程**上（Milvus Lite 的
数据目录是进程级 flock，拆两个进程要么新写入的向量对检索面不可见、要么第二个进程直接
`vector_store_locked`）。前一段"要先补 observation 事件契约"的判断被这一切片**改写**：事件只是
通知，`payload_ref = material:<id>:<rev>` 形状显式校验，可编码文本按引用回查
`observation.payload_jsonb`（读 payload 的规则仍只有插件那一份实现），回查不到即
`event_missing_facts`（事实与事件同事务 → 那是写侧缺陷，不是"没数据"）。顺序是
编码 → 落库 → 确认写入 → `record_consumed` → `ack`；少一条向量就不算消费完成——重投到
`--max-deliver` 上限后以 `event_retry_exhausted` **退出码 3** 显式停止（没有 dead-letter）。
消费端**绝不自动建 stream**（`event_stream_missing`），只建自己的 durable；stream/durable 漂移
一律只报不改（`event_stream_contract_mismatch` / `event_consumer_contract_mismatch`），
subject 是精确订阅而不是 `>` 通配。`make consume-check` 用真实写侧 + 真实 relay + 真 JetStream +
真实 BGE 权重 + 真实 Milvus Lite + 真实 gRPC 检索面跑 **6 个场景**全过（事件驱动写入 → **同一进程**
的检索面立刻检索到、换 durable 重放不重复、目录锁与优雅停止、坏事件 fail-stop、三类启动期显式失败、
状态行不外泄）。决策见 [ADR-025](adr/ADR-025-常驻消费循环与sink接线.md)，实测见
`docs/verification.md` 的"常驻消费循环与 sink 接线（ADR-025）"一节。
**仍未做**：dead-letter 与按原因分流、`ack_wait` 到期重投的单独验收、多副本消费、吞吐曲线。
（"消费侧/relay 进 compose"与"ADR-019 的队列上限接到这一层"已由下一段的 ADR-027 收掉。）

**事件链路的分级背压与容器化常驻也已接线**
（[ADR-027](adr/ADR-027-事件链路分级背压与容器化常驻.md)）：ADR-019 的"分级是准入上限"从媒体面
（Rust `replay`/`ingest`）对称地接到了事件链路。分级表新增**独立一列** `event_queue_capacity`
（16/32/64/128，今天与媒体队列同值——要分化只改这一张表，而不是让事件链路悄悄继承媒体队列那个数），
渲染成 `SENSORYPLEX_EVENT_QUEUE_CAPACITY`。relay 的 `--batch`（每轮认领）与 `serve --consume` 的
`--consume-batch`（= `max_ack_pending`，在飞未 ack）任一超过本档上限就**拒绝启动**
（`event_inflight_exceeds_tier_cap: declared=<n> tier_capacity=<n> tier=<name>`，与 Rust 侧逐字对齐），
变量缺失是显式 `not_injected`（上限记 0，**绝不**填默认值）、空串与坏值报 `invalid_resident_limit`
——三种输入三种结果，不夹取、不改写。两个进程**各自判定**、不共享令牌桶：relay 的在飞是"已认领未发布"、
消费的是"已投递未 ack"，合成一个数会把两件事抹平并引入新的跨进程协调点。两个状态行（`relay.status` /
`consume.status`）与 `serve` 的就绪行各多 4 个字段（`inflight_state` / `inflight_declared` /
`inflight_capacity` / `resident_tier`）。`relay` 与 `index` 以 `events` profile 进 compose
（`make events-up` / `events-down` / `events-logs`）：默认栈不拖起 BGE 与向量库；向量库落仓库
bind mount 的 `.data/index/`（**不用命名卷**——挂载点 root 所有会让非 root 容器 `PermissionError`）；
relay 的健康检查是"状态行还在滚动"（停在那里的 relay 不该被读成在跑），index 的是"端口真的开了"
（端口在编码器与向量库契约**之后**才开，但**早于**消费侧接上——"消费真的接上了"的凭据是 ready 行，
它在 `runner.wait_ready` 确认之后才写）；api 顺带接上检索面（`index:50077` + 令牌，`.env` 里
`AUTH_TOKEN` / `SEARCH_TOKEN` 两个名字由 `tools/configure.py` **一次** replace 写成同一个随机值）。
`make event-pipeline-check`（`tools/verify_event_pipeline.py`，**api 容器内**）用真实写侧 +
**compose 里常驻的** relay/index + 真实 NATS JetStream + 真实 BGE + 真实 Milvus Lite + 运行中 api 的
`POST /v1/materials:search` 跑 **7 步**全过（准入对账与越界拒绝、真事件被搬运、真向量 ready、
HTTP 语义检索命中且排第一、事实回查挡住 stale、清理为 0、不外泄）。
**边界**：接的是**准入**不是吞吐（没有吞吐/延迟曲线）；向量本体**不随事实行删除而消失**（验收因此用
"本次运行唯一的批次标记 + 相对基线"判定，并如实报告留下的条数），向量 GC 未做；
服务端 Milvus 形态、多副本消费、跨主机 NATS 集群、`ack_wait` 到期重投仍未验收；
`make event-pipeline-check` **不**覆盖检索面停机降级（那由集成测试覆盖）。
本切片顺带修掉 4 个真实缺陷（见 ADR-027 §10）：宿主环境绑架契约/集成用例、准入抛错位置在
`try` 之外、空串配置让 api 拒绝启动，以及**检索面的配置来源曾经是"venv 放在哪"的函数**——
`pymilvus.settings` 在 import 期调用 `load_dotenv()`，python-dotenv 从它所在目录向上找 `.env`，
宿主 venv 就在仓库根下所以命中了仓库 `.env`（容器里在 `/app/.venv`，不命中），于是
`serve` 的"缺失即拒绝启动"契约在主机上不成立。现在 `cli` 只读 import 期取好的
`BASE_ENVIRON`（`services/index-worker/src/sensoryplex_index_worker/environ.py`）。
实测见 `docs/verification.md` 的"事件链路分级背压与容器化常驻（ADR-027）"一节。

Runtime → Timeline 接线（[ADR-028](adr/ADR-028-Runtime到Timeline接线与授权追加.md)）也已经落地：
`crates/timeline` 此前是一个**没有任何调用方**的叶子 crate（workspace 里只有它自己的 `Cargo.toml`
提到它），两条 pipeline 里的 `- type: timeline_fusion` 也没有实现。现在新增 Runtime 子命令
`sensoryplex-runtime timeline`（真探测原片身份 → 读真运行报告 → 按 pipeline 声明的栅格选窗 →
逐条准入 → 调融合核心 → 写素材 protobuf 与 JSON 报告；**不碰数据库、不发事件**）、授权写入口
`tools/timeline_handoff.py`（登记引用事实后调真实写侧 `append_material()`，事实与 outbox 行同事务）
与验收 `make timeline-check MEDIA=<授权样本>`（**主机执行**：runtime 二进制是主机 Mach-O，容器里
`Exec format error`；PostgreSQL 与 NATS 仍在 compose 里，从宿主回环端口连，验收自建隔离 schema 与
独立 JetStream stream）。真实授权样本实测：6 帧观测 → 7 窗（5 窗有观测）→ 5 条素材、`rejected=0`；
入库字节与磁盘 protobuf 逐字节相同；第二遍追加 `appended=0 replayed=5`、relay 第二遍 `published=0`；
三类失败（owner 漂移 / 同 revision 换内容 / 报告视图被改）显式拒绝。落地时修掉一个"单测全绿但
真链路一条都过不了"的缺陷：worker 报告的 protojson 把 int64 写成**字符串**并省略零值字段，
读取侧此前只接受 JSON 数字，于是每一条从 0 ms 开始的观测都被判为不可解析。
**边界**：只接文件源与单次运行（一律 `revision=1`）；`make timeline-check` 自己只有 VLM 一种模态
⇒ 每条素材 `status=partial`（**不是** `fast_ready`），因此它停在"事件已确认发到 JetStream"，
`vector_index_not_exercised` / `semantic_search_not_exercised` 这两条 blocker 对**这条命令**仍然成立。
"融合出的素材被**常驻** relay/index 写成向量、再被 api 语义检索命中"由同一个切片的第二段
`make timeline-resident-check`（`tools/verify_timeline_semantic.py`，2026-09-25）验收：那份验收跑
**两遍**回放（VLM + OCR），实测 5 条素材**全部**同窗带 `ocr_blocks` 与 `vision.scene_description`、
常驻搬运 `published +5` / `consumed +5` / `embedded +6`、6 行向量 ready、语义检索 28 条命中里包含
本次全部 5 条素材；换第二个样本（`officehours-panel.480p.vp9.webm`）同样通过，且该样本 6 条
`ocr_blocks` 观测里**真的**出现 1 条空文本——按原因计数跳过而不是 fail-stop，两个常驻容器
`RestartCount` 为 0。**仍未验收**：revision 前进、HTTP 查询回看、SRT 实时源、向量 GC。
实测见 `docs/verification.md` 的"真实媒体端到端：Runtime → Timeline 融合与授权追加（ADR-028）"与
"真实媒体端到端（续）"两节。

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


## VLM 延迟满足 WorkQueue（ADR-031）已接入并完成消息层验收

2026-09-28 起，新发布的 `orchestrated_v2` 文件多模态 Revision 不再将 `vlm_enrich` 编译为同步 DAG
节点：OCR、ASR 与 Timeline 仍是必需 L1；VLM 的制品、配置、提示词和采样策略作为 Timeline 的不可变
`delayed_enrichments` 写入 revision。L1 事实入库后，任务即转为 `ready_for_review`，基础素材、原片回放
与文字定位不等待 VLM。

- 迁移 `0014_vlm_delayed_enrichment.sql` / `0015_vlm_failure_result_receipt.sql` 新增 VLM 任务、任务
  outbox 与结果回执账本；发布前不写 `published_at`，结果融合在 PostgreSQL 事务成功后才 ACK；成功和
  不可重试失败都可幂等重放。
- `sensoryplex-tasks` 是独立的 JetStream WorkQueue；`vlm-publisher` 创建/严格对账流，宿主 VLM
  Consumer 以 `fetch(batch=1)` 竞争任务，`vlm-result-fuser` 消费结果后增量追加 Observation/Material，
  再触发现有 material outbox → relay → index 链路。
- Consumer 只收 content-hash 受控定位符和时间锚点，本机按需解码单帧；它必须有显式可用内存水位，低于
  水位或无法探测时不会拉取任务。没有 Raw Buffer、宿主路径、模型密钥或解压像素越过消息边界。
- Console 对该 VLM 的新配置固定为 `local_decode` 并携带最低 256 MiB 的本机余量水位；其完整配置摘要
  与 Pull Consumer 严格比对。`per_request` 只保留给 OCR/ASR 的 descriptor 路径，不能再发布成 VLM
  慢路径 Revision；Consumer 模块以 `python -m` 原生启动时会实际进入拉取循环，而不是静默退出。
- `make vlm-workqueue-check` 已在 Compose NATS 验证两个独立 Consumer 竞争十条任务、每条只 ACK 一次，
  以及未 ACK 任务经 AckWait 的第 2 次投递。`./deploy/up-events.sh` 会启动并健康检查 publisher/fuser；
  其状态行在 `.data/events/`，不能把“容器启动”读作 Consumer 已就绪。
- 既有 index Consumer 已扩展为只对 OCR `blocks[].text` 与 VLM `vision.scene_description.text` 两种受控
  文本形态生成 BGE 向量；prompt、帧元数据、ASR 和未知 modality 仍显式排除。PostgreSQL 集成测试验证
  延迟 VLM Observation 会写成 ready 向量记录。

**仍未验收：** 授权媒体在 native VLM Consumer 节点上的真实按需解码、真实 Moondream 推理、延迟描述进入
常驻索引并经语义检索/Range 回放可见，以及 VLM 满负载时的 RSS/统一内存曲线。因此
`golden_path_verified=false` 保持不变；本节不能替代真实媒体 Golden Path。

## 多模态文件方案执行闭环（ADR-028 / ADR-029 / ADR-030 桥接，历史同步 VLM 记录）

> 2026-09-28 的 ADR-031 已将新 Revision 的 VLM 从本节描述的同步 DAG 路径拆出。以下记录保留此前
> 实现与验收背景，不能被用来宣称当前延迟满足路径的真实模型媒体闭环已经完成。

依据 [多模态文件任务执行闭环方案](design/multimodal-pipeline-execution-plan.md)，已完成同机 `local_native` 完整多模态执行闭环：

1. **执行模式升级为不可变 Revision**：
   - Console 方案发布不再仅写入逻辑配置，而是编译为不可变 `PipelineRevision`（DAG 依赖、placement、modality、deadline 与采样策略）。
   - 数据库迁移 `0011_multimodal_execution_bridge.sql` 建立不可变外键，新任务强制绑定 `orchestrated_v2`，任务分发时在同一事务中登记不可变快照 `console_job_execution`。
2. **受控执行器与回执审计**：
   - 宿主原生 `tools/task_executor.py` 由 Node Agent（`tools/node_agent.py`）按分配认领任务，从 ADR-030 本机活跃插件获取 loopback 端点，通过同机共享内存与 GStreamer 解码完成分段分帧；
   - 真实调度 PP-OCRv6、Whisper MLX 与 Moondream VLM，每项任务必须提交带制品与配置摘要的不可变 `TaskExecutionReceipt` 才能终态；
   - 遗留工作器 `tools/task_worker.py` 与 `task_runner.py` 隔离为仅处理历史兼容任务，无法越权领取 v2 任务。
3. **1 秒网格覆盖层与素材隔离**：
   - 迁移 `0012_timeline_coverage.sql` 引入不可变追加式 `timeline_window_state`，按 1 秒网格记录每一秒在各模态下的真实状态（`observed`、`not_sampled_by_policy`、`not_applicable`、`failed`），空秒绝不捏造假素材；
   - 素材落库关联 `material_execution`，素材列表与详情查询默认按 `execution_id` 过滤隔离。
4. **自动化验收覆盖**：
   - `make multimodal-pipeline-check`：20 项通过（验证方案校验拒绝、不可变 Revision 发布、节点实例预检与快照生成）；
   - `make multimodal-execution-check MEDIA=...`：实测通过，有声真实样本 103.352s 真实产生 11 个素材单元、104 个覆盖窗口，三模型与 Timeline 融合全部真实执行并通过回执验收。
   - 边界：`golden_path_verified=false` 保持不变；MLX ASR 仅限 macOS；Linux 未伪装可用。
5. **语义覆盖取代固定抽帧（全帧判别 + 有界事件证据窗口）**：
   - Runtime 的 `replay` / `ingest` 在 `--evidence` 下对**每一个被解码且可用的视频帧**判别一次，
     结论只能是选中（基线锚点 / 事件锚点 / 事件前上下文 / 事件后上下文）或**带稳定原因**被覆盖
     （`no_change_yet`、`event_rate_limited`、`event_budget_exhausted`、`missing_signature`、
     `non_monotonic_pts`）；逐帧账本写成独立 JSONL artifact，报告只带聚合计数与有界窗口预览，
     `frame_ledger_entries` 只统计帧记录；
   - 静态画面的语义刷新上界由 `evidence_max_gap_ms`（默认 1000 ms）保证，变化画面由
     「预上下文 + 锚点 + 后上下文（默认各 2 帧，单请求 ≤ 9 帧）」的有界事件窗口覆盖；
     超限是显式计数（`suppressed_event_keeps`、`event_budget_exhausted`），不静默降级成“没有变化”；
   - 执行器（`tools/task_executor.py`）不再自行采样：VLM 按证据窗口（外加没有窗口的静态基线锚点）
     请求，OCR 只按锚点请求，ASR 仍按有序音频段；请求数与单请求帧数都有硬上限；
   - 数据面是**真背压**：handoff 服务在解码**之前**启动，执行器在解码进行中按窗口增量领料并立刻
     归还，容量类拒绝在有消费者时按 `handoff-wait-timeout-ms` 有界重试；
   - **诚实降级**：锚点帧被拒（或窗口锚点从未交接）时整窗登记为
     `data_plane_retention_rejected` 并跳过该输入单位，已交接的上下文帧立刻归还，覆盖层把对应
     秒格标成 `not_observed:data_plane_retention_rejected`；账本说交接成功、保留表却已经没有这一帧
     记可重试的 `data_plane_buffer_missing`。两者都不再让整条 Task 以 `evidence_window_empty` 失败；
   - 真实授权样本核对（`uv run --frozen python tools/verify_replay.py --media <样本>`，默认启用全帧判别）：
     720p / 34.5 s 样本判别 1034 帧 = 选中 59 + 带原因覆盖 975、8 个事件窗口、
     `max_selected_gap_ms=1000`；1080p / 103.35 s 样本判别 3099 帧 = 选中 187 + 带原因覆盖 2912、
     19 个事件窗口、`max_selected_gap_ms=1000`；同一样本的执行器分组核对得到 VLM 118 次请求 /
     170 帧（19 个窗口 + 99 个静态基线锚点）、OCR 118 次请求 / 118 帧，单请求最大 4 帧；
   - **有界保留面的两种真实结局**（同一份 1080p / 20 s 授权样本，两次完整 `orchestrated_v2`
     Execution，只改 Revision 里的保留面大小）：2 GiB 下三个消费节点 `retain_rejected=0`、
     `characterized_frames=600`、`windows=19`、`requests=84`，Execution `succeeded`；
     128 MiB 下数据面 `backpressure state=saturated`、`retain_rejected=243`
     （`arena_capacity_exceeded`，其中 `video_frame` 59 帧），执行器记
     `retention_rejected_frames=59` / `retention_rejected_units=27` 并跳过这些单位，Execution
     仍 `succeeded`；覆盖层 20 个 1 秒窗中 17 个标成 `data_plane_retention_rejected`，
     **没有** `evidence_window_empty`。两次都满足 `handoff_shutdown reason=plane_drained`、
     `concurrent_with_decode=true` 与保留/拒绝恒等式；
   - **同轮修掉的缺陷**：被拒帧的账目先把 `buffer_id` / `source_digest` 写成空串，Timeline
     账本构建把它读成真实描述符并以 `worker_report_invalid_digest` 让整条 Task 失败；改成只留
     时间范围与原因码后，受压运行整条 Execution `completed`；
   - **未解决（待决策）**：1 秒窗格与 6 秒音频段互斥，ASR Observation 被 Timeline 以
     `observation_crosses_window` 显式拒绝（不裁剪），因此只进覆盖层、不进素材
     （素材保持 `pending_enrichments:["asr_segment"]`）；
   - **证据边界**：这一层证明的是**输入完整性**与**语义覆盖**（间隔上界 + 变化窗口），
     **不**证明**模型完整性**——每个窗口/锚点是否真的产出了可用的模型输出仍未验收；
     `golden_path_verified=false` 保持不变。

## 2026-09-28：插件中心一键装配恢复真实安装

- 一键装配改为同步受控制品仓并创建 ADR-030 部署操作；只选择与当前目录的版本、代码摘要和
  节点平台完全匹配的首方 release。每个插件独立事务，进行中的操作复用，已就绪实例跳过；
  候选从 planned 起预留资源，批量最多 16 项，不再发送仅写本地记录的旧 install 意图。
- Console 展示逐项未受理原因、实际部署阶段与持续刷新的活跃实例数；目录上的
  “本机已安装运行”要求 ready、active 指针、endpoint 和当前代码摘要均匹配。
- 旧 Agent install/start/rollback 意图显式报 `controlled_release_required`，不再虚报安装成功；
  legacy task worker 不覆盖已登记 Agent 的心跳或下线状态。
- 本机 Agent 的 LaunchAgent 已从失效工作区路径恢复到当前仓库并重新登记；保留原有插件台账。
  启动已有 Ollama 服务，保存本机 BGE 权重配置。VLM 0.1.3 修复 `local_decode` 生命周期启动
  误读静态 handoff 地址，以及 protobuf Struct 将整数内存水位变成 float 的校验问题；
  Embedding 当前源码打包为不可变 0.1.1 发布包，旧包及失败操作不覆盖、不删除。
- 实机证据：通过浏览器一键装配，四项部署均 `succeeded`，逐一 gRPC Describe 的插件身份、
  版本和摘要与 active 台账一致，Health 均 ready：ASR 0.1.1
  (`op_70f1615f5b694dbbac9d5871550dc6c2`)、Embedding 0.1.1
  (`op_d40125f094c34321aa1e2acf3ab4c72a`)、OCR 0.1.1
  (`op_a8f4c2ae772f440da76ffd1dd6eb1200`)、VLM 0.1.3
  (`op_e1319c9a93cd4a979de6c985dfcc82af`)。
- 验证：容器内部署 API、Agent/legacy worker、VLM/BGE 契约共 81 项通过；定向 ruff 与
  Console 构建通过；新增发布包整包与成员摘要独立复算通过。该证据限于本机安装与生命周期，
  不代表媒体业务 Process、Linux 或完整 Golden Path 验收。

## 2026-09-28：一键装配配置接入处理方案

- 修复一键装配只写部署槽位、不保存 Console 参数方案，导致 OCR / ASR / VLM 下拉框全空的问题。
  新部署与进行中操作使用的多模态配置会保存为可引用的不可变版本；已 ready 的旧安装再次装配
  也会补齐配置，按插件和配置摘要幂等复用，保留已有自定义参数。手动保存与装配共用校验和
  持久化逻辑，恢复 Struct 数字类型，保证重新保存后的配置摘要稳定。
- Console 装配后刷新配置缓存；方案弹窗在缺配置时提供插件中心入口和刷新操作，错误在弹窗内
  可见。音频重叠说明与当前 500 毫秒默认值保持一致。
- 本机通过浏览器再次装配：配置数从 1 增至 4，新增三份配置均与 ready 槽位摘要一致；部署
  操作数仍为 7，返回 0 新操作 / 4 已就绪 / 0 未受理。浏览器实际选择 OCR、ASR、VLM 后
  显示“编排校验通过”，保存按钮启用，浏览器无 error / warn。未创建额外业务任务。
- 验证：容器内相关集成测试 34 项通过，定向 ruff、Console 构建、差异检查通过。另有
  `test_demo_account_is_explicit_and_respects_account_state` 失败：演示账号接口自动重新启用
  已禁用用户，与测试预期冲突；使用改动前 HEAD 的 API 源码复测同样失败，本次未修改身份逻辑。
  本次验收范围为配置持久化和方案校验，不代表媒体推理或完整 Golden Path 验收。
