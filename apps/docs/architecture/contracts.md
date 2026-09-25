# Contracts & codegen

`proto/` is the **single cross-language contract source**. If a rule is not expressible in the Protobuf
contracts, it is not part of the interface — and generated code is never the place to fix that.

## Contract packages

| Package | File | Covers |
| --- | --- | --- |
| `common/v1` | `proto/common/v1/common.proto` | Shared value types and enums |
| `material/v1` | `proto/material/v1/material.proto` | Materials, revisions, observations, lineage |
| `media/v1` | `media.proto`, `live.proto`, `handoff.proto` | Sources, anchors, replay reports, live ingest, cross-process handoff |
| `runtime/v1` | `proto/runtime/v1/runtime.proto` | Health, capability description, processor plugin service |
| `gateway/v1` | `gateway.proto`, `console.proto` | Query entry and console-facing API |
| `index/v1` | `proto/index/v1/index.proto` | Vector write and search face |
| `node/v1` | `proto/node/v1/node.proto` | Node registration, heartbeat, preflight, deployment intent |
| `orchestration/v1` | `proto/orchestration/v1/orchestration.proto` | Pipeline revisions, runs, tasks, scheduler |

## Code generation

| Target | How | Committed? |
| --- | --- | --- |
| Rust (`tonic`/`prost`) | `build.rs` at build time | No — generated during the build |
| Python SDK messages | `make proto` → `tools/generate_proto.py` | Yes — committed into the SDK, never hand-edited |
| Console types | `make proto` → `tools/generate_console_types.py` | Yes — same rule |

```sh
make proto     # runs inside the api container; requires ./deploy/up.sh to be up
```

## Field rules

- **No renumbering.** Field numbers are permanent.
- **Removal requires `reserved`.** A deleted field's number can never be reused.
- **Breaking semantic changes go to a new protocol major.** The current line is a development preview; no
  stable SDK has been released.
- **Time ranges** are `[start_ms, end_ms)` on one stream, with `end_ms` strictly greater than `start_ms`.
- **`confidence` is optional.** When it is missing, a reason must be stated; `0`/`1` never stand in for
  "unknown".
- **Two distinct SHA-256 fields** exist for plugin artifacts and model artifacts. They are not
  interchangeable: an artifact that describes the code is not an artifact that describes the weights.

## REST mapping

The HTTP surface follows the Protobuf JSON mapping with a few project-specific decisions:

| Rule | Value |
| --- | --- |
| Field naming | `snake_case` |
| `int64` | Returned as a **string** |
| `optional` | Missing means unknown; it is never defaulted |
| Query time condition | Interval **overlap** |
| Tags | Combined with **AND** |
| `modalities` | Combined with **OR** |
| `keyword` | PostgreSQL literal substring search, **no relevance ranking** |
| `semantic` | Real vector search against the index search face |

The distinction between `keyword` and `semantic` matters: a keyword query has no notion of "best match", so
its ordering must not be presented as relevance.

## Media contract highlights

- `MediaSourceRef` carries a secret name plus a content digest; local files use a `sha256:` digest, live
  streams have an empty digest. `stream_id`/`source_id` derive from the digest, making file replay
  idempotent.
- Anchors are presentation-order intervals without media bytes; decode-order PTS is reordered and counted in
  `out_of_order_items`.
- Every dropped sampling point carries a reason code. No clamping.
- `ReplayReport.golden_path_verified` stays `false` unless a real authorised sample has passed full
  acceptance, and reports never contain media paths.

## Decoded data plane contract

- All-zero means "no decode happened": with an empty `arena_id`, `tracks`, `evidence_descriptors` and a
  non-zero `descriptors_built` must not be present.
- `first_pts_ms` / `last_end_ms` use `-1` for "not observed" — never `0`.
- `timeline_offset_ms` aligns descriptors with `ffprobe` anchors on one presentation timeline.
- `overlapping_samples` counts real payloads that repeat the previous segment; they are kept, not dropped
  silently.

## Capability contract

`RuntimeService.DescribeCapabilities` (ADR-008) is normative: platform identity, mandatory
`unavailable_reason`, unknown-stays-unknown, `admitted_memory_kinds`, and the separation of `backends` from
`host_accelerators`. `Health.unavailable_capabilities` and `DescribeCapabilities.unavailable_capabilities`
must have a single source — a mismatch between them is a contract defect, not a cosmetic bug.

See [Capability model](/concepts/capability-model) for the semantics.

## Tests

```sh
make test-contracts    # pytest tests/contracts — no external services required
```

Contract tests need no database, so they are the cheapest gate in the project and the one place where a
field-level rule can be pinned without any infrastructure.

## Read next

- [Services & nodes](/architecture/services)
- [Timeline semantics](/concepts/timeline)
