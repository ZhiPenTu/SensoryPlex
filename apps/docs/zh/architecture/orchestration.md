# 可编排执行核心

可编排执行核心（ADR-029）把一份已发布方案变成持久、可审计的执行。它是底座里决定**什么在哪里跑**的那一部分，
并且刻意保守：宁可拒绝调度，也不去数据在法律上不能去的地方跑。

## 链路

```text
不可变 Pipeline revision
   → DAG 编译（受限图、modality 与 placement 校验）
   → PipelineRun / PipelineTask 状态机（确定性）
   → 按数据本地性与资源上限的集群调度
   → 跨节点认领、过期结果拒绝
   → 带完整 assignment 历史的可审计故障转移
```

## P0 —— 编译内核

- `orchestration/v1` 契约、受限 DAG 编译、modality 与 placement 校验，以及确定性的 Run/Task 状态机。
- 校验发生在任何分发之前：声明了无法满足的内存类型或 placement 的图在编译期就被拒绝，而不是跑到一半才发现。

## P1 —— 持久执行

迁移 `0008_orchestration_run_task.sql` 落 `pipeline_definition`、`pipeline_revision`、`pipeline_run`、
`pipeline_task`、`pipeline_task_edge` 与 `scheduler_assignment`。

| 保证 | 如何强制 |
| --- | --- |
| 已发布 revision 不可变 | `pipeline_revision` 上的 `deny_fact_update` 触发器禁止原地更新与删除 |
| 活跃 Run 严格幂等 | `(pipeline_id, revision, input_ref, idempotency_key)` 上的部分唯一索引 |
| 结果不能在错误上下文里重放 | 对账严格校验 `(run_id, task_id, attempt, assignment_id)`，其余一律以 `stale_task_result` 拒绝 |
| 解锁只在成功时向下传播 | 必需上游成功后递归级联解锁下游就绪任务 |
| 失败阻断下游 | 必需上游失败时下游落 `state='blocked'`、`reason_code='upstream_failed'` |
| 取消优先 | Run 被取消后，迟到结果带审计记录丢弃，绝不能把任务翻成成功，也绝不解锁下游 |
| 重试有界 | 可重试错误进入 `retry_wait`，退避后刷新 deadline 重派；超过 `max_attempts` 落 `retry_exhausted:<reason>` |
| 崩溃恢复不留孤儿 | 原子扫描过期租约：已写入事实的安全收敛，否则回到 `ready` 重新派发 |

### API 面

| 端点 | 权限 |
| --- | --- |
| `/v1/orchestration/pipelines[:validate]` | `pipelines:manage` |
| `/v1/orchestration/runs[/:id/cancel]` | `jobs:write` / `jobs:read` |
| `/v1/orchestration/scheduler:step` | `jobs:write` |
| `/v1/orchestration/tasks/:id:result` | `jobs:write` |

## P2 —— 多节点集群

- **本地性硬过滤。** 同机节点（`is_co_located`）独占消费 raw `BufferDescriptor`；远程 GPU 或边缘节点只处理
  观测与对象引用。
- **raw 边被双重拒绝。** 跨主机 raw buffer 边在解析图时与调度时各被拦截一次，原因码 `data_locality_violation`。
- **draining 与 offline 是硬阻断。** 预检与任务认领都以 `409` 应答；不静默降级，也不任意改派。
- **可审计故障转移。** 节点失联或租约过期时，恢复器回收任务并重新派发，在账目中保留第一任与第二任 assignment。

## 插件 Manifest 在这里的位置

Manifest 里的 `consumes` / `produces` 是**编排器验证边的依据**——不是“按列表顺序自动调用插件”的指令。
已发布 revision 会锁定具体 `plugin_id`、版本、产物 digest、配置 hash、输入/输出 modality、时间 join、
placement、deadline 与 attempt 策略；编排器只在这份不可变版本上展开 Task。

## 验收

```sh
make orchestration-check       # 编译内核
make orchestration-p1-check    # 真实 PostgreSQL 上的 7 个持久执行场景
make orchestration-p2-check    # 6 个分布式场景
pytest tests/integration/test_orchestration_api.py
```

## 下一步（P3）

场景产品包、面向编排的控制台/API 运维，以及真实媒体业务闭环。P3 是下一个里程碑门禁，不是已交付功能——
见 [能力实现状态](/zh/reference/status)。
