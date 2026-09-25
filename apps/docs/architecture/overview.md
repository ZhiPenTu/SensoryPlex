# System overview

The system is layered so that **contracts are shared, runtimes are not**. Rust owns the runtime, Python owns
model adaptation, and the two only meet through generated Protobuf code.

## Layers

```text
                    ┌───────────────────────────────────────────────┐
   Web console      │  apps/console  (React + TS + Vite, nginx 5173)│
                    └───────────────────────┬───────────────────────┘
                                            │ /v1 /auth /admin (same origin)
                    ┌───────────────────────▼───────────────────────┐
   Control plane    │  services/api (8091)      services/gateway    │
                    │  business/admin/identity  compatibility entry  │
                    └───────┬───────────────────────────────┬───────┘
                            │ SQL                           │ gRPC
                    ┌───────▼────────┐            ┌─────────▼─────────┐
   State & events   │  PostgreSQL    │            │  index search face │
                    │  25432         │            │  (index:50077)     │
                    │  outbox ──────►│  NATS      └─────────▲─────────┘
                    └────────────────┘  JetStream           │
                            ▲           24222                │
                            │                              │
                    ┌───────┴──────────────────────────────┴────────┐
   Runtime layer    │  Rust crates: runtime / media / timeline /    │
                    │  storage / execution / sdk                    │
                    │  decode → bounded arena → BufferDescriptor    │
                    └───────────────────────┬───────────────────────┘
                                            │ lease (shared memory, host-local)
                    ┌───────────────────────▼───────────────────────┐
   Edge plugins     │  vlm-moondream  asr-whisper-mlx  ocr-rapidocr │
                    │  embed-bge-onnx   (gRPC processor plugins)     │
                    └───────────────────────────────────────────────┘
```

## Process inventory

| Process | Port (loopback) | Language | Responsibility |
| --- | --- | --- | --- |
| `console` | `5173` | nginx + React | Static console, reverse proxy of `/v1`, `/auth`, `/admin` to the API |
| `docs` | `5174` | nginx + VitePress | Static framework documentation site (no reverse proxy, no credentials) |
| `api` | `8091` | Python / FastAPI | Business, admin and identity endpoints; dev image also carries ruff/pytest/proto tooling |
| `gateway` | `8090` | Python / FastAPI | Legacy query entry and startup compatibility layer |
| `migrate` | — | Python | One-shot `tools/migrate.py`, exits after applying migrations |
| `relay` | — | Python | Transactional outbox → NATS JetStream (`events` profile) |
| `index` | `50077` (compose network only) | Python | Vector write, resident JetStream consumer, gRPC search face |
| `postgres` | `25432` | PostgreSQL 16 | Metadata, facts, outbox |
| `nats` | `24222` | NATS 2.11 | JetStream event bus (monitoring on `28222`) |

## How a piece of media becomes a material

1. **Ingest** — a file or an SRT stream is referenced by a `MediaSourceRef` (secret name plus digest).
2. **Decode** — GStreamer decodes into a **bounded arena**; the pipeline is deliberately leaky at a controlled
   point rather than buffering everything.
3. **Describe** — the arena produces `BufferDescriptor`s: intervals, memory kind, and a digest.
4. **Hand off** — the descriptor is turned into a **lease** that a separate process can read, verify and
   release. Bytes never travel as protobuf payloads.
5. **Perceive** — plugins produce observations (text blocks, transcripts, descriptions, embeddings) onto the
   presentation timeline.
6. **Fuse** — the timeline layer checks the observations and their lineage, then writes facts transactionally
   together with an outbox event.
7. **Distribute** — the relay confirms publication to JetStream, writing `published_at` only after
   confirmation.
8. **Index** — the resident index process consumes events, encodes text with the BGE plugin, writes to the
   vector store and serves retrieval.
9. **Retrieve** — the gateway/API forwards a semantic query to the search face and hydrates hits against
   PostgreSQL before returning them.

## Boundary rules

- **Plugins never import service internals.** A plugin depends on the SDK only, holds no database credentials,
  and reads media exclusively through a lease reader.
- **Rust owns the runtime; Python owns model adaptation.** Acceleration stays on the host that owns it.
- **Nothing raw crosses a control boundary.** No frames, PCM, tensors or secrets in control messages, events
  or logs — only controlled references.
- **Every queue and concurrency limit is bounded**, with observable timeout, cancel, retry and failure
  semantics.

## Repository layout

| Path | Contents |
| --- | --- |
| `proto/` | Versioned contracts for `common`, `material`, `media`, `runtime`, `gateway`, `index`, `node`, `orchestration` |
| `crates/` | Rust workspace: `runtime`, `media`, `timeline`, `storage`, `execution`, `sdk` |
| `plugins/python/common/` | `edge_material_sdk` and the generated Python messages |
| `plugins/python/processors/` | The four edge model plugins |
| `apps/console/` | React + TypeScript + Vite console |
| `apps/docs/` | This documentation site |
| `services/` | `api`, `gateway`, `outbox-relay`, `index-worker` |
| `db/migrations/` | Append-only, explicit PostgreSQL migrations |
| `config/pipelines/` | File and SRT pipeline configurations |
| `deploy/` | Compose files, nginx config, up/down/status scripts, macOS launchd templates |
| `tests/` | Contract tests, real-PostgreSQL integration tests, media sample notes |
| `tools/` | Configure, codegen, migrate, verification and real-media probe tooling |

## Read next

- [Contracts & codegen](/architecture/contracts)
- [Services & nodes](/architecture/services)
- [Orchestration core](/architecture/orchestration)
- [Event pipeline & retrieval](/architecture/event-pipeline)

