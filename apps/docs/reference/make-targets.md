# Make targets

The Makefile is the project's entry point, and it encodes an architectural rule: **control-plane validation
runs inside containers**, while anything needing a host accelerator, host media files or `cargo` runs on the
host.

## Where a target runs

| Location | Meaning |
| --- | --- |
| **api container** | `docker compose exec -T api ...` — Python tooling, pytest, proto generation, orchestration and event checks |
| **gateway / console container** | Gateway smoke tests; console build and dev server |
| **host** | `cargo`, `launchd`, HF weights, CoreML/MLX, MediaMTX, authorised media samples |
| **mixed** | Some steps run in a container and some on the host (for example a build then a verify) |

There is one deliberate exception: `make configure` must run on the host because the repository bind mount
inside containers cannot write new credentials back to the host `.env`.

## Containers

| Target | Runs in | Purpose |
| --- | --- | --- |
| `make up` / `make down` | host shell | Start or stop the compose stack |
| `make infra` | host shell | Start only `postgres` and `nats` |
| `make gateway` | host shell | Start only the compatibility gateway |
| `make migrate` | api container | Apply append-only migrations |

## Code generation and quality

| Target | Runs in | Purpose |
| --- | --- | --- |
| `make proto` | api container | Regenerate Python bindings and console types from `proto/` |
| `make lint-ruff` | api container | `ruff check` + `ruff format --check` |
| `make format` | mixed | Rust `cargo fmt` plus Python `ruff format` |
| `make test-contracts` | api container | Contract tests; no external services |
| `make test-py` / `make test-integration` | api container | Contract + integration tests against real PostgreSQL and NATS |
| `make check` | mixed | lint + Python tests + Rust fmt/clippy/test |

## Media and runtime

| Target | Runs in | Purpose |
| --- | --- | --- |
| `make runtime` | host | Run the Rust runtime service |
| `make media-test` | host | Decode-path unit tests (needs GStreamer development files) |
| `make media-replay MEDIA=...` | mixed | Real decode, anchors and the decode report on one sample |
| `make media-check MEDIA=...` | host | Anchor/media acceptance |
| `make pipeline-check` | host | Pipeline configuration checks |
| `make live-check` | mixed | SRT ingest scenarios; run `make stream-up` first |
| `make handoff-check MEDIA=...` | mixed | Cross-process shared-memory handoff |
| `make backpressure-check` | mixed | Backpressure metrics |
| `make capability-check` | mixed | Capability reporting and unavailable reasons |
| `make accelerator-check` | host | Four-way reconciliation of the accelerator probe |

## Edge model plugins

| Target | Runs in | Purpose |
| --- | --- | --- |
| `make model-check MEDIA=...` | mixed | VLM via local ollama |
| `make asr-check MEDIA=...` | mixed | ASR via MLX Whisper |
| `make ocr-check MEDIA=...` | mixed | OCR via bundled PP-OCR ONNX (`EXPECT=empty` for no-text samples, `PROVIDER=coreml` for CoreML) |
| `make embed-check MEDIA=...` | mixed | BGE text embedding |
| `make parallelism-check MEDIA="a b" INPUTS=4` | host | Model worker concurrency admission and retry ledger |

## Events and retrieval

| Target | Runs in | Purpose |
| --- | --- | --- |
| `make outbox-check` / `make outbox-run` | api container | Outbox → JetStream publish hop |
| `make index-check EMBEDDINGS=...` | host | Vector write-back closure on Milvus Lite |
| `make consume-check` | host | Single-process JetStream → vector consumption |
| `make event-pipeline-check` | api container | Both resident services end to end inside compose |
| `make semantic-check` | host | Gateway semantic search scenarios |
| `make events-up` / `events-down` / `events-logs` | host shell | The compose `events` profile |

## Topology and orchestration

| Target | Runs in | Purpose |
| --- | --- | --- |
| `make node-check` | api container | Node topology acceptance |
| `make orchestration-check` | host | Compile kernel |
| `make orchestration-p1-check` | api container | Durable orchestration scenarios |
| `make orchestration-p2-check` | api container | Distributed multi-node scenarios |

## Product surface and acceptance

| Target | Runs in | Purpose |
| --- | --- | --- |
| `make console-build` / `console-dev` | console container | Rebuild or run the console |
| `make console-check MEDIA=...` | api container | Verify console behaviour against a real sample |
| `make golden-path-check` | mixed | The 9-scenario business loop |
| `make docs-check` | docs container | Build this documentation site and validate its links |

## Documentation site

| Target | Runs in | Purpose |
| --- | --- | --- |
| `make docs-install` | docs container | One-off `npm ci`; the only docs target that needs the npm registry |
| `make docs-build` | docs container | Build the static site from `apps/docs` into `apps/docs/.vitepress/dist` |
| `make docs-check` | docs container | Build plus locale-parity and internal link/asset validation |
| `make docs-dev` | docs container | VitePress dev server with HMR, on `DOCS_DEV_PORT` |
| `make docs-serve` | docs container | Preview the built artifact (shares the port with `docs-dev`) |

The docs site is plain static output, so `make docs-build` is also how you produce an artefact to host
anywhere else — see [Deployment](/operations/deployment). Building and installing are separate targets so
that a registry outage cannot turn a documentation edit into a failed build; `make docs-check` is the gate
to run before committing.

## Host-only groups

| Target | Purpose |
| --- | --- |
| `make configure` | Write random credentials into `.env` |
| `make resident-probe` / `resident-install` / `resident-status` / `resident-uninstall` | macOS `launchd` resident shape |
| `make task-worker` / `task-worker-daemon` / `task-worker-status` / `task-worker-stop` | Host task worker (foreground or daemonised) |
| `make stream-up` / `stream-down` / `stream-status` / `stream-logs` | Independent MediaMTX compose |
| `make demo-seed` / `demo-reset` | Demo account lifecycle |
| `make setup` | Configure, generate proto, build the Rust workspace |

