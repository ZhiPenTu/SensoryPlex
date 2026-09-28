# ADR-031：VLM 延迟满足 WorkQueue（底座发布、插件竞争拉取与增量融合）

**状态：** Accepted（首期代码与消息层验收完成；真实授权媒体上的本机 Moondream 消费仍待验收）  
**日期：** 2026-09-28  
**上游决策：** ADR-001（控制面/数据面分离）、ADR-004（NATS JetStream）、ADR-012（模型插件边界）、ADR-019（有界队列）、ADR-024/025/027（outbox、sink 与常驻事件链路）、ADR-028（Runtime → Timeline）、ADR-029（持久编排）  
**相关文档：** [实现状态](../implementation-status.md)、[插件开发文档](../../开放式插件开发文档.md)、[多模态执行设计](../design/multimodal-pipeline-execution-plan.md)

---

## 1. 决策背景

OCR/ASR 是用户能够立即查看与定位素材的 L1 事实；Moondream 场景描述是计算密集、允许渐进补全的
VLM 慢路径。把三者作为同一同步执行链会让本机统一内存同时承受媒体解码、OCR、ASR 与 VLM，且用户
必须等到每个 VLM 锚点处理完才可回放或检索文字。

本 ADR 将 VLM 从 `orchestrated_v2` 的同步 DAG 节点移出：不可变 Revision 仍锁定 VLM 的制品、配置、
提示词与采样策略，但把它记录为 Timeline 的 `delayed_enrichments` 元数据。快路径入库后，底座只发布
时间锚点任务；插件按自身余量竞争拉取并在本机抽取单帧。PostgreSQL 的事实账本，而不是 JetStream
投递或 ACK，是任务终态的唯一权威。

## 2. WorkQueue 与控制消息

底座发布器是唯一可以创建 `sensoryplex-tasks` 的角色。该流固定为 JetStream `WorkQueue`，并且已有流
发生以下任意漂移时只报错、不自动修改：

| 字段 | 固定值 |
| --- | --- |
| subjects | `sensoryplex.tasks.vlm.v1`、`sensoryplex.results.vlm.v1` |
| retention | `WorkQueue` |
| storage | `File` |
| max messages / bytes | 100,000 / 128 MiB |
| max age / duplicate window | 2 天 / 2 小时 |
| VLM task durable | `vlm-moondream-workers`，explicit ACK、90 秒、最多 8 次投递 |
| result durable | `vlm-result-fuser`，explicit ACK、90 秒、最多 8 次投递 |

`TaskInputManifest` 和 `VlmTaskResult` 的唯一跨进程契约在 `proto/orchestration/v1/`。任务只携带
`asset_id`、content hash、`media_asset:<asset_id>`、`[start_ms,end_ms)`、prompt、来源/素材身份和
锁定的插件版本、制品摘要、配置摘要。它不得携带 Raw Buffer、lease、绝对宿主路径、任意 URL、密钥或
Observation 原文。结果在发送前也必须复算 `result_digest`。

## 3. 事实、发布和状态收敛

快路径 Timeline 事务完成时，同一 PostgreSQL 事务会写入：

1. `vlm_enrichment_task`：时间锚点、受控定位符、插件身份与初始 `queued` 状态；
2. `vlm_task_outbox`：不可变 protobuf bytes 与 `Nats-Msg-Id`；
3. `timeline_window_state` 的 `queued` 覆盖事实。

发布器用 `FOR UPDATE SKIP LOCKED` 短暂认领 outbox。只有 JetStream 确认后才写 `published_at`；若进程
在确认与提交之间崩溃，重新发布的相同 `Nats-Msg-Id` 由 duplicate window 吸收。它不跨网络持有
PostgreSQL 行锁。

快路径已落下 OCR/ASR/Timeline 事实时，`console_job_execution` 和草稿转为 `ready_for_review`：用户可
立即浏览基础素材、原片回放和文字定位。所有延迟任务终态后，结果融合器才把执行收敛为 `succeeded` 或
`succeeded_with_partial_enrichment`。不可重试失败同样持久化结果回执；这样“数据库已提交、NATS ACK
之前崩溃”的重投只会得到幂等重放，不会耗尽投递预算。

结果融合器先将回传的 `TaskInputManifest` 与 outbox 中发布的不可变 protobuf bytes 逐字比对，再校验
时间范围、结果摘要与 Observation 血缘；因此制品版本、配置摘要、attempt 或其他任务身份不能被结果端
悄悄替换。随后它在一个事务中追加 Observation、Material、`timeline_window_state` 和既有 material
outbox。常驻 index 从该受控 material 引用回查事实：BGE 只接受 OCR 的 `blocks[].text` 与 VLM 的
`vision.scene_description.text` 两种显式形态，VLM prompt、帧元数据、ASR 或任意未知 JSON 都不会被编码。
事务成功后才 ACK result 消息；任何校验或写入错误会 rollback 并 NAK，由 WorkQueue 依投递预算重试。

## 4. 插件 Pull 与本地解码

`vlm-moondream` 的 `slow_consumer` 是宿主原生进程，不依赖控制面内部模块。每个同 durable 的进程只调用
`fetch(batch=1)`，因此多个实例竞争同一队列，而不是由底座把慢任务同步推给某个节点。

每个 Consumer 的 `local_decode` 配置必须显式给出 `min_free_memory_bytes`（最低 256 MiB）。每次 fetch
之前它读取当前可用内存：Linux 使用 `/proc/meminfo` 的 `MemAvailable`；macOS 统一内存只计 `vm_stat`
的 free 与 speculative 页。探测失败会以 `vlm_consumer_memory_probe_unavailable` 显式停止；余量未达到
水位时只等待，既不 fetch、也不 ACK、更不把总内存误报为可用内存。这使每个插件根据自己的余量决定
何时领取下一条任务。

Console 为 `org.sensoryplex.vlm-moondream` 新建的配置固定为 `data_plane_mode=local_decode`，并把上述
水位、prompt、模型和超时一起写入不可变 `config_hash`。`per_request` 只属于 OCR/ASR 的 Runtime
descriptor 路径；它与 WorkQueue Consumer 的按锚点本地解码语义不兼容，因此 VLM 配置会被控制面拒绝，
不能等到 Consumer 收到任务后才以身份不匹配失败。

收到任务后，Consumer 只把 `media_asset:<id>` 映射至预配置只读媒体根中以 content hash 命名的文件，
重新校验 SHA-256，并以锚点的 `start_ms` 通过本机 FFmpeg/GStreamer 等解码器抽一张 PNG。原始像素仅在
本机管道中传给模型，永不进入 NATS、数据库控制消息或日志。确认 result 已发布后才 ACK 原任务；结果
未确认或短暂解码/模型错误会 NAK，交给 90 秒 AckWait 与最大投递预算处理。

## 5. 运行与健康语义

控制面常驻服务通过项目脚本启动：

```sh
./deploy/up.sh
./deploy/up-events.sh
```

`events` profile 除原有 relay/index 外启动 `vlm-publisher` 与 `vlm-result-fuser`。两者必须加载 bind-mounted
API/SDK 源码，并分别滚动刷新 `.data/events/vlm-publisher-status.json` 与
`.data/events/vlm-result-fuser-status.json`。Docker healthcheck 仅在状态文件 15 秒内更新时通过；因此
缺流、durable 漂移或启动期导入错误不能再被“容器已启动”掩盖。

插件 Consumer 仍运行在拥有模型与授权媒体挂载的宿主节点。启动时必须提供受控 `--media-root`、
`local_decode` 配置与 NATS 地址；它不能从 NATS 消息推断本机文件路径，也不能在部署期下载模型。

## 6. 验收层级与边界

| 层级 | 证据 | 说明 |
| --- | --- | --- |
| 契约与写侧 | `make proto`、VLM contract/数据库集成测试 | 验证受控锚点、outbox、幂等回执与 `ready_for_review` |
| WorkQueue | `make vlm-workqueue-check` | 两个独立 Pull 进程竞争 10 条任务；未 ACK 的任务在 AckWait 后第 2 次投递 |
| 增量索引契约 | BGE/index contract 与 PostgreSQL 集成测试 | 验证 VLM `payload.text` 被向量化，未知 modality 不会混入索引 |
| 容器常驻 | `./deploy/up-events.sh`、状态文件、publisher/fuser 日志 | 验证真实 NATS stream/durable 与容器就绪，不验证模型质量 |
| 真实媒体 | 授权样本 + 宿主 native Consumer + `make golden-path-check` | 仍需证明本机媒体可解析、模型真实返回、增量素材/向量可检索与 Range 回放 |

前三层通过不等于最后一层。特别是它们不证明 Ollama/Moondream 已就绪、媒体在 Consumer 节点可用、VLM
描述质量可用，亦不将 `golden_path_verified` 改为 `true`。

## 7. 非目标

首期不支持跨机共享内存、向 NATS 发送原始帧、自动 CPU fallback、容器化 VLM 模型进程、任意 URL 或
宿主路径任务、自动下载模型、dead-letter 分流、吞吐/延迟基准，或把 VLM 的可用性重新变成 L1 文字与
播放路径的阻塞条件。任何扩展都必须保留本 ADR 的受控引用、数据库权威和有界重试语义。
