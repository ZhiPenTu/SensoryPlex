# Quickstart

Goal: get the container stack running, log into the Web console, and understand exactly which parts of
the loop are live in this build.

## Requirements

| Need | Why | Where it must exist |
| --- | --- | --- |
| Docker Compose v2 | The whole control plane runs in containers | Host |
| `uv` + Python 3.12 | `make configure` writes `.env`; host-side media checks | Host |
| Rust 1.96 (`cargo`) | `cargo build --workspace --locked`; no project image ships a Rust toolchain yet | Host |
| GStreamer development files | Real decode paths (`make media-replay`, worker decode) | Host, only for media work |
| Authorised media samples | Every media check runs on a real, licensed sample | Host, exposed via `MEDIA_DIR` |

Ports default to console `5173`, docs `5174`, api `8091`, gateway `8090`, postgres `25432`, nats `24222`
and are overridable in the repository-root `.env` — see [Configuration](/operations/configuration).

## 1. Configure and build

```sh
make configure   # host: writes random credentials into .env (never commit .env)
make setup       # api container: proto generation; host: cargo build --workspace --locked
```

`make configure` deliberately runs on the host: the repository is bind-mounted into the containers as a
view that cannot write new credentials.

## 2. Start the stack

```sh
./deploy/up.sh          # docker compose up -d --build --wait, waits for every healthcheck
./deploy/status.sh      # probes console / api / gateway over HTTP
```

`./deploy/up.sh` refuses to report success if a service does not reach `healthy`. Use
`./deploy/up.sh api console` to rebuild only part of the stack, `./deploy/logs.sh [service]` to follow
logs, and `./deploy/down.sh` to stop (add `--volumes` to delete the data volumes). The stack also brings
up the static documentation site (`docs`), which serves the site built into its image.

Then open:

- Web console — <http://127.0.0.1:5173>
- Framework documentation — <http://127.0.0.1:5174> (English at `/`, Chinese at `/zh/`)
- API docs — <http://127.0.0.1:8090/docs> (the compatibility gateway also serves `/docs`)

## 3. Log in

```sh
make demo-seed    # creates demo account, writes the random password to .data/demo-password
```

The console login page shows a "fill in demo account" button when demo mode is enabled. Demo credentials
are privileged — never seed them on a production-like node; leave `SENSORYPLEX_DEMO_*` empty there.

## 4. Optional: start the resident event chain

```sh
./deploy/up-events.sh   # relay (outbox → JetStream) + index (consume → vector → search face)
```

This is a separate `events` profile and is **not** started by `./deploy/up.sh`. It pre-checks the BGE
weights at `.data/models/bge-small-zh-v1.5/onnx/model_quantized.onnx` and exits non-zero when they are
missing — it never downloads them for you.

## 5. Walk the real loop

```sh
make task-worker-daemon   # resident host worker: keeps local-host online, runs GStreamer + OCR + timeline fusion
make task-worker-status
```

Then, in the console: **Video library** → import a `.mp4`/`.webm` → **Processing tasks** → create a task
against the uploaded video and a published plan → **Start**. When the host worker finishes, the row turns
into "view materials": **Material search** shows timeline-aligned slices, the text observations, and
Range streaming playback of the original video.

Finally, run the automated regression:

```sh
make golden-path-check    # 9 scenarios: auth, node readiness, upload, plan publish, dispatch,
                          # on-device compute, vector write, semantic search, original playback
```

## What you should see on a fresh install

| Observation | Why it is correct |
| --- | --- |
| Search returns an empty array | The initial database has no business data. The base never invents model output. |
| `make golden-path-check` reports an unmet precondition | There is no real video in the library yet, the host worker is not running, or the node is offline. |
| Semantic search reports unavailable until `up-events.sh` runs | The search face is a separate resident process; without it the API reports an explicit failure instead of a fake hit. |

::: warning Do not conclude from a green healthcheck
A `200` from `/v1/health`, a healthy container and a healthy node are **not** end-to-end evidence. The
only evidence for the media loop is a real authorised sample traversing upload → dispatch → compute →
vector → search → playback. Anything else is infrastructure readiness.
:::

## Next

- [Console walkthrough](/guide/console-walkthrough) — the same loop, screen by screen.
- [Deployment](/operations/deployment) — ports, volumes, the macOS resident shape and rollback.
- [Capability status](/reference/status) — what is verified, unverified and unimplemented.

