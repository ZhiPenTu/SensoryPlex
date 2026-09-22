//! Adaptive frame sampling over decoded video.
//!
//! The sampler never decides on its own to "drop" hard data silently: every observed frame ends
//! up either as a keep (with a reason) or as a skip (with a reason), and the keep rate is bounded
//! by construction, so a long high-motion stream cannot produce an unbounded sample set.

use crate::MediaError;

/// Grid resolution of the frame signature. 8x8 keeps the comparison cheap and stable against
/// compression noise while still reacting to a whole-screen replacement.
pub const SIGNATURE_GRID: usize = 8;
/// Samples taken per grid cell (at most `SIGNATURE_CELL_SAMPLES^2` pixels contribute).
const SIGNATURE_CELL_SAMPLES: usize = 4;

pub const DEFAULT_MIN_INTERVAL_MS: i64 = 1_000;
pub const DEFAULT_STATIC_HOLD_MS: i64 = 5_000;
pub const DEFAULT_CHANGE_THRESHOLD: u32 = 8;
pub const MIN_INTERVAL_LOWER_MS: i64 = 100;
pub const MAX_INTERVAL_MS: i64 = 60_000;

/// Why a frame was kept. Keeps are never anonymous.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeepReason {
    FirstFrame,
    ContentChange,
    StaticHeartbeat,
}

impl KeepReason {
    pub fn name(self) -> &'static str {
        match self {
            Self::FirstFrame => "first_frame",
            Self::ContentChange => "content_change",
            Self::StaticHeartbeat => "static_heartbeat",
        }
    }
}

/// Why a frame was skipped. A skip must be explainable, never implicit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SkipReason {
    RateLimited,
    NoChangeYet,
    NonMonotonic,
    MissingSignature,
}

impl SkipReason {
    pub fn name(self) -> &'static str {
        match self {
            Self::RateLimited => "rate_limited",
            Self::NoChangeYet => "no_change_yet",
            Self::NonMonotonic => "non_monotonic_pts",
            Self::MissingSignature => "missing_signature",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    Keep { reason: KeepReason, delta: u32 },
    Skip { reason: SkipReason, delta: u32 },
}

impl Decision {
    pub fn kept(self) -> bool {
        matches!(self, Self::Keep { .. })
    }
}

#[derive(Debug, Clone, Copy)]
pub struct SamplingPolicy {
    pub min_interval_ms: i64,
    pub static_hold_ms: i64,
    pub change_threshold: u32,
}

impl Default for SamplingPolicy {
    fn default() -> Self {
        Self {
            min_interval_ms: DEFAULT_MIN_INTERVAL_MS,
            static_hold_ms: DEFAULT_STATIC_HOLD_MS,
            change_threshold: DEFAULT_CHANGE_THRESHOLD,
        }
    }
}

impl SamplingPolicy {
    pub fn new(
        min_interval_ms: i64,
        static_hold_ms: i64,
        change_threshold: u32,
    ) -> Result<Self, MediaError> {
        if !(MIN_INTERVAL_LOWER_MS..=MAX_INTERVAL_MS).contains(&min_interval_ms) {
            return Err(MediaError::SamplingRejected(
                "min_interval_ms_out_of_range".into(),
            ));
        }
        if static_hold_ms < min_interval_ms || static_hold_ms > MAX_INTERVAL_MS * 10 {
            return Err(MediaError::SamplingRejected(
                "static_hold_ms_out_of_range".into(),
            ));
        }
        if change_threshold == 0 || change_threshold > 128 {
            return Err(MediaError::SamplingRejected(
                "change_threshold_out_of_range".into(),
            ));
        }
        Ok(Self {
            min_interval_ms,
            static_hold_ms,
            change_threshold,
        })
    }

    /// Hard upper bound on keeps for a stream of `duration_ms`: the rate limit alone decides it.
    pub fn max_keeps(&self, duration_ms: i64) -> u64 {
        if duration_ms <= 0 {
            return 1;
        }
        (duration_ms / self.min_interval_ms) as u64 + 1
    }
}

/// Compact luma signature of one frame: `SIGNATURE_GRID^2` cell averages.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FrameSignature {
    cells: [u8; SIGNATURE_GRID * SIGNATURE_GRID],
}

impl FrameSignature {
    pub fn zeros() -> Self {
        Self {
            cells: [0; SIGNATURE_GRID * SIGNATURE_GRID],
        }
    }

    pub fn cells(&self) -> &[u8; SIGNATURE_GRID * SIGNATURE_GRID] {
        &self.cells
    }

    /// Mean absolute difference against another signature, in luma units (0..=255).
    pub fn delta(&self, other: &Self) -> u32 {
        let sum: u32 = self
            .cells
            .iter()
            .zip(other.cells.iter())
            .map(|(a, b)| a.abs_diff(*b) as u32)
            .sum();
        sum / self.cells.len() as u32
    }

    /// Builds a signature from an RGBA frame. The layout must already be known: a frame whose
    /// geometry is unknown cannot be sampled, and the caller reports that instead of guessing.
    pub fn from_rgba(bytes: &[u8], width: u32, height: u32) -> Option<Self> {
        if width == 0 || height == 0 {
            return None;
        }
        let expected = width as usize * height as usize * 4;
        if bytes.len() < expected {
            return None;
        }
        let mut cells = [0u8; SIGNATURE_GRID * SIGNATURE_GRID];
        let cell_w = width as usize / SIGNATURE_GRID;
        let cell_h = height as usize / SIGNATURE_GRID;
        if cell_w == 0 || cell_h == 0 {
            return None;
        }
        let step_x = (cell_w / SIGNATURE_CELL_SAMPLES).max(1);
        let step_y = (cell_h / SIGNATURE_CELL_SAMPLES).max(1);
        for row in 0..SIGNATURE_GRID {
            for col in 0..SIGNATURE_GRID {
                let base_x = col * cell_w;
                let base_y = row * cell_h;
                let mut sum = 0u32;
                let mut count = 0u32;
                let mut y = base_y;
                while y < base_y + cell_h {
                    let mut x = base_x;
                    while x < base_x + cell_w {
                        let index = (y * width as usize + x) * 4;
                        let r = bytes[index] as u32;
                        let g = bytes[index + 1] as u32;
                        let b = bytes[index + 2] as u32;
                        // Integer luma, enough for change detection and cheaper than f32 maths.
                        sum += (77 * r + 150 * g + 29 * b) >> 8;
                        count += 1;
                        x += step_x;
                    }
                    y += step_y;
                }
                cells[row * SIGNATURE_GRID + col] = if count == 0 {
                    0
                } else {
                    sum.checked_div(count).unwrap_or(0).min(255) as u8
                };
            }
        }
        Some(Self { cells })
    }
}

/// Counters for one sampling run. Every observed frame is accounted for exactly once.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SamplingCounters {
    pub observed: u64,
    pub kept: u64,
    pub kept_first_frame: u64,
    pub kept_content_change: u64,
    pub kept_static_heartbeat: u64,
    pub skipped_rate_limited: u64,
    pub skipped_no_change: u64,
    pub skipped_non_monotonic: u64,
    pub skipped_missing_signature: u64,
    pub max_gap_ms: i64,
}

impl SamplingCounters {
    pub fn skipped(&self) -> u64 {
        self.skipped_rate_limited
            + self.skipped_no_change
            + self.skipped_non_monotonic
            + self.skipped_missing_signature
    }

    /// A skip reason must always exist when frames were skipped.
    pub fn skip_reasons(&self) -> Vec<(&'static str, u64)> {
        [
            (SkipReason::RateLimited.name(), self.skipped_rate_limited),
            (SkipReason::NoChangeYet.name(), self.skipped_no_change),
            (SkipReason::NonMonotonic.name(), self.skipped_non_monotonic),
            (
                SkipReason::MissingSignature.name(),
                self.skipped_missing_signature,
            ),
        ]
        .into_iter()
        .filter(|(_, count)| *count > 0)
        .collect()
    }
}

#[derive(Debug, Clone)]
pub struct AdaptiveSampler {
    policy: SamplingPolicy,
    last_keep_ms: Option<i64>,
    last_pts_ms: Option<i64>,
    last_signature: Option<FrameSignature>,
    counters: SamplingCounters,
}

impl AdaptiveSampler {
    pub fn new(policy: SamplingPolicy) -> Self {
        Self {
            policy,
            last_keep_ms: None,
            last_pts_ms: None,
            last_signature: None,
            counters: SamplingCounters::default(),
        }
    }

    pub fn policy(&self) -> SamplingPolicy {
        self.policy
    }

    pub fn counters(&self) -> &SamplingCounters {
        &self.counters
    }

    /// Observes one video frame. `signature` is `None` when the frame layout was unusable, which
    /// is counted as an explicit skip rather than treated as "no change".
    pub fn observe(&mut self, pts_ms: i64, signature: Option<FrameSignature>) -> Decision {
        self.counters.observed += 1;
        let delta = match signature {
            Some(current) => self
                .last_signature
                .map_or(0, |previous| previous.delta(&current)),
            None => {
                self.counters.skipped_missing_signature += 1;
                return Decision::Skip {
                    reason: SkipReason::MissingSignature,
                    delta: 0,
                };
            }
        };
        if let Some(previous) = self.last_pts_ms {
            if pts_ms <= previous {
                self.counters.skipped_non_monotonic += 1;
                return Decision::Skip {
                    reason: SkipReason::NonMonotonic,
                    delta,
                };
            }
        }
        let decision = match self.last_keep_ms {
            None => Decision::Keep {
                reason: KeepReason::FirstFrame,
                delta,
            },
            Some(last_keep) => {
                let elapsed = pts_ms - last_keep;
                if elapsed < self.policy.min_interval_ms {
                    self.counters.skipped_rate_limited += 1;
                    return Decision::Skip {
                        reason: SkipReason::RateLimited,
                        delta,
                    };
                }
                if delta >= self.policy.change_threshold {
                    Decision::Keep {
                        reason: KeepReason::ContentChange,
                        delta,
                    }
                } else if elapsed >= self.policy.static_hold_ms {
                    Decision::Keep {
                        reason: KeepReason::StaticHeartbeat,
                        delta,
                    }
                } else {
                    self.counters.skipped_no_change += 1;
                    return Decision::Skip {
                        reason: SkipReason::NoChangeYet,
                        delta,
                    };
                }
            }
        };
        if let Decision::Keep { reason, .. } = decision {
            if let Some(previous_keep) = self.last_keep_ms {
                self.counters.max_gap_ms = self.counters.max_gap_ms.max(pts_ms - previous_keep);
            }
            self.last_keep_ms = Some(pts_ms);
            self.counters.kept += 1;
            match reason {
                KeepReason::FirstFrame => self.counters.kept_first_frame += 1,
                KeepReason::ContentChange => self.counters.kept_content_change += 1,
                KeepReason::StaticHeartbeat => self.counters.kept_static_heartbeat += 1,
            }
        }
        if let Some(current) = signature {
            self.last_signature = Some(current);
        }
        self.last_pts_ms = Some(pts_ms);
        decision
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn signature(value: u8) -> FrameSignature {
        let mut cells = [0u8; SIGNATURE_GRID * SIGNATURE_GRID];
        cells.fill(value);
        FrameSignature { cells }
    }

    fn rgba(value: u8, width: u32, height: u32) -> Vec<u8> {
        vec![value; width as usize * height as usize * 4]
    }

    #[test]
    fn policy_rejects_silent_degradation() {
        assert!(SamplingPolicy::new(10, 5_000, 8).is_err(), "too fast");
        assert!(
            SamplingPolicy::new(1_000, 500, 8).is_err(),
            "heartbeat must not be faster than the rate limit"
        );
        assert!(
            SamplingPolicy::new(1_000, 5_000, 0).is_err(),
            "zero threshold"
        );
        assert!(SamplingPolicy::new(1_000, 5_000, 8).is_ok());
    }

    #[test]
    fn a_static_stream_keeps_the_first_frame_and_heartbeats() {
        let policy = SamplingPolicy::default();
        let mut sampler = AdaptiveSampler::new(policy);
        let mut keeps = Vec::new();
        let frame_ms = 40;
        let frames = 250; // 10 seconds at 25 fps
        let mut pts = 0;
        for _ in 0..frames {
            if sampler.observe(pts, Some(signature(100))).kept() {
                keeps.push(pts);
            }
            pts += frame_ms;
        }
        let counters = sampler.counters();
        assert_eq!(counters.observed, frames as u64);
        assert_eq!(counters.kept, keeps.len() as u64);
        assert_eq!(counters.kept_first_frame, 1);
        assert_eq!(counters.kept_content_change, 0, "nothing changed");
        assert!(
            counters.kept_static_heartbeat <= policy.max_keeps(10_000),
            "heartbeats stay under the rate bound"
        );
        assert_eq!(keeps[0], 0);
        assert!(
            counters.max_gap_ms <= policy.static_hold_ms,
            "a static stream must not go longer than the hold without a keep: {}",
            counters.max_gap_ms
        );
        assert!(
            counters.skipped_no_change + counters.skipped_rate_limited
                == counters.observed - counters.kept,
            "every skip has a reason"
        );
    }

    #[test]
    fn a_whole_screen_change_is_kept_as_a_content_change() {
        let policy = SamplingPolicy::new(500, 5_000, 8).unwrap();
        let mut sampler = AdaptiveSampler::new(policy);
        assert!(sampler.observe(0, Some(signature(10))).kept());
        assert!(
            !sampler.observe(40, Some(signature(10))).kept(),
            "rate limited"
        );
        assert!(
            !sampler.observe(600, Some(signature(10))).kept(),
            "no change yet"
        );
        let decision = sampler.observe(1_200, Some(signature(200)));
        match decision {
            Decision::Keep { reason, delta } => {
                assert_eq!(reason, KeepReason::ContentChange);
                assert!(delta >= 8, "delta {delta}");
            }
            other => panic!("expected a content change keep, got {other:?}"),
        }
    }

    #[test]
    fn the_keep_rate_is_bounded_even_when_every_frame_changes() {
        let policy = SamplingPolicy::new(1_000, 5_000, 4).unwrap();
        let mut sampler = AdaptiveSampler::new(policy);
        let frames = 300; // 20 seconds at 15 fps, always changing
        let duration_ms = (frames - 1) * 66;
        let mut pts = 0;
        for index in 0..frames {
            let value = if index % 2 == 0 { 10 } else { 240 };
            sampler.observe(pts, Some(signature(value)));
            pts += 66;
        }
        let counters = sampler.counters();
        assert!(
            counters.kept <= policy.max_keeps(duration_ms),
            "kept {} must stay within the bound {}",
            counters.kept,
            policy.max_keeps(duration_ms)
        );
        assert!(
            counters.skipped_rate_limited > 0,
            "the rate limit must bite"
        );
    }

    #[test]
    fn unusable_or_reordered_frames_are_counted_not_ignored() {
        let mut sampler = AdaptiveSampler::new(SamplingPolicy::default());
        sampler.observe(1_000, Some(signature(50)));
        match sampler.observe(1_040, None) {
            Decision::Skip { reason, .. } => assert_eq!(reason, SkipReason::MissingSignature),
            other => panic!("expected a signature skip, got {other:?}"),
        }
        match sampler.observe(1_000, Some(signature(50))) {
            Decision::Skip { reason, .. } => assert_eq!(reason, SkipReason::NonMonotonic),
            other => panic!("expected an ordering skip, got {other:?}"),
        }
        let counters = sampler.counters();
        assert_eq!(counters.observed, 3);
        assert_eq!(counters.kept, 1);
        assert_eq!(counters.skipped(), 2);
        assert_eq!(
            counters.skip_reasons().len(),
            2,
            "reasons are reported separately"
        );
    }

    #[test]
    fn a_signature_reacts_to_content_and_rejects_unknown_geometry() {
        let width = 64;
        let height = 64;
        let flat = FrameSignature::from_rgba(&rgba(30, width, height), width, height).unwrap();
        let brighter = FrameSignature::from_rgba(&rgba(200, width, height), width, height).unwrap();
        assert_eq!(flat.delta(&flat), 0);
        assert_eq!(brighter.delta(&flat), 170);
        assert!(
            FrameSignature::from_rgba(&rgba(30, width, height), 0, height).is_none(),
            "zero width cannot be sampled"
        );
        assert!(
            FrameSignature::from_rgba(&[0u8; 16], width, height).is_none(),
            "a short buffer must not be read past its end"
        );
    }
}
