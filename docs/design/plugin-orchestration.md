# 可编排插件执行核心：实现设计

**状态：** P0 编译内核已实现；P1-P3 执行面仍待落地（对应 ADR-029）  
**目标读者：** Runtime、API、Node Agent、插件 SDK 与 Console 开发者

## 1. 目标与成功判据

本设计把已存在的单插件调用能力升级为可恢复、受策略约束的多插件执行。它解决的是“谁在何时、何处、
以什么输入和版本调用哪个插件”，而不是重新实现模型、Timeline 或向量索引。

成功不以进程存活或 DAG 图能渲染为准。一个可接受的 P3 证据必须同时证明：真实授权媒体经 Runtime
数据面进入真实插件；下游由已确认的上游事实解锁；Task 可在崩溃/重投后按幂等键收敛；调度决策符合
数据本地性；最终素材能经受鉴权 API 检索并回跳到相同半开时间范围。

## 2. 范围与非目标

首期范围是静态发布的 Pipeline revision、单主 Scheduler、有限 DAG、受控插件、同机 descriptor 路径，
以及显式允许的 Observation/object reference 跨机路径。

首期不做任意用户代码、循环/递归图、运行中修改图、跨主高可用、共享内存跨机、无限队列、无限重试、
自动把不支持的 GPU 任务降级到 CPU，或用通用工作流产品覆盖媒体专有状态机。

## 3. 逻辑模型

```text
PipelineDefinition ──< immutable PipelineRevision ──< PipelineNode / PipelineEdge
                                   │
                                   └──< PipelineRun(input_ref, deadline, cancel state)
                                                 │
                              ┌──────────────────┴──────────────────┐
                              ▼                                     ▼
                       PipelineTask ── depends-on ── PipelineTask
                              │
                              ├──< SchedulerAssignment (decision / lease)
                              └──< TaskAttempt (delivery / result)
```

`PipelineRevision` 是唯一可执行图；草稿永远不能被 Scheduler 读取。`PipelineRun` 是提交意图的事实，
`PipelineTask` 是 revision 节点在一次 Run 中的实例。Task 的 `idempotency_key` 由 revision digest、node ID、
规范化 input reference 与业务语义配置派生，不能使用时钟或 delivery ID。

## 4. 图定义、编译与发布

### 4.1 节点和边

节点定义至少有 `id`、`plugin_ref`、`consumes`、`produces`、`placement`、`deadline_ms`、`max_attempts`、
`priority`。`plugin_ref` 是 `plugin_id/version/artifact_digest/config_hash` 的不可变组合；显示名称与 Docker tag
不得参与执行身份。

边定义有 `from_node_id`、`to_node_id`、`modality`、`join`、`required`。`join` 初版只允许：

| join | 含义 |
| --- | --- |
| `same_item` | 同一个 descriptor / Observation 身份 |
| `same_stream_window` | 同一 `stream_id` 且输入半开范围完全相等 |
| `window_contains` | 下游窗口完整包含上游 Observation；只供 Timeline 这类聚合节点使用 |

跨窗裁剪不在 join 中出现；收到不匹配范围是稳定失败，不裁成一个看似能执行的输入。

### 4.2 编译顺序

1. 验证 Pipeline schema、revision 元数据与节点 ID；
2. 从 Registry 锁定每个 plugin ref，对照 manifest 的 consumes/produces、SDK 范围、资源与安全策略；
3. 验证边 endpoint、modality、join、必需输入和图无环；
4. 验证 placement：任何 `BufferDescriptor` 边只能使用 `data_plane_local`；
5. 依据每个目标节点的能力与分级上限做**发布前可行性**预检，不选择实际节点；
6. 规范化 JSON 后计算 `graph_digest`，在同一事务写 revision 与审计事件。

发布失败不应残留半个 revision。缺少可用节点只在图要求显式节点时拒绝；其它可合法排队的图可发布，
但提交 Run 时必须再次以实时节点状态决策。

## 5. 调度算法与数据本地性

Scheduler 每次只处理有限批量的 `ready` Task。候选节点依次经过可达性/外发策略、数据面同机、显式约束、
插件与 artifact、后端/资源、并发与队列余量筛选。任一步淘汰都计入 `scheduler_assignment`，最终没有候选
时依据策略写 `blocked:<reason>` 或 `failed:<reason>`。

资源预算是 admission 不是建议值：Task 的 max concurrency、队列容量与模型预算任一超过节点已报告的档位，
调度器不夹取参数。调度器可等待资源释放，也可按 Pipeline 策略拒绝；两种结果均需记录。

```text
ready task
  → enumerate candidates
  → data/locality filter
  → plugin/artifact/config filter
  → resource & deadline admission
  → persist assignment lease
  → transactional outbox command
  → Node Agent / local Runtime executes
  → persist verified result, then unlock downstream
```

## 6. 状态、重试、取消与恢复

状态转换由数据库事务完成，消息处理只请求转换。每条任务命令携带 `run_id/task_id/attempt/assignment_id/
idempotency_key/deadline`；回报缺任意一个或与当前 assignment 不一致时拒绝为 `stale_task_result`。

- 成功：先验证 Observation/结果血缘，再写成功 Task 和输出引用；事务提交后再由 outbox 发状态事件。
- 重试：仅 `retryable=true`，指数退避有上限；attempt 增加、语义幂等键不变。
- 取消：取消标记先入库；未发出的 Task 直接终态，已分配 Task 发 `Cancel`，迟到结果审计为 discarded。
- 恢复：lease 过期不等于必然重跑。恢复器先检查同 idempotency key 的已确认结果和 Agent 最终回报，
  只有未知执行才按 retry 策略生成新 assignment。
- 下游：必需上游失败阻塞下游并向 Run 汇总失败；可选慢路径失败只改变 enrichment 状态。

## 7. Proto、存储与 API

新增 `proto/orchestration/v1/orchestration.proto`，由 `make proto` 生成各端绑定。最小 RPC/消息集合：

```text
PublishPipelineRevision / ValidatePipelineRevision
SubmitPipelineRun / GetPipelineRun / CancelPipelineRun / RetryPipelineTask
ClaimTask / ReportTaskResult / CancelTask
PipelineRevision / PipelineRun / PipelineTask / SchedulerAssignment / TaskResult
```

API 层只做认证、RBAC、参数验证与持久接受命令；它不调用插件。`pipelines:manage` 管发布，`jobs:write`
管提交/取消，`jobs:read` 管状态查询，节点 Agent 使用独立 enrollment 身份。数据库迁移追加六张 ADR-029
表，且将状态迁移、唯一幂等键、必需上游解锁条件和 assignment lease 放在数据库约束或事务内。

## 8. 可观测性与安全

每个 Run/Task/assignment 使用稳定 trace ID；日志只记录 ID、状态、计数、reason code 与受控引用。禁止
记录 buffer 内容、locator 实体、原始 URL、密钥、模型输入文本或完整 plugin config。指标按 plugin/node/
state/reason 聚合，避免把高基数 request ID 放入 Prometheus label。

`blocked`、`retry_wait`、`partial`、`cancel_timeout` 和 `retry_exhausted` 必须从 API 返回；空 Observation、
默认 CPU fallback、悄悄迁移到远端和“假成功”均为缺陷。

## 9. 实施顺序、核心需求与验收

P0 先实现 Proto、图编译器、纯状态机和单元/契约测试；P1 增加数据库、outbox 与单节点真实插件调用；P2
让 Agent 执行真实生命周期及多节点策略；P3 接 Console 和完整真实媒体路径。P1–P3 共同构成本项目向
第三方场景团队提供“插件 + 编排”能力的核心交付，不是可选的 UI 或集成工作。

| 阶段 | Definition of Done | 不能用作替代的证据 |
| --- | --- | --- |
| P1 | 追加迁移落 `PipelineRevision`、`PipelineRun`、`PipelineTask`、边和 assignment；实现事务 CAS/幂等、取消传播、lease 恢复和 outbox；经真实同机 Runtime 调 OCR 或 VLM 的 `Start`/`Process`/`Cancel`；API 以 scopes 保护发布/任务操作 | 内存状态机、CLI 图编译、`tools/ai_worker.py`、单个 plugin health |
| P2 | Agent 实际 `Describe`、`ValidateConfig`、`Start`、`Drain`、`Stop` 并回报事实；Scheduler 应用 locality/capability/resource/policy 过滤，持久化 assignment 与 failover 决策；受控 object reference 跨机路径有独立策略 | 仅有 Agent 注册、部署意图、心跳或“找到一台 CPU 能跑”的隐式改派 |
| P3 | 场景产品包锁定插件 digest、Pipeline revision、配置 schema、权限与外发策略；Console/API 覆盖发布、提交、取消、详情、升级/回滚与观测；在隔离项目中完成真实输入 → 插件 → Timeline/索引 → 鉴权检索/时间回跳 | 草稿保存、合成 Observation、健康检查、静态 Console 页面 |

场景产品包是外部团队复用底座的唯一交付单元：它可以选择已认证 Plugin、声明它们的有向无环依赖并暴露
领域配置，但不能携带任意宿主脚本、数据库凭据、NATS 管理权限或跨机共享内存引用。Runtime 始终保留调度、
配额、数据位置、重试、取消与审计的控制权。

每一阶段只在真实验收通过后更新 `docs/implementation-status.md` 和 `docs/verification.md`。P1 之前不得关闭
`runtime_task_service_not_attached` 或 `runtime_pipeline_validation_not_attached`。P3 之前，不得将 Console 的
草稿、Node Agent 心跳、插件 server 健康或单独的 `make model-check` 表述为“可编排”或“可供第三方交付场景产品”。
