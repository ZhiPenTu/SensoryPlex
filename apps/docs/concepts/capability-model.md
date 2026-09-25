# Capability model

Most media frameworks fail silently: a missing model produces zero results, and zero results look like a
successful empty query. SensoryPlex treats every capability question as a **fact to report**, not a default
to fall back on.

## Two different questions

The runtime answers two questions that must never be merged:

| Field | Question it answers |
| --- | --- |
| `backends` | Can **this process** execute inference with this backend? |
| `host_accelerators` | Does **this host** physically have this accelerator? |

A Rust process today has no in-process `ExecutionBackend`, so `model_inference` is permanently listed in
`unavailable_capabilities` — while the host may still report a perfectly available Metal or CUDA
accelerator. Reporting "no accelerator" because "no execution backend" would be a lie in the other
direction.

## Platform identity

Capability reports carry a platform identifier in `<os>-<arch>` form, for example `macos-aarch64` or
`linux-x86_64`. Any performance or sampling conclusion must carry the same identifier, and results from
different platforms are never merged into one statistic.

## Unknown stays unknown

`runtime_version`, `precisions`, `max_concurrency` and host memory all use empty/zero when a probe failed.
Filling them with a guess or a "reasonable default capacity" is forbidden — that is how a fabricated
capacity ends up in a capacity plan.

## Accelerators are tri-state

| State | Meaning |
| --- | --- |
| `ACCELERATOR_STATE_AVAILABLE` | The host has it, and no reason may be attached |
| `ACCELERATOR_STATE_UNAVAILABLE` | The host does not have it, and a reason is mandatory |
| `ACCELERATOR_STATE_UNKNOWN` | The probe could not tell — missing tool, timeout, unreadable output |

`UNKNOWN` must never collapse into `UNAVAILABLE`. A missing `nvidia-smi` is not evidence that a machine has
no GPU; it is evidence that we could not find out. `UNKNOWN` entries carry a `probe_*:<source>` reason, and
`evidence` contains real read values only — never filesystem paths.

## Memory kinds are admission inputs

`admitted_memory_kinds` is the set of memory kinds a platform is allowed to admit, and media admission may
not exceed it. Apple Silicon additionally admits zero-copy `unified_memory`; other platforms get
`cpu_shared_memory` only. Plugins declare what they accept, and a plugin must not declare a kind it cannot
actually map — GPU and unified-memory mapping are not implemented, so processors declare
`cpu_shared_memory` (or nothing at all, if they consume upstream observations instead of bytes).

## Failure is typed by where it happened

| Signal | Meaning |
| --- | --- |
| `501` | The capability is not implemented in this build |
| `503` | The capability exists but the dependency is not configured, not reachable, or failed |
| `502` | The search face answered, but the answer did not satisfy the contract |

`retryable` is an independent marker, not a synonym for the status code: a `503` caused by a wrong token is
a configuration error and is *not* retryable. Callers must read both.

## The one flag that must not drift

`ReplayReport.golden_path_verified` defaults to — and currently remains — `false`. It may only be set to
true when a real authorised sample has completed the full acceptance path. Unimplemented capabilities must
appear in `blockers`, and the report never contains media filesystem paths.

## How to verify these claims

```sh
make capability-check      # unavailable reasons + host accelerator tri-state
make accelerator-check     # four-way reconciliation of the accelerator probe
make gateway-smoke         # capability reporting through the gateway
```

## Read next

- [Contracts & codegen](/architecture/contracts) — where these fields are defined.
- [Capability status](/reference/status) — the current, verified state of each area.

