//! 对已解码视频做自适应帧采样。
//!
//! 采样器从不擅自"静默丢弃"硬数据：每个观测到的帧要么成为 keep（带原因），
//! 要么成为 skip（带原因），keep 速率在构造上就有界，因此一段长时高动态的流
//! 也不会产生无界的样本集合。

use crate::MediaError;

/// 帧签名的网格分辨率。8x8 让比较足够廉价、对压缩噪声稳定，
/// 同时仍能响应整屏替换。
pub const SIGNATURE_GRID: usize = 8;
/// 每个网格单元的采样数（最多 `SIGNATURE_CELL_SAMPLES^2` 个像素参与）。
const SIGNATURE_CELL_SAMPLES: usize = 4;

pub const DEFAULT_MIN_INTERVAL_MS: i64 = 1_000;
pub const DEFAULT_STATIC_HOLD_MS: i64 = 5_000;
pub const DEFAULT_CHANGE_THRESHOLD: u32 = 8;
pub const MIN_INTERVAL_LOWER_MS: i64 = 100;
pub const MAX_INTERVAL_MS: i64 = 60_000;

/// 帧被保留的原因。keep 绝不是匿名的。
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

/// 帧被跳过的原因。skip 必须可解释，绝不能隐式发生。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SkipReason {
    RateLimited,
    NoChangeYet,
    NonMonotonic,
    MissingSignature,
    /// 队列背压期间的阶段一降级：这一帧本来会被 keep，但下游有界队列已经在
    /// `degraded`/`saturated`，于是按放大后的最小间隔放弃它。它与 `RateLimited`
    /// 分开计数，因为普通限速是策略常数，而这里是被下游压力驱动的、会随压力消失的降速。
    BackpressureThrottled,
}

impl SkipReason {
    pub fn name(self) -> &'static str {
        match self {
            Self::RateLimited => "rate_limited",
            Self::NoChangeYet => "no_change_yet",
            Self::NonMonotonic => "non_monotonic_pts",
            Self::MissingSignature => "missing_signature",
            Self::BackpressureThrottled => "backpressure_throttled",
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

    /// 在 `duration_ms` 长度的流上 keep 数量的硬性上限，仅由速率限制决定。
    pub fn max_keeps(&self, duration_ms: i64) -> u64 {
        if duration_ms <= 0 {
            return 1;
        }
        (duration_ms / self.min_interval_ms) as u64 + 1
    }
}

/// 单帧的紧凑亮度签名：共 `SIGNATURE_GRID^2` 个网格的平均值。
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

    /// 与另一签名的平均绝对差，单位为亮度（0..=255）。
    pub fn delta(&self, other: &Self) -> u32 {
        let sum: u32 = self
            .cells
            .iter()
            .zip(other.cells.iter())
            .map(|(a, b)| a.abs_diff(*b) as u32)
            .sum();
        sum / self.cells.len() as u32
    }

    /// 从一帧 RGBA 数据构造签名。布局信息必须已知：几何信息未知的帧无法采样，
    /// 调用方应如实上报，而不是猜测。
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
                        // 使用整数亮度，足以检测变化且比 f32 更廉价 maths.
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

/// 一次采样运行的计数器。每一个被观察的帧都会被精确地计入一次。
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
    /// 被背压抑制的 keep。它必须与 `observed`/`dropped_samples` 一起读：
    /// 少了这些帧不是"内容没变"，而是下游队列在降级。
    pub skipped_backpressure_throttled: u64,
    /// 观测到的最大相邻 pts 间隔。抽帧的 gap 上界由 `static_hold_ms + 本值` 决定，
    /// 因此它必须可复核，而不是靠"应该不会太大"。
    pub max_frame_interval_ms: i64,
    pub max_gap_ms: i64,
}

impl SamplingCounters {
    pub fn skipped(&self) -> u64 {
        self.skipped_rate_limited
            + self.skipped_no_change
            + self.skipped_non_monotonic
            + self.skipped_missing_signature
            + self.skipped_backpressure_throttled
    }

    /// 出现跳过帧时，必须存在一个 skip reason。
    pub fn skip_reasons(&self) -> Vec<(&'static str, u64)> {
        [
            (SkipReason::RateLimited.name(), self.skipped_rate_limited),
            (SkipReason::NoChangeYet.name(), self.skipped_no_change),
            (SkipReason::NonMonotonic.name(), self.skipped_non_monotonic),
            (
                SkipReason::MissingSignature.name(),
                self.skipped_missing_signature,
            ),
            (
                SkipReason::BackpressureThrottled.name(),
                self.skipped_backpressure_throttled,
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
    first_pts_ms: Option<i64>,
    last_keep_ms: Option<i64>,
    last_pts_ms: Option<i64>,
    last_signature: Option<FrameSignature>,
    counters: SamplingCounters,
}

impl AdaptiveSampler {
    pub fn new(policy: SamplingPolicy) -> Self {
        Self {
            policy,
            first_pts_ms: None,
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

    /// 已观测跨度（毫秒）。`max_keeps` 用它算 keep 的硬上限，
    /// 因此报告里能给出一个可复核的界，而不是"看起来没超"。
    pub fn observed_span_ms(&self) -> i64 {
        match (self.first_pts_ms, self.last_pts_ms) {
            (Some(first), Some(last)) => (last - first).max(0),
            _ => 0,
        }
    }

    /// 观测一帧视频。当帧布局不可用时 `signature` 为 `None`，
    /// 此时会被作为显式 skip 计入，而不是当作"无变化"。
    pub fn observe(&mut self, pts_ms: i64, signature: Option<FrameSignature>) -> Decision {
        self.observe_with_pressure(pts_ms, signature, None)
    }

    /// 带下游压力的观测。`throttle_min_interval_ms` 为 `Some` 时，最小间隔换成这个值
    /// （它由背压策略从本策略的最小间隔放大而来，因此只可能更大）；被它挡下的 keep
    /// 计入 `skipped_backpressure_throttled`，与普通 `rate_limited` 分开。
    pub fn observe_with_pressure(
        &mut self,
        pts_ms: i64,
        signature: Option<FrameSignature>,
        throttle_min_interval_ms: Option<i64>,
    ) -> Decision {
        // 降级只降低 keep 速率：一个小于本策略最小间隔的入参会被忽略，而不是被执行。
        let min_interval_ms = throttle_min_interval_ms
            .filter(|throttled| *throttled > self.policy.min_interval_ms)
            .unwrap_or(self.policy.min_interval_ms);
        self.counters.observed += 1;
        if self.first_pts_ms.is_none() {
            self.first_pts_ms = Some(pts_ms);
        }
        // 顺序与相邻间隔是"流"的属性，而不是"keep"的属性：每个被观测的帧都要推进游标，
        // 否则一次 skip 之后的乱序帧会被当成顺序正常，max_frame_interval_ms 也会退化成
        // keep 之间的间隔，使 gap 上界无法复核。
        if let Some(previous) = self.last_pts_ms {
            if pts_ms <= previous {
                self.counters.skipped_non_monotonic += 1;
                return Decision::Skip {
                    reason: SkipReason::NonMonotonic,
                    delta: 0,
                };
            }
            self.counters.max_frame_interval_ms =
                self.counters.max_frame_interval_ms.max(pts_ms - previous);
        }
        self.last_pts_ms = Some(pts_ms);
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
                if elapsed < min_interval_ms {
                    // 走了基础限速，却还没到背压放宽后的间隔：这一帧是被下游挡下的。
                    self.counters.skipped_backpressure_throttled += 1;
                    return Decision::Skip {
                        reason: SkipReason::BackpressureThrottled,
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
        let frames = 250; // 10 秒 @ 25 fps
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
        assert_eq!(counters.max_frame_interval_ms, frame_ms);
        assert!(
            counters.skipped_no_change + counters.skipped_rate_limited
                == counters.observed - counters.kept,
            "every skip has a reason"
        );
    }

    #[test]
    fn the_keep_gap_is_bounded_by_the_hold_plus_an_observed_frame_interval() {
        let policy = SamplingPolicy::new(1_000, 5_000, 8).unwrap();
        let mut sampler = AdaptiveSampler::new(policy);
        let mut pts = 0;
        for _ in 0..11 {
            sampler.observe(pts, Some(signature(70)));
            pts += 1_000;
        }
        let counters = sampler.counters();
        assert_eq!(counters.observed, 11);
        assert_eq!(counters.max_frame_interval_ms, 1_000);
        assert_eq!(
            counters.kept_static_heartbeat, 2,
            "a static stream keeps a heartbeat per hold"
        );
        assert_eq!(counters.max_gap_ms, 5_000);
        assert!(
            counters.max_gap_ms <= policy.static_hold_ms + counters.max_frame_interval_ms,
            "gap {} is not recomputable from hold + frame interval",
            counters.max_gap_ms
        );
        assert_eq!(sampler.observed_span_ms(), 10_000);
        assert!(
            counters.kept <= policy.max_keeps(sampler.observed_span_ms()),
            "kept {} exceeds the rate bound over the observed span",
            counters.kept
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
        let frames = 300; // 20 秒 @ 15 fps，全程都在变化
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
    fn backpressure_throttles_the_keep_rate_and_says_why() {
        let mut sampler = AdaptiveSampler::new(SamplingPolicy::default());
        // 首帧永远保留：降级不能把一条流变成空的。
        assert!(sampler
            .observe_with_pressure(0, Some(signature(10)), Some(4_000))
            .kept());
        // 2 秒后有内容变化：基础限速（1 秒）已经满足，但背压放宽后的间隔（4 秒）还没到。
        match sampler.observe_with_pressure(2_000, Some(signature(200)), Some(4_000)) {
            Decision::Skip { reason, .. } => {
                assert_eq!(reason, SkipReason::BackpressureThrottled)
            }
            other => panic!("expected a backpressure skip, got {other:?}"),
        }
        // 压力消失后，同一帧会被正常 keep——降级是随压力可逆的，不是永久丢帧策略。
        assert!(sampler.observe(3_000, Some(signature(200))).kept());
        let counters = sampler.counters();
        assert_eq!(counters.observed, 3);
        assert_eq!(counters.kept, 2);
        assert_eq!(counters.skipped_backpressure_throttled, 1);
        assert_eq!(
            counters.skipped_rate_limited, 0,
            "folding them together hides the cause"
        );
        assert_eq!(
            counters.skip_reasons(),
            vec![("backpressure_throttled", 1)],
            "the skip reason must be readable from the counters"
        );
    }

    #[test]
    fn a_throttle_below_the_base_interval_is_ignored_not_applied() {
        let mut sampler = AdaptiveSampler::new(SamplingPolicy::default());
        sampler.observe(0, Some(signature(10)));
        // 入参想放宽到 500ms，比策略的 1s 更密：必须被忽略，否则压力会加快采样。
        let decision = sampler.observe_with_pressure(1_500, Some(signature(200)), Some(500));
        assert!(decision.kept(), "the base rate limit still governs");
        assert_eq!(sampler.counters().skipped_backpressure_throttled, 0);
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
