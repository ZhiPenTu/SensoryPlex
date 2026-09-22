//! Media source adapters and the stream-relative interval construction they feed.
//!
//! Every adapter either produces anchors backed by probe output, or fails with a reason.
//! There is no adapter that fabricates a timeline.

use std::collections::{BTreeSet, VecDeque};
use std::path::Path;

use sensoryplex_sdk::common::TimeRange;
use sensoryplex_sdk::media::{
    MediaSourceDescription, MediaSourceKind, MediaSourceRef, TimelineAnchor,
};

use crate::probe::probe_file;
use crate::MediaError;

/// A probed timeline point whose end is not known yet. Stream order is preserved by the adapter.
#[derive(Debug, Clone, PartialEq)]
pub struct PendingAnchor {
    pub track_kind: String,
    pub pts_ms: i64,
    pub keyframe: bool,
}

pub trait MediaSource: Send {
    fn describe(&self) -> &MediaSourceDescription;
    /// Next anchor in stream order. `None` means the source ended (EOF).
    fn next_anchor(&mut self) -> Result<Option<PendingAnchor>, MediaError>;
    /// Items the adapter could not turn into anchors. Never silent, never optimistic.
    fn dropped_items(&self) -> u64 {
        0
    }
    fn drop_reasons(&self) -> Vec<String> {
        Vec::new()
    }
}

/// Offline file adapter driven by ffprobe (ADR-003).
pub struct FileSource {
    description: MediaSourceDescription,
    queue: VecDeque<PendingAnchor>,
    dropped_items: u64,
    drop_reasons: Vec<String>,
}

impl FileSource {
    pub fn open(uri_secret_ref: &str, path: &Path) -> Result<Self, MediaError> {
        let probed = probe_file(uri_secret_ref, path)?;
        let mut queue = VecDeque::new();
        let mut dropped_items = 0;
        for track in &probed.tracks {
            for point in &track.points {
                match point.pts_ms {
                    Some(pts_ms) => queue.push_back(PendingAnchor {
                        track_kind: track.track_kind.clone(),
                        pts_ms,
                        keyframe: point.keyframe,
                    }),
                    // N/A PTS is dropped with a reason; inventing a timestamp is forbidden.
                    None => dropped_items += 1,
                }
            }
        }
        let drop_reasons = if dropped_items > 0 {
            vec!["pts_unavailable".to_string()]
        } else {
            Vec::new()
        };
        Ok(Self {
            description: probed.description,
            queue,
            dropped_items,
            drop_reasons,
        })
    }
}

impl MediaSource for FileSource {
    fn describe(&self) -> &MediaSourceDescription {
        &self.description
    }

    fn next_anchor(&mut self) -> Result<Option<PendingAnchor>, MediaError> {
        Ok(self.queue.pop_front())
    }

    fn dropped_items(&self) -> u64 {
        self.dropped_items
    }

    fn drop_reasons(&self) -> Vec<String> {
        self.drop_reasons.clone()
    }
}

/// Adapter placeholder for ingestion this build does not implement. It refuses to emit
/// anchors instead of returning an empty - and therefore misleading - timeline.
pub struct UnavailableSource {
    description: MediaSourceDescription,
    reason: String,
}

impl UnavailableSource {
    /// SRT ingest needs the GStreamer adapter (ADR-003); until then it stays explicitly missing.
    pub fn srt(stream_id: &str, uri_secret_ref: &str) -> Self {
        Self {
            description: MediaSourceDescription {
                source: Some(MediaSourceRef {
                    stream_id: stream_id.to_string(),
                    source_id: String::new(),
                    kind: MediaSourceKind::Srt as i32,
                    uri_secret_ref: uri_secret_ref.to_string(),
                    content_hash: String::new(),
                }),
                tracks: Vec::new(),
                duration_ms: 0,
                probe_tool: String::new(),
            },
            reason: "gstreamer_srt_ingest_not_implemented".to_string(),
        }
    }
}

impl MediaSource for UnavailableSource {
    fn describe(&self) -> &MediaSourceDescription {
        &self.description
    }

    fn next_anchor(&mut self) -> Result<Option<PendingAnchor>, MediaError> {
        Err(MediaError::UnsupportedSource(self.reason.clone()))
    }
}

#[derive(Debug, Default, Clone)]
pub struct TrackIntervals {
    pub anchors: Vec<TimelineAnchor>,
    pub dropped_items: u64,
    pub drop_reasons: Vec<String>,
    pub out_of_order_items: u64,
    pub gap_items: u64,
}

impl TrackIntervals {
    pub fn emitted(&self) -> u64 {
        self.anchors.len() as u64
    }
}

/// Builds half-open `[start_ms, end_ms)` anchors for one track.
///
/// Points must already be in presentation order (see `reorder_to_presentation`). Every drop
/// is explained: a point that collapses into the previous millisecond, arrives out of order
/// despite sorting, or has no known closing timestamp is counted and named instead of clamped.
pub fn build_track_intervals(
    points: &[PendingAnchor],
    duration_ms: i64,
    gap_threshold_ms: i64,
) -> TrackIntervals {
    let mut intervals = TrackIntervals::default();
    let mut reasons = BTreeSet::new();
    let mut pending: Option<&PendingAnchor> = None;
    for point in points {
        if let Some(previous) = pending.take() {
            if point.pts_ms < previous.pts_ms {
                intervals.out_of_order_items += 1;
                intervals.dropped_items += 1;
                reasons.insert("out_of_order_pts");
            } else if point.pts_ms == previous.pts_ms {
                intervals.dropped_items += 1;
                reasons.insert("collapsed_interval");
            } else {
                if point.pts_ms - previous.pts_ms > gap_threshold_ms {
                    intervals.gap_items += 1;
                }
                intervals
                    .anchors
                    .push(anchor(previous, previous.pts_ms, point.pts_ms));
            }
        }
        pending = Some(point);
    }
    if let Some(last) = pending {
        if duration_ms > last.pts_ms {
            intervals
                .anchors
                .push(anchor(last, last.pts_ms, duration_ms));
        } else {
            intervals.dropped_items += 1;
            reasons.insert("duration_unknown_last_frame_interval");
        }
    }
    intervals.drop_reasons = reasons.iter().map(|reason| (*reason).to_string()).collect();
    intervals
}

fn anchor(point: &PendingAnchor, start_ms: i64, end_ms: i64) -> TimelineAnchor {
    TimelineAnchor {
        anchor_id: String::new(), // filled by `label_anchors`, which knows the track index
        track_kind: point.track_kind.clone(),
        time_range: Some(TimeRange { start_ms, end_ms }),
        pts_ms: point.pts_ms,
        keyframe: point.keyframe,
    }
}

/// Assigns deterministic anchor ids after the interval list is final.
pub fn label_anchors(intervals: &mut TrackIntervals) {
    for (index, anchor) in intervals.anchors.iter_mut().enumerate() {
        anchor.anchor_id = format!("{}-{:08}", anchor.track_kind, index + 1);
    }
}

/// Converts decoder output order into presentation order.
///
/// ffprobe reports frames in decode order, so streams with B-frames legitimately arrive with
/// non-monotonic PTS. Sorting is a reorder, not a repair, and the moved count stays visible.
pub fn reorder_to_presentation(points: &[PendingAnchor]) -> (Vec<PendingAnchor>, u64) {
    let mut reordered = 0;
    let mut previous: Option<i64> = None;
    for point in points {
        if previous.is_some_and(|pts_ms| point.pts_ms < pts_ms) {
            reordered += 1;
        }
        previous = Some(point.pts_ms);
    }
    let mut sorted = points.to_vec();
    sorted.sort_by_key(|point| point.pts_ms);
    (sorted, reordered)
}

/// Drains a source and builds one interval list per track, preserving adapter drop reasons.
pub fn drain_source(
    source: &mut dyn MediaSource,
    duration_ms: i64,
    gap_threshold_ms: i64,
    max_points: usize,
) -> Result<(Vec<TrackIntervals>, bool), MediaError> {
    let mut points: Vec<PendingAnchor> = Vec::new();
    let mut truncated = false;
    while let Some(anchor) = source.next_anchor()? {
        if points.len() >= max_points {
            truncated = true;
            break;
        }
        points.push(anchor);
    }
    let mut track_kinds: Vec<String> = points
        .iter()
        .map(|point| point.track_kind.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    track_kinds.sort();
    let mut intervals = Vec::new();
    for track_kind in track_kinds {
        let track_points: Vec<PendingAnchor> = points
            .iter()
            .filter(|point| point.track_kind == track_kind)
            .cloned()
            .collect();
        let (ordered, reordered_items) = reorder_to_presentation(&track_points);
        let mut built = build_track_intervals(&ordered, duration_ms, gap_threshold_ms);
        built.out_of_order_items += reordered_items;
        label_anchors(&mut built);
        intervals.push(built);
    }
    Ok((intervals, truncated))
}

#[cfg(test)]
mod tests {
    use super::*;
    use sensoryplex_sdk::validate_range;

    fn points(pts: &[i64]) -> Vec<PendingAnchor> {
        pts.iter()
            .map(|pts_ms| PendingAnchor {
                track_kind: "video".into(),
                pts_ms: *pts_ms,
                keyframe: *pts_ms == 0,
            })
            .collect()
    }

    fn ranges(intervals: &TrackIntervals) -> Vec<(i64, i64)> {
        intervals
            .anchors
            .iter()
            .map(|anchor| {
                let range = anchor.time_range.as_ref().expect("range is always set");
                (range.start_ms, range.end_ms)
            })
            .collect()
    }

    #[test]
    fn builds_half_open_intervals_from_monotonic_points() {
        let intervals = build_track_intervals(&points(&[0, 40, 80]), 120, 1_000);
        assert_eq!(ranges(&intervals), vec![(0, 40), (40, 80), (80, 120)]);
        assert_eq!(intervals.emitted(), 3);
        assert_eq!(intervals.dropped_items, 0);
        assert_eq!(intervals.out_of_order_items, 0);
        assert_eq!(intervals.gap_items, 0);
        for anchor in &intervals.anchors {
            validate_range(anchor.time_range.as_ref().unwrap()).expect("anchor range");
            assert!(anchor.pts_ms >= 0);
        }
    }

    #[test]
    fn counts_gaps_without_inventing_frames() {
        let intervals = build_track_intervals(&points(&[0, 40, 5_000]), 5_080, 1_000);
        assert_eq!(
            ranges(&intervals),
            vec![(0, 40), (40, 5_000), (5_000, 5_080)]
        );
        assert_eq!(intervals.gap_items, 1);
        assert_eq!(intervals.dropped_items, 0);
    }

    #[test]
    fn explains_collapsed_points() {
        let collapsed = build_track_intervals(&points(&[0, 0, 40]), 80, 1_000);
        assert_eq!(ranges(&collapsed), vec![(0, 40), (40, 80)]);
        assert_eq!(collapsed.dropped_items, 1);
        assert_eq!(collapsed.drop_reasons, vec!["collapsed_interval"]);
    }

    #[test]
    fn decoder_order_is_reordered_and_counted_but_never_smoothed_away() {
        // B-frame pattern: decode order 0, 160, 80 with presentation order 0, 80, 160.
        let decode_order = points(&[0, 160, 80]);
        let (ordered, reordered) = reorder_to_presentation(&decode_order);
        assert_eq!(reordered, 1);
        let intervals = build_track_intervals(&ordered, 200, 1_000);
        assert_eq!(ranges(&intervals), vec![(0, 80), (80, 160), (160, 200)]);
        assert_eq!(intervals.dropped_items, 0);
        assert_eq!(intervals.gap_items, 0);
    }

    #[test]
    fn unknown_duration_drops_the_last_interval_instead_of_guessing() {
        let intervals = build_track_intervals(&points(&[0, 40]), 0, 1_000);
        assert_eq!(ranges(&intervals), vec![(0, 40)]);
        assert_eq!(intervals.dropped_items, 1);
        assert_eq!(
            intervals.drop_reasons,
            vec!["duration_unknown_last_frame_interval"]
        );
    }

    #[test]
    fn labels_are_deterministic_and_per_track() {
        let mut intervals = build_track_intervals(&points(&[0, 40]), 80, 1_000);
        label_anchors(&mut intervals);
        let ids: Vec<_> = intervals
            .anchors
            .iter()
            .map(|anchor| anchor.anchor_id.clone())
            .collect();
        assert_eq!(ids, vec!["video-00000001", "video-00000002"]);
    }
}
