# ADR 索引

决策以 ADR（Architecture Decision Record）形式记录。ADR-001 至 ADR-008 在根目录蓝图文档里，ADR-009 起在
`docs/adr/`。全文目前为中文。

| ADR | 标题 | 决定了什么 |
| --- | --- | --- |
| ADR-001 | 将控制面与媒体数据面分离 | 原始媒体绝不走控制总线 |
| ADR-002 | Rust 负责运行时，Python 负责模型插件 | 让加速保持宿主原生的职责划分 |
| ADR-003 | 实时媒体使用 GStreamer，离线工具使用 FFmpeg | 哪条路径以哪个工具为准 |
| ADR-004 | NATS JetStream 作为控制与事件总线 | 事件与命令的传输 |
| ADR-005 | 以 ONNX Runtime Execution Provider 作为硬件适配主轴 | 不写逐模型厂商代码就能到达加速器 |
| ADR-006 | Timeline 和 MaterialUnit 是事实中心，向量库不是事实数据库 | 为什么命中要用 PostgreSQL 水合 |
| ADR-007 | 采用快慢双路径并拆分延迟目标 | 哪些路径的延迟预算可以不同 |
| ADR-008 | Apple Silicon 是一等端侧目标 | 统一内存，以及能力规则引用的内存类型契约 |
| ADR-009 | 媒体格式支持矩阵与拒绝语义 | 什么可以准入，拒绝如何表达 |
| ADR-010 | 跨进程数据面的安全边界 | 用有界共享内存 lease 而不是复制载荷 |
| ADR-011 | 保留窗口按种类分配 | 每类保留数据的存活时长 |
| ADR-012 | 模型插件与端侧推理边界 | 模型插件契约，含 lease reader |
| ADR-013 | 应用API模块化合并与部署边界 | API 如何拆分与部署 |
| ADR-014 | ASR插件与音频样本布局契约 | 严格按 `sample_format` 解释，不假设宽度 |
| ADR-015 | macOS常驻形态与统一内存分级 | `launchd` 服务与分级表 |
| ADR-016 | OCR与ONNX执行后端 | 显式失败，而不是静默退回 CPU |
| ADR-017 | BGE文本向量与维度版本化 | collection 键与版本化向量维度 |
| ADR-019 | 运行时消费分级队列上限 | 越界配置即拒绝启动的上限 |
| ADR-020 | 向量索引落库与检索闭环 | 读回确认之后才置 `ready` |
| ADR-021 | 模型worker按分级并发上限限流 | 准入、重试账目与实测在飞峰值 |
| ADR-022 | 宿主加速器能力探测与上报 | 与进程后端分离的三态探测 |
| ADR-023 | 网关语义检索接线与索引检索面 | 同源守卫、失败分类、`retryable` |
| ADR-024 | outbox分发接线与消费去重边界 | 确认后才写 `published_at`；`Nats-Msg-Id = event_id` |
| ADR-025 | 常驻消费循环与sink接线 | 坏事件 fail-stop，退出码 3 |
| ADR-026 | Web主节点与局域网插件worker拓扑 | 节点注册、预检、数据本地性、可审计部署 |
| ADR-027 | 事件链路分级背压与容器化常驻 | `events` profile 与档位准入 |
| ADR-028 | Runtime到Timeline接线与授权追加 | runtime 输出如何变成被授权的事实 |
| ADR-029 | 可编排插件执行核心 | 不可变 revision、持久 Run、多节点调度 |
| ADR-030 | 插件热部署执行器 | 版本化蓝绿本机 native 插件进程 |
| ADR-031 | VLM 延迟满足 WorkQueue | 底座发布、插件拉取与增量融合 |
| ADR-032 | 插件平台通用接入与能力契约 | Manifest v2、Ed25519 签名与运行能力声明 |

::: tip 关于编号
仓库里没有 ADR-018 文件；编号刻意留了空档而没有重排，因为 ADR 编号被代码与迁移引用。
:::

## 去哪里读

- ADR-001 … ADR-008 —— 仓库根的 `技术选型ADR与V1实施蓝图.md`
- ADR-009 … ADR-032 —— [`docs/adr/`](https://github.com/ZhiPenTu/SensoryPlex/tree/master/docs/adr)

与 ADR 一起读会更有用的设计文档：

| 文档 | 内容 |
| --- | --- |
| `docs/contracts/README.md` | 本站反复引用的契约规则 |
| `docs/design/plugin-orchestration.md` | 编排设计：状态、调度、恢复 |
| `docs/design/console-mvp.md` | 控制台 MVP 工程设计稿 |
| `docs/implementation-status.md` | 长版实现清单 |
| `docs/verification.md` | 可复现的证据记录 |
| `docs/development-checklist.md` | 当前执行顺序与收口状态 |
| `开放式插件开发文档.md` | 开放式插件开发规范 |
