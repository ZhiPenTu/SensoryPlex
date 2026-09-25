# ADR index

Decisions are recorded as Architecture Decision Records. ADR-001 to ADR-008 live in the root blueprint
document; ADR-009 onwards live in `docs/adr/`. The full texts are currently written in Chinese.

| ADR | Title | What it decides |
| --- | --- | --- |
| ADR-001 | Separate the control plane from the media data plane | Raw media never travels over the control bus |
| ADR-002 | Rust owns the runtime, Python owns model plugins | The split that keeps acceleration host-native |
| ADR-003 | GStreamer for live media, FFmpeg for offline tooling | Which tool is authoritative for which path |
| ADR-004 | NATS JetStream as the control and event bus | The transport for events and commands |
| ADR-005 | ONNX Runtime execution providers as the hardware adaptation axis | How accelerators are reached without per-model vendor code |
| ADR-006 | Timeline and MaterialUnit are the source of truth; the vector store is not | Why hits are hydrated from PostgreSQL |
| ADR-007 | Fast and slow paths with separate latency targets | Where latency budgets are allowed to differ |
| ADR-008 | Apple Silicon is a first-class edge target | Unified memory and the memory-kind contract cited by the capability rules |
| ADR-009 | Media format support matrix and rejection semantics | What gets admitted, and how a rejection is expressed |
| ADR-010 | Security boundary of the cross-process data plane | Bounded shared-memory leases instead of copied payloads |
| ADR-011 | Retention windows allocated per kind | How long each kind of retained data lives |
| ADR-012 | Model plugins and the edge inference boundary | The plugin contract for models, incl. the lease reader |
| ADR-013 | Modular API consolidation and deployment boundary | How the API is split and deployed |
| ADR-014 | ASR plugin and audio sample layout contract | Strict `sample_format` interpretation, no assumed width |
| ADR-015 | macOS resident shape and unified-memory tiers | `launchd` services and the tier table |
| ADR-016 | OCR and the ONNX execution backend | Explicit failure instead of silent CPU fallback |
| ADR-017 | BGE text embeddings and dimension versioning | Collection keys and versioned vector dimensions |
| ADR-019 | Per-tier queue caps for runtime consumption | The caps that refuse over-limit configuration |
| ADR-020 | Vector index persistence and retrieval closure | Write-then-read confirmation before `ready` |
| ADR-021 | Model worker concurrency limiting by tier | Admission, retry ledger and peak in-flight accounting |
| ADR-022 | Host accelerator probing and reporting | The tri-state probe separated from process backends |
| ADR-023 | Gateway semantic search wiring and the index search face | Same-source guard, failure typing, `retryable` |
| ADR-024 | Outbox dispatch wiring and consumption dedup boundary | `published_at` only after confirmation; `Nats-Msg-Id = event_id` |
| ADR-025 | Resident consumption loop and sink wiring | Bad-event fail-stop with exit code 3 |
| ADR-026 | Web main node and LAN plugin worker topology | Node registry, preflight, data locality, audited deployment |
| ADR-027 | Tiered backpressure and containerised residency for the event chain | The `events` profile and tier admission |
| ADR-028 | Runtime → Timeline wiring and authorised append | How runtime output becomes authorised facts |
| ADR-029 | Orchestratable plugin execution core | Immutable revisions, durable runs, multi-node scheduling |

::: tip Numbering
There is no ADR-018 file in the repository; the numbering is deliberately left with a gap rather than
renumbered, because ADR identifiers are referenced from code and migrations.
:::

## Where to read them

- ADR-001 … ADR-008 — `技术选型ADR与V1实施蓝图.md` in the repository root
- ADR-009 … ADR-029 — [`docs/adr/`](https://github.com/ZhiPenTu/SensoryPlex/tree/master/docs/adr)

Related design documents worth reading alongside them:

| Document | Contents |
| --- | --- |
| `docs/contracts/README.md` | The contract rules quoted throughout this site |
| `docs/design/plugin-orchestration.md` | The orchestration design: states, scheduling, recovery |
| `docs/design/console-mvp.md` | Console MVP engineering design |
| `docs/implementation-status.md` | The long-form implementation inventory |
| `docs/verification.md` | The reproducibility evidence log |
| `docs/development-checklist.md` | Current execution order and closeout state |
| `开放式插件开发文档.md` | The open plugin development specification |

