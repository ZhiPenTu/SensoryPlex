# Timeline semantics

Time is the hardest contract in the system, so it gets the strictest rules.

## The interval rule

Every temporal value is a **half-open `[start_ms, end_ms)` interval on exactly one stream**, in
millisecond offsets from that stream's origin. `end_ms` must be strictly greater than `start_ms`.

Consequences:

- Adjacent intervals never double-count a boundary instant.
- An interval from one stream can never be compared with an interval from another stream without saying so
  explicitly — offsets are stream-relative.
- The framework never "repairs" a suspicious interval by clamping it. An unknown or absent value stays
  unknown.

## Anchors are presentation-order, and carry no bytes

`TimelineAnchor` is an interval of the **presentation** order of a stream; its `pts_ms` equals the interval
start. An anchor is a reference: it carries no media bytes, only the coordinates.

`ffprobe` emits frames in **decode** order, so streams with B-frames produce non-monotonic PTS. The runtime
reorders them into presentation order and records how many items it moved in `out_of_order_items`.
Reordering is an explicit, counted action — not a silent fix.

## Every dropped point has a reason

Sampling is adaptive, and every point that is not kept is counted with a reason code such as:

| Reason | Meaning |
| --- | --- |
| `collapsed_interval` | The interval did not survive sampling at this density |
| `duration_unknown_last_frame_interval` | The duration of the final frame interval is unknown |
| `pts_unavailable` | No reliable presentation timestamp was available |

Unknown duration, unknown PTS or a point beyond the declared duration are reported — never clamped into a
legal-looking interval.

## The decoded data plane is either real or all-zero

`ReplayReport.decoded` is populated only when this build actually decoded media:

- **All-zero means "no decode happened."** When `arena_id` is empty, `tracks`, `evidence_descriptors` and a
  non-zero `descriptors_built` must not appear. A zero-filled structure is not a success.
- `first_pts_ms` / `last_end_ms` use `-1` for "not observed" — never `0`, which would impersonate the stream
  origin.
- `timeline_offset_ms` records the presentation origin subtracted from raw timestamps (non-zero when the
  container has an edit list), so descriptors and `ffprobe` anchors share one presentation timeline.
- `overlapping_samples` counts buffers whose interval repeats the previous segment: the payload is real, so
  it is kept — and counted.

## Audio and live specifics

- Audio is segmented into 5-second windows. Plugins interpret the bytes strictly according to the
  `sample_format` reported in the descriptor; an unknown layout is explicitly rejected. There is no default
  sample width and no "assume 4 bytes per sample" fallback.
- A sub-segment that runs outside its window is **marked**, not clamped.
- Live streams have no known duration, so they produce no anchor intervals. Disconnects are measured, and
  reconnection is owned by the decode element (`srtsrc auto-reconnect`), not reimplemented by the pipeline.

## Why this is strict

A material library whose time semantics are approximate cannot answer "what happened at 00:12:30" and cannot
be used as evidence. Strictness here is what makes region queries, timeline fusion and original-range
playback agree with each other.

