# Independent Python plugins (v2)

**Verified scope:** Python 3.12, macOS Apple Silicon, `local_native`, same-host file media, video
frames and upstream Observations, synchronous graphs and asynchronous enrichment. The audio input
adapter has been exercised on a real soundtrack. A third-party audio algorithm and Linux execution
remain unaccepted. Shared decoding, cross-host raw media, container plugins, Source and Sink are outside
this release.

The platform discovers external processors from verified registrations. Adding a processor does not
require changing a built-in plugin list, model switch, result parser or Console component. Existing v1
packages and published revisions retain their original identities.

## Create and release outside the platform repository

Install the delivered SDK wheel into a Python 3.12 virtual environment. This release is distributed as
a wheel, not as a claimed public package-registry release. Each plugin owns its `pyproject.toml` and
`uv.lock`. Install `uv` in the developer environment for dependency locking and bundle construction.

```sh
python3.12 -m venv .venv
.venv/bin/pip install edge_material_sdk-0.1.4-py3-none-any.whl
.venv/bin/sensoryplex-plugin create my-plugin --id com.example.my-plugin
cd my-plugin
uv add ../edge_material_sdk-0.1.4-py3-none-any.whl
uv lock
cd ..
.venv/bin/sensoryplex-plugin validate my-plugin
.venv/bin/sensoryplex-plugin compat my-plugin
.venv/bin/sensoryplex-plugin build my-plugin --out releases/my-plugin --sdk-wheel edge_material_sdk-0.1.4-py3-none-any.whl
.venv/bin/sensoryplex-plugin keygen --private-key publisher.private.pem --public-key publisher.public.pem
.venv/bin/sensoryplex-plugin sign releases/my-plugin --private-key publisher.private.pem
.venv/bin/sensoryplex-plugin verify releases/my-plugin --public-key publisher.public.pem
```

The generated implementation is a starting point that explicitly reports no detection. Replace it with
an algorithm reading real inputs before claiming business acceptance. `build` reads the manifest without
importing plugin code, verifies the SDK wheel against the plugin lock and creates a platform-specific
bundle containing source, schemas, SBOM, lock, SDK and offline wheelhouse. Never replace signed release
bytes in place; publish a new version. Keep private keys out of source control and distribution bundles.

## Manifest, types and results

Use `apiVersion: edge.material.plugin/v2`, `kind: ProcessorPlugin` and a reverse-domain plugin ID.
`entrypoint.module` and `factory` identify the SDK server factory. The factory receives configuration,
registration and artifact identity and returns `ProcessorPlugin`; it never imports platform services.
The independent examples illustrate the complete implementation.

```yaml
spec:
  sdk: { runtime: '>=0.1.1,<0.2', protocol: v1 }
  entrypoint: { transport: grpc, module: custom_plugin.plugin, factory: create_plugin }
  artifacts: { form: local_native }
  config: { schema: config.schema.json }
  modelApplicability: not_applicable
  executionModes: [sync, async_enrichment]
  ordering: ordered
  resources:
    maxConcurrency: 1
    maxBatchSize: 1
    defaultDeadlineMs: 30000
    memoryBytes: 268435456
    cpuMillicores: 1000
  inputs: [{ modality: media.video_frame }]
  outputs:
    - modality: com.example.brightness.measurement
      schema: { id: com.example.brightness.measurement, version: 1.0.0, path: measurement.schema.json }
      textFields: []
```

One input type per node is supported in this first release: `media.video_frame`, `media.audio_segment`
or a declared upstream Observation type. An Observation input also declares its schema. Every custom
output declares a namespace, schema identity/version/path and optional JSON Pointer `textFields`.
Schemas are bundled and digest-locked; remote references are rejected. Input and output limits and
schema validation are enforced by the SDK and platform. Configuration objects, arrays, enums, basic
values, defaults, integer normalization and null follow the locked JSON Schema.

Output uses the common Observation envelope with source, half-open `[start_ms, end_ms)` time range,
content digest and schema identity. Deterministic processors set `model_applicability=NOT_APPLICABLE`
and `processor_release_id`; model fields stay empty. Model processors keep full model provenance.
Return `PROCESS_OUTCOME_NO_OBSERVATIONS` with a machine reason such as `brightness_below_threshold`
after processing without a detection. This counts in receipts and coverage, but creates no fake
Observation. Empty responses without an explicit outcome are errors.

## Trust, install and publish

An administrator registers the Ed25519 public key in **Plugin center → Publisher trust** or through
`POST /admin/v1/plugin-signers`. Import `bundle.tar.gz`, `release.json` and `release.sig` through the
Console import form. The API equivalent is `POST /admin/v1/plugin-releases:import`: raw bundle body,
base64 JSON in `X-Plugin-Descriptor`, signature in `X-Plugin-Signature`, approved signer identity in
`X-Plugin-Signer`, authenticated session and CSRF token. The server rechecks all identities, signatures,
schemas, digests and platform conditions. It never trusts a client-supplied trust flag.

Save a configuration for the specific release. Install it on the same-host approved node through
the existing blue-green deployment workflow. In **Processing plans → Plugin graph**, select release,
configuration, input selector and execution mode for each node. Validate connections, inspect the
read-only graph and publish. API clients use `POST /admin/v1/plugin-graphs` with `nodes` and `edges`,
then the existing pipeline publish and job submit endpoints. `input_selector` is `media` or
`node:<upstream-id>`. This release supports asynchronous leaves after synchronous upstream nodes;
unsupported asynchronous dependency chains are rejected.

Publication fixes the release, artifact, schema, configuration hash and policy. Upgrading a slot does
not change an existing plan's version. Pinned runtimes remain available until the published revision
is explicitly retired and no execution uses it. Retirement preserves all facts. The maximum resident
version count remains bounded. Candidate failures preserve the old active; rollback creates a reverse
deployment and restores that version's configuration. Revocation blocks new imports/deployments and
keeps running instances visible for administrator disposition. Publisher trust is authorization for
native code, not a process sandbox claim.

## Execution, retrieval and evidence

Start the container base and event stack with the deployment scripts, then `./deploy/up-enrichments.sh`.
Run the native Node Agent and `tools/enrichment_worker.py --state-file <agent-state> --nats-url <host-nats>`
on the media node. The Consumer uses authenticated, lease-fenced input APIs and standard `Process`,
without model-specific imports or database credentials. PostgreSQL remains the only task authority;
bounded outbox/JetStream deliveries carry immutable identities and controlled references. Media bytes
and host paths never enter those messages. Cancellation wins over late results; duplicate fusion does
not append facts twice. Failed enrichment records its reason and affected windows while the fast path
remains reviewable.

Declared text fields enter the existing vector-index path. Results without text fields remain queryable
and playable with index status `not_declared`. Material detail shows typed fields, bounded JSON and
provenance. To verify ownership, query using `execution_id` and compare returned material revisions and
Observation IDs. Semantic search currently rejects an execution filter; acceptance reconciles semantic
hits against those exact execution-owned identities rather than treating any hit as proof.

The repository's `docs/verification-plugin-platform-v2.md` records actual artifact identities, executions,
failures and remaining acceptance boundaries. `make plugin-platform-check MEDIA=... RELEASE_VERSION=...`
drives the isolated real-media control workflow after signed bundles and host Workers are ready.
`make plugin-platform-fault-check` checks the signed rejection matrix; crash, timeout, cancellation,
duplicate, retirement and rollback commands are described in `docs/runbooks/plugin-platform-v2.md`.

See [v1 packaging](/plugins/package-and-manifest) and [SDK lifecycle](/plugins/python-sdk) for the retained legacy API.
