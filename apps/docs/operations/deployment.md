# Deployment

Everything is driven through the scripts in `deploy/`. They always run from the repository root and pin the
same `--env-file .env -f deploy/compose/docker-compose.poc.yml`, so a missing `--env-file` or a wrong `-f`
path cannot happen by accident. **Do not call `docker compose up` directly.**

## Scripts

| Script | Effect |
| --- | --- |
| `./deploy/up.sh [services...]` | `docker compose up -d --build --wait`; fails if any healthcheck does not pass |
| `./deploy/down.sh [--volumes]` | Stops and removes containers; keeps named volumes unless `--volumes` is given |
| `./deploy/status.sh` | Container status plus independent HTTP probes of console/api/gateway/docs |
| `./deploy/logs.sh [service]` | Follows logs (`--tail 200`), all services by default |
| `./deploy/up-events.sh` | Starts the `events` profile (`relay`, `index`) after checking BGE weights |
| `./deploy/down-events.sh [--volumes]` | Stops only `relay` and `index`; `--volumes` also removes `.data/index` |

Equivalent Make targets: `make up`, `make down`, `make infra`, `make events-up`, `make events-down`,
`make events-logs`.

## Services and ports

| Service | Host binding | Purpose |
| --- | --- | --- |
| `console` | `127.0.0.1:5173` | Web console, nginx static hosting + reverse proxy |
| `api` | `127.0.0.1:8091` | Business/admin/identity API |
| `gateway` | `127.0.0.1:8090` | Compatibility query entry |
| `docs` | `127.0.0.1:5174` | This documentation site (static) |
| `postgres` | `127.0.0.1:25432` | Metadata store |
| `nats` | `127.0.0.1:24222` (monitor `28222`) | Event bus |
| `relay`, `index` | compose network only | Resident event chain; the search face is never exposed to the host |

Every port is loopback-bound by default. Ports come from `.env`; see
[Configuration](/operations/configuration).

## Data and volumes

| Storage | Contents | Removed by |
| --- | --- | --- |
| `postgres-data` (named volume) | Metadata, facts, outbox, migrations history | `./deploy/down.sh --volumes` |
| `nats-data` (named volume) | JetStream stream state | `./deploy/down.sh --volumes` |
| `.data/` (repository directory) | Model weights, Milvus Lite store, generated demo password | Manual removal, or `./deploy/down-events.sh --volumes` for `.data/index` |

The repository root is bind-mounted into `api`, `gateway` and `console` so that container-based validation can
see the host working tree. Authorised media is mounted separately and **read-only** via `MEDIA_DIR`.

## Deployment profiles

**Control plane only** — `./deploy/up.sh`. This is the default and is enough for the console, the API and
metadata work.

**With the event chain** — `./deploy/up-events.sh` as well. It requires
`.data/models/bge-small-zh-v1.5/onnx/model_quantized.onnx`; a missing weight file fails fast rather than
downloading a model behind your back.

**With a host worker** — `make task-worker-daemon`, which keeps the co-located `local-host` node online and
executes dispatched work with GStreamer + OCR + timeline fusion.

**Media stream (optional, separate)** — `make stream-up` / `make stream-down` run MediaMTX independently;
this does not affect the console stack.

**macOS resident shape** — `deploy/macos/launchd/` renders `launchd` plists and `deploy/macos/bin/` wraps the
resident runtime. `make resident-probe` (read-only), `resident-install`, `resident-status`,
`resident-uninstall`. `launchctl`/`sysctl` exist only on macOS, so this group is a documented host exception.

## Accepting a deployment

A deployment is accepted when an **action** works, not when the status endpoints answer. Concretely:

1. `./deploy/status.sh` reports `OK` for every probe;
2. a real login succeeds and a write action completes (create a task, publish a plan revision, or import a
   video);
3. the capability report matches what you claim: `make capability-check`;
4. if you claim the media loop, `make golden-path-check` passes on a real authorised sample.

A healthy container, a `200` from `/v1/health` and a registered node are infrastructure readiness — they are
not product acceptance. A browser that cannot get past login is not visual acceptance either.

## Rollback

- Images are pinned by digest. Rebuild forward with `./deploy/up.sh`; there is no in-place edit of a running
  image.
- Migrations are append-only. Rolling back means **writing a new migration**, never rewriting a historical
  revision.
- `./deploy/down.sh --volumes` is destructive: it removes the database and the JetStream state. Confirm you
  have a dump before running it.
- After replacing a service, restart `console`: its nginx resolves the `api` upstream at startup and can keep
  an obsolete container IP. See [Troubleshooting](/operations/troubleshooting).

## Hosting the documentation

The docs site is a plain VitePress build, so the artifact can be served by any static server:

```sh
make docs-install   # one-off npm ci inside the docs container (the only step that needs the registry)
make docs-build     # write the artifact to apps/docs/.vitepress/dist
make docs-serve     # preview the built artifact
```

Inside compose it is served by the `docs` service on `127.0.0.1:5174`, from the site built into the image.
`make docs-build` also copies a root-path artifact into the running container, so rebuilding the image
(`./deploy/up.sh docs`) is only needed after changing the site constants or the nginx config.

`DOCS_BASE` and `DOCS_SITE_URL` are **build-time** values, passed on the command line:

```sh
make docs-build DOCS_BASE=/sensoryplex/                  # artifact for sub-path hosting
make docs-build DOCS_SITE_URL=https://docs.example.com    # artifact with sitemap + absolute URLs
```

They change the artifact only. The local service always serves from the root of `127.0.0.1:5174`, so a
sub-path artifact is written to `apps/docs/.vitepress/dist` and **not** copied into the container.

`make docs-check` is the gate to run before committing documentation changes: it builds the site and
validates locale parity plus every internal link and asset.

### GitHub Pages

The site is published as a project page at <https://jaytu211.github.io/SensoryPlex/>, by the
**manually dispatched** workflow `.github/workflows/docs-pages.yml`:

```sh
gh workflow run docs-pages.yml --ref master      # or: Actions → docs-pages → Run workflow
```

It is manual on purpose: this repository does not consume hosted runner minutes on push or pull
request (see [Contributing](/project/contributing)). Pages is enabled on the repository with source
`GitHub Actions`, so a publish is a successful run of that workflow — nothing is pushed to a branch.

The workflow owns the two build inputs, and they must stay in sync with this page:

```yaml
DOCS_BASE: /SensoryPlex/                   # a project page is served from a sub-path
DOCS_SITE_URL: https://jaytu211.github.io   # origin only — the sub-path is separate
```

`DOCS_SITE_URL` must **not** include the sub-path. VitePress hands relative page paths to the sitemap
package, which resolves them as `new URL(path, hostname)`: a leading `/` discards any path inside
`hostname`, so `https://jaytu211.github.io/SensoryPlex` would silently emit sitemap entries pointing at
`https://jaytu211.github.io/...`. `sitemap.transformItems` in `.vitepress/config.mts` re-adds `base`,
and `scripts/check-docs.mjs` fails the build when any sitemap URL falls outside it.

The workflow builds the artifact, runs the same `check-docs.mjs` gate, and only then deploys — a
failed validation keeps the previously published version live instead of shipping inconsistent links.
To reproduce the exact published artifact locally:

```sh
make docs-check DOCS_BASE=/SensoryPlex/ DOCS_SITE_URL=https://jaytu211.github.io
```
