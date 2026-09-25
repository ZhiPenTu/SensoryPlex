# Orchestration core

The orchestration core (ADR-029) turns a published plan into durable, auditable execution. It is the part of
the base that decides **what runs where**, and it is deliberately conservative: it would rather refuse to
schedule than run somewhere the data cannot legally be.

## The chain

```text
immutable Pipeline revision
   → DAG compilation (restricted graph, modality + placement validation)
   → PipelineRun / PipelineTask state machine (deterministic)
   → cluster scheduling by data locality and resource caps
   → cross-node claim, stale-result rejection
   → auditable failover with full assignment history
```

## P0 — compile kernel

- `orchestration/v1` contracts, restricted DAG compilation, modality and placement validation, and a
  deterministic Run/Task state machine.
- Validation happens before anything is dispatched: a graph that cannot satisfy its declared memory kinds or
  placement is rejected at compile time, not discovered halfway through a run.

## P1 — durable execution

Migration `0008_orchestration_run_task.sql` adds `pipeline_definition`, `pipeline_revision`, `pipeline_run`,
`pipeline_task`, `pipeline_task_edge` and `scheduler_assignment`.

| Guarantee | How it is enforced |
| --- | --- |
| Published revisions are immutable | A `deny_fact_update` trigger on `pipeline_revision` blocks in-place update and delete |
| Active runs are strictly idempotent | A partial unique index on `(pipeline_id, revision, input_ref, idempotency_key)` |
| Results cannot be replayed out of context | Reconciliation validates `(run_id, task_id, attempt, assignment_id)`; anything else is rejected as `stale_task_result` |
| Unlock propagates only on success | Required upstream success cascades readiness downward |
| Failure blocks downstream work | A failed required upstream marks dependents `blocked` with `reason_code='upstream_failed'` |
| Cancellation wins | After a run is cancelled, late results are discarded with an audit record and can never flip a task into success or unlock downstream work |
| Retries are bounded | Retryable errors enter `retry_wait`, back off, refresh the deadline and re-dispatch; exceeding `max_attempts` lands as `retry_exhausted:<reason>` |
| Crash recovery leaves no orphans | Stale leases are scanned atomically: facts already written are converged safely, otherwise the task returns to `ready` and is re-dispatched |

### API surface

| Endpoint | Permission |
| --- | --- |
| `/v1/orchestration/pipelines[:validate]` | `pipelines:manage` |
| `/v1/orchestration/runs[/:id/cancel]` | `jobs:write` / `jobs:read` |
| `/v1/orchestration/scheduler:step` | `jobs:write` |
| `/v1/orchestration/tasks/:id:result` | `jobs:write` |

## P2 — multi-node cluster

- **Locality-aware filtering.** A co-located node (`is_co_located`) exclusively owns raw `BufferDescriptor`
  consumption. Remote GPU or edge nodes handle observations and object references only.
- **Double rejection of raw edges.** Cross-host raw-buffer edges are blocked when the graph is parsed *and*
  when it is scheduled, with `data_locality_violation`.
- **Draining and offline are hard blocks.** Both preflight and task claim answer `409`; there is no silent
  degradation and no arbitrary reassignment.
- **Auditable failover.** When a node is unreachable or a lease expires, recovery reclaims the task and
  re-dispatches it, retaining the first and second assignments in the ledger.

## Where plugin manifests fit

`consumes` / `produces` in a manifest are the **evidence the orchestrator validates edges against** — they are
not an instruction to call plugins in list order. A published revision pins concrete `plugin_id`, version,
artifact digest, config hash, input/output modality, time join, placement, deadline and attempt policy; the
orchestrator only expands tasks over that immutable version.

## Verification

```sh
make orchestration-check       # compile kernel
make orchestration-p1-check    # 7 durable-execution scenarios on real PostgreSQL
make orchestration-p2-check    # 6 distributed scenarios
pytest tests/integration/test_orchestration_api.py
```

## What comes next (P3)

Scenario product packs, console/API operations for orchestration, and the real-media business loop. P3 is the
next milestone gate, not a delivered feature — see [Capability status](/reference/status).

