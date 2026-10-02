# Packaging & manifest

This page describes the retained v1 format. For signed external releases, use [Independent plugins (v2)](/plugins/independent-v2).

Every plugin ships a `plugin.yaml` at the root of its package. The runtime discovers and pre-checks plugins
**from the manifest only** — it never imports plugin code to read metadata.

## Recommended layout

```text
my-ocr-plugin/
├── plugin.yaml          # required: the only discovery input
├── config.schema.json   # JSON Schema for ordinary configuration
├── README.md
├── pyproject.toml
├── sbom.cdx.json        # SBOM referenced by the manifest
├── src/my_ocr/
│   ├── plugin.py        # ProcessorPlugin implementation
│   ├── config.py
│   ├── adapter.py
│   └── health.py
└── tests/
    ├── contract/
    ├── integration/
    └── fixtures/
```

## Annotated manifest

This is the shape of a real shipped processor (OCR), trimmed to the parts worth explaining:

```yaml
apiVersion: edge.material.plugin/v1   # constant for this protocol major
kind: ProcessorPlugin                 # Source | Processor | Sink | ExecutionBackend | Enricher
metadata:
  name: org.sensoryplex.ocr-rapidocr  # reverse-domain, >= 3 labels, immutable after release
  version: 0.1.0                      # semantic version
  displayName: Screen text recognition (bundled PP-OCR / ONNX Runtime)
  vendor: SensoryPlex
  license: Apache-2.0                 # required
  description: Reads real video frames from the data plane and produces text blocks with frame-pixel
    coordinates and provenance, using bundled PP-OCR ONNX weights.
spec:
  sdk:
    runtime: ">=0.1.0 <0.2.0"         # the SDK range this plugin was built against
    protocol: v1
  entrypoint:
    transport: grpc                   # gRPC is the only v1 transport
    command:                          # the exact command the runtime will start
      - uv
      - run
      - --frozen
      - python
      - -m
      - edge_material_plugin_ocr_rapidocr
    port: 50073
  capabilities:
    consumes: [media.video_frame]     # validated as graph edges, not as call order
    produces: [observation.ocr_blocks]
    acceptsMemoryKinds: [cpu_shared_memory]   # do not declare a kind you cannot map
    ordering: per_stream
    supports:
      batch: false
      cancellation: true              # must be true in v1
      retry: idempotent               # must be "idempotent" in v1
  resources:                          # admission and isolation input, not advice
    cpu: "2"
    memory: 3Gi                       # measured resident memory plus headroom
    maxConcurrency: 1
    maxBatchSize: 4
    defaultDeadlineMs: 120000
    # gpu: { required: false, count: 0 }
  config:
    schema: config.schema.json
    secretRefs: []                    # secret names only; never values
  security:
    network: allowlist                # none | allowlist
    allowedHosts: [www.modelscope.cn] # required when network: allowlist
    dataEgress: local_only            # the only permitted value in v1
    filesystem:
      readOnly: true                  # must be true in v1
      writablePaths: []
  artifacts:
    form: local_native                # container | local_native
    image: local:plugins/python/processors/ocr-rapidocr
    digest: sha256:008baa66a2e8baa9f8b7d4a410c94f2a17f1195680600a70546dcaf73b9736fc
    sbom: sbom.cdx.json
    signatureUnavailableReason: local_native_plugin_is_not_signed_in_v1
```

## Field rules that are enforced

| Area | Rule |
| --- | --- |
| `apiVersion` | Constant `edge.material.plugin/v1` |
| `metadata.name` | Reverse domain with at least three labels; immutable after release |
| `metadata.version` | Semantic version: patch for compatible fixes, minor for compatible capability, major for breaking SDK or result semantics |
| `spec.entrypoint.transport` | `grpc` only in v1 |
| `spec.capabilities.supports` | `cancellation: true` and `retry: idempotent` are constants |
| `spec.capabilities.acceptsMemoryKinds` | From the fixed set `cpu_shared_memory`, `file_object_ref`, `cuda_ipc`, `dma_buf` |
| `spec.resources` | `cpu`, `memory`, `maxConcurrency`, `maxBatchSize`, `defaultDeadlineMs` are all required; `gpu` is optional |
| `spec.resources.cpu` | Numeric string, and not zero |
| `spec.resources.memory` | `Mi` or `Gi`, minimum `1Mi` |
| `spec.security.network` | `none` or `allowlist`; `allowedHosts` becomes required — and non-empty — for `allowlist` |
| `spec.security.dataEgress` | `local_only` only |
| `spec.security.filesystem.readOnly` | Always `true` |
| `spec.artifacts.digest` | Must match `sha256:` plus 64 hex characters |
| `spec.artifacts` for `container` | `image` must not end in `:latest`; a `signature` is required |
| `spec.artifacts` for `local_native` | `image` must start with `local:`; `signatureUnavailableReason` is required instead of a signature |

## Immutability and identity

- All published artifacts must lock a content digest. Releasing with only `latest` or a mutable tag is
  rejected.
- The runtime unique key for a plugin is `(name, version, artifact_digest)` — **not** the display name.
- `capabilities`, resources, network allowlists and data-egress declarations are policy inputs. If the
  running image behaves differently from its declaration, that is a security issue, not a documentation
  gap.

## Configuration and secrets

- Ordinary configuration is injected as JSON validated against `config.schema.json`, and its hash is folded
  into the release identity.
- Secrets arrive **only** via `secretRef`, into a temporary file, environment variable or host secret
  mechanism. They must never appear in the manifest, an image layer, a log, an observation or an event.
- Anything that changes output semantics — prompt, language, sampling threshold, preprocessing version —
  must be part of the `model_release_id` / `processor_release_id` config hash.

## Validation

```sh
uv run python tools/validate_plugin.py /path/to/plugin.yaml
make plugin-artifact          # SDK and VLM artifacts (ASR: structural validation only)
make plugin-artifact-check
```

::: warning What the validator does not do
`tools/validate_plugin.py` is a **structural precheck**. It does not perform SDK version negotiation, image
signature or SBOM verification, sandboxing, or egress-policy enforcement. Remote and native ABI forms are
future work. Do not describe a passing manifest check as a certified or signed plugin.
:::

## Release checklist

- [ ] `plugin.yaml` passes schema validation; name, version, SDK range and capabilities are complete.
- [ ] Artifacts lock a digest and carry an SBOM, licence and changelog.
- [ ] No secret, stream URL, internal path or real user sample is present in code, logs or image layers.
- [ ] `Process` handles deadline, cancellation, duplicate delivery, transient failure and resource pressure.
- [ ] Inputs and outputs carry correct stream time anchors, confidence, release identity and config hash.
- [ ] No raw buffer is sent to the control plane; no data-plane handle is used outside its lease.
- [ ] Every failure maps to a standard error code; there is no "catch and return empty success" path.
- [ ] Replay on a real sample, a performance baseline, a security scan and a least-privilege deployment have
      all been executed.
- [ ] The README states capability boundaries, known limitations, resource needs, egress behaviour and the
      upgrade/rollback procedure.
