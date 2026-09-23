//! 实时接入（SRT）的公共语义：窗口、断流测量与恢复证据。
//!
//! 这里刻意不依赖 GStreamer：`StallTracker` 是纯计数逻辑，可以直接单测。
//! 真正的 pipeline 与样本拉取在 `decode::decode_live`（`gstreamer` feature）。
//!
//! 与 AGENTS.md 的关系：
//! - 断流是**正常事件**，但必须有计数、有原因、有恢复证据，不能悄悄吞掉；
//! - 重试与并发都要有上限：卡顿次数超过预算即显式失败，而不是无限等待；
//! - 没有样本的窗口绝不能报成"成功但为空"，那是合成业务成功数据。

use crate::MediaError;

/// 一次 ingest 的默认窗口长度。
pub const DEFAULT_LIVE_DURATION_MS: u64 = 20_000;
pub const MIN_LIVE_DURATION_MS: u64 = 1_000;
/// 上限只为了不让一次运行无限期占着数据面；超出即拒绝启动，不做夹取。
pub const MAX_LIVE_DURATION_MS: u64 = 900_000;

/// 多久没有样本算一次卡顿/断流。
pub const DEFAULT_STALL_THRESHOLD_MS: u64 = 1_000;
pub const MIN_STALL_THRESHOLD_MS: u64 = 100;
pub const MAX_STALL_THRESHOLD_MS: u64 = 60_000;

/// 卡顿次数预算：超过就失败（不无限重试）。
pub const DEFAULT_MAX_STALLS: u32 = 8;
pub const MAX_MAX_STALLS: u32 = 1_000;

/// 报告里保留的明细条数上限；`stalls` 字段仍然是总数。
pub const MAX_STALL_EVENTS: usize = 16;

/// 恢复后的媒体时间与断流前媒体时间终点都未知时的占位值。
pub const UNKNOWN_PTS_JUMP_MS: i64 = -1;

#[derive(Debug, Clone, Copy)]
pub struct LiveConfig {
    /// ingest 窗口长度（墙钟）。
    pub duration_ms: u64,
    /// 超过这个时长没有样本即视为一次卡顿/断流。
    pub stall_threshold_ms: u64,
    /// 卡顿次数预算。
    pub max_stalls: u32,
}

impl Default for LiveConfig {
    fn default() -> Self {
        Self {
            duration_ms: DEFAULT_LIVE_DURATION_MS,
            stall_threshold_ms: DEFAULT_STALL_THRESHOLD_MS,
            max_stalls: DEFAULT_MAX_STALLS,
        }
    }
}

impl LiveConfig {
    /// 参数越界即拒绝，不夹取成"看起来合理"的值。
    pub fn validate(&self) -> Result<(), MediaError> {
        if !(MIN_LIVE_DURATION_MS..=MAX_LIVE_DURATION_MS).contains(&self.duration_ms) {
            return Err(MediaError::IoFailed(format!(
                "live_duration_ms_out_of_range: {}",
                self.duration_ms
            )));
        }
        if !(MIN_STALL_THRESHOLD_MS..=MAX_STALL_THRESHOLD_MS).contains(&self.stall_threshold_ms) {
            return Err(MediaError::IoFailed(format!(
                "stall_threshold_ms_out_of_range: {}",
                self.stall_threshold_ms
            )));
        }
        if self.max_stalls == 0 || self.max_stalls > MAX_MAX_STALLS {
            return Err(MediaError::IoFailed(format!(
                "max_stalls_out_of_range: {}",
                self.max_stalls
            )));
        }
        Ok(())
    }
}

/// 一次断流：从"超阈值没样本"到"样本恢复"（或窗口结束）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StreamStall {
    pub started_ms: u64,
    pub ended_ms: u64,
    pub gap_ms: u64,
    /// 恢复后首个样本的媒体时间减去断流前该流的媒体时间终点；两处任一未知即 `-1`。
    pub pts_jump_ms: i64,
    pub reason: String,
}

impl StreamStall {
    /// 窗口结束时仍处于断流状态的记录：`ended_ms` 就是窗口结束时间。
    pub const REASON_GAP: &'static str = "stream_gap";
    pub const REASON_WINDOW_ENDED: &'static str = "stream_gap_at_window_end";
}

/// ingest 窗口内的实时统计。全部是计数与测量值，没有估计值。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LiveStats {
    pub requested_duration_ms: u64,
    pub elapsed_ms: u64,
    pub samples: u64,
    pub stalls: u64,
    pub stalled_ms: u64,
    pub max_stall_ms: u64,
    pub pts_gap_total_ms: i64,
    pub recovered: bool,
    pub ended_by_deadline: bool,
    /// 断流前该流的媒体时间终点（`None` = 还没观察到任何样本）。
    pub last_media_end_ms: Option<i64>,
    /// 有界的明细列表。
    pub stall_events: Vec<StreamStall>,
}

impl LiveStats {
    /// 本次窗口是否产出过任何样本。没有样本的窗口不是一个成功的 ingest。
    pub fn has_samples(&self) -> bool {
        self.samples > 0
    }
}

#[derive(Debug)]
struct OpenStall {
    started_ms: u64,
    /// 进入断流时该流的媒体时间终点，用来算恢复后的媒体时间缺口。
    media_end_before_ms: Option<i64>,
}

/// 断流/恢复的纯计数状态机。
///
/// 调用方在"这一轮有没有拿到样本"时二选一调用 `on_progress` / `on_idle`，
/// 并传入当前的墙钟偏移与媒体时间；所有判定都在这里完成，便于单测。
#[derive(Debug)]
pub struct StallTracker {
    threshold_ms: u64,
    max_stalls: u32,
    open: Option<OpenStall>,
    /// 最后一次拿到样本的墙钟偏移。断流的起点是它，而不是"发现超阈值的那一刻"：
    /// 否则 gap 会把阈值本身也算成断流时间。
    last_progress_ms: u64,
    stats: LiveStats,
}

impl StallTracker {
    pub fn new(config: &LiveConfig) -> Self {
        Self {
            threshold_ms: config.stall_threshold_ms,
            max_stalls: config.max_stalls,
            open: None,
            last_progress_ms: 0,
            stats: LiveStats {
                requested_duration_ms: config.duration_ms,
                ..Default::default()
            },
        }
    }

    /// 本轮拿到了样本。
    ///
    /// `samples_in_round` 是本轮交给数据平面的样本数（不是"轮数"），
    /// `first_sample_pts_ms` 是恢复后第一个样本的媒体时间（没有则 `None`），
    /// `media_end_ms` 是当前该流的媒体时间终点。
    pub fn on_progress(
        &mut self,
        now_ms: u64,
        samples_in_round: u64,
        first_sample_pts_ms: Option<i64>,
        media_end_ms: Option<i64>,
    ) {
        self.stats.samples += samples_in_round;
        self.last_progress_ms = now_ms;
        if let Some(open) = self.open.take() {
            let pts_jump_ms = match (open.media_end_before_ms, first_sample_pts_ms) {
                (Some(before), Some(first)) => first - before,
                _ => UNKNOWN_PTS_JUMP_MS,
            };
            self.close_stall(
                open.started_ms,
                now_ms,
                pts_jump_ms,
                StreamStall::REASON_GAP,
            );
            self.stats.recovered = true;
        }
        self.stats.last_media_end_ms = media_end_ms.or(self.stats.last_media_end_ms);
    }

    /// 本轮没有任何样本。`media_end_ms` 用于在进入断流时记住媒体时间终点。
    pub fn on_idle(&mut self, now_ms: u64, media_end_ms: Option<i64>) -> Result<(), MediaError> {
        if self.open.is_none() {
            if now_ms.saturating_sub(self.last_progress_ms) < self.threshold_ms {
                return Ok(());
            }
            self.open = Some(OpenStall {
                started_ms: self.last_progress_ms,
                media_end_before_ms: media_end_ms.or(self.stats.last_media_end_ms),
            });
        }
        if let Some(open) = &self.open {
            let gap_ms = now_ms.saturating_sub(open.started_ms);
            if gap_ms >= self.threshold_ms && self.stats.stalls + 1 > u64::from(self.max_stalls) {
                return Err(MediaError::DecodeFailed(format!(
                    "live_stall_budget_exceeded: {} stalls over {}",
                    self.stats.stalls + 1,
                    self.max_stalls
                )));
            }
        }
        Ok(())
    }

    fn close_stall(&mut self, started_ms: u64, ended_ms: u64, pts_jump_ms: i64, reason: &str) {
        let gap_ms = ended_ms.saturating_sub(started_ms);
        self.stats.stalls += 1;
        self.stats.stalled_ms += gap_ms;
        self.stats.max_stall_ms = self.stats.max_stall_ms.max(gap_ms);
        if pts_jump_ms > 0 {
            self.stats.pts_gap_total_ms += pts_jump_ms;
        }
        if self.stats.stall_events.len() < MAX_STALL_EVENTS {
            self.stats.stall_events.push(StreamStall {
                started_ms,
                ended_ms,
                gap_ms,
                pts_jump_ms,
                reason: reason.to_string(),
            });
        }
    }

    /// 结束窗口。仍在断流中的话，最后一条记录以窗口结束时间收尾并标注原因，
    /// 不会因为"还没恢复"就不计。
    pub fn finish(mut self, elapsed_ms: u64, ended_by_deadline: bool) -> LiveStats {
        if let Some(open) = self.open.take() {
            self.close_stall(
                open.started_ms,
                elapsed_ms,
                UNKNOWN_PTS_JUMP_MS,
                StreamStall::REASON_WINDOW_ENDED,
            );
        }
        self.stats.elapsed_ms = elapsed_ms;
        self.stats.ended_by_deadline = ended_by_deadline;
        self.stats
    }

    pub fn stats(&self) -> &LiveStats {
        &self.stats
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(threshold_ms: u64, max_stalls: u32) -> LiveConfig {
        LiveConfig {
            duration_ms: 10_000,
            stall_threshold_ms: threshold_ms,
            max_stalls,
        }
    }

    #[test]
    fn rejects_out_of_range_parameters_instead_of_clamping() {
        assert!(LiveConfig {
            duration_ms: 0,
            ..Default::default()
        }
        .validate()
        .is_err());
        assert!(LiveConfig {
            stall_threshold_ms: 10,
            ..Default::default()
        }
        .validate()
        .is_err());
        assert!(LiveConfig {
            max_stalls: 0,
            ..Default::default()
        }
        .validate()
        .is_err());
        assert!(LiveConfig::default().validate().is_ok());
    }

    #[test]
    fn idle_below_threshold_is_not_a_stall() {
        let mut tracker = StallTracker::new(&config(1_000, 4));
        tracker.on_progress(10, 3, Some(0), Some(40));
        tracker.on_idle(500, Some(40)).unwrap();
        tracker.on_idle(1_009, Some(40)).unwrap();
        assert!(
            tracker.open.is_none(),
            "999 ms of silence is below the threshold"
        );
        tracker.on_idle(1_010, Some(40)).unwrap();
        // 1_010 ms：距最后一个样本正好 1_000 ms，断流从那一刻起算。
        assert_eq!(tracker.stats().stalls, 0, "still open, not yet closed");
        let stall = tracker.open.as_ref().expect("stall opened");
        assert_eq!(stall.started_ms, 10);
    }

    #[test]
    fn recovery_records_wall_gap_and_media_jump_separately() {
        let mut tracker = StallTracker::new(&config(1_000, 4));
        tracker.on_progress(0, 25, Some(0), Some(1_000));
        tracker.on_idle(1_000, Some(1_000)).unwrap();
        assert_eq!(
            tracker.stats().stalls,
            0,
            "no sample arrived, the stall is still open"
        );
        // 4.5 s 时样本恢复，媒体时间从 1_000 跳到 6_000。
        tracker.on_progress(4_500, 30, Some(6_000), Some(6_040));
        assert_eq!(
            tracker.stats().stalls,
            1,
            "recovery closes the stall with a counted gap"
        );
        let stats = tracker.finish(5_000, true);
        assert_eq!(stats.stalls, 1);
        assert_eq!(stats.stall_events.len(), 1);
        let event = &stats.stall_events[0];
        // 最后一个样本在 0 ms，恢复在 4_500 ms：墙钟缺口就是 4_500 ms。
        assert_eq!(event.gap_ms, 4_500);
        assert_eq!(event.pts_jump_ms, 5_000);
        assert_eq!(stats.stalled_ms, 4_500);
        assert_eq!(stats.pts_gap_total_ms, 5_000);
        assert!(stats.recovered);
    }

    #[test]
    fn stalled_window_end_is_counted_not_dropped() {
        let mut tracker = StallTracker::new(&config(500, 4));
        tracker.on_progress(0, 12, Some(0), Some(100));
        tracker.on_idle(600, Some(100)).unwrap();
        let stats = tracker.finish(2_000, true);
        assert_eq!(stats.stalls, 1);
        assert_eq!(stats.stall_events[0].ended_ms, 2_000);
        assert_eq!(
            stats.stall_events[0].reason,
            StreamStall::REASON_WINDOW_ENDED
        );
        assert!(!stats.recovered);
        assert_eq!(stats.stall_events[0].pts_jump_ms, UNKNOWN_PTS_JUMP_MS);
    }

    #[test]
    fn unknown_media_time_stays_unknown_instead_of_zero() {
        let mut tracker = StallTracker::new(&config(500, 4));
        tracker.on_progress(0, 5, None, None);
        tracker.on_idle(600, None).unwrap();
        tracker.on_progress(2_000, 5, None, None);
        let stats = tracker.finish(2_500, true);
        assert_eq!(stats.stall_events[0].pts_jump_ms, UNKNOWN_PTS_JUMP_MS);
        assert_eq!(stats.pts_gap_total_ms, 0);
        assert_eq!(stats.last_media_end_ms, None);
    }

    #[test]
    fn stall_budget_is_enforced_instead_of_retrying_forever() {
        let mut tracker = StallTracker::new(&config(100, 1));
        tracker.on_progress(0, 8, Some(0), Some(10));
        tracker.on_idle(200, Some(10)).unwrap();
        tracker.on_progress(300, 8, Some(20), Some(30));
        // 预算只允许一次卡顿；第二次进入卡顿就必须显式失败，而不是继续等下去。
        let error = tracker
            .on_idle(500, Some(30))
            .expect_err("a second stall must exceed the budget");
        assert!(error.to_string().contains("live_stall_budget_exceeded"));
    }

    #[test]
    fn only_bounded_event_details_are_kept_but_the_count_stays() {
        let mut tracker = StallTracker::new(&config(100, u32::MAX));
        tracker.on_progress(0, 1, Some(0), Some(0));
        for round in 0..(MAX_STALL_EVENTS + 5) {
            let base = 200 + round as u64 * 1_000;
            tracker.on_idle(base, Some(0)).unwrap();
            tracker.on_progress(base + 500, 1, Some(10), Some(10));
        }
        let stats = tracker.finish(100_000, true);
        assert_eq!(stats.stalls as usize, MAX_STALL_EVENTS + 5);
        assert_eq!(stats.stall_events.len(), MAX_STALL_EVENTS);
        assert_eq!(stats.samples, MAX_STALL_EVENTS as u64 + 6);
    }
}
