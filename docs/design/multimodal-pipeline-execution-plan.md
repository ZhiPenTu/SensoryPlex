# 多模态文件任务执行闭环：可执行改造方案

**状态：** Implemented（已实施并通过全链路真实验收）  
**关联：** ADR-012、ADR-014、ADR-016、ADR-025、ADR-028、ADR-029、ADR-030  
**解决的问题：** Console 选择了包含 ASR、OCR、VLM 的处理方案，但任务实际只执行 OCR；时间轴不能区分“未调度”“正在运行”“没有语音/文字”和“执行失败”，并且不同执行批次与不同切片策略的素材会混在同一视图中。

## 1. 结论与实施边界

当前问题不是模型质量，也不是 1 秒窗口导致 ASR 或 VLM 被 Timeline 丢弃。真实执行路径是：

```text
Console Pipeline（单插件配置记录）
  → job/task_process（仅传 pipeline_id）
  → tools/task_worker.py
  → tools/task_runner.py（无条件 OCR；VLM 仅 with_vlm=true 且 Ollama 就绪；没有 ASR 调用）
  → Timeline（按 file-material.yaml 要求 ASR/OCR/VLM，但只能看到 OCR）
```

因此 `file-material.yaml` 目前只是 Runtime 回放和 Fusion 的策略输入，**不是** Console 任务的可执行计划。`tools/ai_worker.py`、`make asr-check`、`make model-check` 分别证明插件链路可以独立工作，但均是验收工具，不能作为 Console 生产调度器。

本方案不在现有 OCR 旁路上增加 `with_asr`、`with_vlm` 之类的布尔开关；那会继续让“页面上的方案”和“实际跑的程序”分裂。目标是让每次任务严格绑定一个不可变的多模态 `PipelineRevision`，由受控执行器按图调度真实本机插件，并把每一个模态的结果或缺失原因写成可查询事实。

本期范围是**同机 `local_native` 文件处理闭环**。跨机 raw buffer、容器插件、任意第三方命令、自动 CPU fallback 和“进程内热重载”均不纳入本期。ASR 的 MLX 后端继续只在 `macos-aarch64` 作为已验证范围；Linux 不能把它伪装为可用。

## 2. 可验收的目标体验

用户从 Console 创建并发布“文件多模态 v2”方案、选择视频并启动任务后，应能获得以下事实：

1. 任务详情显示 `execution_id`、`pipeline_id:revision`、`graph_digest`、目标节点与每个任务的状态；不可只显示“已完成”。
2. 每一秒都有一个时间轴覆盖格。覆盖格不是虚构素材，状态只能是 `queued`、`running`、`observed`、`not_applicable`、`failed`、`not_sampled_by_policy` 或 `not_scheduled`。
3. OCR、ASR、VLM 各自显示计数与最后状态。用户能分辨“无音轨/无语音”“未命中画面文字”“VLM 正在补全”“模型未部署”“模型调用失败”。
4. 真实的 `Observation` 才会进入 `MaterialUnit`；空秒、无语音、无文字绝不合成 OCR/ASR/VLM 业务结果。
5. 素材检索和详情默认限定当前 `execution_id`；切换到历史批次后才会显示旧 5 秒策略或先前 PipelineRevision 的结果。
6. 一次运行的必需快路径成功、可选慢路径失败时，Run 可以协调完成，但 UI 必须标为“已完成，含未完成补全”，并列出失败原因；不能把它显示成完整多模态成功。

## 3. 目标架构

```text
Console 方案作者
    │ 发布：锁定插件制品、模型配置、策略和 DAG
    ▼
Console Pipeline Release ────────► immutable PipelineRevision
    │                                      │
    │ 启动任务，写 execution snapshot       ▼
    ▼                              PipelineRun / PipelineTask / Assignment
Console Job ────────────────────────────────┬───────────────────────────────┐
                                            │ 同机、受控 descriptor/lease     │ Observation 引用
                                            ▼                               ▼
                                  Local Task Executor                Timeline / Metadata
                                   ├─ sampler → OCR                  ├─ 覆盖窗口事实
                                   ├─ audio segmenter → ASR           ├─ MaterialUnit
                                   └─ scene/sample gate → VLM         └─ material-execution 归属
                                            │                               │
                                            └────── outbox / index ──────────┘
```

控制面只下发稳定的任务身份、受控 descriptor/Observation 引用、deadline、策略摘要和结果摘要；不传原始帧、PCM、宿主路径或密钥。插件仍通过 `LeaseBufferReader` 在同机领取字节，控制面只接收经契约校验的派生 Observation。

## 4. v2 文件方案的规范化执行图

发布时不再解释 `processors` 的顺序列表，而是将其编译为如下 DAG。节点中的插件均用 `plugin_id + version + artifact_digest + config_hash` 定位，模型名只是展示字段，不能作为执行身份。

| 节点 | 输入 → 输出 | placement | 必需性 | 初始策略 |
| --- | --- | --- | --- | --- |
| `decode_sample` | 文件 → `media.video_frame` | `data_plane_local` | 必需 | 基线 1 fps，场景变化可补帧；每一次保留/跳过都记录原因 |
| `audio_segment` | 文件 → `media.audio_segment` | `data_plane_local` | 必需 | 6 秒段、500 ms overlap；实际值进入 revision 配置 hash |
| `ocr_fast` | 视频帧 → `observation.ocr_blocks` | `data_plane_local` | 快路径必需 | PP-OCR，按采样帧处理 |
| `asr_fast` | 音频段 → `observation.asr_segment` | `data_plane_local` | 条件必需 | MLX Whisper；仅有可解码音轨且 VAD 判为含语音的段要求文字结果 |
| `vlm_enrich` | 场景候选帧 → `observation.vision.scene_description` | `data_plane_local` | 可选慢路径 | 场景切换加低频关键帧，不要求每秒推理；Ollama/model 版本与 prompt 均锁定 |
| `timeline_fusion` | Observation + 覆盖状态 → MaterialUnit / TimelineWindow | 本机控制逻辑 | 必需 | 1 秒展示格；仅真实 Observation 进入素材 |
| `embedding` | 可嵌入 Observation → `text_embedding` | `object_ref_allowed` | 依现有索引策略 | 复用 BGE 与常驻 index，不把向量写入插件 |

### 4.1 必需、条件必需、可选的精确语义

- **OCR：** 若采样策略将该帧送入 OCR，OCR 任务必须有成功或失败终态。识别到零个文字块是成功 Observation 的业务内容，不是“任务没有跑”。
- **ASR：** 无音轨时窗口为 `not_applicable:no_audio_track`；有音轨但 VAD 无语音时为 `not_applicable:no_speech_detected`；两种情况都不创建假转写。VAD 判有语音后，ASR 超时或模型不可用必须是 `failed:<reason>`，不能降成“未观测”。ASR 输出沿用真实段/词级锚点；映射到 1 秒格时只关联，不截断或篡改原 Observation 的 `[start_ms,end_ms)`。
- **VLM：** 默认是慢路径。未被场景/采样策略选中的秒格是 `not_sampled_by_policy`；被选中但尚未完成是 `queued/running`；模型未部署、Ollama 不可用或调用失败必须显示可操作 reason code。它不因 1 秒窗口而被强制跑十次。
- **Fusion：** 快路径缺失使相应 MaterialUnit 为 `partial` 或 `failed`；VLM 慢路径只影响 enrichment 状态。Run 的 `succeeded` 只表示所有必需任务以及可选任务的最终处置已完成，绝不等价于“每个秒格都有三个模型输出”。

## 5. 契约、存储与 API 改造

### 5.1 追加 Proto，不在 JSON 中私扩字段

在 `proto/orchestration/v1/orchestration.proto` 与必要的 material/timeline Proto 中追加以下概念；完成后必须运行 `make proto`，不得手改生成文件。

| 新契约 | 必需字段 | 作用 |
| --- | --- | --- |
| `TaskInputManifest` | `run_id/task_id/attempt/assignment_id`、`data_plane_node_id`、受控 `descriptor_ref` 或 Observation ref、`stream_id`、范围、内容摘要 | 执行器领取输入；不含字节、真实路径或密钥 |
| `TaskExecutionReceipt` | 与 assignment 完全相同的身份、插件制品/配置摘要、开始/结束时间、每类输入与输出计数、稳定失败码 | 只有带此回执的调用才能让 Task 终态 |
| `TaskResult` 扩展 | 受校验 Observation 或受控 `result_manifest_ref`、`retryable`、`reason_code`、`receipt_digest` | 让 API 在同一事务中验证、登记 Observation、更新 Task、解锁下游 |
| `TimelineWindow` | `execution_id`、`stream_id`、`start_ms/end_ms`、采样原因、每模态状态/原因、更新时间 | 覆盖层事实；不是 MaterialUnit 的替代品 |
| `ExecutionSummary` | `execution_id`、绑定 revision、快/慢路径计数、降级与失败汇总 | Job 和 Console 的稳定读取模型 |

输出 Observation 可作为控制面派生事实传回，但日志、审计事件和指标只保留 ID、摘要、计数和 reason code，不写全文转写或原始 payload。

### 5.2 追加迁移（预定为 `0011`、`0012`，不改写历史迁移）

`0011_multimodal_execution_bridge.sql`：

- 给 `console_pipeline` 增加可空的 `orchestration_pipeline_id`、`orchestration_revision`、`graph_digest`、`execution_mode`；三者必须同时存在或同时为空。新发布的 v2 行强制为 `execution_mode='orchestrated_v2'`，并以复合外键指向 `pipeline_revision`。
- 增加 `console_job_execution`：`job_id`、`execution_id`、`run_id`、`pipeline_id/revision/graph_digest`、`target_node_id`、`state`、`created_at/completed_at`、摘要计数。它是任务启动时的不可变快照，不能通过之后修改方案来改变历史任务。
- 增加 `task_execution_receipt`：按 `(task_id, attempt, assignment_id)` 唯一，保存制品/配置摘要、计数、原因码、`receipt_digest` 与时间；重复回报必须幂等，比对不一致必须拒绝为 `task_receipt_conflict`。
- 增加 `material_execution`：以 `(material_unit_id, material_revision, execution_id)` 关联素材与执行批次/Revision。查询默认按 execution 过滤，不再只取跨策略的 `material_unit_id` 最新 revision。

`0012_timeline_coverage.sql`：

- 增加不可变追加式 `timeline_window_state`，唯一键为 `(execution_id, stream_id, start_ms, end_ms, state_revision)`；读取侧取该窗的最高 `state_revision`。字段包括 `sampling_state`、`modality_states jsonb`、`reason_codes jsonb`、`observed_at`。
- 检查 `end_ms > start_ms`、窗口严格对齐绑定 revision 的 `window_ms`，以及 state/reason 枚举。不能用 MaterialUnit 行表示空格，也不能通过更新旧的 Window 事实抹掉失败史。

迁移只为新执行创建关联数据。所有旧素材、旧 Job 和旧 5 秒 Timeline 保留原状，标注 `execution_mode='legacy_ocr_v1'`；不猜测它们本应运行的 ASR/VLM，也不回填合成窗口。

### 5.3 API 和授权

| API | 授权 | 行为 |
| --- | --- | --- |
| `POST /admin/v1/multimodal-pipelines`、`:validate`、`:publish` | `pipelines:manage` | 校验插件可用性、资源、DAG、策略并创建不可变 Revision；发布失败不残留半份配置 |
| `POST /v1/job-drafts/{id}:dispatch` | `jobs:write` | 仅接受已发布的 `orchestrated_v2` 方案；事务写 Job snapshot、PipelineRun、任务边和 outbox 命令 |
| `GET /v1/jobs/{id}/execution` | `jobs:read` | 返回 revision 身份、任务/回执状态、模态计数和安全 reason code |
| `GET /v1/executions/{id}/timeline` | `materials:read` | 返回覆盖格和 Observation 定位；支持按 execution 过滤 |
| `POST /v1/agent/tasks:claim`、`:result`、`:cancel` | 节点 enrollment 身份 | 只领取本节点、未过期 assignment；结果必须附带受验证回执 |

旧 `/v1/orchestration/*` 的 Run/Task API 继续是底层能力；Console 不能再绕过它直接向 `console_deployment_intent` 写一个无执行语义的 `task_process`。`task_process` 在 v2 中仅作为有 `run_id/task_id/assignment_id` 的受控交付，不再接受自由 JSON 配置。

## 6. 执行器实施设计

### 6.1 替换临时 OCR 旁路

1. 把 [tools/task_runner.py](../../tools/task_runner.py) 标为仅用于历史复现/验收，不再作为新 Job 的执行入口；不得继续读取全局 `file-material.yaml` 后无条件运行 OCR。
2. 新建宿主原生 `tools/task_executor.py`（后续可提升为 Node Agent 子服务）。它只认 Agent 从本机控制面领取、且具备 active assignment 的任务。
3. 执行器从 ADR-030 的 active `plugin_runtime_instance` 获取已验证的本机 endpoint，复核 `plugin_id/artifact_digest/config_hash`；没有匹配 active 实例时返回 `plugin_instance_unavailable`，不自行下载、启动任意命令或退化至另一个模型。
4. 提取 `tools/ai_worker.py` 中可复用的输入发现、deadline、受限并发、lease 归还与插件调用逻辑到受测试的库；`tools/ai_worker.py` 保留为调用该库的验收 CLI，不能被 Console 通过 shell 启动。
5. Runtime 负责生成视频帧/音频段与本机 `TaskInputManifest`；Executor 通过同机 handoff 服务领取 lease，插件只读一次并在 success/failure/cancel 均释放。
6. Executor 将 Observation、每输入结果和 `TaskExecutionReceipt` 回报给 API。API 先校验 assignment、时间锚点、来源、插件身份、配置 hash 与 payload 大小上限，再在事务内持久化 Observation、写回执、变更 Task、生成 outbox、解锁下游。

### 6.2 状态、失败与恢复

- Task 成功前必须同时有有效 assignment、回执和符合节点 `produces` 的 Observation/显式 `not_applicable` 结果；“插件进程在”不能充当成功。
- 可重试失败按 revision 的 `max_attempts` 与退避策略进入 `retry_wait`；超过上限为 `failed:retry_exhausted:<reason>`。不可重试、模型配置错误、缺能力和外发策略拒绝不重试。
- cancellation 先持久化；Executor 仅对相同 assignment 的本机插件发送 Cancel。迟到结果写审计为 `discarded_stale_result`，不得生成 Observation 或解锁下游。
- lease 到期的恢复器先按 `idempotency_key` 查回执/结果；有一致回执则收敛，未知执行才重派。重复结果若摘要不同，任务失败为 `task_receipt_conflict`，绝不静默选一个。
- 资源不足、节点离线、模型未部署、Ollama 离线、无音轨和无语音分别使用不同 reason code；前四种不得在 UI 汇总成“0 条结果”。

## 7. Console 与时间轴改造

### 7.1 方案编辑和发布

Console 将当前“选择一个插件 + 一个配置”的方案表单替换为受限的场景方案编辑器：可选 OCR/ASR/VLM、每个插件的已认证 Release 与 ConfigRevision、`window_ms`、采样/VAD/场景策略、必需性、deadline、并发档位和数据外发策略。它不允许输入 Python module、URL、命令、模型路径或密钥。

发布页必须显示图预览、锁定 digest、节点能力预检、资源预算和策略差异。修改任一影响语义的字段都新建 Revision；已发布 Revision 不可编辑，回滚仅选旧 Revision 新建任务。

### 7.2 任务与素材详情

- Job 列表新增“执行版本”“快路径/补全状态”“失败原因”列；`completed` 改为明确的 `succeeded`、`succeeded_with_partial_enrichment`、`failed` 或 `cancelled` 展示，不把缺模态藏到素材页。
- Timeline 顶部展示每一秒覆盖率与每模态统计。行中使用状态色和文字：没有音轨/无语音与 ASR 故障不同；未被 VLM 采样与 VLM 排队不同。
- Material 卡只显示真实 Observation；卡片外的覆盖格可链接到相同范围内的任务/失败详情。固定使用半开区间 `[start_ms,end_ms)`，不会为适配秒格裁剪 ASR 段。
- 素材搜索默认带当前 Job 的 `execution_id`；提供显式“查看所有历史执行”的开关。详情面板显示 `pipeline revision`、模型 release、执行节点、task/receipt ID 和 history，而不是仅显示 `pipeline_version` 字符串。

## 8. 分阶段实施清单

### 阶段 A：冻结旁路并建立可观察基线

1. 为 `tools/task_runner.py` 与 `tools/task_worker.py` 新增回归测试，证明当前 Console 配置只携带 `pipeline_id`、ASR 未调用、无 `with_vlm` 时 VLM 未调用；测试名称明确其为 legacy 行为。
2. 新 Job 创建/分发处返回 `legacy_execution_path_deprecated`，并在 UI 标注“旧 OCR 兼容路径”；不删除已有 `.data/tasks` 或数据库历史。
3. 对当前三个首方插件执行能力探测：OCR 权重、ASR 的 MLX/模型、VLM 的 active instance + Ollama/model。探测失败只产生预检失败，不启动一个会产生空数据的 Run。
4. 输出一次基线报告：同一授权视频的音轨、采样帧、OCR/ASR/VLM 实际调用数、空/失败数、执行策略摘要。该报告只用于比较，不改写历史事实。

**阶段门槛：** 新实现尚未启用；遗留任务仍可回放，且被明确标为 legacy。

### 阶段 B：Revision 与执行归属

1. 追加 `0011_multimodal_execution_bridge.sql`，实现 Pipeline 绑定、Job snapshot、回执和 `material_execution`。
2. 扩展 Proto、运行 `make proto`，同步 API/Console 类型；为所有新 message 写字段边界和兼容测试。
3. 实现多模态 Revision validator：图无环、模态/placement 合法、所有有 raw descriptor 的边同机、deadline/attempt/concurrency 有界、每项插件 ref 都已认证且 ConfigRevision 匹配。
4. 将 Console 发布、Job dispatch 接到 `pipeline_revision` 与 `pipeline_run`；通过一次事务创建 job/execution/run/task/edge/outbox，任一失败回滚全部。

**阶段门槛：** 可以提交 v2 Run 并完整显示 Task 图，但仍不得声称模型已由 Console 调用。

### 阶段 C：同机受控多模态执行

1. 实现 Local Task Executor、Agent 领取/回报协议和 active plugin endpoint 校验；把 `node_agent.py` 对 v2 `task_process` 的固定拒绝改为委派给受控 Executor，而不是直接宣告成功。
2. 先打通 `decode_sample → ocr_fast → timeline_fusion`，用真实授权视频验证 descriptor、lease、回执、Observation 入库、素材执行归属和取消。
3. 接入 `audio_segment → asr_fast`：真实有声样本、静音样本、无音轨样本均覆盖；验证段级时间锚点绝不被秒格截断。
4. 接入 `vlm_enrich`：运行前要求已验证的 active `org.sensoryplex.vlm-moondream` 和可用 Ollama 模型；未部署/离线、超时、取消和可重试错误均留痕。
5. 接入 `timeline_fusion` 与 `material_execution` 写侧，确认 outbox/index 只消费真实追加的事实。

**阶段门槛：** 同机真实视频可同时产生 OCR、ASR、VLM 事实；任一插件失败会准确呈现，不允许退回硬编码 OCR 或模拟 Observation。

### 阶段 D：覆盖层、历史隔离与 Console

1. 追加 `0012_timeline_coverage.sql`，由 Runtime/Fusion 为完整视频时长建立 1 秒覆盖窗；末尾不足 1 秒保留真实尾窗，例如 `[9000,9056)`。
2. 用采样、VAD、任务回执和 Fusion 结果推进 Window 状态版本；Window 的空状态不写入 MaterialUnit。
3. 修改 materials 查询与详情，默认按 `execution_id` 过滤，增加历史批次切换；修复不同 `window_ms` 产生的素材混看。
4. 改造 Jobs 和 MaterialDetail 页面，并用真实浏览器验证错误、部分完成、无音轨、无语音、未采 VLM 和旧批次隔离。

**阶段门槛：** 用户在页面能解释每一秒与每一模态为什么有或没有数据，不再把“空结果”误读为“没有调度”。

### 阶段 E：回归、逐步启用与退役

1. 先只允许管理员选择 `orchestrated_v2`，保留 `legacy_ocr_v1` 只读/仅重放入口；通过开关逐项目启用，而不是切换旧任务。
2. 每次启用前运行真实授权样本回归，检查模型 Release、端点、资源余量、索引与回放。若 v2 失败，停止新 v2 调度并保留全部 Run/Task/receipt/coverage 证据；已有 legacy 数据不回滚、不删除。
3. 当 v2 完成重复投递、取消、恢复、三模型、覆盖层和浏览器验收后，禁止创建新的 legacy Job；历史读取至少保留至既定数据保留期结束。
4. 更新 `docs/implementation-status.md`、`docs/verification.md`、Console 使用手册和 ADR 状态。只有完整真实闭环通过才更新对应能力状态；`golden_path_verified` 不因单项通过而提前置真。

## 9. 验收矩阵与命令

Python/API/Console 底座验证必须在 `api`、`console` 容器内执行；Rust 仍按当前约定在宿主 Cargo 工具链执行。模型插件和宿主执行器可在宿主原生运行。以下命令名称是实施时应新增或扩展的目标，不能用健康检查替代：

| 场景 | 必须证明 | 验收方式 |
| --- | --- | --- |
| 发布拒绝 | 图环、错误 modality、未认证 digest、无界重试、raw buffer 跨机 | API 集成测试 + `make multimodal-pipeline-check` |
| 正常三模态 | 有声视频真实产生 OCR/ASR/VLM，三者均有回执和锚点 | `make multimodal-execution-check MEDIA=...` |
| 无音轨/静音 | `no_audio_track`、`no_speech_detected` 不产生伪转写 | 同一检查的两份真实样本 |
| VLM 不可用 | 模型/endpoint 未就绪时预检或 Task 失败可见，不能变成 0 条成功 | API + Executor 契约测试 |
| 失败/重试 | 临时插件失败、有界重试、耗尽后失败；不可重试不重投 | Executor/Orchestration 集成测试 |
| 取消与崩溃 | Cancel 无下游解锁；租约到期通过回执收敛或受控重派 | PostgreSQL 集成测试 |
| 覆盖层 | 9.056 秒输入产生 10 个窗口（最后 `[9000,9056)`），空窗不生成假素材 | Runtime + Timeline 测试 |
| 批次隔离 | 同视频跑 5 秒 legacy 与 1 秒 v2，默认查询不混合 | API + Console 浏览器测试 |
| 向量与回看 | 真实 Observation → Material/outbox/index → 受鉴权检索 → 同一半开区间回看 | 扩展 `make golden-path-check` |

新增验收的报告必须输出：平台、执行后端、模型/插件 digest、config hash、采样与音频策略、窗口数、每模态调度/成功/失败/not-applicable 数、P50/P95、队列峰值、lease 统计和所有 reason code。不能只报“任务完成”。

## 10. Definition of Done

以下条件全部满足前，不得称为“Console 多模态 Pipeline 已完成”或“全链路 Golden Path 已完成”：

- [ ] 新 Console Job 绑定不可变多模态 Revision，并保存 execution snapshot。
- [ ] 新 Job 不再调用硬编码 OCR 的 `task_runner` 旁路。
- [ ] OCR、ASR、VLM 由同机受控 Executor 实际调用，且插件 identity/lease/回执可复核。
- [ ] 无音轨、无语音、未采样、模型不可用、执行失败、取消、重试耗尽都各有稳定且可见语义。
- [ ] 1 秒覆盖层完整呈现真实时长，空窗不伪造 MaterialUnit 或 Observation。
- [ ] Material/搜索按 execution/PipelineRevision 隔离，历史结果可显式查看但不默认混合。
- [ ] 数据库迁移、Proto 生成、API/Console/Rust/宿主模型验证与真实浏览器验收全部通过。
- [ ] 真实授权媒体经上传、调度、三模态、融合、入库、索引、检索和 Range 回看完成端到端验证，并如实记录仍未覆盖的平台或模型范围。

