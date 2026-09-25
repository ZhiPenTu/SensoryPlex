# Capability status

This page is the single place where the project states what is proven. Three labels are used, and they are
**not** interchangeable:

| Label | Meaning |
| --- | --- |
| **Verified** | A documented command was executed successfully on the stated platform and inputs |
| **Unverified** | The design and code exist, but no successful end-to-end execution is recorded |
| **Not implemented** | It does not exist in this version |

The version described here is **0.1.0 (runnable engineering base)**. The labels below reflect the repository's
own verification records; when in doubt, the command in the evidence column is the authority.

## Contract, runtime and media

| Area | Status | Evidence | Boundary |
| --- | --- | --- | --- |
| Versioned Protobuf contracts, codegen for Rust/Python/console | Verified | `make proto`, `make test-contracts` | Development preview; no stable SDK released |
| Rust core: config validation, bounded queues, descriptor validation | Verified | `make check` (cargo fmt/clippy/test on the host) | Pipelines, scheduling, process and lease management are not implemented in Rust |
| Real GStreamer decode → bounded arena → `BufferDescriptor` | Verified | `make media-replay MEDIA=...` | Authorised samples plus 10 openly licensed samples; 6 of them used for the ADR-009 format matrix |
| Lease issue / verify / release, audio 5s segmentation, adaptive frame sampling with reasons | Verified | `make media-replay`, `make media-test` | Requires GStreamer development files |
| Cross-process data plane (shared-memory handoff) | Verified | `make handoff-check MEDIA=...` | The consumer is an acceptance script, not yet a model worker in production |
| SRT live ingest with disconnect/recovery measurement | Verified | `make stream-up && make live-check` | Loopback only in this build; SRT encryption and credentialed publish are open |
| Runtime capability reporting, host accelerator tri-state | Verified | `make capability-check`, `make accelerator-check` | Probed on `macos-aarch64`; the `cuda` branch is **unverified** |
| Edge model plugins: VLM / ASR / OCR / BGE | Verified | `make model-check`, `make asr-check`, `make ocr-check`, `make embed-check` | Each runs four processes on the host with a real authorised sample |
| Model plugin concurrency admission and retry ledger | Verified | `make parallelism-check MEDIA="..." INPUTS=4` | Tier values come from the runtime report |

## Facts, vectors and events

| Area | Status | Evidence | Boundary |
| --- | --- | --- | --- |
| Immutable material revisions, lineage validation, authenticated keyword/tag/time queries | Verified | `make test-integration` | Requires a writable PostgreSQL |
| Vector write and retrieval closure | Verified | `make index-check EMBEDDINGS=...` | 11 scenarios on real PostgreSQL + Milvus **Lite**; server-side Milvus is unverified |
| Transactional outbox → NATS JetStream publish | Verified | `make outbox-check` | The **publish hop only**; NATS→sink consumption is a different path |
| Resident consumption loop (JetStream → vector sink) | Verified | `make consume-check` | Host-executed, single process; Milvus Lite is process-exclusive |
| Event chain inside compose with tier admission | Verified | `make event-pipeline-check` | Container-executed end to end through the running API |
| Gateway semantic search (`mode=semantic`) | Verified | `make semantic-check` | 13 scenarios. RRF/hybrid retrieval and relevance calibration are not implemented |
| NATS task dispatch | **Not implemented** | — | The only wired direction is fact → index |

## Topology and orchestration

| Area | Status | Evidence | Boundary |
| --- | --- | --- | --- |
| Node topology: agent, registration, heartbeat, 5 hard preflight checks, data locality, deployment intent, rollback, audit | Verified | `make node-check` | 6 acceptance scenarios |
| Cross-host mTLS rotation, main-node HA election | **Not implemented** | — | Future stage |
| Orchestration P1: durable runs, idempotency, cascade unlock/block, cancel precedence, bounded retry, crash recovery | Verified | `make orchestration-p1-check` | 7 scenarios on real PostgreSQL |
| Orchestration P2: locality-aware multi-node scheduling, drain/offline refusal, auditable failover | Verified | `make orchestration-p2-check` | 6 distributed scenarios |
| Orchestration P3: scenario product packs, console/API operations, real-media business loop | **Not implemented** | — | The next milestone gate |

## Product surface

| Area | Status | Evidence | Boundary |
| --- | --- | --- | --- |
| Console: login, session/CSRF/RBAC, real upload, Range playback, plan/task drafts, plugin config versions, scoped credentials, accounts/roles, audit | Verified | Browser acceptance recorded for material review; `make console-check MEDIA=...` | Runbook: `docs/runbooks/console.md` |
| Timeline: material/observation validation | Verified | `make timeline-check MEDIA=...` | Actual ASR/OCR/VLM fusion and **conflict adjudication** are not implemented |
| Complete business Golden Path (GP-01) | **Unverified** | `make golden-path-check` — 9 scenarios defined | `golden_path_verified` remains `false`; no successful real-media end-to-end run is recorded |
| Automatic model-to-material source mapping | **Not implemented** | — | — |
| In-process `ExecutionBackend` in Rust (`model_inference`) | **Not implemented** | `make capability-check` | Permanently listed in `unavailable_capabilities` today |

## Platform and engineering

| Area | Status | Evidence | Boundary |
| --- | --- | --- | --- |
| `macos-aarch64` as a first-class edge target | Verified | Remote `macos-15-arm64` runner passed; host acceptance runs | Accelerated processes must run natively |
| `linux-x86_64` control plane | Verified | Containerised control plane; hosted CI job | No NVIDIA-side accelerator acceptance recorded |
| `linux-aarch64` control plane | Verified | Containers | — |
| Windows | **Unverified** | — | WSL2/Docker compatibility only; not a first-class target |
| Object storage (NAS/MinIO) and ONNX/TensorRT adapters | **Not implemented** | — | Rust adapter traits and hash contracts exist; the implementations do not |

## How to re-verify

```sh
make check                       # lint + python tests + rust fmt/clippy/test
make test-integration            # real PostgreSQL
make node-check                  # node topology
make orchestration-p1-check      # durable orchestration
make event-pipeline-check        # resident event chain in compose
make semantic-check              # gateway semantic search
make golden-path-check           # the loop that is still unverified
```

Skipped tests and unexecuted CI jobs are never counted as passes. Remote GitHub Actions runs are
**manual-only** by design, so "the workflow exists" is not evidence that it ran.

