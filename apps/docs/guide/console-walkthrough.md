# Console walkthrough

The Web console is the only product surface. This page walks the upload → task → material → playback
loop and states what each screen reports when a capability is missing.

## Before you start

```sh
./deploy/up.sh && ./deploy/up-events.sh     # control plane + resident event chain
make demo-seed                              # demo account for the login page
make task-worker-daemon                     # resident host worker
```

In the local environment, open `http://127.0.0.1:5173`. The standalone preview activates Demo quick login by default:

![Login and studio base](/images/console/01-login.png)

## Screens

| Route | Screen | What it is for |
| --- | --- | --- |
| `/assets` | Video library | Import media, see admission state |
| `/jobs` | Processing tasks | Create and start tasks against a published plan, inspect DAG topology and Gantt timings |
| `/materials` | Material search | Query slices, read observations, replay the original, toggle keyword and semantic search |
| `/plugins` | Plugins | Plugin configuration versions, health gates, and dual-slot blue-green hot deploy |
| `/nodes` | Nodes | Node topology, hardware accelerator detection (Metal/CUDA/CoreML), and resource metrics |
| `/pipelines` | Plans | Draft and publish pipeline revisions with model binding |
| `/users`, `/audit`, `/access` | Management | Accounts and roles, immutable audit trail, scoped credentials |

## 1. Import media — `/assets`

Sign in with the demo account, open **Video library** and choose **Import video**. `.mp4` and `.webm` are
accepted. After the upload finishes the row is admitted as "pending admission" — the file exists and is
addressable, but no inference has run on it and no material exists yet. The console never pre-creates a
material row to make the shelf look populated.

![Video library](/images/console/02-assets.png)

## 2. Create and start a task — `/jobs`

Open **Processing tasks**, create a task, associate the uploaded video with a **published** plan (the
built-in OCR+Timeline plan is a reasonable first choice), then choose **Start**.

![Task queue and state machine](/images/console/03-jobs.png)

Clicking **Execution detail** opens the DAG execution topology and Gantt timeline dialog:
- **Phase topology swimlanes**: clearly differentiates data-plane input, L1 fast discriminant path (OCR/ASR), timeline fusion, and L2 delayed enrichment slow path (VLM);
- **Execution Gantt chart**: visually displays start/end offsets and real durations of all modality operators with millisecond precision.

![DAG execution topology](/images/console/03-job-detail-topo.png)

![Execution Gantt chart](/images/console/03-job-detail-modal.png)

A task only becomes dispatchable when:

- the plan has a published, immutable revision;
- the target node is online and has passed preflight;
- the node satisfies the plan's placement and data-locality constraints.

If any of those is false the console shows the specific unmet precondition. A missing node is reported as
an offline node (HTTP 503 semantics), not as a queued job that silently waits.

## 3. Let the host worker do the work

```sh
make task-worker-status     # is the resident worker alive?
```

The host worker keeps the co-located `local-host` node heartbeating, consumes task execution intents,
decodes with GStreamer, extracts text and speech observations, fuses them onto the timeline, writes the
facts transactionally, and marks the task `completed` or `ready_for_review`. Completion is what triggers
vectorisation and makes the search face ready for that material.

## 4. Read the result — `/materials`

When the row turns into **view materials**, the material search screen gives you:

- **SENSORYPLEX HUD Monitor** — left timecode and coverage monitor, center HTTP Range video player (supporting accurate cursor stepping and sync), right and bottom cards for OCR text, ASR transcript, and VLM visual descriptions; bottom multi-track millisecond Timeline Bus;
- **1-second grid continuous slices** — each row is a `[start_ms, end_ms)` window of the source stream with verified observations attached, marking pending states when no model detected content;
- **Dual-mode search (Keyword & Semantic)** — supports substring filtering or switching to "Semantic search" with natural language queries (e.g., "red long arrow"), backed by resident BGE embeddings and Milvus Lite;
- **Original playback** — clicking any matched slice jumps the player to the exact millisecond offset for streaming playback.

![Material search and HUD monitor](/images/console/04-materials-search.png)

![Cross-video semantic search](/images/console/04-materials-semantic.png)

![1-second grid slice checklist](/images/console/04-materials-slices-modal.png)

## 5. Compute nodes and plugin management — `/nodes` & `/plugins`

- **Node topology (`/nodes`)**: real-time monitoring of CPU/unified memory load, hardware accelerator detection (Metal/CoreML/CUDA), and zero-copy shared memory (LeaseBuffer) data mode;
- **Plugin hot deployment (`/plugins`)**: per ADR-030, supports independent-process blue-green hot deployment of in-house Python plugins with digest verification, config validation, and consecutive health gates.

![Node topology and hardware accelerators](/images/console/07-nodes.png)

![Plugin blue-green hot deploy](/images/console/06-plugins-hotdeploy.png)

## What is deliberately reported as unavailable

- Semantic search before the index process is up.
- Any capability whose backend is missing on this platform — the reason is rendered from
  `unavailable_reason`, not replaced with a generic message.
- Media admission and as-yet-unwired execution paths in the management screens. The base does not render
  a disabled feature as if it worked.

## Acceptance

```sh
make golden-path-check
```

Nine scenarios: authentication, node readiness, video upload, plan publish, task dispatch, on-device
compute, vector write, semantic search, original playback. If this check has not been executed
successfully on real media, do not describe the loop as complete — see
[Capability status](/reference/status).
