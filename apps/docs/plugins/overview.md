# Plugin system

Plugins are how the framework gains capability, and the design assumption is that a plugin is **untrusted
third-party code running on someone's edge hardware**. Openness therefore stops well before "load arbitrary
code".

## What a plugin may extend

| `kind` | Extends | Typical use |
| --- | --- | --- |
| `SourcePlugin` | Ingest | A new container format, capture device or streaming protocol |
| `ProcessorPlugin` | Perception | OCR, ASR, VLM description, embedding, normalisation |
| `SinkPlugin` | Export | A new downstream store or notification target |
| `EnricherPlugin` | Fact enrichment | Deriving additional observations from existing facts |
| `ExecutionBackendPlugin` | Hardware execution | A vendor runtime (TensorRT, vendor NPU SDK) used by other plugins |

## What a plugin may never change

- **Time semantics.** The timeline is `[start_ms, end_ms)` on one stream. A plugin cannot invent its own
  offsets or clamp an unknown value into a legal interval.
- **Provenance.** Every observation must carry the producing release identity and the config hash that
  affects its output semantics.
- **Confidence semantics.** Unknown confidence is stated as unknown, with a reason — never `0` or `1` as a
  stand-in.
- **The data boundary.** No raw buffers, PCM or tensors to the control plane, and no secrets in manifests,
  image layers, logs, observations or events.
- **Resource declarations.** They are admission and isolation inputs, not best-effort hints.

## Lifecycle

```text
Discover → Validate → Register → Start → Ready
                                   │
                         Process / Cancel / Health
                                   │
                    Drain → Stop → Unregister
```

| Stage | Runtime expectation | Plugin guarantee |
| --- | --- | --- |
| `Describe` | Read capabilities, protocol and config schema | No model loading, no side effects, fast return |
| `ValidateConfig` | Validate config and dependencies before deployment | Field-level errors, no secret leakage |
| `Start` | Create the instance and controlled resources | Bounded initialisation; no traffic before ready |
| `Health` | Periodic liveness/serviceability probe | Distinguish `healthy`, `degraded`, `unhealthy` |
| `Process` | Deliver one or a batch of inputs with a deadline | Idempotent, cancellable, traceable results |
| `Cancel` | Cancel an unfinished request | Stop promptly; never write back a success |
| `Drain` | Stop new work, wait for in-flight work | Finish or explicitly abort within the grace period |
| `Stop` | Reclaim models, handles and temporary resources | Release resources without losing confirmed results |

## Error codes

Errors are typed. A plugin that catches an exception and returns an empty success response is a defect, not a
resilience feature.

| Code | Meaning | Default runtime handling |
| --- | --- | --- |
| `INVALID_INPUT` | Input does not satisfy the declared format/schema | Mark failed, no retry |
| `UNSUPPORTED_CAPABILITY` | Modality, language, model or config not supported | Re-route or mark unsupported |
| `UNSUPPORTED_MEMORY_KIND` | Cannot read this buffer type | Copy/convert or re-route |
| `DEADLINE_EXCEEDED` | Did not finish before the deadline | Cancel, then retry/degrade per policy |
| `RESOURCE_EXHAUSTED` | GPU, memory or queue pressure | Backpressure, queue or re-route |
| `TRANSIENT_BACKEND_FAILURE` | Recoverable model/device failure | Bounded retry |
| `DATA_POLICY_DENIED` | Privacy, residency or egress policy forbids it | No retry; audit and alert |
| `INTERNAL_PLUGIN_ERROR` | Internal error | Isolate the instance, log diagnostics, restart per policy |

An error response carries a safe `reason_code` and, when applicable, an actionable `retry_after_ms`. Access
tokens, raw object URLs and internal stack traces never go upstream.

## Delivery semantics

- **At-least-once.** `Process` must return a semantically identical result for the same `idempotency_key`, or
  explicitly return a reference to the earlier result.
- **Output identity is derived from stable input**, never from the current clock or a random number.
- **Ordering is not guaranteed by default.** A model that needs strict per-stream ordering declares
  `ordering: per_stream` and states the throughput and recovery cost that comes with it.
- **Retryable and non-retryable failures must be distinguished.** A temporary VRAM shortage is retryable; an
  input-format violation is not.

## Resources and backpressure

`resources` in the manifest is the scheduler's admission and isolation basis. If a plugin persistently exceeds
its declaration, the runtime may reduce concurrency, pause, migrate or terminate the instance.

An unbounded internal queue is treated as a defect: a plugin must report queue depth, in-flight, rejections
and deadline misses. Backpressure is applied in order:

```text
queue level rises
  → reduce optional sampling / merge batches
  → pause slow-path enrichment
  → lower non-critical task priority
  → return RESOURCE_EXHAUSTED / runtime.backpressure
  → deliberately drop reconstructable, low-value work
```

Every drop has a counter and a reason code. Live ingest and already-confirmed fact writes outrank expensive
slow-path inference.

## Performance claims

A throughput number is only meaningful with context: hardware and driver, model version, quantisation, input
size, stream count, concurrency, batch size, data-plane memory kind, P50/P95/P99, warm-up state and failure
rate. Without those, "N frames per second" is not comparable to anything.

## Forms and signing

- `artifacts.form` is either `container` or `local_native` (a host-resident process such as a `launchd`
  service on macOS). The four shipped processors are currently `local_native`.
- **In v1, plugins are not signed.** A missing signature must carry an explicit
  `signatureUnavailableReason`; a placeholder digest or placeholder signature string is never used to
  impersonate one.
- Signature/SBOM handling is a structural precheck today; sandbox and egress-policy **enforcement** are not
  implemented.

## Read next

- [Packaging & manifest](/plugins/package-and-manifest)
- [Python SDK](/plugins/python-sdk)

