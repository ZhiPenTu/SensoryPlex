# Contributing & translations

## Before you write code

Read, in this order: the two requirement documents in the repository root, `docs/implementation-status.md`,
and the ADR that covers the area you are about to touch. A change that contradicts an ADR should either come
with an ADR amendment or be reconsidered — the ADRs are load-bearing, not historical notes.

## Where code runs

| Layer | Rule |
| --- | --- |
| Control plane (`api`, `gateway`, `console`, `postgres`, `nats`, `relay`, `index`) | Build, lint, migrate and validate **inside containers** via `docker compose exec -T <service> ...` |
| Host exceptions | `cargo`/Rust builds, `make configure`, `launchd`, model plugins, GStreamer and HF weights, MediaMTX, authorised media |

The point of the container rule is to remove "works on my machine but not in production" drift. Do not add a
host dependency to a control-plane path, and do not add a container step to a path that needs a host
accelerator.

## Non-negotiable rules

1. **`proto/` is the only cross-language contract source.** Change a contract, run `make proto`, commit the
   regenerated artefacts. Never hand-edit generated code.
2. **Field numbers are permanent.** Removal requires `reserved`; a semantic break needs a new protocol major.
3. **Migrations are append-only**, applied explicitly through `tools/migrate.py`. Never edit a historical
   revision to "roll back" — add a new migration.
4. **No synthetic business data.** No fabricated model output, chart series or minute values. Empty results
   are returned as empty.
5. **Unknown stays unknown.** Missing confidence, unknown PTS, unprobeable accelerators and unknown capacities
   are reported as unknown with a reason, never defaulted.
6. **No raw media or secrets outside the data plane.** Not in control messages, not in events, not in logs.
7. **Every queue and concurrency limit is bounded** and every failure has observable semantics.
8. **Comment language:** hand-written Rust (`///`, `//!`, `//`) and Python (docstrings, `#`) comments are
   written in Chinese by default; `proto/` files and generated code stay English. Inside Chinese prose, keep
   API, protocol, container, library and ADR identifiers in their original English form.

## Validation expectations

Run the checks that cover what you touched, and be precise about what they prove:

```sh
make check                # lint + python tests + rust fmt/clippy/test
make test-integration     # real PostgreSQL (and NATS where relevant)
make orchestration-p1-check
make event-pipeline-check
```

Media end-to-end work must use a real authorised sample. A skipped test, an unexecuted CI job or a healthy
container never becomes evidence. If you cannot run a check, say so in the change description rather than
leaving it implied.

## Continuous integration

GitHub Actions is **manual-only** by design: the workflows do not consume hosted runner minutes on every push
or pull request. Daily gates run locally. When you do dispatch a run, the expensive macOS job only starts if
`run_apple_silicon` is checked — and an unexecuted remote macOS job must never be described as passing.

The published documentation site is produced by the same rule: `docs-pages.yml` publishes `apps/docs` to
GitHub Pages only when dispatched, so the live site is as current as its last run rather than as current as
`master`. Its build inputs and local reproduction command are on [Deployment](/operations/deployment).

## Contributing to this documentation

The site lives in `apps/docs` and is built with VitePress. Structure:

```text
apps/docs/
├── .vitepress/config.mts     # locales, nav, sidebars, edit links
├── public/                   # favicon.svg / logo.svg, copied verbatim into the artifact
├── scripts/check-docs.mjs    # locale parity + built-link validation (no dependencies)
├── <page>.md                 # English pages (served at /)
└── zh/<page>.md              # Chinese pages (served at /zh/)
```

Site assets live in `public/`, **not** `.vitepress/public/`: VitePress resolves its public directory as
`srcDir/public`. A file placed in the wrong one is silently absent from the artifact, which is why
`make docs-check` validates every built link and asset.

### Adding or editing a page

1. Create the page in the English tree (`apps/docs/<section>/<slug>.md`).
2. Create the matching Chinese page at the same path under `zh/` — **both locales must have the page**, and
   VitePress does not fall back across languages, so a missing file is a 404 rather than untranslated text.
3. Add the entry to **both** sidebars in `.vitepress/config.mts` (`EN_SIDEBAR` and `ZH_SIDEBAR`) with the same
   path shape (`/section/slug` and `/zh/section/slug`).
4. Run `make docs-check` — it builds the site and validates internal links and assets. Run
   `make docs-install` once first if the container has no `node_modules` yet; it is the only docs target
   that needs the npm registry, and every other target fails with an explicit message if it was skipped.

To read the site while you write, run `make docs-dev` (VitePress dev server with hot reload on
`http://127.0.0.1:5175`); the served site at `http://127.0.0.1:5174` is the built artifact.

### Adding a new language

1. Create the locale directory (for example `ja/`) and mirror the page tree.
2. Add a `locales.<code>` entry in `.vitepress/config.mts` with `label`, `lang`, `title`, `description`, its
   own `nav`, its own `sidebar`, and an `editLink.pattern` prefixed with the locale directory
   (`apps/docs/ja/:path`).
3. Keep the navigation structure identical across locales so that a reader can switch language on any page
   and stay on the same topic.
4. **Status claims must be re-stated, not re-translated.** The three labels (verified / unverified / not
   implemented) and the evidence commands must survive translation exactly. Do not soften "unverified" into
   something more optimistic, and do not add a capability claim that the English page does not make.
5. Keep commands, file paths, identifiers and error codes verbatim; translate only the prose around them.

### Documentation style

- Lead with what a reader can do, then state the boundary.
- Every capability statement should be accompanied by its evidence command or an explicit "not verified".
- Prefer a runnable command over a description of a command.
- Link to the ADR or the source file rather than paraphrasing a contract in a way that can drift.
