//! 描述符之后那条有界队列的背压策略与可观察量。
//!
//! 蓝图 §4.2 要求"队列超过阈值时先降低非关键帧采样率，最后报告可见的 `backpressure` 状态"。
//! 这里把三件事分开实现，避免把它们混成一个模糊的"忙"：
//!
//! 1. **策略**（[`BackpressurePolicy`]）：深度占容量的百分比决定 `ok / degraded / saturated`，
//!    以及降级时把采样最小间隔放大多少倍。
//! 2. **阶段一降级**：不在这里发生，而是由解码会话把它翻译成采样器的一个入参
//!    （见 [`crate::sampler::AdaptiveSampler::observe_with_pressure`]）。被抑制的 keep 必须
//!    带原因计数，绝不静默消失。
//! 3. **可观察量**（[`BackpressureTracker`] + [`BackpressureTracker::report`]）：
//!    队列容量/水位/峰值、按原因拆分的丢弃、超时与等待时间。
//!
//! 覆盖范围要说清楚：本模块只度量**描述符之后**的保留队列。GStreamer 侧 `queue` 元素与
//! `appsink` 的 `max_buffers` 仍然是配置项，没有计数出口，本模块不声称测量了它们。

use std::collections::BTreeMap;

use sensoryplex_sdk::media;

use crate::handoff::HandoffStats;
use crate::MediaError;

/// 进入 `degraded` 的默认深度比例（占容量百分比）。50% 留出足够的提前量，让采样降速
/// 在队列真正触顶之前生效。
pub const DEFAULT_DEGRADED_PERCENT: u32 = 50;
/// 背压时采样最小间隔的默认放大倍数：把 keep 速率压到四分之一。
pub const DEFAULT_THROTTLE_FACTOR: u32 = 4;
/// 降级后采样间隔的上限，避免一次背压把心跳也压没了。
pub const DEFAULT_THROTTLE_CAP_MS: i64 = 10_000;

const MAX_THROTTLE_FACTOR: u32 = 64;

/// 队列深度相对容量的压力等级。名称是契约的一部分：报告里的 `state` 就取这里的字符串。
/// `Ord` 让"取两条队列里更紧的那条"可以直接写成 `max`。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Default)]
pub enum PressureLevel {
    #[default]
    Ok,
    Degraded,
    Saturated,
}

impl PressureLevel {
    pub fn name(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Degraded => "degraded",
            Self::Saturated => "saturated",
        }
    }

    /// 只有 `Ok` 之外的等级才触发阶段一降级。
    pub fn is_pressured(self) -> bool {
        self != Self::Ok
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BackpressurePolicy {
    /// `degraded` 的起点：深度 ≥ 容量的这个百分比。
    pub degraded_percent: u32,
    /// 降级期间采样最小间隔的放大倍数；1 表示不降速（仍然计数）。
    pub throttle_factor: u32,
    pub throttle_cap_ms: i64,
}

impl Default for BackpressurePolicy {
    fn default() -> Self {
        Self {
            degraded_percent: DEFAULT_DEGRADED_PERCENT,
            throttle_factor: DEFAULT_THROTTLE_FACTOR,
            throttle_cap_ms: DEFAULT_THROTTLE_CAP_MS,
        }
    }
}

impl BackpressurePolicy {
    pub fn new(
        degraded_percent: u32,
        throttle_factor: u32,
        throttle_cap_ms: i64,
    ) -> Result<Self, MediaError> {
        if !(1..=99).contains(&degraded_percent) {
            // 100 会让 `degraded` 与 `saturated` 重合，0 会让任何非空队列都算降级。
            return Err(MediaError::SamplingRejected(
                "degraded_percent_out_of_range".into(),
            ));
        }
        if !(1..=MAX_THROTTLE_FACTOR).contains(&throttle_factor) {
            return Err(MediaError::SamplingRejected(
                "throttle_factor_out_of_range".into(),
            ));
        }
        if !(crate::sampler::MIN_INTERVAL_LOWER_MS..=crate::sampler::MAX_INTERVAL_MS)
            .contains(&throttle_cap_ms)
        {
            return Err(MediaError::SamplingRejected(
                "throttle_cap_ms_out_of_range".into(),
            ));
        }
        Ok(Self {
            degraded_percent,
            throttle_factor,
            throttle_cap_ms,
        })
    }

    /// 由深度与容量得到压力等级。容量为 0 是不可能的配置，但真出现时按 `saturated` 处理：
    /// 一个没有容量的队列不能接受任何工作。
    pub fn level(&self, depth: u64, capacity: u64) -> PressureLevel {
        if capacity == 0 || depth >= capacity {
            return PressureLevel::Saturated;
        }
        if depth.saturating_mul(100) >= capacity.saturating_mul(self.degraded_percent as u64) {
            return PressureLevel::Degraded;
        }
        PressureLevel::Ok
    }

    /// 降级期间生效的采样最小间隔：放大 `throttle_factor` 倍，但不超过 `throttle_cap_ms`。
    /// 结果永远不会低于 `base_ms`——降级只降低 keep 速率，绝不提高。
    pub fn throttled_min_interval_ms(&self, base_ms: i64) -> i64 {
        base_ms
            .saturating_mul(self.throttle_factor as i64)
            .min(self.throttle_cap_ms)
            .max(base_ms)
    }
}

/// 描述符之后三条有界队列的瞬时水位。
///
/// 用结构体而不是一串位置参数：调用点把"条数"和"字节"写反是很容易犯的错，
/// 而这两个数的量级差着六个数量级，写反了不会报错，只会静默给出错误的压力。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct QueueWatermarks {
    /// 保留表深度（条数）与上限。消费者慢下来时它是第一道墙。
    pub retained: u64,
    pub retained_limit: u64,
    /// 占用最多的那个 buffer 种类的槽位数。保留表按种类对半分配（见
    /// [`crate::handoff::retained_kind_limit`]），所以"总深度还没触顶"不等于
    /// "某一类还进得来"：音频块 47 Hz、视频 keep 只有几 Hz，实测里正是音频先把
    /// 自己那一半顶满。压力取这三条里更紧的一条。
    pub retained_kind: u64,
    /// 共享段已用字节与容量。它常常比保留表先触顶：一帧 720p RGBA 就是 3.6 MB。
    pub arena_used_bytes: u64,
    pub arena_capacity_bytes: u64,
}

impl QueueWatermarks {
    /// 压力取**更紧的那条**：三条队列都在同一条路径上，任何一条触顶都意味着新的样本
    /// 交接不出去。
    pub fn level(&self, policy: &BackpressurePolicy) -> PressureLevel {
        let kind_limit = crate::handoff::retained_kind_limit(self.retained_limit as usize) as u64;
        policy
            .level(self.retained, self.retained_limit)
            .max(policy.level(self.retained_kind, kind_limit))
            .max(policy.level(self.arena_used_bytes, self.arena_capacity_bytes))
    }
}

/// 一次运行内的压力等级迁移与阶段一降级计数。
///
/// 它**不**复制队列的容量、水位与峰值：那些是 [`HandoffStats`] 的真实测量值，
/// 由 [`BackpressureTracker::report`] 在结束时一并装配，避免两份数字慢慢漂移。
#[derive(Debug, Clone)]
pub struct BackpressureTracker {
    policy: BackpressurePolicy,
    level: PressureLevel,
    degraded_entries: u64,
    saturated_entries: u64,
    throttled_samples: u64,
    /// 是否观测过一条有界队列。为 `false` 时报告里的其余数字没有意义。
    observed: bool,
    /// 观测到过的最大深度/字节，仅用于自检：全为 0 时说明跟踪器从未被喂过数据。
    max_observed_depth: u64,
    max_observed_arena_bytes: u64,
}

impl Default for BackpressureTracker {
    fn default() -> Self {
        Self::new(BackpressurePolicy::default())
    }
}

impl BackpressureTracker {
    pub fn new(policy: BackpressurePolicy) -> Self {
        Self {
            policy,
            level: PressureLevel::Ok,
            degraded_entries: 0,
            saturated_entries: 0,
            throttled_samples: 0,
            observed: false,
            max_observed_depth: 0,
            max_observed_arena_bytes: 0,
        }
    }

    pub fn policy(&self) -> BackpressurePolicy {
        self.policy
    }

    pub fn level(&self) -> PressureLevel {
        self.level
    }

    /// 采样器是否需要降速。等价于"当前等级不是 `ok`"。
    pub fn throttle_requested(&self) -> bool {
        self.level.is_pressured()
    }

    pub fn throttled_min_interval_ms(&self, base_ms: i64) -> i64 {
        self.policy.throttled_min_interval_ms(base_ms)
    }

    /// 记录一次队列观测。等级迁移在此发生，并且每次迁移都计数（不是只记结束时的那一个）。
    /// 一次调用只可能产生一次迁移，哪怕两条队列同时越过阈值。
    pub fn observe_queue(&mut self, watermarks: QueueWatermarks) -> PressureLevel {
        self.observed = true;
        self.max_observed_depth = self.max_observed_depth.max(watermarks.retained);
        self.max_observed_arena_bytes = self
            .max_observed_arena_bytes
            .max(watermarks.arena_used_bytes);
        let next = watermarks.level(&self.policy);
        if next != self.level {
            match next {
                PressureLevel::Ok => {}
                PressureLevel::Degraded => self.degraded_entries += 1,
                PressureLevel::Saturated => self.saturated_entries += 1,
            }
            self.level = next;
        }
        self.level
    }

    /// 记录一次"因为背压被采样器抑制的 keep"。
    pub fn record_throttled_keep(&mut self) {
        self.throttled_samples += 1;
    }

    pub fn throttled_samples(&self) -> u64 {
        self.throttled_samples
    }

    pub fn observed(&self) -> bool {
        self.observed
    }

    /// 装配报告。`stats` 为 `None` 表示本次运行没有保留队列（例如未暴露数据面的 replay）：
    /// 报告会显式写 `observed = false`，而不是写一组"看起来很干净"的零。
    pub fn report(
        &self,
        stats: Option<&HandoffStats>,
        drop_kinds: &BTreeMap<String, u64>,
    ) -> media::BackpressureReport {
        let mut report = media::BackpressureReport {
            observed: self.observed && stats.is_some(),
            state: self.level.name().to_string(),
            degraded_entries: self.degraded_entries,
            saturated_entries: self.saturated_entries,
            sampling_throttled_samples: self.throttled_samples,
            throttle_factor: self.policy.throttle_factor as u64,
            ..Default::default()
        };
        let Some(stats) = stats else {
            return report;
        };
        report.queues = vec![
            media::BackpressureQueue {
                name: "handoff_retained_table".to_string(),
                unit: "items".to_string(),
                capacity: stats.retained_limit,
                current: stats.retained,
                peak: stats.retained_peak,
            },
            // 按种类分配的那条上限单独成一条队列：只报总深度会让人以为"表还有富余"，
            // 而真实情况常常是某一类已经顶到自己那一半了。
            media::BackpressureQueue {
                name: "handoff_retained_kind".to_string(),
                unit: "items".to_string(),
                capacity: stats.retained_kind_limit,
                current: stats.retained_by_kind.values().copied().max().unwrap_or(0),
                peak: stats.retained_kind_peak,
            },
            media::BackpressureQueue {
                name: "handoff_arena_bytes".to_string(),
                unit: "bytes".to_string(),
                capacity: stats.arena_capacity_bytes,
                current: stats.arena_used_bytes,
                peak: stats.arena_peak_bytes,
            },
        ];
        report.dropped_total = stats.retain_rejections;
        report.drop_reasons = drop_reasons(&stats.retain_rejection_reasons);
        report.drop_kinds = drop_kinds
            .iter()
            .map(|(kind, count)| media::BackpressureDropKind {
                kind: kind.clone(),
                count: *count,
            })
            .collect();
        report.timeouts_total = stats.expired_total;
        report.residency_samples = stats.residency_samples;
        report.residency_max_ms = stats.residency_max_ms;
        // 没有样本就没有平均值：宁可留 0，并让 `residency_samples = 0` 说明它，
        // 也不编一个"看起来正常"的数。
        report.residency_avg_ms = stats
            .residency_total_ms
            .checked_div(stats.residency_samples)
            .unwrap_or(0);
        report
    }
}

/// 原因表按原因名排序输出，保证同样的运行产出同样的字节。
fn drop_reasons(reasons: &BTreeMap<String, u64>) -> Vec<media::BackpressureDropReason> {
    reasons
        .iter()
        .map(|(reason, count)| media::BackpressureDropReason {
            reason: reason.clone(),
            count: *count,
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 只给保留表水位：arena 给一个永远不可能触顶的容量，把它的影响排除掉；
    /// 种类水位也置 0，让这一条不影响断言。
    fn table(depth: u64, limit: u64) -> QueueWatermarks {
        QueueWatermarks {
            retained: depth,
            retained_limit: limit,
            retained_kind: 0,
            arena_used_bytes: 0,
            arena_capacity_bytes: u64::MAX,
        }
    }

    #[test]
    fn policy_rejects_configurations_that_would_hide_pressure() {
        assert!(
            BackpressurePolicy::new(0, 4, 10_000).is_err(),
            "0% is not a threshold"
        );
        assert!(
            BackpressurePolicy::new(100, 4, 10_000).is_err(),
            "100% collapses the levels"
        );
        assert!(
            BackpressurePolicy::new(50, 0, 10_000).is_err(),
            "factor 0 is a division trap"
        );
        assert!(
            BackpressurePolicy::new(50, 65, 10_000).is_err(),
            "factor beyond the cap"
        );
        assert!(
            BackpressurePolicy::new(50, 4, 10).is_err(),
            "cap below the sampling floor"
        );
        assert!(BackpressurePolicy::new(50, 4, 10_000).is_ok());
    }

    #[test]
    fn levels_follow_depth_and_never_round_down_to_ok() {
        let policy = BackpressurePolicy::default();
        assert_eq!(policy.level(0, 8), PressureLevel::Ok);
        assert_eq!(policy.level(3, 8), PressureLevel::Ok, "3/8 is 37%");
        assert_eq!(
            policy.level(4, 8),
            PressureLevel::Degraded,
            "4/8 is exactly 50%"
        );
        assert_eq!(policy.level(7, 8), PressureLevel::Degraded);
        assert_eq!(policy.level(8, 8), PressureLevel::Saturated);
        assert_eq!(policy.level(9, 8), PressureLevel::Saturated);
        // 没有容量的队列不能接受工作，必须自报饱和而不是"恰好有空位"。
        assert_eq!(policy.level(0, 0), PressureLevel::Saturated);
    }

    #[test]
    fn throttling_only_lowers_the_keep_rate() {
        let policy = BackpressurePolicy::default();
        assert_eq!(policy.throttled_min_interval_ms(1_000), 4_000);
        // 上限生效：60 秒的间隔不会被放大到 240 秒；上限比基准还短时以基准为准，
        // 因为降级只能降低 keep 速率，不能反向把它提上去。
        assert_eq!(policy.throttled_min_interval_ms(60_000), 60_000);
        // 上限比基准长、比放大结果短时，上限说了算。
        assert_eq!(policy.throttled_min_interval_ms(9_000), 10_000);
        let no_op = BackpressurePolicy::new(50, 1, 10_000).unwrap();
        assert_eq!(no_op.throttled_min_interval_ms(1_000), 1_000);
    }

    #[test]
    fn every_entry_into_a_level_is_counted_not_only_the_last_one() {
        let mut tracker = BackpressureTracker::default();
        assert_eq!(tracker.observe_queue(table(0, 8)), PressureLevel::Ok);
        assert_eq!(tracker.observe_queue(table(4, 8)), PressureLevel::Degraded);
        assert_eq!(tracker.observe_queue(table(8, 8)), PressureLevel::Saturated);
        assert_eq!(tracker.observe_queue(table(1, 8)), PressureLevel::Ok);
        assert_eq!(tracker.observe_queue(table(6, 8)), PressureLevel::Degraded);
        assert_eq!(tracker.degraded_entries, 2, "two separate entries");
        assert_eq!(tracker.saturated_entries, 1);
        assert!(tracker.throttle_requested(), "the run ended degraded");
    }

    #[test]
    fn the_tighter_of_the_two_queues_decides_and_a_burst_cannot_double_count() {
        let mut tracker = BackpressureTracker::default();
        // 保留表 6/8 是降级，但 arena 已经用满：压力取更紧的那条。
        let burst = QueueWatermarks {
            retained: 6,
            retained_limit: 8,
            retained_kind: 0,
            arena_used_bytes: 1_024,
            arena_capacity_bytes: 1_024,
        };
        assert_eq!(tracker.observe_queue(burst), PressureLevel::Saturated);
        assert_eq!(tracker.saturated_entries, 1, "one observation, one entry");
        assert_eq!(
            tracker.degraded_entries, 0,
            "the looser queue does not add an entry"
        );
        // 同一时刻的另一条队列最多也只能算同一级，不再产生第二次迁移。
        assert_eq!(tracker.observe_queue(burst), PressureLevel::Saturated);
        assert_eq!(tracker.saturated_entries, 1);
    }

    #[test]
    fn an_empty_table_can_still_be_saturated_by_the_arena() {
        let mut tracker = BackpressureTracker::default();
        let watermarks = QueueWatermarks {
            retained: 0,
            retained_limit: 32,
            retained_kind: 0,
            arena_used_bytes: 64 * 1024 * 1024,
            arena_capacity_bytes: 64 * 1024 * 1024,
        };
        assert_eq!(tracker.observe_queue(watermarks), PressureLevel::Saturated);
        assert!(
            tracker.throttle_requested(),
            "the default-retained-limit case must still throttle: the arena fills first"
        );
    }

    #[test]
    fn a_run_without_a_retention_queue_reports_unobserved_not_clean() {
        let tracker = BackpressureTracker::default();
        let report = tracker.report(None, &BTreeMap::new());
        assert!(
            !report.observed,
            "no queue means no measurement, not zero pressure"
        );
        assert_eq!(report.state, "ok");
        assert!(report.queues.is_empty());
        let mut tracker = BackpressureTracker::default();
        tracker.observe_queue(table(2, 8));
        assert!(
            !tracker.report(None, &BTreeMap::new()).observed,
            "observing watermarks is not the same as having a queue to describe"
        );
    }

    #[test]
    fn dropped_total_equals_the_sum_of_its_reasons() {
        let mut tracker = BackpressureTracker::default();
        tracker.observe_queue(table(8, 8));
        let stats = HandoffStats {
            retained_limit: 8,
            retained: 8,
            retained_peak: 8,
            retained_kind_limit: 4,
            retained_by_kind: BTreeMap::from([("video_frame".to_string(), 5)]),
            retained_kind_peak: 5,
            retain_rejections: 3,
            retain_rejection_reasons: BTreeMap::from([
                ("handoff_backlog_full".to_string(), 2),
                ("arena_capacity_exceeded".to_string(), 1),
            ]),
            expired_total: 1,
            residency_samples: 1,
            residency_total_ms: 250,
            residency_max_ms: 250,
            arena_capacity_bytes: 1_024,
            arena_used_bytes: 512,
            arena_peak_bytes: 1_024,
            ..Default::default()
        };
        // 队列是所有 buffer 种类共用的：只有按种类拆开，总数才能被读成"谁被挡住了"。
        let drop_kinds =
            BTreeMap::from([("video_frame".to_string(), 2), ("audio_pcm".to_string(), 1)]);
        let report = tracker.report(Some(&stats), &drop_kinds);
        assert!(report.observed);
        assert_eq!(report.state, "saturated");
        let summed: u64 = report.drop_reasons.iter().map(|entry| entry.count).sum();
        assert_eq!(summed, report.dropped_total);
        let summed_kinds: u64 = report.drop_kinds.iter().map(|entry| entry.count).sum();
        assert_eq!(
            summed_kinds, report.dropped_total,
            "the kind breakdown must not lose anyone"
        );
        let kinds: BTreeMap<&str, u64> = report
            .drop_kinds
            .iter()
            .map(|entry| (entry.kind.as_str(), entry.count))
            .collect();
        assert_eq!(
            kinds,
            BTreeMap::from([("audio_pcm", 1), ("video_frame", 2)]),
            "the breakdown must name who was refused"
        );
        assert_eq!(report.dropped_total, 3);
        assert_eq!(report.timeouts_total, 1);
        assert_eq!(report.residency_avg_ms, 250);
        let table = &report.queues[0];
        assert_eq!(
            (
                table.capacity,
                table.current,
                table.peak,
                table.unit.as_str()
            ),
            (8, 8, 8, "items")
        );
        // 按种类的那条上限单独成一条队列：总深度 8/8 与种类 5/4 是两条不同的约束。
        let kind = &report.queues[1];
        assert_eq!(
            (
                kind.name.as_str(),
                kind.capacity,
                kind.current,
                kind.peak,
                kind.unit.as_str()
            ),
            ("handoff_retained_kind", 4, 5, 5, "items")
        );
        let arena = &report.queues[2];
        assert_eq!(
            (arena.capacity, arena.peak, arena.unit.as_str()),
            (1_024, 1_024, "bytes")
        );
    }

    #[test]
    fn residency_average_has_no_sample_to_average_over() {
        let mut tracker = BackpressureTracker::default();
        tracker.observe_queue(table(0, 8));
        let stats = HandoffStats {
            retained_limit: 8,
            arena_capacity_bytes: 1_024,
            ..Default::default()
        };
        let report = tracker.report(Some(&stats), &BTreeMap::new());
        assert_eq!(report.residency_samples, 0);
        assert_eq!(
            report.residency_avg_ms, 0,
            "no sample, never a fabricated average"
        );
    }
}
