//! 跨进程数据面交接：保留已解码的 buffer，按 lease 交给消费者。
//!
//! 与 `descriptor.rs` 的进程内自校验不同，这里的 buffer 在交接完成前**不会**被释放：
//! 消费者领取 lease 后从共享内存里读字节，读完显式释放；超时未释放由运行时回收并计数。
//! 字节永远不进入控制消息，descriptor 里也永远只有不透明句柄。

use std::collections::BTreeMap;

use sensoryplex_sdk::common::{BufferDescriptor, BufferFormat, BufferLocator, TimeRange};
use sensoryplex_sdk::{validate_range, ContractError};

use crate::arena::Arena;
use crate::descriptor::{content_hash, ADMITTED_MEMORY_KINDS};
use crate::lease::LeaseRegistry;
use crate::{validate_descriptor, MediaError};

/// 保留表上限。数据面无界是缺陷，不是限流策略。
pub const DEFAULT_RETAINED_LIMIT: usize = 32;
pub const MAX_RETAINED_LIMIT: usize = 4_096;
/// 单一 buffer 种类在保留表里能占用的最大槽位数。
///
/// 保留表是**所有种类共用**的一张 FIFO，而不按种类分区：实时流里音频块每 21 ms 一个、
/// 视频 keep 只有几 Hz，只要音频先到（实测直播窗口里就是如此），它会把整张表占满，
/// 视频帧此后每一帧都被拒绝——下游拿到的窗口里一帧视频都没有。有界窗口因此按种类对半
/// 分配：任何一种都不得占用超过一半，谁也不能把另一类挤出去。
///
/// 单一种类的流（例如纯音频）只会用到自己那一半：这是显式接受的代价，换来的是
/// "窗口里必然同时容得下两类"这条可断言的性质。
pub fn retained_kind_limit(retained_limit: usize) -> usize {
    (retained_limit / 2).max(1)
}
/// lease TTL 的下限保证消费端真的有机会读完；上限避免一句话占住 buffer。
pub const MIN_LEASE_TTL_MS: u32 = 50;
pub const MAX_LEASE_TTL_MS: u32 = 60_000;
/// 保留式交接的默认共享段容量：够放几十个大帧，但不会把机器内存吃满。
pub const DEFAULT_RETAIN_ARENA_BYTES: usize = 64 * 1024 * 1024;
/// 与进程内交接一致：单条 buffer 覆盖的区间不超一分钟。
const MAX_BUFFER_INTERVAL_MS: i64 = 60_000;

/// 保留式交接的开关与上限。关闭时沿用进程内自校验路径（签发 → 校验 → 释放），
/// 打开时字节会留在共享内存里，等真正的第二个进程来领取 lease。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetainPolicy {
    pub enabled: bool,
    /// 共享段容量。与保留表一样是硬上限，触顶即显式拒绝。
    pub arena_capacity_bytes: usize,
    pub retained_limit: usize,
}

impl Default for RetainPolicy {
    fn default() -> Self {
        Self {
            enabled: false,
            arena_capacity_bytes: DEFAULT_RETAIN_ARENA_BYTES,
            retained_limit: DEFAULT_RETAINED_LIMIT,
        }
    }
}

impl RetainPolicy {
    /// 打开保留式交接。上限越界时直接拒绝配置，而不是夹取成一个"差不多"的值。
    pub fn shared(arena_capacity_bytes: usize, retained_limit: usize) -> Result<Self, MediaError> {
        if arena_capacity_bytes == 0 {
            return Err(MediaError::IoFailed("invalid_retain_arena_bytes".into()));
        }
        if retained_limit == 0 || retained_limit > MAX_RETAINED_LIMIT {
            return Err(MediaError::IoFailed("invalid_retained_limit".into()));
        }
        Ok(Self {
            enabled: true,
            arena_capacity_bytes,
            retained_limit,
        })
    }
}

/// 交给消费者的受控引用：descriptor（含 lease）+ 段名。段名只在应答里出现，不是句柄的一部分。
#[derive(Debug, Clone)]
pub struct LeasedBuffer {
    pub descriptor: BufferDescriptor,
    pub segment_name: Option<String>,
    pub arena_capacity_bytes: u64,
}

/// 保留表里的一条 buffer。`lease_id` 为空表示尚未被领取。
#[derive(Debug, Clone)]
pub struct RetainedBuffer {
    pub buffer_id: String,
    pub kind: String,
    pub stream_id: String,
    pub time_range: TimeRange,
    pub format: BufferFormat,
    pub offset: u64,
    pub length: u64,
    pub content_hash: String,
    pub lease_id: Option<String>,
    /// 保留发生的墙钟时间，用来算消费者等待时长。它不是时间轴的一部分，也不进 descriptor。
    pub retained_at_ms: i64,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct HandoffStats {
    pub retained_limit: u64,
    pub retained: u64,
    pub leased: u64,
    /// 保留请求的累计数（成功 + 被容量拒绝）。它与解码侧交接的样本数一一对应。
    pub offered_total: u64,
    pub retained_total: u64,
    pub released_total: u64,
    pub expired_total: u64,
    /// 保留期的写侧拒绝（有界容量：保留表满、段满）。
    pub retain_rejections: u64,
    /// 消费期被拒绝的请求（越界、未知 buffer、非法 TTL、重复领取、迟到释放）。
    pub request_rejections: u64,
    /// 两类拒绝的原因拆分。`retain_rejections + request_rejections` 是它的总和。
    pub rejection_reasons: BTreeMap<String, u64>,
    pub arena_capacity_bytes: u64,
    pub arena_used_bytes: u64,
    pub arena_peak_bytes: u64,
    pub arena_live_slabs: u64,
    /// 保留表深度的高水位（条数）。它是本次运行真正达到过的最大值，
    /// 因此可以直接和 `retained_limit` 比对"离触顶有多近"。
    pub retained_peak: u64,
    /// 单一 buffer 种类能占用的槽位数上限。与 `retained_by_kind` 一起读，
    /// 才能看出"是不是某一类把窗口占满了"，而不是只看总深度还没触顶就以为有富余。
    pub retained_kind_limit: u64,
    /// 当前保留的 buffer 按种类拆分。总数是 `retained`，这里说明它由谁组成。
    pub retained_by_kind: BTreeMap<String, u64>,
    /// 单一 buffer 种类占用槽位数的高水位（种类取最大值）。
    pub retained_kind_peak: u64,
    /// 只统计写侧（保留）拒绝的原因拆分。`rejection_reasons` 是写侧 + 读侧的合并口径，
    /// 用它反推写侧会算错；背压报告需要一条能对账的写侧因果表。
    pub retain_rejection_reasons: BTreeMap<String, u64>,
    /// 保留到释放/过期之间的等待时间（毫秒）。只统计已经结束的 buffer，
    /// 所以这三个数的样本数就是 `residency_samples`，可以小于 released + expired。
    pub residency_samples: u64,
    pub residency_max_ms: u64,
    pub residency_total_ms: u64,
}

pub struct BufferHandoff {
    arena: Arena,
    leases: LeaseRegistry,
    retained: Vec<RetainedBuffer>,
    limit: usize,
    offered_total: u64,
    retained_total: u64,
    released_total: u64,
    expired_total: u64,
    retain_rejections: u64,
    request_rejections: u64,
    rejection_reasons: BTreeMap<String, u64>,
    retain_rejection_reasons: BTreeMap<String, u64>,
    retain_rejection_kinds: BTreeMap<String, u64>,
    retained_peak: u64,
    retained_kind_peak: u64,
    residency_samples: u64,
    residency_max_ms: u64,
    residency_total_ms: u64,
    /// 保留时刻的取值方式。生产用真实墙钟；测试可以换成确定性的时钟，
    /// 否则驻留时长只能靠 sleep 来测。
    clock: fn() -> i64,
}

impl BufferHandoff {
    /// `seed` 为 `Some` 时使用共享内存 arena（跨进程交接），否则退化为进程内 arena。
    pub fn new(
        arena_id: &str,
        capacity: usize,
        seed: Option<&[u8]>,
        limit: usize,
    ) -> Result<Self, MediaError> {
        if limit == 0 || limit > MAX_RETAINED_LIMIT {
            return Err(MediaError::IoFailed("invalid_retained_limit".into()));
        }
        let arena = match seed {
            Some(seed) => Arena::shared(arena_id, capacity, seed)?,
            None => Arena::new(arena_id, capacity)?,
        };
        Ok(Self {
            arena,
            leases: LeaseRegistry::default(),
            retained: Vec::new(),
            limit,
            offered_total: 0,
            retained_total: 0,
            released_total: 0,
            expired_total: 0,
            retain_rejections: 0,
            request_rejections: 0,
            rejection_reasons: BTreeMap::new(),
            retain_rejection_reasons: BTreeMap::new(),
            retain_rejection_kinds: BTreeMap::new(),
            retained_peak: 0,
            retained_kind_peak: 0,
            residency_samples: 0,
            residency_max_ms: 0,
            residency_total_ms: 0,
            clock: crate::now_unix_ms,
        })
    }

    /// 测试辅助：换一个确定性的时钟，让驻留时长可断言，而不是靠 sleep。
    #[cfg(test)]
    fn set_clock_for_test(&mut self, clock: fn() -> i64) {
        self.clock = clock;
    }

    pub fn arena_id(&self) -> &str {
        self.arena.id()
    }

    pub fn segment_name(&self) -> Option<&str> {
        self.arena.segment_name()
    }

    pub fn arena_capacity_bytes(&self) -> u64 {
        self.arena.capacity() as u64
    }

    pub fn arena(&self) -> &Arena {
        &self.arena
    }

    pub fn list(&self) -> &[RetainedBuffer] {
        &self.retained
    }

    pub fn stats(&self) -> HandoffStats {
        HandoffStats {
            retained_limit: self.limit as u64,
            retained: self.retained.len() as u64,
            leased: self
                .retained
                .iter()
                .filter(|b| b.lease_id.is_some())
                .count() as u64,
            offered_total: self.offered_total,
            retained_total: self.retained_total,
            released_total: self.released_total,
            expired_total: self.expired_total,
            retain_rejections: self.retain_rejections,
            request_rejections: self.request_rejections,
            rejection_reasons: self.rejection_reasons.clone(),
            arena_capacity_bytes: self.arena.capacity() as u64,
            arena_used_bytes: self.arena.used_bytes() as u64,
            arena_peak_bytes: self.arena.peak_bytes() as u64,
            arena_live_slabs: self.arena.live_slabs() as u64,
            retained_peak: self.retained_peak,
            retained_kind_limit: self.kind_quota(),
            retained_by_kind: self.retained_by_kind(),
            retained_kind_peak: self.retained_kind_peak,
            retain_rejection_reasons: self.retain_rejection_reasons.clone(),
            residency_samples: self.residency_samples,
            residency_max_ms: self.residency_max_ms,
            residency_total_ms: self.residency_total_ms,
        }
    }

    /// 保留表当前深度。背压策略按它与 `retained_limit` 的比例判定压力等级。
    pub fn depth(&self) -> u64 {
        self.retained.len() as u64
    }

    pub fn limit(&self) -> u64 {
        self.limit as u64
    }

    /// 单一 buffer 种类能占用的槽位数上限。它和总上限一样是硬上限，因此压力读数必须
    /// 同时看这两条：表里还有空位不等于"某一类还进得来"。
    pub fn kind_quota(&self) -> u64 {
        retained_kind_limit(self.limit) as u64
    }

    /// 当前保留的 buffer 按种类拆分。按种类名排序，保证同样的状态产出同样的字节。
    pub fn retained_by_kind(&self) -> BTreeMap<String, u64> {
        let mut counts = BTreeMap::new();
        for held in &self.retained {
            *counts.entry(held.kind.clone()).or_insert(0) += 1;
        }
        counts
    }

    /// 占用最多的那个种类的槽位数。它是"按种类分配"这条上限的实际水位。
    pub fn deepest_kind(&self) -> u64 {
        self.retained_by_kind().values().copied().max().unwrap_or(0)
    }

    /// 写侧拒绝按 buffer 种类拆分的计数。与原因表一样，总和等于 `retain_rejections`。
    pub fn retain_rejections_by_kind(&self) -> &BTreeMap<String, u64> {
        &self.retain_rejection_kinds
    }

    /// 写侧（保留）拒绝：有界容量把这次保留挡在外面。
    ///
    /// 记录 `kind` 是必要的：保留表是所有 buffer 种类共用的，只有按种类拆开，
    /// `dropped_total` 才能被读成"谁被挡住了"，而不是一个无从解释的总数。
    fn reject_retain(&mut self, kind: &str, reason: &str) -> MediaError {
        self.retain_rejections += 1;
        self.count_rejection(reason);
        self.count_retain_rejection(reason);
        *self
            .retain_rejection_kinds
            .entry(kind.to_string())
            .or_insert(0) += 1;
        MediaError::DescriptorRejected(reason.to_string())
    }

    /// 读侧（消费）拒绝：请求本身不合法或状态不允许。
    fn reject_request(&mut self, reason: &str) -> MediaError {
        self.request_rejections += 1;
        self.count_rejection(reason);
        MediaError::DescriptorRejected(reason.to_string())
    }

    fn count_rejection(&mut self, reason: &str) {
        *self
            .rejection_reasons
            .entry(reason.to_string())
            .or_insert(0) += 1;
    }

    fn count_retain_rejection(&mut self, reason: &str) {
        *self
            .retain_rejection_reasons
            .entry(reason.to_string())
            .or_insert(0) += 1;
    }

    /// 一条保留的 buffer 结束了它的一生（被释放或被回收），把等待时长记进驻留统计。
    /// 只在这里累计：没有归宿的 buffer 没有等待时间可测，绝不用当前时刻凑一个数。
    fn settle_residency(&mut self, retained_at_ms: i64, now_ms: i64) {
        let waited = now_ms.saturating_sub(retained_at_ms).max(0) as u64;
        self.residency_samples += 1;
        self.residency_total_ms = self.residency_total_ms.saturating_add(waited);
        self.residency_max_ms = self.residency_max_ms.max(waited);
    }

    /// 把字节保留在 arena 里等待消费者。这里**不**签发 lease：lease 是"读取窗口"，
    /// 只有在消费者真的来领取时才有意义，否则会在没人读的情况下白白过期。
    pub fn retain(
        &mut self,
        buffer_id: &str,
        kind: &str,
        stream_id: &str,
        time_range: TimeRange,
        format: BufferFormat,
        bytes: &[u8],
    ) -> Result<RetainedBuffer, MediaError> {
        // 先记"提出了多少次保留请求"，成功与否后面再看：调用方需要的是
        // `offered_total = retained_total + retain_rejections` 这条恒等式。
        self.offered_total += 1;
        if bytes.is_empty() {
            return Err(self.reject_retain(kind, "empty_buffer_payload"));
        }
        if buffer_id.is_empty() || kind.is_empty() || stream_id.is_empty() {
            return Err(self.reject_retain(kind, "missing_buffer_identity"));
        }
        if validate_range(&time_range).is_err() {
            return Err(self.reject_retain(kind, "invalid_half_open_time_range"));
        }
        if time_range.end_ms - time_range.start_ms > MAX_BUFFER_INTERVAL_MS {
            return Err(self.reject_retain(kind, "buffer_interval_longer_than_a_minute"));
        }
        if self.retained.len() >= self.limit {
            return Err(self.reject_retain(kind, "handoff_backlog_full"));
        }
        // 第二重有界：单一 kind 不得超过自己的配额。少了这一条，先到的种类（实时流里是
        // 每 21 ms 一个的音频块）会把整张表占满，另一类此后一帧也进不来。
        if self
            .retained
            .iter()
            .filter(|held| held.kind == kind)
            .count()
            >= retained_kind_limit(self.limit)
        {
            return Err(self.reject_retain(kind, "handoff_kind_quota_full"));
        }
        if self.retained.iter().any(|held| held.buffer_id == buffer_id) {
            return Err(self.reject_retain(kind, "duplicate_buffer_id"));
        }
        let offset = match self.arena.allocate(bytes.len()) {
            Ok(offset) => offset,
            Err(error) => {
                // arena 是第二重上限。它被触及时同样要出现在原因表里：调用方需要
                // 看到"拒绝"这件事，而不是只看到保留表还有空位。
                self.retain_rejections += 1;
                self.count_rejection("arena_capacity_exceeded");
                self.count_retain_rejection("arena_capacity_exceeded");
                *self
                    .retain_rejection_kinds
                    .entry(kind.to_string())
                    .or_insert(0) += 1;
                return Err(error);
            }
        };
        self.arena.write(offset, bytes)?;
        let held = RetainedBuffer {
            buffer_id: buffer_id.to_string(),
            kind: kind.to_string(),
            stream_id: stream_id.to_string(),
            time_range,
            format,
            offset: offset as u64,
            length: bytes.len() as u64,
            content_hash: content_hash(bytes),
            lease_id: None,
            retained_at_ms: (self.clock)(),
        };
        self.retained.push(held.clone());
        self.retained_total += 1;
        self.retained_peak = self.retained_peak.max(self.retained.len() as u64);
        // 种类水位同样记高水位：只报总深度会让人以为"表还有富余"，
        // 而实际上是某一类已经顶到自己那一半了。
        self.retained_kind_peak = self.retained_kind_peak.max(self.deepest_kind());
        Ok(held)
    }

    /// 流式生产中的保留入口：容量类拒绝是**正常的有界行为**（已计入统计，返回 `Ok(None)`），
    /// 契约违规（标识缺失、区间非法、重复 buffer_id）仍然是错误。
    pub fn retain_or_reject(
        &mut self,
        buffer_id: &str,
        kind: &str,
        stream_id: &str,
        time_range: TimeRange,
        format: BufferFormat,
        bytes: &[u8],
    ) -> Result<Option<RetainedBuffer>, MediaError> {
        match self.retain(buffer_id, kind, stream_id, time_range, format, bytes) {
            Ok(held) => Ok(Some(held)),
            Err(MediaError::ArenaCapacityExceeded { .. }) => Ok(None),
            Err(MediaError::DescriptorRejected(reason)) if is_capacity_rejection(&reason) => {
                Ok(None)
            }
            Err(error) => Err(error),
        }
    }

    /// 领取一个 buffer 的读取窗口。窗口必须落在该 buffer 自己的区间内：给消费者
    /// 一个更大的窗口等于让它读到相邻 buffer 的字节。
    pub fn acquire(
        &mut self,
        buffer_id: &str,
        window: Option<(u64, u64)>,
        ttl_ms: u32,
        now_ms: i64,
    ) -> Result<LeasedBuffer, MediaError> {
        if !(MIN_LEASE_TTL_MS..=MAX_LEASE_TTL_MS).contains(&ttl_ms) {
            return Err(self.reject_request("invalid_lease_ttl"));
        }
        self.expire(now_ms);
        let Some(index) = self
            .retained
            .iter()
            .position(|held| held.buffer_id == buffer_id)
        else {
            return Err(self.reject_request("unknown_buffer"));
        };
        let held = self.retained[index].clone();
        let (offset_in_buffer, length) = match window {
            Some((offset, length)) => (offset, length),
            None => (0, held.length),
        };
        // 请求合法性先判：一个越界的请求即使落在"已经被领走"的 buffer 上，也是越界，
        // 不能因为状态顺手而报成别的原因。
        if length == 0 || offset_in_buffer.checked_add(length).is_none() {
            return Err(self.reject_request("mapping_out_of_range"));
        }
        if offset_in_buffer + length > held.length {
            return Err(self.reject_request("mapping_out_of_range"));
        }
        if self.retained[index].lease_id.is_some() {
            return Err(self.reject_request("buffer_already_leased"));
        }

        let window_bytes = self
            .arena
            .read(
                held.offset as usize + offset_in_buffer as usize,
                length as usize,
            )
            .map(<[u8]>::to_vec);
        let bytes = match window_bytes {
            Ok(bytes) => bytes,
            Err(_) => return Err(self.reject_request("mapping_out_of_range")),
        };
        let lease = match self.leases.issue(now_ms, ttl_ms) {
            Ok(lease) => lease,
            Err(error) => return Err(self.reject_request(&contract_code(error))),
        };
        let descriptor = BufferDescriptor {
            buffer_id: held.buffer_id.clone(),
            kind: held.kind.clone(),
            memory_kind: ADMITTED_MEMORY_KINDS[0].to_string(),
            locator: Some(BufferLocator {
                handle: self.arena.id().to_string(),
                offset: held.offset + offset_in_buffer,
                length,
                // HandoffService 不持有监听地址；调用方只可写入本次受控的 loopback
                // endpoint，空值保持“已在 Start 阶段绑定”的既有语义。
                handoff_endpoint: String::new(),
            }),
            format: Some(held.format.clone()),
            stream_id: held.stream_id.clone(),
            time_range: Some(held.time_range),
            lease: Some(lease.clone()),
            // 摘要覆盖的是**被授予的窗口**，消费者照这个摘要校验自己读到的那一段。
            content_hash: content_hash(&bytes),
        };
        if let Err(error) = validate_descriptor(&descriptor, now_ms, &ADMITTED_MEMORY_KINDS) {
            let reason = contract_code(error);
            let _ = self.leases.release(&lease.lease_id);
            return Err(self.reject_request(&reason));
        }
        self.retained[index].lease_id = Some(lease.lease_id.clone());
        Ok(LeasedBuffer {
            descriptor,
            segment_name: self.segment_name().map(str::to_string),
            arena_capacity_bytes: self.arena_capacity_bytes(),
        })
    }

    /// 消费者显式释放。lease 不存在、已被回收或已过期都会失败，而不是被当成成功。
    pub fn release(&mut self, lease_id: &str, now_ms: i64) -> Result<String, MediaError> {
        if !self.leases.release(lease_id) {
            return Err(self.reject_request("unknown_or_released_lease"));
        }
        let Some(index) = self
            .retained
            .iter()
            .position(|held| held.lease_id.as_deref() == Some(lease_id))
        else {
            // lease 有效但没有对应 buffer：内部不一致，必须显式失败。
            return Err(self.reject_request("lease_without_buffer"));
        };
        let held = self.retained.remove(index);
        self.arena.release(held.offset as usize);
        self.released_total += 1;
        self.settle_residency(held.retained_at_ms, now_ms);
        Ok(held.buffer_id)
    }

    /// 回收超时未释放的 lease。被回收的 buffer 立即释放 slab，消费者再释放会得到明确失败。
    pub fn expire(&mut self, now_ms: i64) -> usize {
        // 先让 registry 回收，再按"lease 已不在册"反查保留表。顺序反了会留下
        // 挂着失效 lease 的 buffer：表里还在，消费者永远领不到，slab 也永远不释放。
        let reclaimed = self.leases.expire(now_ms);
        if reclaimed == 0 {
            return 0;
        }
        let expired: Vec<String> = self
            .retained
            .iter()
            .filter(|held| {
                held.lease_id
                    .as_deref()
                    .is_some_and(|lease_id| !self.lease_is_live(lease_id))
            })
            .filter_map(|held| held.lease_id.clone())
            .collect();
        let mut dropped = 0;
        for lease_id in &expired {
            if let Some(index) = self
                .retained
                .iter()
                .position(|held| held.lease_id.as_deref() == Some(lease_id.as_str()))
            {
                let held = self.retained.remove(index);
                self.arena.release(held.offset as usize);
                self.settle_residency(held.retained_at_ms, now_ms);
                dropped += 1;
            }
        }
        self.expired_total += dropped as u64;
        dropped
    }

    fn lease_is_live(&self, lease_id: &str) -> bool {
        self.leases
            .outstanding_ids()
            .iter()
            .any(|candidate| candidate == lease_id)
    }
}

/// 只由"有界容量"产生、且已经在 `count_rejection` 里计过数的拒绝码。
const CAPACITY_REJECTIONS: [&str; 2] = ["handoff_backlog_full", "handoff_kind_quota_full"];

fn is_capacity_rejection(reason: &str) -> bool {
    CAPACITY_REJECTIONS.contains(&reason)
}

fn contract_code(error: ContractError) -> String {
    error.0.to_string()
}

#[cfg(test)]
impl BufferHandoff {
    /// 测试辅助：领取后立刻释放，等价于消费者读完就走。
    fn release_lease_for_test(&mut self, buffer_id: &str) {
        let leased = self.acquire(buffer_id, None, 500, 0).unwrap();
        let lease_id = leased.descriptor.lease.unwrap().lease_id;
        self.release(&lease_id, 1).unwrap();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicI64, Ordering};

    const MB: usize = 1024 * 1024;

    /// 保留时刻用的可控时钟。驻留时长要能被断言，就不能靠 sleep。
    static TEST_CLOCK_MS: AtomicI64 = AtomicI64::new(0);

    fn test_clock() -> i64 {
        TEST_CLOCK_MS.load(Ordering::Relaxed)
    }

    fn set_test_clock(now_ms: i64) {
        TEST_CLOCK_MS.store(now_ms, Ordering::Relaxed);
    }

    fn frame_format() -> BufferFormat {
        BufferFormat {
            pixel_format: "RGBA".into(),
            width: 16,
            height: 16,
            ..Default::default()
        }
    }

    fn range(start_ms: i64, end_ms: i64) -> TimeRange {
        TimeRange { start_ms, end_ms }
    }

    fn handoff(limit: usize) -> BufferHandoff {
        BufferHandoff::new("arena-handoff", 4 * MB, None, limit).unwrap()
    }

    #[test]
    fn retained_bytes_survive_until_the_consumer_releases_them() {
        let mut plane = handoff(4);
        let bytes = vec![3u8; 64];
        let held = plane
            .retain(
                "buf-1",
                "video_frame",
                "stream-1",
                range(0, 40),
                BufferFormat {
                    pixel_format: "RGBA".into(),
                    width: 4,
                    height: 4,
                    ..Default::default()
                },
                &bytes,
            )
            .unwrap();
        assert_eq!(held.content_hash, content_hash(&bytes));
        assert!(plane.arena().read(held.offset as usize, 64).is_ok());

        let leased = plane.acquire("buf-1", None, 500, 1_000).unwrap();
        assert_eq!(leased.descriptor.locator.as_ref().unwrap().length, 64);
        assert_eq!(leased.descriptor.content_hash, content_hash(&bytes));
        let lease = leased.descriptor.lease.clone().unwrap();
        assert!(lease.read_only && lease.expires_at_unix_ms == 1_500);
        // 交接完成前 slab 仍然存活，这一点与进程内自校验路径不同。
        assert_eq!(plane.arena().live_slabs(), 1);

        assert_eq!(plane.release(&lease.lease_id, 1_100).unwrap(), "buf-1");
        assert_eq!(plane.arena().live_slabs(), 0);
        let stats = plane.stats();
        assert_eq!((stats.retained, stats.leased), (0, 0));
        assert_eq!(stats.released_total, 1);
        assert_eq!(stats.request_rejections, 0);
        assert_eq!(stats.retain_rejections, 0);
    }

    #[test]
    fn a_window_narrower_than_the_buffer_is_hashed_on_its_own() {
        let mut plane = handoff(4);
        let bytes: Vec<u8> = (0..64).collect();
        plane
            .retain(
                "buf-1",
                "audio_segment",
                "stream-1",
                range(0, 20),
                BufferFormat::default(),
                &bytes,
            )
            .unwrap();
        let leased = plane.acquire("buf-1", Some((8, 16)), 500, 0).unwrap();
        let locator = leased.descriptor.locator.clone().unwrap();
        assert_eq!((locator.offset, locator.length), (8, 16));
        assert_eq!(leased.descriptor.content_hash, content_hash(&bytes[8..24]));
    }

    #[test]
    fn requests_beyond_a_buffer_are_refused_instead_of_clamped() {
        let mut plane = handoff(4);
        plane
            .retain(
                "buf-1",
                "video_frame",
                "stream-1",
                range(0, 40),
                BufferFormat::default(),
                &[1u8; 32],
            )
            .unwrap();
        for window in [(0, 33), (32, 1), (u64::MAX, 1), (4, 0)] {
            let error = plane.acquire("buf-1", Some(window), 500, 0).unwrap_err();
            assert!(
                error.to_string().contains("mapping_out_of_range"),
                "{error}"
            );
        }
        let error = plane.acquire("buf-missing", None, 500, 0).unwrap_err();
        assert!(error.to_string().contains("unknown_buffer"), "{error}");
        let error = plane.acquire("buf-1", None, 0, 0).unwrap_err();
        assert!(error.to_string().contains("invalid_lease_ttl"), "{error}");
        let stats = plane.stats();
        assert_eq!(stats.leased, 0, "a rejected acquire hands out nothing");
        assert_eq!(stats.request_rejections, 6);
        assert_eq!(stats.retain_rejections, 0);
    }

    #[test]
    fn one_consumer_at_a_time_and_expiry_reclaims_the_buffer() {
        let mut plane = handoff(4);
        plane
            .retain(
                "buf-1",
                "video_frame",
                "stream-1",
                range(0, 40),
                BufferFormat::default(),
                &[7u8; 16],
            )
            .unwrap();
        let leased = plane.acquire("buf-1", None, 100, 1_000).unwrap();
        let lease_id = leased.descriptor.lease.as_ref().unwrap().lease_id.clone();
        let error = plane.acquire("buf-1", None, 100, 1_000).unwrap_err();
        assert!(
            error.to_string().contains("buffer_already_leased"),
            "{error}"
        );

        // 超时未释放：到期后被回收，slab 释放，迟到的 release 必须失败。
        assert_eq!(plane.expire(1_050), 0, "still inside the ttl");
        assert_eq!(plane.expire(1_200), 1);
        assert_eq!(plane.arena().live_slabs(), 0);
        let error = plane.release(&lease_id, 1_200).unwrap_err();
        assert!(
            error.to_string().contains("unknown_or_released_lease"),
            "{error}"
        );
        let stats = plane.stats();
        assert_eq!(stats.expired_total, 1);
        assert_eq!(stats.released_total, 0);
        assert_eq!(stats.retained, 0);
    }

    #[test]
    fn one_kind_cannot_take_the_whole_window() {
        // 8 条上限 → 每类 4 条。实时流里音频块 47 Hz、视频 keep 只有几 Hz：没有这条上限时
        // 先到的音频把 8 条全占满，视频一帧都进不来（直播实测就是这样，见验证记录）。
        let mut plane = BufferHandoff::new("arena-quota", MB, None, 8).unwrap();
        for index in 0..4 {
            plane
                .retain(
                    &format!("audio-{index}"),
                    "audio_pcm",
                    "stream-1",
                    range(index * 20, index * 20 + 20),
                    BufferFormat::default(),
                    b"pcm",
                )
                .unwrap();
        }
        let error = plane
            .retain(
                "audio-4",
                "audio_pcm",
                "stream-1",
                range(80, 100),
                BufferFormat::default(),
                b"pcm",
            )
            .unwrap_err();
        assert!(
            error.to_string().contains("handoff_kind_quota_full"),
            "the quota must fire before the table is full: {error}"
        );
        assert_eq!(plane.depth(), 4, "表里还有空位，但这一类不能再用");
        for index in 0..4 {
            plane
                .retain(
                    &format!("video-{index}"),
                    "video_frame",
                    "stream-1",
                    range(index * 40, index * 40 + 40),
                    BufferFormat::default(),
                    b"frame",
                )
                .unwrap();
        }
        assert_eq!(plane.depth(), 8, "两类各占一半后，窗口才真正满");
        // 两类都到配额后总上限接手：原因码必须换成"表满"，不能含糊成同一个。
        let error = plane
            .retain(
                "video-4",
                "video_frame",
                "stream-1",
                range(160, 200),
                BufferFormat::default(),
                b"frame",
            )
            .unwrap_err();
        assert!(
            error.to_string().contains("handoff_backlog_full"),
            "{error}"
        );
        let stats = plane.stats();
        assert_eq!(stats.retained_kind_limit, 4);
        assert_eq!(
            stats.retained_by_kind,
            BTreeMap::from([("audio_pcm".to_string(), 4), ("video_frame".to_string(), 4),]),
            "窗口由谁组成必须能读出来"
        );
        assert_eq!(stats.retained_kind_peak, 4);
        assert_eq!(stats.retain_rejections, 2);
        assert_eq!(
            stats.retain_rejection_reasons,
            BTreeMap::from([
                ("handoff_backlog_full".to_string(), 1),
                ("handoff_kind_quota_full".to_string(), 1),
            ])
        );
        assert_eq!(
            stats.offered_total,
            stats.retained_total + stats.retain_rejections,
            "第二条恒等式在配额拒绝下同样成立"
        );
    }

    #[test]
    fn the_backlog_and_the_arena_are_both_bounded() {
        let mut plane = BufferHandoff::new("arena-small", 64, None, 1).unwrap();
        plane
            .retain(
                "buf-1",
                "video_frame",
                "stream-1",
                range(0, 40),
                BufferFormat::default(),
                &[1u8; 32],
            )
            .unwrap();
        let error = plane
            .retain(
                "buf-2",
                "video_frame",
                "stream-1",
                range(40, 80),
                BufferFormat::default(),
                &[1u8; 32],
            )
            .unwrap_err();
        assert!(
            error.to_string().contains("handoff_backlog_full"),
            "{error}"
        );
        plane.release_lease_for_test("buf-1");
        // 释放后空间可复用；超过容量的请求仍然被拒绝。
        let error = plane
            .retain(
                "buf-3",
                "video_frame",
                "stream-1",
                range(80, 120),
                BufferFormat::default(),
                &[1u8; 128],
            )
            .unwrap_err();
        assert!(
            error.to_string().contains("arena_capacity_exceeded"),
            "{error}"
        );
    }

    #[test]
    fn a_shared_handoff_exposes_the_segment_name_but_never_a_path() {
        let mut plane = BufferHandoff::new("arena-shared", MB, Some(b"handoff-seed"), 4).unwrap();
        let name = plane.segment_name().unwrap().to_string();
        assert!(name.starts_with("/sp."));
        plane
            .retain(
                "buf-1",
                "video_frame",
                "stream-1",
                range(0, 40),
                BufferFormat::default(),
                b"payload",
            )
            .unwrap();
        let leased = plane.acquire("buf-1", None, 500, 0).unwrap();
        assert_eq!(leased.segment_name.as_deref(), Some(name.as_str()));
        let locator = leased.descriptor.locator.unwrap();
        assert_eq!(locator.handle, "arena-shared");
        assert!(!locator.handle.contains('/'), "the handle stays opaque");
        // 消费者映射整段后按窗口读取，读到的就是保留的那份字节。
        let consumer = crate::shm::ShmSegment::open(&name, MB).unwrap();
        let start = locator.offset as usize;
        assert_eq!(
            &consumer.as_slice()[start..start + locator.length as usize],
            b"payload"
        );
    }

    #[test]
    fn retention_peak_and_residency_are_measured_not_assumed() {
        let mut plane = handoff(4);
        plane.set_clock_for_test(test_clock);
        let bytes = vec![5u8; 64];
        set_test_clock(1_000);
        plane
            .retain(
                "buf-1",
                "video_frame",
                "stream-1",
                range(0, 40),
                frame_format(),
                &bytes,
            )
            .unwrap();
        set_test_clock(1_500);
        plane
            .retain(
                "buf-2",
                "video_frame",
                "stream-1",
                range(40, 80),
                frame_format(),
                &bytes,
            )
            .unwrap();
        assert_eq!(
            plane.stats().retained_peak,
            2,
            "the high-water mark is real"
        );

        // 消费者在 1_750 释放 buf-1：等待 750ms。
        let leased = plane.acquire("buf-1", None, 5_000, 1_750).unwrap();
        let lease_id = leased.descriptor.lease.clone().unwrap().lease_id;
        plane.release(&lease_id, 1_750).unwrap();
        let stats = plane.stats();
        assert_eq!(stats.residency_samples, 1);
        assert_eq!(stats.residency_total_ms, 750);
        assert_eq!(stats.residency_max_ms, 750);

        // buf-2 在 1_500 保留、3_000 被 TTL 回收：等待 1_500ms，成为新的最大值。
        plane.acquire("buf-2", None, 500, 1_600).unwrap();
        assert_eq!(plane.expire(3_000), 1);
        let stats = plane.stats();
        assert_eq!(stats.residency_samples, 2);
        assert_eq!(stats.residency_total_ms, 2_250, "750 + 1500");
        assert_eq!(stats.residency_max_ms, 1_500);
        assert_eq!(
            stats.retained_peak, 2,
            "releasing buffers never rewrites the peak downwards"
        );

        // 再保留一条但没人领：它没有归宿，因此没有等待时长可测。
        set_test_clock(3_500);
        plane
            .retain(
                "buf-3",
                "video_frame",
                "stream-1",
                range(80, 120),
                frame_format(),
                &bytes,
            )
            .unwrap();
        let stats = plane.stats();
        assert_eq!(plane.stats().retained_peak, 2);
        assert_eq!(
            stats.residency_samples, 2,
            "a buffer still waiting has no measured wait time"
        );
        assert_eq!(stats.retained, 1);
        // 用一条能独立核对的等式收尾：总和 = 最大值 + 另一条的等待时间。
        assert_eq!(stats.residency_total_ms, stats.residency_max_ms + 750);
    }
}
