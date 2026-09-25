# Troubleshooting

The project's failure style is deliberate: most problems are meant to surface as an explicit code or a
refusal to start. This page maps the symptoms you are most likely to hit to their causes.

## The console loads, but API calls fail

| Symptom | Cause | Action |
| --- | --- | --- |
| Console returns `502` after the API was recreated | The console's nginx resolves the `api` upstream **at startup**, so it can hold a stale container IP | `./deploy/up.sh console` |
| Console loads but every request 401s | Not signed in, or the session/token is from a previous database | Sign in again; re-seed the demo account with `make demo-seed` |
| Demo login button missing | `SENSORYPLEX_DEMO_USERNAME` is empty in `.env` | `make demo-seed`, then `./deploy/up.sh api` |
| Demo password unknown | It was regenerated | Read `.data/demo-password`, or reset with `make demo-reset` |

## Semantic search

| Symptom | Cause | Action |
| --- | --- | --- |
| `503` with `retryable=false` | The search token does not match, or the search face is not configured | Check `SENSORYPLEX_INDEX_SEARCH_TOKEN` / `SENSORYPLEX_INDEX_AUTH_TOKEN` are the same value |
| `503` with `retryable=true` | The search face is not running or not reachable | `./deploy/up-events.sh`, then `./deploy/status.sh` |
| `502` | The search face answered, but not per contract (for example a model-release mismatch) | Read the error body; do not retry blindly |
| Semantic mode reports unavailable | Expected when the events profile is down. The API reports an explicit failure instead of a degraded ranking | Start the events profile |
| Second process cannot open the vector store | Milvus Lite is **process-exclusive** | Stop the other process holding `.data/index`; only one process may own it |

## Event chain refuses to start

| Message | Cause | Action |
| --- | --- | --- |
| `event_inflight_exceeds_tier_cap` | A batch depth is above the tier capacity | Lower `SENSORYPLEX_EVENT_RELAY_BATCH` / `SENSORYPLEX_EVENT_CONSUME_BATCH`, or raise the tier capacity deliberately |
| `not_injected` | The capacity or batch variable is missing | Fill it in `.env`; a missing tier variable is never defaulted silently |
| Stream missing / durable drift | JetStream state does not match the contract | Inspect the reported stream or durable; reset deliberately, never "fix" it by recreating streams at random |
| `up-events.sh` exits non-zero before starting | The BGE weight file is absent | Materialise `.data/models/bge-small-zh-v1.5/onnx/model_quantized.onnx`; the script intentionally does not download it |

## The material loop does not complete

| Symptom | Cause | Action |
| --- | --- | --- |
| Node reported offline (`503`) | The host worker is not running, so `local-host` heartbeats stop | `make task-worker-daemon`, then `make task-worker-status` |
| Node refused as `node_heartbeat_stale` | The heartbeat is older than 60 seconds | Restart the worker; check for a sleeping/suspended host |
| Job stays unschedulable with `data_locality_violation` | The plan wants shared-memory access on a node that is not co-located | Run that step on the co-located node — the constraint is a hard filter, not a warning |
| `make golden-path-check` reports a missed precondition | Usually a missing real video, a stopped worker or a down event chain | Check the three above, then re-run |
| Search returns an empty array on a fresh install | Correct behaviour: no business data, and nothing is invented | Import a video and run a task |

## Media decoding

| Symptom | Cause | Action |
| --- | --- | --- |
| `ffprobe` says `Protocol not found` for an SRT URL | The local `ffprobe` has no SRT protocol support | Drive SRT with GStreamer (`srtsink`/`srtsrc`); `make live-check` already does |
| Decode report is all-zero | No decode happened in this build | Install the GStreamer development files, or accept the `MEDIA_FEATURES=` anchor-only report where the gap is declared in `blockers` |
| `make media-test` fails | GStreamer development files are missing | Install them, or skip the decode-specific test — do not describe the skipped path as verified |
| Tracks look reordered | B-frame streams have non-monotonic PTS in decode order | Expected: reordering is explicit and counted in `out_of_order_items` |

## Toolchain and environment

| Symptom | Cause | Action |
| --- | --- | --- |
| `Exec format error` running a binary in a container | The artefact is a host binary (Mach-O), not a container one | Run it on the host; see the host exceptions in [Installation](/guide/installation) |
| `cargo` / `rustc` not found in a container | No project image ships a Rust toolchain | Run Rust commands on the host until a toolchain container is approved |
| `make test-integration` errors instead of skipping | No `SENSORYPLEX_TEST_DATABASE_URL` | Provide one — a missing database is meant to fail loudly, not to silently reduce coverage |
| Image pull fails | Registry reachability | The compose images are pinned by digest; a mirror prefix is already used for the console build. Server-side Milvus remains unverified for this reason |
| `make configure` cannot write `.env` inside a container | The repository bind mount cannot write back to the host | Run it on the host; it is a documented host target |

## When evidence is ambiguous

Two failure patterns in this project are especially important to get right:

1. **A passing hop is not a passing chain.** `make outbox-check` covers the publish hop only. Vectors are
   written by a different process — verify with `make consume-check` or `make event-pipeline-check`.
2. **Skips are not passes.** A skipped test, an unexecuted CI job or a healthy container never counts as
   acceptance. If a claim depends on real media, it needs a real authorised sample.

