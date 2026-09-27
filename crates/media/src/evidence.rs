//! 全帧判别与事件证据窗口选择（语义覆盖 v1）。
//!
//! 这一层解决的是"采样密度"而不是"抽帧速率"：每个被解码且可用的视频帧都必须先被
//! 判别一次，再决定它是**基线复查锚点**、**事件窗口的一部分**，还是**已被相邻证据覆盖**。
//! 因此：
//!
//! - 输入完整性：每一帧都在逐帧账本里恰好出现一次，结论只能是选中或带原因未选中；
//! - 语义覆盖：静态画面的相邻语义输入不超过 `max_semantic_gap_ms`；变化画面由事件窗口
//!   加上前后有界上下文帧覆盖，窗口的触发原因、锚点与帧引用全部可回放；
//! - 有界性：事件窗口数量、事件触发频率与预上下文帧都有硬上限，超限是显式计数/拒绝，
//!   绝不静默降级成"没有变化"。
//!
//! 时间轴口径与工程约定一致：所有区间都是同一 stream 上的 `[start_ms, end_ms)`。

use std::collections::{BTreeMap, VecDeque};
use std::sync::Arc;

use crate::sampler::FrameSignature;
use crate::MediaError;

/// 静态画面的默认语义复查间隔。用户明确选择"变化触发 + 时间上限，上限 1 秒"。
pub const DEFAULT_MAX_SEMANTIC_GAP_MS: i64 = 1_000;
pub const MIN_MAX_SEMANTIC_GAP_MS: i64 = 100;
pub const MAX_MAX_SEMANTIC_GAP_MS: i64 = 60_000;
/// 窗口前后上下文帧的默认数量：变化前 2 帧 + 锚点 + 变化后 2 帧 = 5 帧。
pub const DEFAULT_CONTEXT_FRAMES: u32 = 2;
pub const MAX_CONTEXT_FRAMES: u32 = 4;
pub const DEFAULT_CHANGE_THRESHOLD: u32 = 8;
pub const DEFAULT_TEXT_CHANGE_THRESHOLD: u32 = 12;
/// 两个事件窗口之间的最小间隔。它保证"每一帧都在变"的流不会退化成逐帧调用模型。
pub const DEFAULT_MIN_EVENT_INTERVAL_MS: i64 = 100;
pub const MIN_EVENT_INTERVAL_MS: i64 = 20;
pub const MAX_EVENT_INTERVAL_MS: i64 = 5_000;
/// 单次运行的事件窗口硬上限。超出即显式失败，不做"只保留前 N 个"。
pub const MAX_WINDOWS_PER_RUN: u64 = 200_000;
/// 报告里窗口预览的条数上限；完整窗口表写进逐帧账本 artifact。
pub const MAX_LISTED_WINDOWS: usize = 256;

/// 这一帧被判别成什么。判别对所有可用帧执行，未选中的帧同样有结论。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FrameDecision {
    FirstFrame,
    ContentChange,
    TextChange,
    StaticHeartbeat,
    NoChange,
}

impl FrameDecision {
    pub fn name(self) -> &'static str {
        match self {
            Self::FirstFrame => "first_frame",
            Self::ContentChange => "content_change",
            Self::TextChange => "text_change",
            Self::StaticHeartbeat => "static_heartbeat",
            Self::NoChange => "no_change",
        }
    }
}

/// 这一帧在证据计划里的角色。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FrameSelection {
    BaselineAnchor,
    EventAnchor,
    EventPreContext,
    EventPostContext,
    NotSelected,
}

impl FrameSelection {
    pub fn name(self) -> &'static str {
        match self {
            Self::BaselineAnchor => "baseline_anchor",
            Self::EventAnchor => "event_anchor",
            Self::EventPreContext => "event_pre_context",
            Self::EventPostContext => "event_post_context",
            Self::NotSelected => "not_selected",
        }
    }
}

/// 未选中帧的稳定原因码。它必须能解释"为什么这一帧不进模型"。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CoverageSkipReason {
    /// 变化还没发生，而且距离上一个语义锚点还没到复查间隔。
    NoChangeYet,
    /// 变化发生了，但距离上一个事件窗口不足 `min_event_interval_ms`。
    EventRateLimited,
    /// 变化发生了，但本次运行的事件窗口预算已经用尽。
    EventBudgetExhausted,
    /// 帧布局不可用（非 RGBA 或几何未知），无法判别。
    MissingSignature,
    /// PTS 回退：这一帧不能进入单调的证据计划。
    NonMonotonic,
}

impl CoverageSkipReason {
    pub fn name(self) -> &'static str {
        match self {
            Self::NoChangeYet => "no_change_yet",
            Self::EventRateLimited => "event_rate_limited",
            Self::EventBudgetExhausted => "event_budget_exhausted",
            Self::MissingSignature => "missing_signature",
            Self::NonMonotonic => "non_monotonic_pts",
        }
    }
}

/// 语义覆盖策略。所有数值都来自不可变 Revision，越界即拒绝，不夹取。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EvidencePolicy {
    pub max_semantic_gap_ms: i64,
    pub pre_frames: u32,
    pub post_frames: u32,
    pub change_threshold: u32,
    pub text_change_threshold: u32,
    pub min_event_interval_ms: i64,
}

impl Default for EvidencePolicy {
    fn default() -> Self {
        Self {
            max_semantic_gap_ms: DEFAULT_MAX_SEMANTIC_GAP_MS,
            pre_frames: DEFAULT_CONTEXT_FRAMES,
            post_frames: DEFAULT_CONTEXT_FRAMES,
            change_threshold: DEFAULT_CHANGE_THRESHOLD,
            text_change_threshold: DEFAULT_TEXT_CHANGE_THRESHOLD,
            min_event_interval_ms: DEFAULT_MIN_EVENT_INTERVAL_MS,
        }
    }
}

impl EvidencePolicy {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        max_semantic_gap_ms: i64,
        pre_frames: u32,
        post_frames: u32,
        change_threshold: u32,
        text_change_threshold: u32,
        min_event_interval_ms: i64,
    ) -> Result<Self, MediaError> {
        if !(MIN_MAX_SEMANTIC_GAP_MS..=MAX_MAX_SEMANTIC_GAP_MS).contains(&max_semantic_gap_ms) {
            return Err(MediaError::SamplingRejected(
                "max_semantic_gap_ms_out_of_range".into(),
            ));
        }
        if pre_frames > MAX_CONTEXT_FRAMES || post_frames > MAX_CONTEXT_FRAMES {
            return Err(MediaError::SamplingRejected(
                "evidence_context_frames_out_of_range".into(),
            ));
        }
        if change_threshold == 0 || change_threshold > 128 {
            return Err(MediaError::SamplingRejected(
                "change_threshold_out_of_range".into(),
            ));
        }
        if text_change_threshold == 0 || text_change_threshold > 255 {
            return Err(MediaError::SamplingRejected(
                "text_change_threshold_out_of_range".into(),
            ));
        }
        if !(MIN_EVENT_INTERVAL_MS..=MAX_EVENT_INTERVAL_MS).contains(&min_event_interval_ms) {
            return Err(MediaError::SamplingRejected(
                "min_event_interval_ms_out_of_range".into(),
            ));
        }
        Ok(Self {
            max_semantic_gap_ms,
            pre_frames,
            post_frames,
            change_threshold,
            text_change_threshold,
            min_event_interval_ms,
        })
    }

    /// 一次运行里最多能开多少个事件窗口（按实际时长估算的硬上限）。
    pub fn max_windows(&self, duration_ms: i64) -> u64 {
        if duration_ms <= 0 {
            return 1;
        }
        (duration_ms / self.min_event_interval_ms) as u64 + 1
    }
}

/// 一个可用视频帧的判别记录。它同时是逐帧账本的一行。
#[derive(Debug, Clone)]
pub struct FrameRecord {
    /// 交接成功时的 descriptor id；为空表示这一帧没有进入数据面。
    pub buffer_id: String,
    pub frame_index: u64,
    pub start_ms: i64,
    pub end_ms: i64,
    pub signature_delta: u32,
    pub text_signature_delta: u32,
    pub signature_available: bool,
    pub decision: FrameDecision,
    pub selection: FrameSelection,
    pub window_id: String,
    /// 未选中（或交接被拒）时的稳定原因码；选中且交接成功时为空。
    pub skip_reason: String,
}

/// 一个事件证据窗口：锚点帧加上有界的前后上下文帧。
#[derive(Debug, Clone)]
pub struct WindowRecord {
    pub window_id: String,
    pub start_ms: i64,
    pub end_ms: i64,
    pub anchor_ms: i64,
    pub trigger: &'static str,
    pub frame_buffer_ids: Vec<String>,
    pub context_before: u32,
    pub context_after: u32,
    pub handed_off_frames: u32,
    /// 这个窗口里**最大**的帧序号。
    ///
    /// 消费方靠它判断"窗口记录出现时，窗口里的每一帧是不是都已经写进账本"：账本是按帧序号
    /// 顺序落盘的，只有 `last_frame_index < 已写出的帧条数` 时才允许写窗口记录，否则消费者
    /// 会看到一个引用着尚未出现的行的窗口。它也是"这一行可以增量消费"的显式依据，
    /// 而不是让消费方去猜窗口什么时候收尾。
    pub last_frame_index: u64,
}

/// 逐帧账本的写入口。Runtime 把它实现成 JSONL 文件；媒体 crate 不依赖 serde。
pub trait FrameLedgerSink: Send + Sync {
    /// 写入一条账本记录（帧或窗口）。失败必须是显式错误，不能吞掉。
    fn write_frame(&self, record: &FrameRecord) -> Result<(), String>;
    fn write_window(&self, record: &WindowRecord) -> Result<(), String>;
}

/// 账本写入口的共享句柄。它让 `DecodeRun` 保持 `Clone`，又不把 JSON 细节带进媒体 crate。
#[derive(Clone)]
pub struct LedgerHandle(Arc<dyn FrameLedgerSink>);

impl LedgerHandle {
    pub fn new(sink: Arc<dyn FrameLedgerSink>) -> Self {
        Self(sink)
    }

    pub fn frame(&self, record: &FrameRecord) -> Result<(), String> {
        self.0.write_frame(record)
    }

    pub fn window(&self, record: &WindowRecord) -> Result<(), String> {
        self.0.write_window(record)
    }
}

impl std::fmt::Debug for LedgerHandle {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("LedgerHandle(<sink>)")
    }
}

/// 语义覆盖的计数器。全部都是"实际发生的事实"，没有一个是估算值。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct EvidenceCounters {
    pub characterized: u64,
    pub baseline_anchor: u64,
    pub event_anchor: u64,
    pub event_pre_context: u64,
    pub event_post_context: u64,
    pub not_selected: u64,
    pub windows: u64,
    pub suppressed_event_keeps: u64,
    pub pre_context_evictions: u64,
    pub max_selected_gap_ms: i64,
}

impl EvidenceCounters {
    pub fn selected(&self) -> u64 {
        self.baseline_anchor + self.event_anchor + self.event_pre_context + self.event_post_context
    }

    pub fn selection_counts(&self) -> Vec<String> {
        [
            (FrameSelection::BaselineAnchor.name(), self.baseline_anchor),
            (FrameSelection::EventAnchor.name(), self.event_anchor),
            (
                FrameSelection::EventPreContext.name(),
                self.event_pre_context,
            ),
            (
                FrameSelection::EventPostContext.name(),
                self.event_post_context,
            ),
            (FrameSelection::NotSelected.name(), self.not_selected),
        ]
        .into_iter()
        .filter(|(_, count)| *count > 0)
        .map(|(name, count)| format!("{name}={count}"))
        .collect()
    }
}

/// 一次 `observe` 的结论。解码会话按它决定是否交接、要不要补交接预上下文帧。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EvidenceStep {
    /// 这一帧在判别计划里的序号，调用方用它回填交接结果。
    pub frame_index: u64,
    pub decision: FrameDecision,
    /// 这一帧此刻是否必须交接。
    pub retain_now: bool,
    pub selection: FrameSelection,
    /// 非空表示这一帧属于某个事件窗口。
    pub window_id: String,
    /// 需要在锚点之前补交接的预上下文帧序号（按时间顺序，来自解码会话的保留环）。
    pub pre_context_indexes: Vec<u64>,
    /// 未选中或交接被拒时写进账本的原因码。
    pub skip_reason: String,
}

impl EvidenceStep {
    fn unselected(frame_index: u64, decision: FrameDecision, reason: CoverageSkipReason) -> Self {
        Self {
            frame_index,
            decision,
            retain_now: false,
            selection: FrameSelection::NotSelected,
            window_id: String::new(),
            pre_context_indexes: Vec::new(),
            skip_reason: reason.name().to_string(),
        }
    }
}

/// 一次运行结束时的汇总。
#[derive(Debug, Clone)]
pub struct EvidenceSummary {
    pub counters: EvidenceCounters,
    pub windows: Vec<WindowRecord>,
    pub listed_limit: u64,
    pub decision_counts: Vec<String>,
    /// 未选中原因按原因码的计数，与逐帧账本一一对应。
    pub skip_reason_counts: Vec<String>,
}

#[derive(Debug, Clone)]
struct OpenWindow {
    window_id: String,
    trigger: &'static str,
    anchor_ms: i64,
    start_ms: i64,
    end_ms: i64,
    remaining_post: u32,
    context_before: u32,
    context_after: u32,
    frame_indexes: Vec<u64>,
    buffer_ids: Vec<String>,
}

/// 全帧判别器 + 事件窗口选择器。
///
/// 它的输入是**每一个可用帧**（pts、结束时间、亮度签名、文字梯度签名），输出是这一帧的
/// 证据角色。未选中帧不会被删除：它们以 `NotSelected` + 原因码进入账本。
#[derive(Debug, Clone)]
pub struct EvidenceSelector {
    policy: EvidencePolicy,
    counters: EvidenceCounters,
    decision_counts: [u64; 5],
    skip_counts: BTreeMap<&'static str, u64>,
    /// 尚未定型、不能写进账本的帧记录（预上下文候选）。按帧序号索引，条数有界。
    tentative: BTreeMap<u64, FrameRecord>,
    /// 已定型、等待按帧序号顺序写出的记录。
    ready: BTreeMap<u64, FrameRecord>,
    next_flush_index: u64,
    next_frame_index: u64,
    last_pts_ms: Option<i64>,
    last_signature: Option<FrameSignature>,
    last_text_signature: Option<FrameSignature>,
    last_selected_ms: Option<i64>,
    last_event_ms: Option<i64>,
    open_window: Option<OpenWindow>,
    windows: Vec<WindowRecord>,
    /// 已经交给账本的窗口条数。窗口在收尾时就定型，但要等窗口里最后一帧写进账本之后
    /// 才轮到它自己（见 `drain_emitted_windows`）。
    emitted_windows: usize,
    /// 预上下文候选环：只保留最近若干帧的序号，字节由解码会话持有。
    pre_ring: VecDeque<u64>,
    max_windows: u64,
}

impl EvidenceSelector {
    pub fn new(policy: EvidencePolicy) -> Self {
        Self {
            policy,
            counters: EvidenceCounters::default(),
            decision_counts: [0; 5],
            skip_counts: BTreeMap::new(),
            tentative: BTreeMap::new(),
            ready: BTreeMap::new(),
            next_flush_index: 1,
            next_frame_index: 1,
            last_pts_ms: None,
            last_signature: None,
            last_text_signature: None,
            last_selected_ms: None,
            last_event_ms: None,
            open_window: None,
            windows: Vec::new(),
            emitted_windows: 0,
            pre_ring: VecDeque::new(),
            max_windows: MAX_WINDOWS_PER_RUN,
        }
    }

    pub fn policy(&self) -> EvidencePolicy {
        self.policy
    }

    /// 测试用的事件窗口预算收紧。生产路径的预算恒为 `MAX_WINDOWS_PER_RUN`。
    #[cfg(test)]
    pub(crate) fn set_max_windows(&mut self, max_windows: u64) {
        self.max_windows = max_windows;
    }

    pub fn counters(&self) -> &EvidenceCounters {
        &self.counters
    }

    pub fn skip_counts(&self) -> &BTreeMap<&'static str, u64> {
        &self.skip_counts
    }

    /// 判别一个可用帧。`signature` 为 `None` 表示布局不可用——那是显式原因，不是"无变化"。
    pub fn observe(
        &mut self,
        pts_ms: i64,
        end_ms: i64,
        signature: Option<FrameSignature>,
        text_signature: Option<FrameSignature>,
    ) -> Result<EvidenceStep, MediaError> {
        let index = self.next_frame_index;
        self.next_frame_index += 1;
        self.counters.characterized += 1;

        // PTS 回退：证据计划必须是单调的，否则窗口区间没有意义。
        if let Some(last) = self.last_pts_ms {
            if pts_ms < last {
                let record = FrameRecord {
                    buffer_id: String::new(),
                    frame_index: index,
                    start_ms: pts_ms,
                    end_ms,
                    signature_delta: 0,
                    text_signature_delta: 0,
                    signature_available: signature.is_some(),
                    decision: FrameDecision::NoChange,
                    selection: FrameSelection::NotSelected,
                    window_id: String::new(),
                    skip_reason: CoverageSkipReason::NonMonotonic.name().to_string(),
                };
                self.tentative.insert(index, record);
                self.bump_skip(CoverageSkipReason::NonMonotonic);
                self.counters.not_selected += 1;
                self.record_decision(FrameDecision::NoChange);
                return Ok(EvidenceStep::unselected(
                    index,
                    FrameDecision::NoChange,
                    CoverageSkipReason::NonMonotonic,
                ));
            }
        }

        let Some(signature) = signature else {
            let record = FrameRecord {
                buffer_id: String::new(),
                frame_index: index,
                start_ms: pts_ms,
                end_ms,
                signature_delta: 0,
                text_signature_delta: 0,
                signature_available: false,
                decision: FrameDecision::NoChange,
                selection: FrameSelection::NotSelected,
                window_id: String::new(),
                skip_reason: CoverageSkipReason::MissingSignature.name().to_string(),
            };
            self.tentative.insert(index, record);
            self.bump_skip(CoverageSkipReason::MissingSignature);
            self.counters.not_selected += 1;
            self.record_decision(FrameDecision::NoChange);
            self.last_pts_ms = Some(pts_ms);
            return Ok(EvidenceStep::unselected(
                index,
                FrameDecision::NoChange,
                CoverageSkipReason::MissingSignature,
            ));
        };

        let signature_delta = self
            .last_signature
            .as_ref()
            .map_or(0, |previous| signature.delta(previous));
        let text_delta = match (&self.last_text_signature, &text_signature) {
            (Some(previous), Some(current)) => current.delta(previous),
            _ => 0,
        };
        let is_first = self.last_pts_ms.is_none();
        let content_changed = !is_first && signature_delta >= self.policy.change_threshold;
        let text_changed = !is_first && text_delta >= self.policy.text_change_threshold;

        self.last_signature = Some(signature);
        if let Some(text) = text_signature {
            self.last_text_signature = Some(text);
        }
        self.last_pts_ms = Some(pts_ms);

        let decision = if is_first {
            FrameDecision::FirstFrame
        } else if content_changed {
            FrameDecision::ContentChange
        } else if text_changed {
            FrameDecision::TextChange
        } else if self.gap_due(pts_ms) {
            FrameDecision::StaticHeartbeat
        } else {
            FrameDecision::NoChange
        };
        self.record_decision(decision);

        // 1) 事件窗口：内容或文字变化都要触发，但受最小事件间隔与窗口预算约束。
        if content_changed || text_changed {
            let trigger = if text_changed && !content_changed {
                "text_change"
            } else {
                "content_change"
            };
            let allowed = self
                .last_event_ms
                .is_none_or(|last| pts_ms - last >= self.policy.min_event_interval_ms);
            if self.windows.len() as u64 >= self.max_windows {
                self.counters.suppressed_event_keeps += 1;
                self.bump_skip(CoverageSkipReason::EventBudgetExhausted);
                self.counters.not_selected += 1;
                return self.select_not_selected(
                    index,
                    pts_ms,
                    end_ms,
                    signature_delta,
                    text_delta,
                    decision,
                    CoverageSkipReason::EventBudgetExhausted,
                );
            }
            if !allowed {
                self.counters.suppressed_event_keeps += 1;
                self.bump_skip(CoverageSkipReason::EventRateLimited);
                self.counters.not_selected += 1;
                return self.select_not_selected(
                    index,
                    pts_ms,
                    end_ms,
                    signature_delta,
                    text_delta,
                    decision,
                    CoverageSkipReason::EventRateLimited,
                );
            }
            // 上一窗口未收尾时直接收尾：新事件比继续补上下文更重要，且窗口数不会因此膨胀。
            self.close_window();
            let pre_indexes = self.take_pre_context();
            let window_id = format!("win-{:08}", self.windows.len() as u64 + 1);
            let context_before = pre_indexes.len() as u32;
            let pre_start = pre_indexes
                .iter()
                .filter_map(|candidate| self.tentative.get(candidate))
                .map(|record| record.start_ms)
                .min()
                .unwrap_or(pts_ms);
            // 预上下文帧在锚点之前就已经被判别过一次、并按"未选中"计入聚合计数；这里把它们
            // 升级成窗口上下文，必须同时撤销跳过原因与 `not_selected`，否则逐帧账本里的选中
            // 结论会和聚合计数互相矛盾（`selected + covered != characterized`）。
            let mut upgraded_pre_context = 0u64;
            for candidate in &pre_indexes {
                if let Some(record) = self.tentative.get_mut(candidate) {
                    record.selection = FrameSelection::EventPreContext;
                    record.window_id = window_id.clone();
                    if clear_skip_reason(&mut self.skip_counts, record) {
                        upgraded_pre_context += 1;
                    }
                    self.ready.insert(*candidate, record.clone());
                }
            }
            for candidate in &pre_indexes {
                self.tentative.remove(candidate);
            }
            self.counters.not_selected = self
                .counters
                .not_selected
                .saturating_sub(upgraded_pre_context);
            self.counters.event_pre_context += pre_indexes.len() as u64;
            self.last_event_ms = Some(pts_ms);
            self.open_window = Some(OpenWindow {
                window_id: window_id.clone(),
                trigger,
                anchor_ms: pts_ms,
                start_ms: pre_start.min(pts_ms),
                end_ms,
                remaining_post: self.policy.post_frames,
                context_before,
                context_after: self.policy.post_frames,
                frame_indexes: pre_indexes
                    .iter()
                    .copied()
                    .chain(std::iter::once(index))
                    .collect(),
                buffer_ids: Vec::new(),
            });
            self.counters.event_anchor += 1;
            self.record_selected(pts_ms);
            let record = FrameRecord {
                buffer_id: String::new(),
                frame_index: index,
                start_ms: pts_ms,
                end_ms,
                signature_delta,
                text_signature_delta: text_delta,
                signature_available: true,
                decision,
                selection: FrameSelection::EventAnchor,
                window_id: window_id.clone(),
                skip_reason: String::new(),
            };
            self.tentative.insert(index, record);
            return Ok(EvidenceStep {
                frame_index: index,
                decision,
                retain_now: true,
                selection: FrameSelection::EventAnchor,
                window_id,
                pre_context_indexes: pre_indexes,
                skip_reason: String::new(),
            });
        }

        // 2) 已打开的窗口：继续补后上下文，直到窗口收尾。
        if let Some(window) = self.open_window.as_mut() {
            if window.remaining_post > 0 {
                window.remaining_post -= 1;
                window.end_ms = end_ms;
                window.frame_indexes.push(index);
                let window_id = window.window_id.clone();
                let closed = window.remaining_post == 0;
                self.counters.event_post_context += 1;
                self.record_selected(pts_ms);
                let record = FrameRecord {
                    buffer_id: String::new(),
                    frame_index: index,
                    start_ms: pts_ms,
                    end_ms,
                    signature_delta,
                    text_signature_delta: text_delta,
                    signature_available: true,
                    decision,
                    selection: FrameSelection::EventPostContext,
                    window_id: window_id.clone(),
                    skip_reason: String::new(),
                };
                self.tentative.insert(index, record);
                if closed {
                    self.close_window();
                }
                return Ok(EvidenceStep {
                    frame_index: index,
                    decision,
                    retain_now: true,
                    selection: FrameSelection::EventPostContext,
                    window_id,
                    pre_context_indexes: Vec::new(),
                    skip_reason: String::new(),
                });
            }
        }

        // 3) 基线复查锚点：静态画面的语义间隔上界靠它保证。
        let gap_due = self.gap_due(pts_ms);
        if gap_due {
            self.take_pre_context();
            self.counters.baseline_anchor += 1;
            self.record_selected(pts_ms);
            let record = FrameRecord {
                buffer_id: String::new(),
                frame_index: index,
                start_ms: pts_ms,
                end_ms,
                signature_delta,
                text_signature_delta: text_delta,
                signature_available: true,
                decision,
                selection: FrameSelection::BaselineAnchor,
                window_id: String::new(),
                skip_reason: String::new(),
            };
            self.tentative.insert(index, record);
            return Ok(EvidenceStep {
                frame_index: index,
                decision,
                retain_now: true,
                selection: FrameSelection::BaselineAnchor,
                window_id: String::new(),
                pre_context_indexes: Vec::new(),
                skip_reason: String::new(),
            });
        }

        // 4) 已判别但不需要单独送模型：它被相邻锚点/窗口的时间范围覆盖。
        self.counters.not_selected += 1;
        self.bump_skip(CoverageSkipReason::NoChangeYet);
        self.select_not_selected(
            index,
            pts_ms,
            end_ms,
            signature_delta,
            text_delta,
            decision,
            CoverageSkipReason::NoChangeYet,
        )
    }

    /// 交接成功后的登记。窗口的帧引用只能来自这里，不能靠猜。
    pub fn mark_retained(&mut self, frame_index: u64, buffer_id: &str) -> Result<(), MediaError> {
        let record = self
            .tentative
            .get_mut(&frame_index)
            .or_else(|| self.ready.get_mut(&frame_index))
            .ok_or_else(|| MediaError::DecodeFailed("evidence_record_missing".into()))?;
        record.buffer_id = buffer_id.to_string();
        // 走到这里说明这一帧确实是"选中后交接"的；正常情况下它没有跳过原因，
        // 这里只是防御性地撤销可能残留的原因码，`not_selected` 的增减由选择路径负责。
        let _ = clear_skip_reason(&mut self.skip_counts, record);
        if let Some(window) = self.open_window.as_mut() {
            if window.frame_indexes.contains(&frame_index) {
                window.buffer_ids.push(buffer_id.to_string());
            }
        }
        if let Some(record) = self.tentative.remove(&frame_index) {
            self.ready.insert(frame_index, record);
        }
        Ok(())
    }

    /// 自适应采样器单独保留了一帧，而语义计划当时判为"不需要模型刷新"。
    ///
    /// 这一帧确实进了数据面，所以账本必须把它标成基线锚点，而不是留成 `NotSelected`：
    /// 否则"账本说没选中、数据面里却有这个 buffer"会变成一条无法解释的矛盾记录。
    pub fn mark_baseline_keep(&mut self, frame_index: u64, pts_ms: i64) {
        let upgraded = {
            let record = self
                .tentative
                .get_mut(&frame_index)
                .or_else(|| self.ready.get_mut(&frame_index));
            match record {
                Some(record)
                    if record.selection == FrameSelection::NotSelected
                        && record.window_id.is_empty() =>
                {
                    record.selection = FrameSelection::BaselineAnchor;
                    let had_reason = clear_skip_reason(&mut self.skip_counts, record);
                    Some(had_reason)
                }
                _ => None,
            }
        };
        if let Some(_had_reason) = upgraded {
            // 这一帧真的交接了，字节不在保留环里，因此它不能再作为"预上下文候选"被窗口请求。
            self.pre_ring.retain(|candidate| *candidate != frame_index);
            self.counters.not_selected = self.counters.not_selected.saturating_sub(1);
            self.counters.baseline_anchor += 1;
            self.record_selected(pts_ms);
        }
    }

    /// 交接被下游有界数据面拒绝时的登记：帧仍然被判别过，但明确不构成证据。
    pub fn mark_rejected(&mut self, frame_index: u64, reason: &str) {
        if let Some(mut record) = self.tentative.remove(&frame_index) {
            record.skip_reason = reason.to_string();
            self.ready.insert(frame_index, record);
        } else if let Some(record) = self.ready.get_mut(&frame_index) {
            record.skip_reason = reason.to_string();
        }
    }

    /// 预上下文候选被环逐出时的显式计数：这些帧本来有机会进入窗口。
    pub fn note_pre_context_eviction(&mut self) {
        self.counters.pre_context_evictions += 1;
    }

    /// 取走已经按帧序号定型、可以写进账本的记录。
    pub fn drain_ready(&mut self) -> Vec<FrameRecord> {
        let mut out = Vec::new();
        while let Some(record) = self.ready.remove(&self.next_flush_index) {
            out.push(record);
            self.next_flush_index += 1;
        }
        out
    }

    /// 取走"窗口里最后一帧已经写进账本"、因此可以增量落盘的窗口记录。
    ///
    /// `flushed_frames` 是账本已经写出的帧条数（帧按序号连续写出，它同时就是水位）。
    /// 增量落盘的意义是让下游在**解码还在进行**时就能拿到窗口：跨进程消费者必须与生产者
    /// 并发，否则有界保留面会在长媒体上必然被顶满。传 `u64::MAX` 取走全部剩余窗口，
    /// 用于流结束后的收尾。
    pub fn drain_emitted_windows(&mut self, flushed_frames: u64) -> Vec<WindowRecord> {
        let mut out = Vec::new();
        while let Some(window) = self.windows.get(self.emitted_windows) {
            if window.last_frame_index >= flushed_frames {
                break;
            }
            out.push(window.clone());
            self.emitted_windows += 1;
        }
        out
    }

    /// 流结束时收尾：未收尾窗口按已收到的后上下文结束，环里的候选帧按未选中定型。
    pub fn finish(&mut self) -> Result<EvidenceSummary, MediaError> {
        self.close_window();
        let pending: Vec<u64> = self.tentative.keys().copied().collect();
        for index in pending {
            if let Some(record) = self.tentative.remove(&index) {
                self.ready.insert(index, record);
            }
        }
        Ok(EvidenceSummary {
            counters: self.counters.clone(),
            windows: self.windows.clone(),
            listed_limit: MAX_LISTED_WINDOWS as u64,
            decision_counts: self.decision_summary(),
            skip_reason_counts: self.skip_reason_counts(),
        })
    }

    /// 判别结果按 `decision` 的计数，用于与逐帧账本对账。
    pub fn decision_summary(&self) -> Vec<String> {
        [
            (FrameDecision::FirstFrame.name(), self.decision_counts[0]),
            (FrameDecision::ContentChange.name(), self.decision_counts[1]),
            (FrameDecision::TextChange.name(), self.decision_counts[2]),
            (
                FrameDecision::StaticHeartbeat.name(),
                self.decision_counts[3],
            ),
            (FrameDecision::NoChange.name(), self.decision_counts[4]),
        ]
        .into_iter()
        .filter(|(_, count)| *count > 0)
        .map(|(name, count)| format!("{name}={count}"))
        .collect()
    }

    pub fn skip_reason_counts(&self) -> Vec<String> {
        self.skip_counts
            .iter()
            .map(|(name, count)| format!("{name}={count}"))
            .collect()
    }

    fn gap_due(&self, pts_ms: i64) -> bool {
        match self.last_selected_ms {
            None => true,
            Some(last) => pts_ms - last >= self.policy.max_semantic_gap_ms,
        }
    }

    fn record_decision(&mut self, decision: FrameDecision) {
        let slot = match decision {
            FrameDecision::FirstFrame => 0,
            FrameDecision::ContentChange => 1,
            FrameDecision::TextChange => 2,
            FrameDecision::StaticHeartbeat => 3,
            FrameDecision::NoChange => 4,
        };
        self.decision_counts[slot] += 1;
    }

    fn bump_skip(&mut self, reason: CoverageSkipReason) {
        *self.skip_counts.entry(reason.name()).or_insert(0) += 1;
    }

    fn record_selected(&mut self, pts_ms: i64) {
        if let Some(last) = self.last_selected_ms {
            let gap = pts_ms - last;
            if gap > self.counters.max_selected_gap_ms {
                self.counters.max_selected_gap_ms = gap;
            }
        }
        self.last_selected_ms = Some(pts_ms);
    }

    #[allow(clippy::too_many_arguments)]
    fn select_not_selected(
        &mut self,
        index: u64,
        pts_ms: i64,
        end_ms: i64,
        signature_delta: u32,
        text_delta: u32,
        decision: FrameDecision,
        reason: CoverageSkipReason,
    ) -> Result<EvidenceStep, MediaError> {
        let record = FrameRecord {
            buffer_id: String::new(),
            frame_index: index,
            start_ms: pts_ms,
            end_ms,
            signature_delta,
            text_signature_delta: text_delta,
            signature_available: true,
            decision,
            selection: FrameSelection::NotSelected,
            window_id: String::new(),
            skip_reason: reason.name().to_string(),
        };
        self.retire_pre_ring(index);
        self.tentative.insert(index, record);
        Ok(EvidenceStep::unselected(index, decision, reason))
    }

    /// 环满时把最老的候选帧定型为 `NotSelected`：它不会再进入任何窗口。
    fn retire_pre_ring(&mut self, index: u64) {
        if self.policy.pre_frames == 0 {
            return;
        }
        self.pre_ring.push_back(index);
        while self.pre_ring.len() > self.policy.pre_frames as usize {
            if let Some(oldest) = self.pre_ring.pop_front() {
                if let Some(record) = self.tentative.remove(&oldest) {
                    self.ready.insert(oldest, record);
                }
            }
        }
    }

    fn take_pre_context(&mut self) -> Vec<u64> {
        let indexes: Vec<u64> = self.pre_ring.drain(..).collect();
        indexes
    }

    fn close_window(&mut self) {
        let Some(window) = self.open_window.take() else {
            return;
        };
        let last_frame_index = window.frame_indexes.last().copied().unwrap_or(0);
        let record = WindowRecord {
            window_id: window.window_id,
            start_ms: window.start_ms,
            end_ms: window.end_ms,
            anchor_ms: window.anchor_ms,
            trigger: window.trigger,
            handed_off_frames: window.buffer_ids.len() as u32,
            frame_buffer_ids: window.buffer_ids,
            context_before: window.context_before,
            context_after: window.context_after,
            last_frame_index,
        };
        self.counters.windows += 1;
        self.windows.push(record);
    }
}

/// 撤销一帧的跳过原因计数，并清空它的原因码。
///
/// 一帧从"未选中"升级成选中（基线锚点、窗口上下文）或被交接之后，聚合跳过计数必须同步
/// 撤销，否则 `skip_reason_counts` 会和逐帧账本里的选中结论互相矛盾。
fn clear_skip_reason(
    skip_counts: &mut BTreeMap<&'static str, u64>,
    record: &mut FrameRecord,
) -> bool {
    if record.skip_reason.is_empty() {
        return false;
    }
    let key = skip_counts
        .keys()
        .copied()
        .find(|name| *name == record.skip_reason.as_str());
    if let Some(key) = key {
        if let Some(count) = skip_counts.get_mut(key) {
            *count = count.saturating_sub(1);
        }
        if skip_counts.get(key).copied().unwrap_or(0) == 0 {
            skip_counts.remove(key);
        }
    }
    record.skip_reason.clear();
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    const W: u32 = 16;
    const H: u32 = 16;

    /// 纯色帧：整帧亮度就是 `shade`，因此两个纯色帧的签名差恒等于亮度差。
    fn solid(shade: u8) -> FrameSignature {
        FrameSignature::from_rgba(&vec![shade; (W * H * 4) as usize], W, H)
            .expect("solid signature")
    }

    /// 列交替帧：单元格均值几乎不变（内容签名差 ≤ 1），水平梯度极高（文字签名差可达 255）。
    fn striped_bytes() -> Vec<u8> {
        let mut bytes = vec![0u8; (W * H * 4) as usize];
        for y in 0..H as usize {
            for x in 0..W as usize {
                let value = if x % 2 == 0 { 0u8 } else { 255u8 };
                let index = (y * W as usize + x) * 4;
                bytes[index] = value;
                bytes[index + 1] = value;
                bytes[index + 2] = value;
            }
        }
        bytes
    }

    /// 列交替帧的亮度签名：所有单元格都是 127，和纯色 128 只差 1。
    fn striped_luma() -> FrameSignature {
        FrameSignature::from_rgba(&striped_bytes(), W, H).expect("striped luma signature")
    }

    /// 纯色帧的水平梯度恒为 0。
    fn flat_text() -> FrameSignature {
        FrameSignature::edge_energy_from_rgba(&vec![128u8; (W * H * 4) as usize], W, H)
            .expect("flat text signature")
    }

    /// 列交替帧的水平梯度：每个单元格都是 255。
    fn striped_text() -> FrameSignature {
        FrameSignature::edge_energy_from_rgba(&striped_bytes(), W, H)
            .expect("striped text signature")
    }

    #[test]
    fn static_stream_keeps_the_semantic_gap_bounded() {
        let policy = EvidencePolicy::default();
        let mut selector = EvidenceSelector::new(policy);
        let frame = solid(128);
        let mut pts = 0i64;
        let mut selected = 0u64;
        for _ in 0..120 {
            let step = selector
                .observe(pts, pts + 33, Some(frame), Some(frame))
                .expect("observe");
            if step.retain_now {
                selected += 1;
            }
            pts += 33;
        }
        let summary = selector.finish().expect("finish");
        assert_eq!(summary.counters.characterized, 120, "每一帧都被判别一次");
        assert_eq!(summary.counters.windows, 0, "静态流不应开事件窗口");
        assert_eq!(summary.counters.baseline_anchor, selected);
        // 33ms 的步长无法刚好命中 1000ms，因此上界是 gap 上限加一个帧间隔。
        assert!(
            summary.counters.max_selected_gap_ms <= policy.max_semantic_gap_ms + 33,
            "静态流的相邻语义输入不得超过 gap 上界加一帧：{}",
            summary.counters.max_selected_gap_ms
        );
        assert!(summary.counters.not_selected >= 100);
        // 聚合跳过计数必须和"未选中"完全对上：多一条少一条都说明账本与计数脱节。
        let counted: u64 = summary
            .skip_reason_counts
            .iter()
            .map(|entry| {
                entry
                    .rsplit_once('=')
                    .expect("name=count")
                    .1
                    .parse::<u64>()
                    .expect("count")
            })
            .sum();
        assert_eq!(counted, summary.counters.not_selected);
    }

    #[test]
    fn content_change_opens_a_bounded_context_window() {
        let policy = EvidencePolicy::default();
        let mut selector = EvidenceSelector::new(policy);
        let quiet = solid(10);
        let loud = solid(200);
        let mut pts = 0i64;
        for _ in 0..10 {
            selector
                .observe(pts, pts + 33, Some(quiet), Some(quiet))
                .expect("observe");
            pts += 33;
        }
        let anchor = selector
            .observe(pts, pts + 33, Some(loud), Some(loud))
            .expect("observe");
        assert_eq!(anchor.decision, FrameDecision::ContentChange);
        assert_eq!(anchor.selection, FrameSelection::EventAnchor);
        assert!(anchor.retain_now, "事件锚点必须被交接");
        assert_eq!(
            anchor.pre_context_indexes.len(),
            policy.pre_frames as usize,
            "预上下文必须来自解码会话的保留环"
        );
        pts += 33;
        for _ in 0..policy.post_frames {
            let step = selector
                .observe(pts, pts + 33, Some(loud), Some(loud))
                .expect("observe");
            assert_eq!(step.selection, FrameSelection::EventPostContext);
            pts += 33;
        }
        let summary = selector.finish().expect("finish");
        assert_eq!(summary.counters.windows, 1);
        assert_eq!(summary.counters.event_anchor, 1);
        assert_eq!(summary.counters.event_pre_context, policy.pre_frames as u64);
        assert_eq!(
            summary.counters.event_post_context,
            policy.post_frames as u64
        );
        let window = &summary.windows[0];
        assert_eq!(window.trigger, "content_change");
        assert_eq!(window.context_before, policy.pre_frames);
        assert_eq!(window.context_after, policy.post_frames);
        assert!(
            window.frame_buffer_ids.is_empty(),
            "没有交接就没有 buffer 引用，不能猜一个 id"
        );
    }

    #[test]
    fn text_gradient_change_triggers_without_a_luminance_jump() {
        let mut selector = EvidenceSelector::new(EvidencePolicy::default());
        let quiet = solid(128);
        selector
            .observe(0, 33, Some(quiet), Some(flat_text()))
            .expect("observe");
        let step = selector
            .observe(33, 66, Some(striped_luma()), Some(striped_text()))
            .expect("observe");
        assert_eq!(
            step.decision,
            FrameDecision::TextChange,
            "整帧亮度几乎不变，只有水平梯度变了"
        );
        assert_eq!(step.selection, FrameSelection::EventAnchor);
        assert!(step.retain_now);
    }

    /// 逐帧结论必须能完成一次严格对账：`选中 + 带原因覆盖 == 判别帧数`，而且聚合跳过计数
    /// 只统计**真正**未选中的帧。预上下文帧会先被告别成"未选中"、再升级成窗口上下文，
    /// 是最容易漏掉撤销的一条路径，所以单独提出来做断言。
    fn coverage_accounting(summary: &EvidenceSummary) -> (u64, u64) {
        let counters = &summary.counters;
        let selected = counters.baseline_anchor
            + counters.event_anchor
            + counters.event_pre_context
            + counters.event_post_context;
        let covered = counters.not_selected;
        assert_eq!(
            selected + covered,
            counters.characterized,
            "每一帧要么被选中、要么带原因被覆盖，不能重叠也不能遗漏"
        );
        let counted: u64 = summary
            .skip_reason_counts
            .iter()
            .map(|entry| {
                entry
                    .rsplit_once('=')
                    .expect("name=count")
                    .1
                    .parse::<u64>()
                    .expect("count")
            })
            .sum();
        assert_eq!(counted, covered, "跳过原因计数必须恰好解释未选中的帧");
        (selected, covered)
    }

    #[test]
    fn pre_context_upgrade_keeps_the_coverage_accounting_exact() {
        let policy = EvidencePolicy::default();
        let mut selector = EvidenceSelector::new(policy);
        let quiet = solid(12);
        let loud = solid(210);
        let mut pts = 0i64;
        for _ in 0..20 {
            selector
                .observe(pts, pts + 33, Some(quiet), Some(quiet))
                .expect("observe");
            pts += 33;
        }
        for _ in 0..6 {
            selector
                .observe(pts, pts + 33, Some(loud), Some(loud))
                .expect("observe");
            pts += 33;
        }
        let summary = selector.finish().expect("finish");
        assert!(
            summary.counters.event_pre_context > 0,
            "事件窗口必须补上变化前的上下文帧"
        );
        let (selected, covered) = coverage_accounting(&summary);
        assert!(selected > 0 && covered > 0);
    }

    #[test]
    fn a_frame_promoted_to_a_baseline_anchor_stops_counting_as_covered() {
        let mut selector = EvidenceSelector::new(EvidencePolicy::default());
        let frame = solid(128);
        let mut promoted = None;
        let mut pts = 0i64;
        for _ in 0..20 {
            let step = selector
                .observe(pts, pts + 33, Some(frame), Some(frame))
                .expect("observe");
            if !step.retain_now && promoted.is_none() {
                promoted = Some(step.frame_index);
            }
            pts += 33;
        }
        let index = promoted.expect("静态流里应当存在未选中的帧");
        selector.mark_baseline_keep(index, 0);
        let summary = selector.finish().expect("finish");
        let (selected, covered) = coverage_accounting(&summary);
        assert_eq!(selected, 20 - covered);
    }

    #[test]
    fn rapid_changes_after_an_event_are_rate_limited_not_kept() {
        let policy = EvidencePolicy::new(1_000, 2, 2, 8, 12, 100).expect("policy");
        let mut selector = EvidenceSelector::new(policy);
        let dark = solid(0);
        let bright = solid(200);
        let mut pts = 0i64;
        selector
            .observe(pts, pts + 33, Some(dark), Some(dark))
            .expect("observe");
        pts += 33;
        for _ in 0..3 {
            selector
                .observe(pts, pts + 33, Some(dark), Some(dark))
                .expect("observe");
            pts += 33;
        }
        let first = selector
            .observe(pts, pts + 33, Some(bright), Some(bright))
            .expect("observe");
        assert!(first.retain_now, "第一个事件必须交接");
        pts += 33;
        let second = selector
            .observe(pts, pts + 33, Some(dark), Some(dark))
            .expect("observe");
        assert!(!second.retain_now);
        assert_eq!(second.skip_reason, "event_rate_limited");
        let summary = selector.finish().expect("finish");
        assert!(summary.counters.suppressed_event_keeps >= 1);
        assert!(summary
            .decision_counts
            .iter()
            .any(|entry| entry.starts_with("content_change=")));
    }

    #[test]
    fn exhausted_event_budget_is_an_explicit_skip() {
        let mut selector = EvidenceSelector::new(EvidencePolicy::default());
        selector.set_max_windows(1);
        let dark = solid(0);
        let bright = solid(200);
        let mut pts = 0i64;
        selector
            .observe(pts, pts + 60, Some(dark), Some(dark))
            .expect("observe");
        pts += 120;
        let first = selector
            .observe(pts, pts + 60, Some(bright), Some(bright))
            .expect("observe");
        assert!(first.retain_now);
        pts += 120;
        let second = selector
            .observe(pts, pts + 60, Some(dark), Some(dark))
            .expect("observe");
        assert!(second.retain_now, "预算还够时第二个事件仍然要交接");
        pts += 120;
        let third = selector
            .observe(pts, pts + 60, Some(bright), Some(bright))
            .expect("observe");
        assert!(!third.retain_now);
        assert_eq!(third.skip_reason, "event_budget_exhausted");
        let summary = selector.finish().expect("finish");
        assert_eq!(summary.counters.windows, 2);
        assert!(summary.counters.suppressed_event_keeps >= 1);
    }

    #[test]
    fn pre_context_is_bounded_by_the_policy() {
        let policy = EvidencePolicy::new(1_000, 1, 1, 8, 12, 100).expect("policy");
        let mut selector = EvidenceSelector::new(policy);
        let quiet = solid(5);
        let loud = solid(220);
        let mut pts = 0i64;
        for _ in 0..8 {
            selector
                .observe(pts, pts + 33, Some(quiet), Some(quiet))
                .expect("observe");
            pts += 33;
        }
        let anchor = selector
            .observe(pts, pts + 33, Some(loud), Some(loud))
            .expect("observe");
        assert_eq!(anchor.pre_context_indexes.len(), 1);
    }

    #[test]
    fn explicit_reasons_cover_bad_pts_and_missing_layout() {
        let mut selector = EvidenceSelector::new(EvidencePolicy::default());
        let frame = solid(128);
        selector
            .observe(1_000, 1_033, Some(frame), Some(frame))
            .expect("observe");
        let backwards = selector
            .observe(500, 533, Some(frame), Some(frame))
            .expect("observe");
        assert_eq!(backwards.skip_reason, "non_monotonic_pts");
        let missing = selector.observe(1_200, 1_233, None, None).expect("observe");
        assert_eq!(missing.skip_reason, "missing_signature");
        let summary = selector.finish().expect("finish");
        assert_eq!(summary.counters.characterized, 3);
        assert!(summary
            .skip_reason_counts
            .iter()
            .any(|entry| entry == "non_monotonic_pts=1"));
    }

    #[test]
    fn every_characterized_frame_appears_exactly_once_in_the_ledger() {
        let mut selector = EvidenceSelector::new(EvidencePolicy::default());
        let frame = solid(128);
        let mut pts = 0i64;
        let total = 50u64;
        for _ in 0..total {
            selector
                .observe(pts, pts + 33, Some(frame), Some(frame))
                .expect("observe");
            pts += 33;
        }
        selector.finish().expect("finish");
        let drained = selector.drain_ready();
        let indexes: Vec<u64> = drained.iter().map(|record| record.frame_index).collect();
        assert_eq!(indexes, (1..=total).collect::<Vec<u64>>());
        assert_eq!(selector.next_flush_index, total + 1);
    }
}
