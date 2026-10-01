# Console walkthrough

The Web console is the only product surface. This page walks the upload → task → material → playback
loop and states what each screen reports when a capability is missing.

## Before you start

```sh
./deploy/up.sh && ./deploy/up-events.sh     # control plane + resident event chain
make demo-seed                              # demo account for the login page
make task-worker-daemon                     # resident host worker
```

## Screens

| Route | Screen | What it is for |
| --- | --- | --- |
| `/assets` | Video library | Import media, see admission state |
| `/jobs` | Processing tasks | Create and start tasks against a published plan |
| `/materials` | Material search | Query slices, read observations, replay the original |
| `/plugins` | Plugins | Plugin configuration versions and their validation |
| `/nodes` | Nodes | Node topology, install location, preflight results |
| `/pipelines` | Plans | Draft and publish pipeline revisions |
| `/users`, `/audit`, `/access` | Management | Accounts and roles, audit trail, scoped credentials |

## 1. Import media — `/assets`

Sign in with the demo account, open **Video library** and choose **Import video**. `.mp4` and `.webm` are
accepted. After the upload finishes the row is admitted as "pending admission" — the file exists and is
addressable, but no inference has run on it and no material exists yet. The console never pre-creates a
material row to make the shelf look populated.

## 2. Create and start a task — `/jobs`

Open **Processing tasks**, create a task, associate the uploaded video with a **published** plan (the
built-in OCR plan is a reasonable first choice), then choose **Start**.

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

The host worker keeps the co-located `local-host` node heartbeating, consumes `task_process` intents,
decodes with GStreamer, extracts text observations with RapidOCR, fuses them onto the timeline, writes the
facts transactionally, and marks the task `completed`. Completion is what triggers vectorisation and makes
the search face ready for that material.

## 4. Read the result — `/materials`

When the row turns into **view materials**, the material search screen gives you:

- **Timeline-aligned slices** — each row is a `[start_ms, end_ms)` window of the source stream, with its
  observations attached;
- **Text observations** — the OCR blocks with their frame-pixel coordinates and producing model release;
- **Filters** — keyword (literal substring), tags (AND) and time range (overlap), with modalities combined
  as OR;
- **Historical revisions** — querying an older revision marks it `superseded` while the original snapshot
  stays immutable;
- **Original playback** — HTTP Range streaming of the source media for the selected interval.

Semantic mode appears here once `./deploy/up-events.sh` is running and the material has been vectorised.
Without the search face the API reports an explicit failure; it never returns a degraded fake ranking.

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

