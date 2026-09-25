# Services & nodes

## Processes

| Service | Port | Stack | Responsibility | Health |
| --- | --- | --- | --- | --- |
| `console` | `5173` | nginx + React/Vite | Serves the SPA and reverse-proxies `/v1`, `/auth`, `/admin` to `api:8091`, so the browser stays same-origin | `GET /` |
| `docs` | `5174` | nginx + VitePress | Serves the static framework documentation site (English at `/`, Chinese at `/zh/`); no reverse proxy, no credentials, no `depends_on` | `GET /` |
| `api` | `8091` | FastAPI | Business, admin and identity endpoints (`/v1`, `/auth`, `/admin`), plus `/livez`, `/v1/health`, `/v1/capabilities` | `GET /livez` |
| `gateway` | `8090` | FastAPI | The older query entry kept as a migration-compatibility layer; runs the same smoke checks | `GET /v1/health` |
| `migrate` | — | Python | One-shot `tools/migrate.py`, gated by strict version validation and a transaction lock; exits when done | — |
| `relay` | — | Python | Claims outbox rows and publishes them to NATS JetStream | ready line on stdout |
| `index` | `50077` | Python | Vector write, resident JetStream consumer, gRPC search face | TCP connect on `50077` |
| `postgres` | `25432` | PostgreSQL 16 | Metadata, facts, outbox | `pg_isready` |
| `nats` | `24222` | NATS 2.11 | JetStream event bus; monitoring on `28222` | — |

`docs` is a pure static site: it is part of the default stack but holds no credentials and proxies
nothing, so it never affects the status of the control plane.

`relay` and `index` live in the compose `events` profile and are started explicitly by
`./deploy/up-events.sh`, never by the default `./deploy/up.sh`. The search face port is exposed **only**
inside the compose network; the host never listens on it.

## Gateway vs API

Both are FastAPI processes over the same schema and credentials. `api` is the current surface (business,
management, identity); `gateway` remains the compatibility entry. The capability declarations of the two
are deliberately kept to the same source, because a capability that depends on "whether this deployment
happened to mount the repository `.env`" is a bug, not a feature.

## Node topology (ADR-026)

A main node holds the **sole control-plane authority**. Sub-nodes are worker hosts that may be co-located
with the main node or sit elsewhere in the same LAN.

| Concept | Behaviour |
| --- | --- |
| Node agent | `tools/node_agent.py` runs on the sub-node, registers it and sends authenticated heartbeats |
| Node lifecycle | `candidate` → `enrolling` → `ready`, plus `draining`, `offline`, `revoked` |
| Plugin instance lifecycle | `planned` → `installing` → `ready`, plus `degraded`, `draining`, `stopped`, `failed`, `rolled_back`, `uninstalled` |
| Install location | Chosen from the Web console per node, not implied by the plugin file layout |
| Deployment intent | Immutable, audited, with an explicit rollback action |
| Audit | Every state change is recorded with its actor |

### Preflight

Deployment and dispatch run through hard preflight checks. They cover:

- **Node identity and heartbeat freshness** — a heartbeat older than 60 seconds is stale, and the node is
  refused with `node_heartbeat_stale`;
- **data locality** — a plugin that accepts shared-memory kinds must run on the node that owns the data
  plane, otherwise `data_locality_violation`;
- **artifact form and platform** — an artifact that cannot run in the declared form on the declared platform
  is rejected (`container_runtime_unsupported`, `native_runtime_unsupported`, `unsupported_platform`);
- **accelerator availability** — a requested provider that the host does not have is rejected with
  `accelerator_not_available`;
- **resource budget** — declared CPU and memory must fit the node (`insufficient_cpu`, `insufficient_memory`);
- **artifact digest** — a digest mismatch is refused (`digest_mismatch`).

### Data locality is a hard filter

Shared memory, `dma_buf` and hardware handles are host-local, so:

- a `data_plane_local` task bound to a stream may only be assigned to a co-located node;
- raw `BufferDescriptor` edges that cross hosts are rejected twice — once when the graph is parsed and once
  when it is scheduled;
- remote GPU/edge nodes handle observation and object references only;
- a node that is `draining` or `offline` is refused with `409` for both preflight and task claim. The
  framework never silently degrades or reassigns work to a node that has not been asked.

### Failover is audited

When a node goes unreachable or a lease expires, the recovery loop detects it atomically, reclaims the task
and re-dispatches it to a backup node — keeping **both** assignment records (first and second) in the durable
ledger.

::: warning Not yet production-grade
Cross-host mTLS certificate **rotation** and main-node HA leader election are not implemented. Treat the
current topology as a working single-authority design, not a hardened cluster.
:::

## Verification

```sh
make node-check              # 6 acceptance scenarios across the node topology
make orchestration-p2-check  # 6 distributed orchestration scenarios
```

