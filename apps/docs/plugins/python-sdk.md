# Python SDK

The Python SDK lives in `plugins/python/common/` and publishes `edge_material_sdk`. It is part of the same uv
workspace as the gateway, and **plugins may depend on the SDK only** — never on `services/` internals.

Generated messages come from `edge_material_sdk.generated`. Never hand-write an RPC data model from this
documentation: the real `.proto` files are the only contract source.

## What the SDK does and does not do

It is a **building block**. It gives you the plugin base class, the buffer lease reader and the generated
messages.

It does **not** start a gRPC worker or load a model. The plugin is responsible for its own lifecycle service,
durable idempotency and lease reclamation. A working reference for the worker side is
`plugins/python/processors/vlm-moondream` (see ADR-012).

## The two methods that matter

Implement `describe()` and the async `process(request, cancel_token)`. Your entry point runs through
`invoke()`, which enforces the deadline, request context, input/output contract, cancellation and concurrency
admission before your code is called.

```python
from edge_material_sdk import Observation, ProcessorPlugin, ProcessResponse


class SubtitleNormalizer(ProcessorPlugin):
    def describe(self):
        return {
            "consumes": ["observation.asr_segment"],
            "produces": ["observation.asr_normalized"],
            "supports": {"cancellation": True, "retry": "idempotent"},
        }

    async def process(self, request, cancel_token):
        results = []
        for item in request.inputs:
            cancel_token.raise_if_cancelled()
            results.append(
                Observation.from_input(
                    item,
                    modality="asr_normalized",
                    payload={"text": normalize(item.payload["text"])},
                    confidence=item.confidence,
                    provenance=self.provenance(request),
                )
            )
        return ProcessResponse(observations=results)
```

Structural example only — the exact imports and generated classes follow the SDK release. What matters is
what the example demonstrates:

- **results inherit the input's time anchor and provenance** rather than inventing new ones;
- **cancellation is checked**, so a cancelled request stops rather than completing quietly;
- **output carries provenance**;
- the same `idempotency_key` never yields mutually contradictory results.

Return values are `runtime.v1.ProcessResponse`; failures use the standard error codes. **An empty success
response is rejected** — "I found nothing" must be expressed as a real result or a typed failure, not as a
silent no-op.

## Reading buffers from the data plane

A plugin may only access `cpu_shared_memory`, and it **must** go through `LeaseBufferReader` — a
`BufferDescriptor` is an address, not data.

```python
from edge_material_sdk import LeaseBufferReader, ProcessorPlugin


class MyPlugin(ProcessorPlugin):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)  # buffer_reader is attached at gRPC Start, from config


# in the gRPC service's Start handler (the data-plane address comes from Start(config):
# handoff_endpoint):
plugin.buffer_reader = LeaseBufferReader(config["handoff_endpoint"], ttl_ms=ttl_ms)
```

The reader takes the real path — gRPC `Acquire` → `shm_open` + `mmap` → digest verification → `Release` — and
in `process()` every entry you do not consume must be explicitly `discard()`-ed. The data plane requires that
**every retained entry reaches a destination**.

Failure modes are explicit, never silent:

| Situation | Result |
| --- | --- |
| No reader attached and a buffer input arrives | `buffer_reader_not_attached` |
| Descriptor memory kind is not `cpu_shared_memory` | `unsupported_memory_kind:*` |
| Unknown audio sample layout | Explicit rejection |
| Sub-segment outside its window | Marked, not clamped |

Audio bytes are interpreted strictly according to the descriptor's `sample_format`. There is no default sample
width and no "assume 4 bytes per sample" fallback — see ADR-014.

::: warning Do not declare memory kinds you cannot map
GPU and unified-memory mapping are **not implemented**. A processor that consumes bytes declares
`cpu_shared_memory`; a processor that consumes upstream facts declares nothing and never gets a data-plane
lease attached.
:::

## Concurrency and admission

`tools/ai_worker.py` performs admission and in-flight limiting using `--model-parallelism` /
`SENSORYPLEX_MODEL_PARALLELISM` against the tier cap reported by the runtime. Bad values, conflicting sources
and over-cap settings exit with code `2` **before connecting to the plugin** — nothing is clamped. Retryable
rejections such as `concurrency_limit` and `deadline_expired` do not make the input vanish: the report records
the measured `peak_in_flight` and the retry ledger (ADR-021).

## Verifying a plugin

```sh
make model-check MEDIA=/absolute/authorized-sample.mp4     # VLM (local ollama)
make asr-check   MEDIA=/absolute/authorized-speech.webm    # ASR (MLX Whisper)
make ocr-check   MEDIA=/absolute/authorized-video.webm     # OCR (bundled PP-OCR ONNX)
make embed-check MEDIA=/absolute/authorized-video.webm     # BGE text embedding
make parallelism-check MEDIA="/abs/a.webm /abs/b.webm" INPUTS=4
```

Two flags are worth knowing:

- `make ocr-check EXPECT=empty` — on a sample with no text, "0 blocks plus an `empty_reason`" is the **correct**
  result. This is how the project proves that "the model found nothing" and "processing failed" are
  distinguishable;
- `PROVIDER=coreml` — routes through the CoreML execution backend. If a CoreML session cannot be obtained it
  fails explicitly and **never silently falls back to CPU** (ADR-016).

Each of these checks runs four processes (orchestration / producer runtime / plugin / worker) and requires a
real authorised sample.

