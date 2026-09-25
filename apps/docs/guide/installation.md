# Installation

Installation is split by **role**, not by package: the control plane is always containerised, while
anything that touches a host accelerator stays on the host by design.

## Pick a shape

| Shape | When | What runs where |
| --- | --- | --- |
| **All-in-one developer** | You want to see the console and run acceptance | Everything from `deploy/compose/docker-compose.poc.yml` on one host; model plugins optionally on the host |
| **Mac mini edge node** | Accelerated inference lives on Apple Silicon | Control plane in containers; runtime + model plugins natively under `launchd` (ADR-015) |
| **Main node + LAN workers** | Accelerators are spread across machines | Main node holds the control plane; worker sub-nodes register with endpoints, heartsbeats and preflight (ADR-026) |
| **Live ingest** | You need SRT in addition to files | `make stream-up` brings up MediaMTX on loopback-only ports |

## Host prerequisites by role

**Control plane (always containers)**

- Docker Compose v2 with enough disk for the Postgres volume, the NATS JetStream data and `.data/`.
- No host `python`, `node` or `uv` needed for control-plane validation: `make lint-ruff`, `make test-py`,
  `make proto`, `make console-build` and the orchestration checks all execute inside containers.

**Host-side exceptions**

| Exception | Why it cannot be containerised |
| --- | --- |
| `make configure` | The repository is bind-mounted into containers as a view that cannot write credentials back to the host `.env`. |
| `cargo` / `make check` / Rust tests | No project image ships `rustc`/`cargo`. |
| Model plugins (`vlm`, `asr`, `ocr`, `embed`) | MLX/Metal, CoreML and ollama endpoints only exist on the host. `mlx-metal` cannot even be compiled inside a Linux container. |
| `launchd` resident shape | `launchctl` / `sysctl` do not exist in containers. |
| Media ingest and replay checks | HF weight cache, GStreamer plugins and CoreML EPs are host-local. |

## Authorised media samples

Every media path runs on a real, licensed sample — the project never generates a synthetic fixture to
stand in for one. Samples are exposed read-only:

```sh
MEDIA_DIR=~/Movies ./deploy/up.sh          # binds ~/Movies to /host-media inside the api container
make media-replay MEDIA=/absolute/path/authorized-sample.mp4
```

The Makefile rewrites `MEDIA=...` to `/host-media/$(basename)`, so the file must be inside `MEDIA_DIR`.

## Model assets

| Plugin | Asset | Layout |
| --- | --- | --- |
| `ocr-rapidocr` | PP-OCR ONNX weights shipped with the plugin | Bundled, or an explicit `model_dir` |
| `embed-bge-onnx` | `bge-small-zh-v1.5` quantised ONNX | `.data/models/bge-small-zh-v1.5/onnx/model_quantized.onnx` (required by `up-events.sh`) |
| `vlm-moondream` | Vision model served by a local ollama | `127.0.0.1:11434` allowlisted in the manifest |
| `asr-whisper-mlx` | MLX Whisper, Apple Silicon only | Host virtualenv (`uv run`) |

Weights are pre-provisioned by the operator. Plugins never download them on their own, with the single
documented exception of `ocr-rapidocr` reaching its model catalogue host when weights are missing or the
digest does not match — which is why its manifest declares `network: allowlist`.

## GStreamer

Real decode (files and SRT) needs the GStreamer development files on the host. Without them,
`make media-replay MEDIA_FEATURES=` still produces the pure anchor report — with the decode data plane
left all-zero and the gap declared in `blockers` rather than silently skipped.

::: tip SRT and `ffprobe`
If `ffprobe` reports `Protocol not found` for an SRT URL, drive SRT with GStreamer (`srtsink`/`srtsrc`)
instead. `make live-check` uses GStreamer directly and does not depend on OBS or an RTMP hop.
:::

## Verify the installation

```sh
./deploy/status.sh          # console / api / gateway HTTP probes
make capability-check       # capability reporting: unavailable reasons, accelerator tri-state
make lint-ruff test-contracts
```

## Upgrade and rollback

- Images are pinned by digest in the compose file; a rebuild is `./deploy/up.sh` and a partial rebuild is
  `./deploy/up.sh api console`.
- Database migrations are append-only and run through `tools/migrate.py`; the `migrate` service runs
  before `gateway`/`api` start. Never edit a historical revision to roll back — add a new migration.
- `./deploy/down.sh` keeps named volumes; `./deploy/down.sh --volumes` deletes them. Treat
  `--volumes` as a destructive action and confirm you have a dump first.
- `./deploy/down-events.sh --volumes` additionally removes `.data/index` (the vector store directory).

## Next

- [Deployment](/operations/deployment) for ports, volumes and the resident shapes.
- [Configuration](/operations/configuration) for the environment variables that refuse to start when
  they are inconsistent.

