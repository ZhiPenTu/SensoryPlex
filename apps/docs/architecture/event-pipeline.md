# Event pipeline & retrieval

The event chain is what turns a written fact into something you can query semantically. It has four hops, and
the project verifies them one hop at a time so that a green light at one hop is never mistaken for a green
light at the next.

```text
fact write (material + observation + outbox, one transaction)
   → services/outbox-relay            publish to NATS JetStream
   → services/index-worker --consume  durable consumer → BGE encode → vector store
   → index search face (gRPC, index:50077)
   → api / gateway                    forward query, hydrate hits from PostgreSQL
```

## Hop 1 — outbox → JetStream (ADR-024)

- The outbox stores **only** an `EventEnvelope`. No frames, PCM, tensors or secrets.
- `published_at` is written **only after** JetStream confirms the publish.
- `Nats-Msg-Id = event_id`, so the stream's duplicate window absorbs re-sends instead of double-counting them.
- Stream drift is **reported, not repaired**: the relay says what it found and does not silently reconcile.
- When NATS is unreachable, the relay writes **not a single row**.
- `make outbox-check` pulls the messages back out of JetStream and reconciles them field by field.

::: tip This hop is not the whole story
`make outbox-check` passing proves the **publish** hop. It does not prove that a vector was written by an
event-driven consumer. Use the next hop's check for that.
:::

## Hop 2 — resident consumption (ADR-025)

`sensoryplex-index serve --consume` is a resident process that consumes JetStream into the vector sink. Its
verified properties:

- a replay with the same durable does **not** duplicate writes;
- a bad event is re-delivered up to a limit and then **fail-stops** with exit code `3` — it is not acked and
  not accounted for, so a poisoned event cannot be silently swallowed;
- three startup failures are explicit: missing stream, durable drift, NATS unreachable;
- status lines do not leak secrets.

`make consume-check` runs on the **host** (Milvus Lite is a process-exclusive local file and the BGE weights
are host-local) and verifies single-process consumption correctness: real write side → real relay → real
JetStream → resident consumer → real BGE → real Milvus Lite → immediately retrievable through the **same
process's** gRPC search face.

## Hop 3 — vector storage and the search face (ADR-020, ADR-023)

- The index process writes to Milvus (locally, the **Lite file form**) and confirms by reading back before
  marking anything `ready`.
- The search face opens its port only **after** the encoder and vector-store contracts are settled — a
  reachable port means "search face ready", and the separate ready line means "consumption actually wired
  up".
- A retrieval hit is re-checked against PostgreSQL (`ready` state, material, `source.owner`) before it is
  returned, so a stale index entry cannot leak a result.
- The query vector is produced by the BGE plugin's own `Start` encoding, and a **collection-level same-source
  guard** rejects a whole request when the model release does not match what the collection was built with.

### Failure typing

| Situation | Answer |
| --- | --- |
| Capability not implemented | `501` |
| Search face not configured / unreachable / token mismatch | `503` |
| Search face answered but not per contract | `502` |
| Wrong token (a configuration error) | `503` **with `retryable = false`** |

`retryable` is an independent marker. Do not infer it from the status code.

## Hop 4 — API and gateway

`mode=semantic` is a real vector query forwarded to the search face; it is not a `501` placeholder. The API
only forwards the query and hydrates the facts, so ranking decisions stay with the index process that owns
the vector store.

## Tier admission (ADR-027)

The event chain is admitted by machine tier. `SENSORYPLEX_EVENT_QUEUE_CAPACITY` sets the cap for the tier, and
the batch depths must be **less than or equal to** it:

| Variable | Meaning |
| --- | --- |
| `SENSORYPLEX_EVENT_QUEUE_CAPACITY` | Per-tier inflight cap |
| `SENSORYPLEX_EVENT_RELAY_BATCH` | Rows the relay claims per round |
| `SENSORYPLEX_EVENT_CONSUME_BATCH` | Inflight unacked messages for the consumer |

An over-cap configuration refuses to start **before connecting to anything** with
`event_inflight_exceeds_tier_cap`; a missing variable is an explicit `not_injected`. Nothing is clamped and
nothing is degraded — the same口径 as the media data plane.

## Verification ladder

```sh
make outbox-check          # hop 1 in the api container
make consume-check         # hop 2 on the host, single process
make event-pipeline-check  # both resident services inside compose, end to end
make index-check           # write-back confirmation in the vector store
make semantic-check        # 13 gateway semantic-search scenarios
```

## Known boundaries

- **NATS task dispatch is not wired.** Only the fact → index direction exists.
- Server-side Milvus topology is **not verified** (the local Docker Hub was unreachable); the verified form is
  Milvus Lite, which cannot be opened by two processes at once.
- RRF / hybrid retrieval and relevance calibration are not implemented.

