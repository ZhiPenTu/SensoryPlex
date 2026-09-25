# Model overview

The data model is small on purpose. Everything the framework stores is either a **stream**, an immutable
**material revision**, an **observation** about a time range of a stream, or the **lineage** that ties an
observation to the thing that produced it.

## Objects

| Object | Meaning | Immutability |
| --- | --- | --- |
| `MediaSourceRef` | Where media came from: a secret name plus a content digest. Local files carry a `sha256:` digest, live streams carry an empty digest. | Immutable |
| `stream_id` / `source_id` | Derived from the file digest, so replaying the same file stays idempotent | Derived |
| `TimestampRange` | `[start_ms, end_ms)` on one stream | Value |
| `TimelineAnchor` | A presentation-order interval on one stream; `pts_ms` equals the interval start; carries no media bytes | Value |
| `MaterialUnit` + `revision` | An addressable slice of a stream with its facts | Append-only; revisions never mutated |
| `Observation` | A fact about a range of a stream (text block, transcript, description, embedding) | Append-only |
| `source.owner` | The principal that owns the source; queries are filtered by it | Immutable |
| `model_release_id` / `processor_release_id` | Which model or processor version produced an observation, including a config hash | Immutable |
| `BufferDescriptor` + lease | A bounded shared-memory reference used inside the data plane | Bounded by TTL |

## Revision rules

- Revisions start at `1` and increase contiguously.
- **The same revision with the same content is replayable.** Writing it again is idempotent, not an error.
- **The same revision with different content is rejected.** Silently overwriting a revision would destroy
  the audit trail, so it is an explicit error instead.
- Reading an older revision marks it `superseded` **dynamically at query time**; the stored snapshot itself
  is never rewritten.

## Lineage rules

An observation is only accepted when the source and producing identity validate:

- the referenced material/stream must exist and be owned by the caller's principal;
- the producing model/processor release must be known, with its configuration hash folded into the release
  identity, so a changed prompt, language or sampling threshold produces a different identity;
- query results re-check the `ready` state, the material and `source.owner` in PostgreSQL before returning a
  vector hit — a stale index entry cannot leak a result.

## Events carry references, not media

The transactional outbox stores only an `EventEnvelope`. Frames, PCM and tensors never enter events,
control messages or logs. That is why the event chain can be replayed and deduplicated safely: the payload
is data, not bytes.

## Confidence is either known or explicitly unknown

`confidence` is optional. When it is absent, the reason for the omission must be stated. The framework does
not use `0` or `1` to stand in for "no opinion", because that would turn an unknown into a false claim.

## Read next

- [Timeline semantics](/concepts/timeline) — the time rules in detail.
- [Capability model](/concepts/capability-model) — how the framework says "I cannot do this".
- [Contracts & codegen](/architecture/contracts) — the field-level rules.

