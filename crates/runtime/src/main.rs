use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use prost::Message;
use sensoryplex_media::backpressure::BackpressurePolicy;
use sensoryplex_media::evidence::{
    EvidencePolicy, LedgerHandle, DEFAULT_CHANGE_THRESHOLD, DEFAULT_CONTEXT_FRAMES,
    DEFAULT_MAX_SEMANTIC_GAP_MS, DEFAULT_MIN_EVENT_INTERVAL_MS, DEFAULT_TEXT_CHANGE_THRESHOLD,
};
use sensoryplex_media::handoff::{HandoffStats, RetainPolicy, SharedHandoff};
use sensoryplex_media::live::{LiveConfig, LiveStats};
use sensoryplex_media::sampler::SamplingPolicy;
use sensoryplex_media::segment::{MAX_AUDIO_SEGMENT_MS, MIN_AUDIO_SEGMENT_MS};
use sensoryplex_media::source::{drain_source, FileSource, MediaSource, UnavailableSource};
use sensoryplex_media::MediaError;
use sensoryplex_runtime::{accelerator, capability, MediaQueueAdmission, Pipeline, ResidentLimits};
use sensoryplex_sdk::media::{
    DecodedDataPlane, LiveIngestReport, LiveStreamStats,
    MediaQueueAdmission as MediaQueueAdmissionProto, MediaSourceDescription, MediaSourceKind,
    MediaSourceRef, MediaTrack, ReplayReport, StreamStall,
};
use sensoryplex_sdk::runtime::{
    runtime_service_server::{RuntimeService, RuntimeServiceServer},
    DescribeCapabilitiesRequest, DescribeCapabilitiesResponse, HealthRequest, HealthResponse,
};
use tonic::{Request, Response, Status};

use crate::handoff_service::{HandoffService, DEFAULT_HANDOFF_TTL_MS};

mod handoff_service;
mod media_ledger;
mod timeline;

/// 大于该阈值的间隔会被计入时间轴断层（discontinuity），不会被平滑掉。
const GAP_THRESHOLD_MS: i64 = 1_000;
const DEFAULT_MAX_POINTS: usize = 1_000_000;
/// 等第一个消费者的上限。等不到就退出并报"账面对不上"，而不是永远挂着。
const DEFAULT_HANDOFF_WAIT_TIMEOUT_MS: u64 = 30_000;
/// 最后一个消费者调用之后多久关闭数据面。
const DEFAULT_HANDOFF_IDLE_TIMEOUT_MS: u64 = 3_000;
const DEFAULT_AUDIO_SEGMENT_MS: u32 = sensoryplex_media::segment::DEFAULT_AUDIO_SEGMENT_MS;

/// 列出本次构建仍无法完成的能力，按构建显式声明。报告中绝不声明二进制不具备的能力。
fn replay_blockers() -> Vec<String> {
    let mut blockers = Vec::new();
    if !cfg!(feature = "gstreamer") {
        blockers.push("gstreamer_decode_not_implemented".to_string());
    }
    // 跨进程交接（保留字节 + 独立进程按 lease 读取）已实现并在 make handoff-check 中验收，
    // 因此不再出现在 blockers 里。注意：一次"没带 --handoff-listen 的 replay"并没有
    // 运行消费者，这一点由输出里的 handoff=not_exercised 说明，而不是靠 blocker 暗示。
    blockers
}

/// 将文件解码为已校验的 descriptor。未启用 GStreamer feature 的构建没有解码器，
/// 会通过 `replay_blockers` 显式说明，而不是返回空 data plane。
///
/// `plane` 是**解码之前**就已经打开的共享保留面（见 `HandoffRunner::start`）：它必须由
/// 调用方创建，服务端才能在解码期间就开始把字节交给消费者。
#[cfg(feature = "gstreamer")]
#[allow(clippy::type_complexity)]
fn decode_pass(
    media: &str,
    stream_id: &str,
    arena_id: &str,
    args: &ResolvedRunArgs,
    plane: Option<Arc<SharedHandoff>>,
) -> Result<(Option<DecodedDataPlane>, Option<Arc<SharedHandoff>>, bool), String> {
    let run = sensoryplex_media::decode::DecodeRun {
        decode: sensoryplex_media::decode::DecodeConfig::default(),
        max_samples: args.max_points,
        audio_segment_ms: args.audio_segment_ms,
        audio_overlap_ms: args.audio_overlap_ms,
        sampling: args.sampling,
        handoff: plane,
        backpressure: args.backpressure,
        evidence: args.evidence,
        ledger: args.ledger.clone(),
        ledger_path: args.ledger_path.clone(),
    };
    sensoryplex_media::decode::decode_file(Path::new(media), stream_id, arena_id, &run)
        .map(|outcome| (Some(outcome.plane), outcome.handoff, outcome.truncated))
        .map_err(|error| error.to_string())
}

#[cfg(not(feature = "gstreamer"))]
#[allow(clippy::type_complexity)]
fn decode_pass(
    _media: &str,
    _stream_id: &str,
    _arena_id: &str,
    args: &ResolvedRunArgs,
    _plane: Option<Arc<SharedHandoff>>,
) -> Result<(Option<DecodedDataPlane>, Option<Arc<SharedHandoff>>, bool), String> {
    validate_run_args(args)?;
    Ok((None, None, false))
}

/// 两条解码路径共用的运行参数校验。缺 GStreamer 的构建也要走一遍：
/// 没有解码器不等于可以把非法配置报成"只是没有解码器"。
#[cfg(not(feature = "gstreamer"))]
fn validate_run_args(args: &ResolvedRunArgs) -> Result<(), String> {
    if args.max_points == 0 {
        return Err("max_points must be positive".into());
    }
    if !(MIN_AUDIO_SEGMENT_MS..=MAX_AUDIO_SEGMENT_MS).contains(&args.audio_segment_ms) {
        return Err("audio_segment_ms out of range".into());
    }
    if args.audio_overlap_ms >= args.audio_segment_ms {
        return Err("audio_overlap_ms must be less than audio_segment_ms".into());
    }
    SamplingPolicy::new(
        args.sampling.min_interval_ms,
        args.sampling.static_hold_ms,
        args.sampling.change_threshold,
    )
    .map_err(|error| error.to_string())?;
    BackpressurePolicy::new(
        args.backpressure.degraded_percent,
        args.backpressure.throttle_factor,
        args.backpressure.throttle_cap_ms,
    )
    .map_err(|error| error.to_string())?;
    if let Some(config) = &args.handoff {
        RetainPolicy::shared(config.arena_bytes, config.retained_limit)
            .map_err(|error| error.to_string())?;
    }
    // 语义覆盖策略与账本必须成对出现：只有策略没有账本时"每一帧都有记录"无法复核，
    // 只有账本没有策略时更不知道每帧该按什么判。缺 GStreamer 的构建同样要拒绝这种组合，
    // 不能把非法配置报成"只是没有解码器"。
    match (&args.evidence, &args.ledger, &args.ledger_path) {
        (Some(policy), _, _) => {
            if !(sensoryplex_media::evidence::MIN_MAX_SEMANTIC_GAP_MS
                ..=sensoryplex_media::evidence::MAX_MAX_SEMANTIC_GAP_MS)
                .contains(&policy.max_semantic_gap_ms)
            {
                return Err("evidence_max_gap_ms out of range".into());
            }
        }
        (None, None, None) => {}
        (None, _, _) => return Err("an evidence ledger requires --evidence".into()),
    }
    Ok(())
}

const REPLAY_USAGE: &str = "usage: sensoryplex-runtime replay <pipeline.yaml> <media-path> --report <report.pb> [--max-points N] [--audio-segment-ms N] [--audio-overlap-ms N] [--sampling-min-interval-ms N] [--sampling-static-hold-ms N] [--sampling-change-threshold N] [--backpressure-degraded-percent N] [--backpressure-throttle-factor N] [--backpressure-throttle-cap-ms N] [--evidence] [--evidence-max-gap-ms N] [--evidence-context-before N] [--evidence-context-after N] [--evidence-change-threshold N] [--evidence-text-change-threshold N] [--evidence-min-event-interval-ms N] [--evidence-ledger <path>] [--handoff-listen 127.0.0.1:PORT] [--handoff-arena-bytes N] [--handoff-retained-limit N] [--handoff-ttl-ms N] [--handoff-wait-timeout-ms N] [--handoff-idle-timeout-ms N]";

/// 跨进程交接的服务端配置。只有在显式给出 `--handoff-listen` 时才存在：
/// 不保留字节的运行仍然是合法运行，但它不算"消费方已验证"。
#[derive(Clone)]
struct HandoffArgs {
    listen: std::net::SocketAddr,
    ttl_ms: u32,
    arena_bytes: usize,
    retained_limit: usize,
    wait_timeout_ms: u64,
    idle_timeout_ms: u64,
}

impl HandoffArgs {
    /// 数据面只在同一台主机上有意义（共享内存不可跨机映射），因此监听地址必须是回环。
    fn loopback(raw: &str) -> Result<std::net::SocketAddr, String> {
        let address: std::net::SocketAddr = raw
            .parse()
            .map_err(|_| format!("invalid --handoff-listen: {raw}"))?;
        if !address.ip().is_loopback() {
            return Err(format!(
                "handoff data plane is host-local; refusing non-loopback listener {address}"
            ));
        }
        Ok(address)
    }
}

/// replay 与 ingest 共用的运行参数。
///
/// 两条路径走的是同一条 arena / descriptor / lease 与（可选）交接链路，
/// 因此这些选项的默认值与越界行为必须完全一致；这里只实现一次，
/// 避免两份解析逻辑慢慢漂移。
struct MediaRunArgs {
    /// 时间轴锚点与解码样本数量的上限；不存在无界模式。
    max_points: usize,
    audio_segment_ms: u32,
    audio_overlap_ms: u32,
    sampling_min_interval_ms: i64,
    sampling_static_hold_ms: i64,
    sampling_change_threshold: u32,
    backpressure_degraded_percent: u32,
    backpressure_throttle_factor: u32,
    backpressure_throttle_cap_ms: i64,
    /// `None` 表示本次运行不暴露数据面（也不声称有消费者）。
    handoff_listen: Option<std::net::SocketAddr>,
    handoff_arena_bytes: usize,
    handoff_retained_limit: usize,
    handoff_ttl_ms: u32,
    handoff_wait_timeout_ms: u64,
    handoff_idle_timeout_ms: u64,
    /// 是否启用语义覆盖判别。`false` 时报告里不会出现语义账本，
    /// 因此"没有语义账本"永远不会被读成"每一帧都没变"。
    evidence_enabled: bool,
    evidence_max_gap_ms: i64,
    evidence_context_before: u32,
    evidence_context_after: u32,
    evidence_change_threshold: u32,
    evidence_text_change_threshold: u32,
    evidence_min_event_interval_ms: i64,
    /// 逐帧账本输出路径。`None` 表示只保留报告里的计数与有界预览。
    evidence_ledger: Option<String>,
    /// 是否出现过任何 `--evidence-*` 取值。它让"给了子选项却没开开关"变成显式错误，
    /// 而不是被静默忽略。
    evidence_touched: bool,
}

/// 已校验、可直接交给解码路径的运行参数。
struct ResolvedRunArgs {
    max_points: usize,
    audio_segment_ms: u32,
    audio_overlap_ms: u32,
    sampling: SamplingPolicy,
    backpressure: BackpressurePolicy,
    handoff: Option<HandoffArgs>,
    evidence: Option<EvidencePolicy>,
    ledger: Option<LedgerHandle>,
    ledger_path: Option<String>,
}

impl ResolvedRunArgs {
    /// 本次运行实际使用的保留窗口深度。没有 `--handoff-listen` 时取内置默认值——
    /// 它仍然是这条队列的真实深度，因此同样要过分级准入（ADR-019）。
    fn retained_limit(&self) -> usize {
        self.handoff.as_ref().map_or(
            sensoryplex_media::handoff::DEFAULT_RETAINED_LIMIT,
            |config| config.retained_limit,
        )
    }
}

/// 取一个带值的选项：缺值或缺值非法都报明确原因，不做默认值兜底。
fn parse_arg<T: std::str::FromStr>(flag: &str, value: Option<&String>) -> Result<T, String> {
    value
        .ok_or_else(|| format!("missing value for {flag}"))?
        .parse()
        .map_err(|_| format!("invalid {flag}"))
}

impl MediaRunArgs {
    fn new() -> Self {
        Self {
            max_points: DEFAULT_MAX_POINTS,
            audio_segment_ms: DEFAULT_AUDIO_SEGMENT_MS,
            audio_overlap_ms: 0,
            sampling_min_interval_ms: sensoryplex_media::sampler::DEFAULT_MIN_INTERVAL_MS,
            sampling_static_hold_ms: sensoryplex_media::sampler::DEFAULT_STATIC_HOLD_MS,
            sampling_change_threshold: sensoryplex_media::sampler::DEFAULT_CHANGE_THRESHOLD,
            backpressure_degraded_percent:
                sensoryplex_media::backpressure::DEFAULT_DEGRADED_PERCENT,
            backpressure_throttle_factor: sensoryplex_media::backpressure::DEFAULT_THROTTLE_FACTOR,
            backpressure_throttle_cap_ms: sensoryplex_media::backpressure::DEFAULT_THROTTLE_CAP_MS,
            handoff_listen: None,
            handoff_arena_bytes: sensoryplex_media::handoff::DEFAULT_RETAIN_ARENA_BYTES,
            handoff_retained_limit: sensoryplex_media::handoff::DEFAULT_RETAINED_LIMIT,
            handoff_ttl_ms: DEFAULT_HANDOFF_TTL_MS,
            handoff_wait_timeout_ms: DEFAULT_HANDOFF_WAIT_TIMEOUT_MS,
            handoff_idle_timeout_ms: DEFAULT_HANDOFF_IDLE_TIMEOUT_MS,
            evidence_enabled: false,
            evidence_max_gap_ms: DEFAULT_MAX_SEMANTIC_GAP_MS,
            evidence_context_before: DEFAULT_CONTEXT_FRAMES,
            evidence_context_after: DEFAULT_CONTEXT_FRAMES,
            evidence_change_threshold: DEFAULT_CHANGE_THRESHOLD,
            evidence_text_change_threshold: DEFAULT_TEXT_CHANGE_THRESHOLD,
            evidence_min_event_interval_ms: DEFAULT_MIN_EVENT_INTERVAL_MS,
            evidence_ledger: None,
            evidence_touched: false,
        }
    }

    /// 消费 `index` 处的一个共享选项。
    /// 返回 `Ok(Some(next))` 表示已消费到下标 `next`；`Ok(None)` 表示该 token 不属于共享集合。
    fn take(&mut self, args: &[String], index: usize) -> Result<Option<usize>, String> {
        let flag = args[index].as_str();
        let next = index + 2;
        match flag {
            "--max-points" => self.max_points = parse_arg(flag, args.get(index + 1))?,
            "--audio-segment-ms" => {
                self.audio_segment_ms = parse_arg(flag, args.get(index + 1))?;
            }
            "--audio-overlap-ms" => {
                self.audio_overlap_ms = parse_arg(flag, args.get(index + 1))?;
            }
            "--sampling-min-interval-ms" => {
                self.sampling_min_interval_ms = parse_arg(flag, args.get(index + 1))?;
            }
            "--sampling-static-hold-ms" => {
                self.sampling_static_hold_ms = parse_arg(flag, args.get(index + 1))?;
            }
            "--sampling-change-threshold" => {
                self.sampling_change_threshold = parse_arg(flag, args.get(index + 1))?;
            }
            "--backpressure-degraded-percent" => {
                self.backpressure_degraded_percent = parse_arg(flag, args.get(index + 1))?;
            }
            "--backpressure-throttle-factor" => {
                self.backpressure_throttle_factor = parse_arg(flag, args.get(index + 1))?;
            }
            "--backpressure-throttle-cap-ms" => {
                self.backpressure_throttle_cap_ms = parse_arg(flag, args.get(index + 1))?;
            }
            "--handoff-listen" => {
                let raw: String = parse_arg(flag, args.get(index + 1))?;
                self.handoff_listen = Some(HandoffArgs::loopback(&raw)?);
            }
            "--handoff-arena-bytes" => {
                self.handoff_arena_bytes = parse_arg(flag, args.get(index + 1))?;
            }
            "--handoff-retained-limit" => {
                self.handoff_retained_limit = parse_arg(flag, args.get(index + 1))?;
            }
            "--handoff-ttl-ms" => {
                self.handoff_ttl_ms = parse_arg(flag, args.get(index + 1))?;
            }
            "--handoff-wait-timeout-ms" => {
                self.handoff_wait_timeout_ms = parse_arg(flag, args.get(index + 1))?;
            }
            "--handoff-idle-timeout-ms" => {
                self.handoff_idle_timeout_ms = parse_arg(flag, args.get(index + 1))?;
            }
            // 语义覆盖是布尔开关：它没有取值，因此消费到 `index + 1`，不是 `index + 2`。
            "--evidence" => {
                self.evidence_enabled = true;
                return Ok(Some(index + 1));
            }
            "--evidence-max-gap-ms" => {
                self.evidence_touched = true;
                self.evidence_max_gap_ms = parse_arg(flag, args.get(index + 1))?;
            }
            "--evidence-context-before" => {
                self.evidence_touched = true;
                self.evidence_context_before = parse_arg(flag, args.get(index + 1))?;
            }
            "--evidence-context-after" => {
                self.evidence_touched = true;
                self.evidence_context_after = parse_arg(flag, args.get(index + 1))?;
            }
            "--evidence-change-threshold" => {
                self.evidence_touched = true;
                self.evidence_change_threshold = parse_arg(flag, args.get(index + 1))?;
            }
            "--evidence-text-change-threshold" => {
                self.evidence_touched = true;
                self.evidence_text_change_threshold = parse_arg(flag, args.get(index + 1))?;
            }
            "--evidence-min-event-interval-ms" => {
                self.evidence_touched = true;
                self.evidence_min_event_interval_ms = parse_arg(flag, args.get(index + 1))?;
            }
            "--evidence-ledger" => {
                self.evidence_touched = true;
                self.evidence_ledger = Some(parse_arg(flag, args.get(index + 1))?);
            }
            _ => return Ok(None),
        }
        Ok(Some(next))
    }

    /// 越界策略一律是拒绝，而不是夹取到"看起来合理"的范围：夹取会悄悄改变抽帧与交接语义。
    fn resolve(
        &self,
        positional: &[String],
        expected_positional: usize,
        usage: &str,
    ) -> Result<ResolvedRunArgs, String> {
        if positional.len() != expected_positional || self.max_points == 0 {
            return Err(usage.into());
        }
        if !(MIN_AUDIO_SEGMENT_MS..=MAX_AUDIO_SEGMENT_MS).contains(&self.audio_segment_ms) {
            return Err(format!(
                "audio-segment-ms must be between {MIN_AUDIO_SEGMENT_MS} and {MAX_AUDIO_SEGMENT_MS}"
            ));
        }
        if self.audio_overlap_ms >= self.audio_segment_ms {
            return Err("audio-overlap-ms must be less than audio-segment-ms".into());
        }
        let sampling = SamplingPolicy::new(
            self.sampling_min_interval_ms,
            self.sampling_static_hold_ms,
            self.sampling_change_threshold,
        )
        .map_err(|error| error.to_string())?;
        let backpressure = BackpressurePolicy::new(
            self.backpressure_degraded_percent,
            self.backpressure_throttle_factor,
            self.backpressure_throttle_cap_ms,
        )
        .map_err(|error| error.to_string())?;
        let handoff = match self.handoff_listen {
            None => None,
            Some(listen) => {
                // 0 表示"用默认 TTL"；其余取值同样按越界即拒绝处理。
                let ttl_ms = if self.handoff_ttl_ms == 0 {
                    DEFAULT_HANDOFF_TTL_MS
                } else {
                    self.handoff_ttl_ms
                };
                if !(sensoryplex_media::handoff::MIN_LEASE_TTL_MS
                    ..=sensoryplex_media::handoff::MAX_LEASE_TTL_MS)
                    .contains(&ttl_ms)
                {
                    return Err(format!(
                        "handoff-ttl-ms must be between {} and {}",
                        sensoryplex_media::handoff::MIN_LEASE_TTL_MS,
                        sensoryplex_media::handoff::MAX_LEASE_TTL_MS
                    ));
                }
                if self.handoff_idle_timeout_ms == 0 || self.handoff_wait_timeout_ms == 0 {
                    return Err("handoff timeouts must be positive".into());
                }
                RetainPolicy::shared(self.handoff_arena_bytes, self.handoff_retained_limit)
                    .map_err(|error| error.to_string())?;
                Some(HandoffArgs {
                    listen,
                    ttl_ms,
                    arena_bytes: self.handoff_arena_bytes,
                    retained_limit: self.handoff_retained_limit,
                    wait_timeout_ms: self.handoff_wait_timeout_ms,
                    idle_timeout_ms: self.handoff_idle_timeout_ms,
                })
            }
        };
        // 语义覆盖策略与账本：两者必须一起决定，因为"有账本却没有策略"或
        // "有策略却不落账本"都会让"每一帧都有记录"这句话无法核对。
        let (evidence, ledger, ledger_path) = if self.evidence_enabled {
            let policy = EvidencePolicy::new(
                self.evidence_max_gap_ms,
                self.evidence_context_before,
                self.evidence_context_after,
                self.evidence_change_threshold,
                self.evidence_text_change_threshold,
                self.evidence_min_event_interval_ms,
            )
            .map_err(|error| error.to_string())?;
            match &self.evidence_ledger {
                Some(path) => {
                    let sink = crate::media_ledger::JsonlLedgerSink::open(path)?;
                    let handle = LedgerHandle::new(Arc::new(sink));
                    (Some(policy), Some(handle), Some(path.clone()))
                }
                None => (Some(policy), None, None),
            }
        } else {
            if self.evidence_touched {
                return Err("--evidence-* options require --evidence".into());
            }
            (None, None, None)
        };
        Ok(ResolvedRunArgs {
            max_points: self.max_points,
            audio_segment_ms: self.audio_segment_ms,
            audio_overlap_ms: self.audio_overlap_ms,
            sampling,
            backpressure,
            handoff,
            evidence,
            ledger,
            ledger_path,
        })
    }
}

/// 把命令行的共享选项与本命令专属选项分开：只有命令自己认识的位置参数与专属选项
/// 才留在各自的 `parse` 里，其余全部交给 `MediaRunArgs`。
fn split_positional(
    args: &[String],
    shared: &mut MediaRunArgs,
    owns: impl Fn(&str) -> bool,
) -> Result<(Vec<String>, Vec<usize>), String> {
    let mut positional = Vec::new();
    let mut owned = Vec::new();
    let mut index = 0;
    while index < args.len() {
        if owns(args[index].as_str()) {
            owned.push(index);
            index += 2;
            continue;
        }
        if let Some(next) = shared.take(args, index)? {
            index = next;
            continue;
        }
        positional.push(args[index].clone());
        index += 1;
    }
    Ok((positional, owned))
}

/// 取一个本命令专属选项的取值（`--report` 一类）。
fn owned_value(args: &[String], index: usize) -> Option<String> {
    args.get(index + 1).cloned()
}

struct ReplayArgs {
    pipeline: String,
    media: String,
    report: String,
    run: ResolvedRunArgs,
}

impl ReplayArgs {
    fn parse(args: &[String]) -> Result<Self, String> {
        let mut shared = MediaRunArgs::new();
        let (positional, owned) = split_positional(args, &mut shared, |token| token == "--report")?;
        let report = owned.last().and_then(|index| owned_value(args, *index));
        let run = shared.resolve(&positional, 2, REPLAY_USAGE)?;
        Ok(Self {
            pipeline: positional[0].clone(),
            media: positional[1].clone(),
            report: report.ok_or("replay requires --report <report.pb>")?,
            run,
        })
    }
}

const INGEST_USAGE: &str = "usage: sensoryplex-runtime ingest <pipeline.yaml> --report <report.pb> [--duration-ms N] [--stall-threshold-ms N] [--max-stalls N] [--max-points N] [--audio-segment-ms N] [--sampling-min-interval-ms N] [--sampling-static-hold-ms N] [--sampling-change-threshold N] [--backpressure-degraded-percent N] [--backpressure-throttle-factor N] [--backpressure-throttle-cap-ms N] [--evidence] [--evidence-max-gap-ms N] [--evidence-ledger <path>] [--handoff-listen 127.0.0.1:PORT] [--handoff-arena-bytes N] [--handoff-retained-limit N] [--handoff-ttl-ms N] [--handoff-wait-timeout-ms N] [--handoff-idle-timeout-ms N]";

/// 单次实时接入的参数。URI 不在命令行上：它只从环境变量读（见 `ingest`）。
struct LiveArgs {
    pipeline: String,
    report: String,
    run: ResolvedRunArgs,
    live: LiveConfig,
}

impl LiveArgs {
    /// 本命令专属的选项名。URI 与 streamid 都不在这里，避免它们出现在进程参数里。
    fn own_token(token: &str) -> bool {
        matches!(
            token,
            "--report" | "--duration-ms" | "--stall-threshold-ms" | "--max-stalls"
        )
    }

    fn parse(args: &[String]) -> Result<Self, String> {
        let mut shared = MediaRunArgs::new();
        let mut report = None;
        let mut duration_ms = sensoryplex_media::live::DEFAULT_LIVE_DURATION_MS;
        let mut stall_threshold_ms = sensoryplex_media::live::DEFAULT_STALL_THRESHOLD_MS;
        let mut max_stalls = sensoryplex_media::live::DEFAULT_MAX_STALLS;
        let (positional, owned) = split_positional(args, &mut shared, Self::own_token)?;
        for index in owned {
            let flag = args[index].as_str();
            match flag {
                "--report" => report = owned_value(args, index),
                "--duration-ms" => duration_ms = parse_arg(flag, args.get(index + 1))?,
                "--stall-threshold-ms" => {
                    stall_threshold_ms = parse_arg(flag, args.get(index + 1))?;
                }
                "--max-stalls" => max_stalls = parse_arg(flag, args.get(index + 1))?,
                other => return Err(format!("unknown ingest option: {other}")),
            }
        }
        let run = shared.resolve(&positional, 1, INGEST_USAGE)?;
        let live = LiveConfig {
            duration_ms,
            stall_threshold_ms,
            max_stalls,
        };
        // 窗口长度、卡顿阈值与预算都会改变报告语义，因此越界即拒绝。
        live.validate().map_err(|error| error.to_string())?;
        Ok(Self {
            pipeline: positional[0].clone(),
            report: report.ok_or("ingest requires --report <report.pb>")?,
            run,
            live,
        })
    }
}

fn open_source<'a>(
    pipeline: &'a Pipeline,
    media_path: &str,
) -> Result<Box<dyn MediaSource + 'a>, String> {
    let uri_secret_ref = &pipeline.spec.source.uri_secret_ref;
    match pipeline.spec.source.r#type.as_str() {
        "file" => FileSource::open(uri_secret_ref, Path::new(media_path))
            .map(|source| Box::new(source) as Box<dyn MediaSource>)
            .map_err(|error| error.to_string()),
        "srt" => Ok(Box::new(UnavailableSource::srt("", uri_secret_ref))),
        other => Err(format!("unsupported_source_type: {other}")),
    }
}

async fn replay(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let args = ReplayArgs::parse(args).map_err(std::io::Error::other)?;
    let pipeline = Pipeline::parse(&std::fs::read_to_string(&args.pipeline)?)
        .map_err(std::io::Error::other)?;
    let media_queue = admit_media_queue(&pipeline, &args.run).map_err(std::io::Error::other)?;
    let mut source = open_source(&pipeline, &args.media).map_err(std::io::Error::other)?;
    let description = source.describe().clone();
    let duration_ms = description.duration_ms;

    let mut report = ReplayReport {
        source: Some(description.clone()),
        platform: capability::platform(),
        blockers: replay_blockers(),
        // 只有真正通过媒体验收的运行才允许设置该标志；本次构建无法达成。
        golden_path_verified: false,
        media_queue: Some(media_queue_admission_field(&media_queue)),
        ..Default::default()
    };

    let drained = drain_source(
        source.as_mut(),
        duration_ms,
        GAP_THRESHOLD_MS,
        args.run.max_points,
    );
    let (intervals, truncated) = match drained {
        Ok(result) => result,
        Err(MediaError::UnsupportedSource(reason)) => {
            report.blockers.push(reason);
            write_report_atomically(&args.report, report.encode_to_vec())?;
            return Err(std::io::Error::other("media_source_not_implemented").into());
        }
        Err(error) => return Err(error.into()),
    };

    let mut reasons: BTreeSet<String> = BTreeSet::new();
    let mut dropped_items = source.dropped_items();
    reasons.extend(source.drop_reasons());
    for interval in &intervals {
        report.anchors.extend(interval.anchors.iter().cloned());
        report.out_of_order_items += interval.out_of_order_items;
        report.gap_items += interval.gap_items;
        dropped_items += interval.dropped_items;
        reasons.extend(interval.drop_reasons.iter().cloned());
    }
    // 交接状态写进报告本身：不跑消费方的运行不能靠"没写"来暗示任何结论。
    report.handoff_state = match &args.run.handoff {
        Some(config) => format!("exposed_on={}", config.listen),
        None => "not_exercised".to_string(),
    };
    report.emitted_anchors = report.anchors.len() as u64;
    report.dropped_items = dropped_items;
    report.decoded_items = report.emitted_anchors + dropped_items;
    report.drop_reasons = reasons.into_iter().collect();
    if truncated {
        report.blockers.push("max_points_truncated".into());
    }

    // 上面的锚点来自 ffprobe。本步骤对同一文件再做解码，使报告携带真实的 descriptor、
    // 真实的 lease 与真实的 arena；若做不到则给出明确原因。
    let (arena_id, stream_id) = arena_identity(&description);
    // 数据面必须在解码**之前**打开并起服务端：消费者要能和生产者并发领料（见 HandoffRunner）。
    let handoff_plane =
        open_handoff_plane(&args.run, &arena_id).map_err(std::io::Error::other)?;
    let runner =
        HandoffRunner::start(&args.run, handoff_plane.clone()).map_err(std::io::Error::other)?;
    let retained;
    match decode_pass(
        &args.media,
        &stream_id,
        &arena_id,
        &args.run,
        handoff_plane,
    ) {
        Ok((plane, handoff, decode_truncated)) => {
            report.decoded = plane;
            retained = handoff;
            if decode_truncated {
                report.blockers.push("decode_truncated".into());
            }
        }
        Err(reason) => {
            // 生产者失败：立刻收尾服务端，不让它替一个已经失败的运行继续等消费者。
            runner.producer_failed();
            // 解码尝试失败本身就是证据，不是沉默：写入报告中，
            // 然后让命令失败，避免任何调用方把它误判为通过。
            report.decoded = Some(DecodedDataPlane {
                failure_reasons: vec![reason.clone()],
                ..Default::default()
            });
            report.blockers.push("decode_failed".into());
            write_report_atomically(&args.report, report.encode_to_vec())?;
            return Err(std::io::Error::other(format!("decode_failed: {reason}")).into());
        }
    }

    write_report_atomically(&args.report, report.encode_to_vec())?;
    let plane = report.decoded.as_ref();
    // 交接是否真的跑过，必须在同一行里说清楚：没跑过就不能被读成"消费方已验证"。
    let handoff_note = match (&retained, &args.run.handoff) {
        (Some(_), Some(config)) => format!("exposed_on={}", config.listen),
        (_, Some(_)) => "unavailable_this_build".to_string(),
        (_, None) => report.handoff_state.clone(),
    };
    println!(
        "replay report written: platform={} anchors={} decoded_items={} dropped={} gaps={} out_of_order={} descriptors={} rejected={} leases_issued={} leases_released={} segments={} arena_peak_bytes={} sampling_kept={}/{} handoff={} blockers={}",
        report.platform,
        report.emitted_anchors,
        report.decoded_items,
        report.dropped_items,
        report.gap_items,
        report.out_of_order_items,
        plane.map_or(0, |plane| plane.descriptors_validated),
        // 源里有、但被准入拒绝的轨道数。它与 descriptors 分开计：被拒轨道一个描述符都不产生，
        // 如果只报 descriptors，被拒轨道读起来就像"源里没有这条轨道"。
        plane.map_or(0, |plane| plane.rejected_tracks.len()),
        plane.map_or(0, |plane| plane.leases_issued),
        plane.map_or(0, |plane| plane.leases_released),
        plane
            .and_then(|plane| plane.audio_segments.as_ref())
            .map_or(0, |segments| segments.segments),
        plane.map_or(0, |plane| plane.arena_peak_bytes),
        plane
            .and_then(|plane| plane.sampling.first())
            .map_or(0, |sampling| sampling.kept),
        plane
            .and_then(|plane| plane.sampling.first())
            .map_or(0, |sampling| sampling.observed),
        handoff_note,
        report.blockers.join(",")
    );
    println!("{}", media_queue.describe());
    println!("{}", describe_backpressure(plane));
    match (&retained, &args.run.handoff) {
        // 报告已经落盘，消费者据此知道"账本完整"；现在收尾服务端并核对释放/过期账。
        (Some(_), Some(_)) => runner.producer_finished().map_err(std::io::Error::other)?,
        // 声明要暴露数据面，却拿不到保留的字节：这是失败，不是"跳过"。
        (None, Some(config)) => {
            runner.producer_failed();
            return Err(format!(
                "handoff_requested_but_no_decoded_bytes: this build cannot serve {}",
                config.listen
            )
            .into())
        }
        (_, None) => runner.producer_finished().map_err(std::io::Error::other)?,
    }
    Ok(())
}

/// 报告用"写临时文件 + 原子改名"落盘。
///
/// 报告先于服务端收尾落盘，是并发消费者判断"逐帧账本已经完整、可以把保留项一次还干净"
/// 的唯一凭据（见 `HandoffRunner`）。就地覆写会让读到半个文件的消费者把一次完整运行误判成
/// 账本缺行，因此这里必须是改名而不是原地写。
fn write_report_atomically(path: &str, bytes: Vec<u8>) -> std::io::Result<()> {
    let path = std::path::Path::new(path);
    let staging = path.with_extension("partial");
    std::fs::write(&staging, bytes)?;
    std::fs::rename(&staging, path)
}

/// 把常驻分级读进一次媒体运行，并按 pipeline 声明值与实际保留窗口做准入（ADR-019）。
///
/// 分级是上限而不是默认值：越界就在这里显式失败，运行不会带着一个"被悄悄夹到上限"的
/// 队列继续跑，也不会把 pipeline 文件里的声明值改写掉。
fn admit_media_queue(
    pipeline: &Pipeline,
    args: &ResolvedRunArgs,
) -> Result<MediaQueueAdmission, String> {
    let limits = ResidentLimits::from_env()?;
    MediaQueueAdmission::new(&limits, pipeline.spec.queue_capacity, args.retained_limit())
}

/// 报告里的分级准入字段。`state` 只会是 `not_injected` 或 `admitted`：越界的运行在写报告
/// 之前就以显式错误退出，所以报告里不会出现"夹取后的成功"。
fn media_queue_admission_field(admission: &MediaQueueAdmission) -> MediaQueueAdmissionProto {
    MediaQueueAdmissionProto {
        state: admission.state().to_string(),
        tier: admission.tier.clone().unwrap_or_default(),
        declared_capacity: admission.declared_capacity.get() as u64,
        tier_capacity: admission
            .tier_capacity
            .map_or(0, |capacity| capacity.get() as u64),
        retained_limit: admission.retained_limit as u64,
    }
}

/// 背压的可观察量必须能被一行读出来，而且**没有测量**与"测到零压力"要能区分开：
/// 前者的 state 后面跟着 `observed=false`，后者才是三条队列水位加计数。
fn describe_backpressure(plane: Option<&DecodedDataPlane>) -> String {
    let report = plane.and_then(|plane| plane.backpressure.as_ref());
    let Some(report) = report else {
        return "backpressure observed=false reason=no_decoded_plane".to_string();
    };
    if !report.observed {
        return format!("backpressure observed=false state={}", report.state);
    }
    let queues = report
        .queues
        .iter()
        .map(|queue| {
            format!(
                "{}[{}]={}/{}/{}",
                queue.name, queue.unit, queue.current, queue.peak, queue.capacity
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    let reasons = if report.drop_reasons.is_empty() {
        "none".to_string()
    } else {
        report
            .drop_reasons
            .iter()
            .map(|entry| format!("{}:{}", entry.reason, entry.count))
            .collect::<Vec<_>>()
            .join(",")
    };
    // 保留表是所有 buffer 种类共用的：只给总数会让人把它读成"视频帧全丢了"。
    let kinds = if report.drop_kinds.is_empty() {
        "none".to_string()
    } else {
        report
            .drop_kinds
            .iter()
            .map(|entry| format!("{}:{}", entry.kind, entry.count))
            .collect::<Vec<_>>()
            .join(",")
    };
    format!(
        "backpressure observed=true state={} degraded_entries={} saturated_entries={} queues={} dropped={} reasons={} kinds={} timeouts={} residency_max_ms={} residency_avg_ms={} residency_samples={} throttled={} throttle_factor={}",
        report.state,
        report.degraded_entries,
        report.saturated_entries,
        queues,
        report.dropped_total,
        reasons,
        kinds,
        report.timeouts_total,
        report.residency_max_ms,
        report.residency_avg_ms,
        report.residency_samples,
        report.sampling_throttled_samples,
        report.throttle_factor
    )
}

/// 实时接入：在有限窗口内从 SRT 拉流、解码、写进与离线路径**同一条**数据面，
/// 并测量断流与恢复。
///
/// URI 可能带 streamid 或凭据，因此它既不进命令行也不进报告与日志：
/// 只从 pipeline 的 `uri_secret_ref` 派生出的环境变量里读，报告里只写引用名。
async fn ingest(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let args = LiveArgs::parse(args).map_err(std::io::Error::other)?;
    let pipeline = Pipeline::parse(&std::fs::read_to_string(&args.pipeline)?)
        .map_err(std::io::Error::other)?;
    let media_queue = admit_media_queue(&pipeline, &args.run).map_err(std::io::Error::other)?;
    let source = &pipeline.spec.source;
    if source.r#type != "srt" {
        return Err(format!("ingest_requires_srt_source: {}", source.r#type).into());
    }
    let uri_secret_ref = source.uri_secret_ref.clone();
    let env_name = live_uri_env_name(&uri_secret_ref);
    let uri = std::env::var(&env_name).map_err(|_| format!("live_uri_env_missing: {env_name}"))?;
    if uri.trim().is_empty() {
        return Err(format!("live_uri_env_empty: {env_name}").into());
    }

    let handoff_state = match &args.run.handoff {
        Some(config) => format!("exposed_on={}", config.listen),
        None => "not_exercised".to_string(),
    };
    let (arena_id, stream_id) = live_identity(&uri_secret_ref);
    let mut report = LiveIngestReport {
        source: Some(live_description(&uri_secret_ref, &stream_id, None)),
        platform: capability::platform(),
        blockers: replay_blockers(),
        // 只有真正通过媒体验收的运行才允许设置该标志；本次构建无法达成。
        golden_path_verified: false,
        handoff_state,
        stream: Some(LiveStreamStats {
            uri_secret_ref: uri_secret_ref.clone(),
            ..Default::default()
        }),
        media_queue: Some(media_queue_admission_field(&media_queue)),
        ..Default::default()
    };
    // 只打印引用名与环境变量名：URI 本身（含 streamid/凭据）不出现在任何输出里。
    println!(
        "live ingest: source_ref={} uri_env={} window_ms={} stall_threshold_ms={} max_stalls={}",
        uri_secret_ref,
        env_name,
        args.live.duration_ms,
        args.live.stall_threshold_ms,
        args.live.max_stalls
    );

    // 与 replay 同一口径：数据面先起，消费者才能与拉流并发领料。
    let handoff_plane =
        open_handoff_plane(&args.run, &arena_id).map_err(std::io::Error::other)?;
    let runner =
        HandoffRunner::start(&args.run, handoff_plane.clone()).map_err(std::io::Error::other)?;
    let retained;
    match live_pass(
        &uri,
        &stream_id,
        &arena_id,
        &args.run,
        &args.live,
        handoff_plane,
    ) {
        Ok((plane, handoff, stats, truncated)) => {
            report.source = Some(live_description(&uri_secret_ref, &stream_id, Some(&plane)));
            report.stream = Some(live_stream_stats(&uri_secret_ref, &args.live, &stats));
            report.decoded = Some(plane);
            retained = handoff;
            if truncated {
                report.blockers.push("max_points_truncated".into());
            }
            if !stats.has_samples() {
                // 窗口里一个样本都没有：这不是"成功但为空"，必须显式失败。
                report
                    .blockers
                    .push("live_window_produced_no_samples".into());
                write_report_atomically(&args.report, report.encode_to_vec())?;
                return Err(std::io::Error::other(format!(
                    "live_window_produced_no_samples: window_ms={} elapsed_ms={}",
                    args.live.duration_ms, stats.elapsed_ms
                ))
                .into());
            }
        }
        Err(reason) => {
            runner.producer_failed();
            // 拉流失败本身就是证据，不是沉默：写进报告，然后让命令失败，
            // 避免任何调用方把它误判为通过。
            report.decoded = Some(DecodedDataPlane {
                failure_reasons: vec![reason.clone()],
                ..Default::default()
            });
            report.blockers.push("live_ingest_failed".into());
            write_report_atomically(&args.report, report.encode_to_vec())?;
            return Err(std::io::Error::other(format!("live_ingest_failed: {reason}")).into());
        }
    }

    write_report_atomically(&args.report, report.encode_to_vec())?;
    let plane = report.decoded.as_ref();
    let stats = report.stream.as_ref();
    println!(
        "live report written: platform={} samples={} descriptors={} rejected={} leases_issued={} stalls={} stalled_ms={} max_stall_ms={} pts_gap_total={} recovered={} ended_by_deadline={} handoff={} blockers={}",
        report.platform,
        stats.map_or(0, |stats| stats.samples),
        plane.map_or(0, |plane| plane.descriptors_validated),
        plane.map_or(0, |plane| plane.rejected_tracks.len()),
        plane.map_or(0, |plane| plane.leases_issued),
        stats.map_or(0, |stats| stats.stalls),
        stats.map_or(0, |stats| stats.stalled_ms),
        stats.map_or(0, |stats| stats.max_stall_ms),
        stats.map_or(0, |stats| stats.pts_gap_total_ms),
        stats.is_some_and(|stats| stats.recovered),
        stats.is_some_and(|stats| stats.ended_by_deadline),
        report.handoff_state,
        report.blockers.join(",")
    );
    println!("{}", media_queue.describe());
    println!("{}", describe_backpressure(report.decoded.as_ref()));
    match (&retained, &args.run.handoff) {
        (Some(_), Some(_)) => runner.producer_finished().map_err(std::io::Error::other)?,
        // 声明要暴露数据面，却拿不到保留的字节：这是失败，不是"跳过"。
        (None, Some(config)) => {
            runner.producer_failed();
            return Err(format!(
                "handoff_requested_but_no_decoded_bytes: this build cannot serve {}",
                config.listen
            )
            .into())
        }
        (_, None) => runner.producer_finished().map_err(std::io::Error::other)?,
    }
    Ok(())
}

/// 实时解码的 feature 门。与 `decode_pass` 同一口径：没有 GStreamer 的构建
/// 拿不到解码器，因此给出稳定原因，而不是返回"空但成功"的数据面。
#[cfg(feature = "gstreamer")]
#[allow(clippy::type_complexity)]
fn live_pass(
    uri: &str,
    stream_id: &str,
    arena_id: &str,
    args: &ResolvedRunArgs,
    live: &LiveConfig,
    plane: Option<Arc<SharedHandoff>>,
) -> Result<(DecodedDataPlane, Option<Arc<SharedHandoff>>, LiveStats, bool), String> {
    let run = sensoryplex_media::decode::DecodeRun {
        decode: sensoryplex_media::decode::DecodeConfig::default(),
        max_samples: args.max_points,
        audio_segment_ms: args.audio_segment_ms,
        audio_overlap_ms: args.audio_overlap_ms,
        sampling: args.sampling,
        handoff: plane,
        backpressure: args.backpressure,
        evidence: args.evidence,
        ledger: args.ledger.clone(),
        ledger_path: args.ledger_path.clone(),
    };
    let outcome = sensoryplex_media::decode::decode_live(uri, stream_id, arena_id, &run, live)
        .map_err(|error| error.to_string())?;
    Ok((
        outcome.plane,
        outcome.handoff,
        outcome.stats,
        outcome.truncated,
    ))
}

#[cfg(not(feature = "gstreamer"))]
#[allow(clippy::type_complexity)]
fn live_pass(
    _uri: &str,
    _stream_id: &str,
    _arena_id: &str,
    args: &ResolvedRunArgs,
    _live: &LiveConfig,
    _plane: Option<Arc<SharedHandoff>>,
) -> Result<(DecodedDataPlane, Option<Arc<SharedHandoff>>, LiveStats, bool), String> {
    validate_run_args(args)?;
    Err("gstreamer_decode_not_implemented".into())
}

/// 本进程只**测量**断流与恢复。重连由解码元素负责，因此这里如实写下归属，
/// 而不是暗示本进程在控制重连。
const RECONNECT_OWNER: &str = "srtsrc auto-reconnect";

fn live_stream_stats(
    uri_secret_ref: &str,
    live: &LiveConfig,
    stats: &LiveStats,
) -> LiveStreamStats {
    LiveStreamStats {
        uri_secret_ref: uri_secret_ref.to_string(),
        requested_duration_ms: live.duration_ms,
        elapsed_ms: stats.elapsed_ms,
        samples: stats.samples,
        stalls: stats.stalls,
        stalled_ms: stats.stalled_ms,
        max_stall_ms: stats.max_stall_ms,
        pts_gap_total_ms: stats.pts_gap_total_ms,
        recovered: stats.recovered,
        ended_by_deadline: stats.ended_by_deadline,
        reconnect_owner: RECONNECT_OWNER.to_string(),
        stall_events: stats
            .stall_events
            .iter()
            .map(|event| StreamStall {
                started_ms: event.started_ms,
                ended_ms: event.ended_ms,
                gap_ms: event.gap_ms,
                pts_jump_ms: event.pts_jump_ms,
                reason: event.reason.clone(),
            })
            .collect(),
    }
}

/// pipeline 的 `uri_secret_ref` 决定从哪个环境变量取 URI。
/// 这样 URI（可能含 streamid 或凭据）不会出现在命令行、报告或日志里。
fn live_uri_env_name(uri_secret_ref: &str) -> String {
    let mut name = String::from("SENSORYPLEX_");
    for character in uri_secret_ref.chars() {
        if character.is_ascii_alphanumeric() {
            name.push(character.to_ascii_uppercase());
        } else {
            name.push('_');
        }
    }
    name
}

/// 直播没有可复现的内容摘要，因此身份来自"本次会话"而不是内容。
/// `content_hash` 保持为空：绝不用占位摘要冒充内容寻址。
fn live_identity(uri_secret_ref: &str) -> (String, String) {
    let label: String = uri_secret_ref
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() {
                character.to_ascii_lowercase()
            } else {
                '-'
            }
        })
        .collect();
    let stamp = sensoryplex_media::now_unix_ms();
    // 同一次启动、同一毫秒内并发的两个实时接入必须拿到不同身份，
    // 因此除了时刻还带上运行种子（时钟 + pid + ASLR），不引入新依赖。
    let seed = sensoryplex_media::shm::run_seed();
    let mut nonce = [0u8; 8];
    if let Some(bytes) = seed.get(..8) {
        nonce.copy_from_slice(bytes);
    }
    let nonce = u64::from_le_bytes(nonce);
    (
        format!("arena-live-{label}-{stamp}-{nonce:016x}"),
        format!("stream-live-{label}-{stamp}-{nonce:016x}"),
    )
}

/// 直播的来源描述。`duration_ms` 固定为 0（未知），`content_hash` 为空（不可复现）：
/// 读数者不得把它当成 0 秒素材或内容寻址的来源。
fn live_description(
    uri_secret_ref: &str,
    stream_id: &str,
    plane: Option<&DecodedDataPlane>,
) -> MediaSourceDescription {
    MediaSourceDescription {
        source: Some(MediaSourceRef {
            stream_id: stream_id.to_string(),
            source_id: format!("srt-live:{uri_secret_ref}"),
            kind: MediaSourceKind::Srt as i32,
            uri_secret_ref: uri_secret_ref.to_string(),
            content_hash: String::new(),
        }),
        tracks: plane.map(observed_tracks).unwrap_or_default(),
        duration_ms: 0,
        probe_tool: "gstreamer-srtsrc".to_string(),
    }
}

/// 只列出**真实观测到**的轨道：既没有样本也没有媒体时间终点的轨道不写进来，
/// 而不是给它一个乐观的默认值。
fn observed_tracks(plane: &DecodedDataPlane) -> Vec<MediaTrack> {
    plane
        .tracks
        .iter()
        .filter(|track| track.last_end_ms >= 0 || track.samples > 0)
        .map(|track| MediaTrack {
            track_kind: track.track_kind.clone(),
            // 源编码来自解码前采集的 caps（ADR-009 §4），不是从载荷反推的；
            // 采集不到时留空表示未知，绝不填一个猜出来的编码名。
            codec: track.source_codec.clone(),
            width: track.width,
            height: track.height,
            sample_rate: track.sample_rate,
            channels: track.channels,
            // 0.0 不是合法帧率，因此它读作"未观测到"，而不是一个估计值。
            average_frame_rate: 0.0,
            timing_known: track.last_end_ms >= 0,
        })
        .collect()
}

/// 打开本次运行的共享保留面（在解码**之前**调用）。
///
/// 返回 `Ok(None)` 表示这次运行不保留字节：进程内自校验路径仍然是合法运行，
/// 只是没有跨进程消费者，报告里也不会声称有人在消费。
fn open_handoff_plane(
    run: &ResolvedRunArgs,
    arena_id: &str,
) -> Result<Option<Arc<SharedHandoff>>, String> {
    let policy = match &run.handoff {
        Some(config) => RetainPolicy::shared(config.arena_bytes, config.retained_limit)
            .map_err(|error| error.to_string())?,
        None => RetainPolicy::default(),
    };
    let wait_timeout_ms = run
        .handoff
        .as_ref()
        .map_or(0, |config| config.wait_timeout_ms as i64);
    policy
        .open(arena_id, wait_timeout_ms)
        .map_err(|error| error.to_string())
}

/// 数据面的收尾判据。生产者与消费者是两个方向，服务端必须能分辨
/// "生产者还在跑（消费者还可以随时接上）"与"生产者已经结束（没消费者就该收摊了）"。
struct HandoffControl {
    producer_done: AtomicBool,
    producer_failed: AtomicBool,
    /// 生产者结束的时刻；没有消费者时等待从这个时刻起算，而不是从服务端起算。
    done_at_ms: AtomicI64,
}

impl HandoffControl {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            producer_done: AtomicBool::new(false),
            producer_failed: AtomicBool::new(false),
            done_at_ms: AtomicI64::new(0),
        })
    }

    /// 生产者成功结束：账本与报告已经落盘，不会再有新的保留。
    fn finish(&self) {
        self.done_at_ms
            .store(sensoryplex_media::now_unix_ms(), Ordering::Relaxed);
        self.producer_done.store(true, Ordering::Release);
    }

    /// 生产者失败：服务端立刻收尾。失败原因由生产者上报更权威，服务端的对账结论不覆盖它。
    fn fail(&self) {
        self.done_at_ms
            .store(sensoryplex_media::now_unix_ms(), Ordering::Relaxed);
        self.producer_failed.store(true, Ordering::Release);
    }

    fn producer_done(&self) -> bool {
        self.producer_done.load(Ordering::Acquire)
    }

    fn producer_failed(&self) -> bool {
        self.producer_failed.load(Ordering::Acquire)
    }

    fn done_at_ms(&self) -> i64 {
        self.done_at_ms.load(Ordering::Relaxed)
    }
}

/// 服务端的收尾判据。四种理由必须分开，不能合并成一个"超时"：
/// - `ProducerFailed`：生产者失败，继续等消费者只是替一个已经死掉的运行拖时间；
/// - `PlaneDrained`：消费者在场、生产者已结束、保留表被清空——交接真的跑完了，
///   立刻收摊，消费者不必在"还干净了"之后再陪一次空闲超时；
/// - `ConsumerIdle`：消费者在场却长时间没有任何调用，它已经离开；
/// - `NoConsumerWaitExpired`：从来没有消费者，从生产者结束那一刻起给的预算已经用完。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum HandoffShutdown {
    ProducerFailed,
    PlaneDrained,
    ConsumerIdle,
    NoConsumerWaitExpired,
}

impl HandoffShutdown {
    /// 收尾理由的稳定文本形式：它进日志，因此不能被读成"这次跑成功了没有"。
    fn label(self) -> &'static str {
        match self {
            Self::ProducerFailed => "producer_failed",
            Self::PlaneDrained => "plane_drained",
            Self::ConsumerIdle => "consumer_idle",
            Self::NoConsumerWaitExpired => "no_consumer_wait_expired",
        }
    }
}

/// 收尾判据本身是纯函数：它是"什么时候能收摊"的唯一权威，因此必须能被逐项断言，
/// 而不是藏在一个 50ms 的循环里靠时间碰运气。
fn handoff_shutdown(
    control: &HandoffControl,
    consumer_seen: bool,
    retained: u64,
    last_activity_ms: i64,
    now_ms: i64,
    idle_timeout_ms: i64,
    wait_timeout_ms: i64,
) -> Option<HandoffShutdown> {
    if control.producer_failed() {
        return Some(HandoffShutdown::ProducerFailed);
    }
    if !consumer_seen {
        return if control.producer_done() && now_ms - control.done_at_ms() >= wait_timeout_ms {
            Some(HandoffShutdown::NoConsumerWaitExpired)
        } else {
            None
        };
    }
    if control.producer_done() && retained == 0 {
        return Some(HandoffShutdown::PlaneDrained);
    }
    if now_ms - last_activity_ms >= idle_timeout_ms {
        return Some(HandoffShutdown::ConsumerIdle);
    }
    None
}

/// 生产者 + 并发数据面服务端的生命周期。
///
/// 顺序不能颠倒：**服务端先起**，否则整段解码期间没有任何人在排空保留表，长媒体从某一帧
/// 起每一帧都只能被拒；生产者结束之后**先落盘报告**（消费者靠它判断"账本已经完整"），
/// 再收尾服务端并核对释放/过期账。
struct HandoffRunner {
    control: Arc<HandoffControl>,
    server: Option<std::thread::JoinHandle<Result<(), String>>>,
}

impl HandoffRunner {
    /// 起服务端。`plane` 为 `None`（本次运行不保留字节）时它只是一个空跑的收尾句柄。
    fn start(run: &ResolvedRunArgs, plane: Option<Arc<SharedHandoff>>) -> Result<Self, String> {
        let control = HandoffControl::new();
        let (Some(config), Some(plane)) = (run.handoff.clone(), plane) else {
            return Ok(Self {
                control,
                server: None,
            });
        };
        let server_control = Arc::clone(&control);
        let server = std::thread::spawn(move || -> Result<(), String> {
            // 服务端有自己的 current-thread runtime：它不借主线程的运行时，
            // 因此解码可以在主线程上同步跑完，而服务端一直在线。
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|error| format!("handoff_runtime_build_failed: {error}"))?;
            runtime
                .block_on(serve_handoff(plane, &config, server_control))
                .map_err(|error| error.to_string())
        });
        Ok(Self {
            control,
            server: Some(server),
        })
    }

    /// 生产者成功收尾：通知服务端不再有新保留，并把它自己的对账结论上抛。
    fn producer_finished(self) -> Result<(), String> {
        self.control.finish();
        match self.server {
            Some(server) => server
                .join()
                .map_err(|_| "handoff_server_panicked".to_string())?,
            None => Ok(()),
        }
    }

    /// 生产者失败：收尾服务端，但不让它的结论覆盖更权威的解码失败原因。
    fn producer_failed(self) {
        self.control.fail();
        if let Some(server) = self.server {
            let _ = server.join();
        }
    }
}

/// 把保留的字节交给独立进程。服务期间数据面一直存活；服务结束后必须能对上账：
/// 每条保留的 buffer 要么被消费者释放，要么因 TTL 过期被回收。
///
/// 服务端与解码是**并发**的（见 `HandoffRunner`）：只有消费者在解码期间就能领料，
/// 有界保留面才不是对长媒体的硬性截断。
async fn serve_handoff(
    plane: Arc<SharedHandoff>,
    config: &HandoffArgs,
    control: Arc<HandoffControl>,
) -> Result<(), Box<dyn std::error::Error>> {
    let service = HandoffService::new(Arc::clone(&plane), config.ttl_ms);
    let retained = plane.stats()?;
    let segment = plane.segment_name()?.unwrap_or_default();
    let capacity = plane.arena_capacity_bytes()?;
    println!(
        "handoff_ready listen={} segment={} arena_capacity_bytes={} retained={} retained_limit={} retained_kind_limit={} offered={} retain_rejected={} rejection_reasons={} residency_samples={} concurrent_with_decode=true wait_timeout_ms={}",
        config.listen,
        segment,
        capacity,
        retained.retained,
        retained.retained_limit,
        retained.retained_kind_limit,
        retained.offered_total,
        retained.retain_rejections,
        describe_rejections(&retained),
        // 就绪时还没有消费者，因此等待时间一定是 0 个样本——它必须带样本数一起读。
        retained.residency_samples,
        // 服务端在解码之前就起来了，因此"等第一个消费者"的预算是从解码结束那一刻起算的。
        config.wait_timeout_ms,
    );
    // 收尾判据由 `handoff_shutdown` 这个纯函数给出，这里只按节奏采样并把理由记下来：
    // 日志必须能分辨"消费者把数据面还干净了"与"消费者跑了"，两者都收摊但含义相反。
    let wait_timeout_ms = config.wait_timeout_ms as i64;
    let idle_timeout_ms = config.idle_timeout_ms as i64;
    let watch_plane = Arc::clone(&plane);
    let shutdown_reason = Arc::new(std::sync::Mutex::new("not_recorded"));
    let reason_slot = Arc::clone(&shutdown_reason);
    let shutdown = async move {
        loop {
            tokio::time::sleep(Duration::from_millis(50)).await;
            let reason = handoff_shutdown(
                &control,
                watch_plane.consumer_connected(),
                // 读不到保留表深度时不能当成"已经清空"：那会把一次失败包装成正常收尾。
                watch_plane.depth().unwrap_or(u64::MAX),
                watch_plane.last_activity_ms(),
                sensoryplex_media::now_unix_ms(),
                idle_timeout_ms,
                wait_timeout_ms,
            );
            if let Some(reason) = reason {
                if let Ok(mut slot) = reason_slot.lock() {
                    *slot = reason.label();
                }
                break;
            }
        }
    };
    tonic::transport::Server::builder()
        .add_service(service.server())
        .serve_with_shutdown(config.listen, shutdown)
        .await?;

    let consumer_seen = plane.consumer_connected();
    // 关闭前先结算过期，否则"迟到的消费者"会让账面对不上。
    plane.expire(sensoryplex_media::now_unix_ms())?;
    let stats = plane.stats()?;
    println!(
        "handoff_stats consumer_seen={} retained={} retained_limit={} retained_total={} retained_peak={} retained_kind_peak={} retained_by_kind={} retained_kind_limit={} offered={} released={} expired={} retain_rejected={} request_rejected={} rejection_reasons={} residency_samples={} residency_max_ms={} residency_total_ms={} arena_live_slabs={} arena_used_bytes={} arena_capacity_bytes={}",
        consumer_seen,
        stats.retained,
        stats.retained_limit,
        stats.retained_total,
        stats.retained_peak,
        stats.retained_kind_peak,
        describe_counts(&stats.retained_by_kind),
        stats.retained_kind_limit,
        stats.offered_total,
        stats.released_total,
        stats.expired_total,
        stats.retain_rejections,
        stats.request_rejections,
        describe_rejections(&stats),
        stats.residency_samples,
        stats.residency_max_ms,
        stats.residency_total_ms,
        stats.arena_live_slabs,
        stats.arena_used_bytes,
        plane.arena_capacity_bytes()?,
    );
    let accounted = stats
        .released_total
        .saturating_add(stats.expired_total)
        .saturating_add(stats.retained);
    let settled = shutdown_reason
        .lock()
        .map(|slot| *slot)
        .unwrap_or("not_recorded");
    println!(
        "handoff_shutdown reason={} consumer_seen={} retained_total={} released={} expired={} retained={}",
        settled, consumer_seen, stats.retained_total, stats.released_total, stats.expired_total, stats.retained
    );
    if !consumer_seen {
        return Err(format!(
            "handoff_consumer_never_connected: retained_total={} still_retained={}",
            stats.retained_total, stats.retained
        )
        .into());
    }
    if stats.retained != 0 || accounted != stats.retained_total {
        return Err(format!(
            "handoff_lease_accounting_failed: retained_total={} released={} expired={} still_retained={}",
            stats.retained_total, stats.released_total, stats.expired_total, stats.retained
        )
        .into());
    }
    Ok(())
}

/// 有界性证据的稳定文本形式：原因码 → 次数，按原因码排序。
fn describe_rejections(stats: &HandoffStats) -> String {
    describe_counts(&stats.rejection_reasons)
}

/// 计数表的稳定文本形式：键 → 次数，按键排序。空表写 `none`，不留一个空白的字段。
fn describe_counts(counts: &BTreeMap<String, u64>) -> String {
    if counts.is_empty() {
        return "none".to_string();
    }
    counts
        .iter()
        .map(|(key, count)| format!("{key}:{count}"))
        .collect::<Vec<_>>()
        .join(",")
}

/// Arena 与 stream 的身份由内容摘要（content digest）派生，因此 replay 可复现，
/// 不会把不同的文件与本文件混淆。
fn arena_identity(description: &MediaSourceDescription) -> (String, String) {
    let digest = description
        .source
        .as_ref()
        .map(|source| source.content_hash.as_str())
        .unwrap_or_default();
    let short = digest.get(7..19).unwrap_or_default();
    let stream_id = description
        .source
        .as_ref()
        .map(|source| source.stream_id.clone())
        .unwrap_or_default();
    (format!("arena-{short}"), stream_id)
}

/// 控制端点进程读到的常驻分级值。
///
/// 这里只**转述**：`serve` 不跑 pipeline，没有队列可以设上限。真正消费
/// `SENSORYPLEX_MEDIA_QUEUE_CAPACITY` 的是 `replay`/`ingest`，它们的报告里带准入结果。
struct Runtime {
    residency: ResidentLimits,
}
#[tonic::async_trait]
impl RuntimeService for Runtime {
    async fn health(&self, _: Request<HealthRequest>) -> Result<Response<HealthResponse>, Status> {
        Ok(Response::new(HealthResponse {
            state: capability::state().into(),
            unavailable_capabilities: capability::unavailable_capabilities(),
        }))
    }

    async fn describe_capabilities(
        &self,
        _: Request<DescribeCapabilitiesRequest>,
    ) -> Result<Response<DescribeCapabilitiesResponse>, Status> {
        Ok(Response::new(capability::describe(&self.residency)))
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("check") && args.len() == 2 {
        Pipeline::parse(&std::fs::read_to_string(&args[1])?).map_err(std::io::Error::other)?;
        println!("pipeline schema valid; capability availability must be checked before execution");
        return Ok(());
    }
    if args.first().map(String::as_str) == Some("orchestration-check") && args.len() == 2 {
        let pipeline =
            Pipeline::parse(&std::fs::read_to_string(&args[1])?).map_err(std::io::Error::other)?;
        let graph = pipeline
            .compile_orchestration()
            .map_err(std::io::Error::other)?;
        println!(
            "orchestration graph valid; nodes={} edges={} execution is not attached",
            graph.node_count(),
            graph.edge_count()
        );
        return Ok(());
    }
    if args.first().map(String::as_str) == Some("replay") {
        return replay(&args[1..]).await;
    }
    if args.first().map(String::as_str) == Some("ingest") {
        return ingest(&args[1..]).await;
    }
    if args.first().map(String::as_str) == Some("timeline") {
        // 真探测 + 真运行报告 → 融合核心 → 素材文件；不碰数据库、不发事件（ADR-028）。
        return timeline::run(&args[1..]);
    }
    if !args.is_empty() && args != ["serve"] {
        return Err(
            "usage: sensoryplex-runtime [serve | check <pipeline.yaml> | orchestration-check <pipeline.yaml> | replay <pipeline.yaml> <media-path> --report <report.pb> | ingest <pipeline.yaml> --report <report.pb> | timeline <pipeline.yaml> <media-path> --report <report.pb> --worker-report <ai-worker.json> --material-dir <dir> --out <timeline.json>]"
                .into(),
        );
    }
    // 分级值在启动时解析一次：坏配置必须让进程起不来，而不是让端点报一份自己都读不出来的清单。
    let residency = ResidentLimits::from_env().map_err(std::io::Error::other)?;
    let address = std::env::var("SENSORYPLEX_RUNTIME_ADDR")
        .unwrap_or_else(|_| "127.0.0.1:50051".into())
        .parse()?;
    tracing::info!(
        %address,
        platform = %capability::platform(),
        state = capability::state(),
        // 宿主加速器与"本进程能不能执行推理"是两回事，日志里也分开写（ADR-022）。
        accelerators = %accelerator::summary(),
        tier = residency.tier.as_deref().unwrap_or("not_injected"),
        media_queue_capacity = residency
            .media_queue_capacity
            .map_or(0, |capacity| capacity.get()),
        model_parallelism = residency.model_parallelism.map_or(0, |value| value.get()),
        "runtime control endpoint started"
    );
    tonic::transport::Server::builder()
        .add_service(RuntimeServiceServer::new(Runtime { residency }))
        .serve_with_shutdown(address, async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use sensoryplex_media::live::StreamStall as LiveStall;
    use sensoryplex_sdk::media::DecodedTrackStat;

    fn replay_args(extra: &[&str]) -> Vec<String> {
        let mut args = vec![
            "p.yaml".to_string(),
            "m.mp4".to_string(),
            "--report".to_string(),
            "r.pb".to_string(),
        ];
        args.extend(extra.iter().map(|item| (*item).to_string()));
        args
    }

    fn ingest_args(extra: &[&str]) -> Vec<String> {
        let mut args = vec![
            "p.yaml".to_string(),
            "--report".to_string(),
            "r.pb".to_string(),
        ];
        args.extend(extra.iter().map(|item| (*item).to_string()));
        args
    }

    #[test]
    fn live_uri_env_name_is_derived_from_the_reference_name() {
        assert_eq!(
            live_uri_env_name("SRT_LIVE_URI"),
            "SENSORYPLEX_SRT_LIVE_URI"
        );
        // 环境变量名不允许 '-' 与 '.'，引用名里的其他字符一律映射成 '_'。
        assert_eq!(
            live_uri_env_name("srt.live-uri"),
            "SENSORYPLEX_SRT_LIVE_URI"
        );
    }

    #[test]
    fn live_identity_is_unique_per_run_and_carries_no_uri() {
        let (arena_a, stream_a) = live_identity("SRT_LIVE_URI");
        let (arena_b, stream_b) = live_identity("SRT_LIVE_URI");
        assert!(arena_a.starts_with("arena-live-"));
        assert!(stream_a.starts_with("stream-live-"));
        assert_ne!(
            arena_a, arena_b,
            "two live sessions must not share an identity"
        );
        assert_ne!(stream_a, stream_b);
        for value in [&arena_a, &stream_a, &arena_b, &stream_b] {
            assert!(!value.contains("srt://") && !value.contains("127.0.0.1"));
        }
    }

    #[test]
    fn ingest_rejects_an_out_of_range_window_instead_of_clamping_it() {
        assert!(LiveArgs::parse(&ingest_args(&["--duration-ms", "0"])).is_err());
        assert!(LiveArgs::parse(&ingest_args(&["--max-stalls", "0"])).is_err());
        assert!(LiveArgs::parse(&ingest_args(&["--stall-threshold-ms", "10"])).is_err());
        assert!(LiveArgs::parse(&ingest_args(&[])).is_ok());
    }

    #[test]
    fn ingest_requires_a_report_and_exactly_one_positional() {
        assert!(
            LiveArgs::parse(&["p.yaml".to_string()]).is_err(),
            "--report is mandatory"
        );
        let mut args = vec!["p.yaml".to_string(), "extra".to_string()];
        args.extend(["--report".to_string(), "r.pb".to_string()]);
        assert!(
            LiveArgs::parse(&args).is_err(),
            "only the pipeline is positional for ingest"
        );
    }

    #[test]
    fn replay_argument_contract_survives_the_shared_parser() {
        let parsed = ReplayArgs::parse(&replay_args(&[])).expect("valid replay arguments");
        assert_eq!(parsed.media, "m.mp4");
        assert_eq!(parsed.run.max_points, DEFAULT_MAX_POINTS);
        assert!(parsed.run.handoff.is_none());
        // 最后一个 --report 生效，与前一份实现的语义一致。
        let duplicated = replay_args(&["--report", "second.pb"]);
        let parsed = ReplayArgs::parse(&duplicated).expect("duplicate --report is last-wins");
        assert_eq!(parsed.report, "second.pb");
        assert!(ReplayArgs::parse(&[
            "only.yaml".to_string(),
            "--report".to_string(),
            "r.pb".to_string()
        ])
        .is_err());
        assert!(ReplayArgs::parse(
            &replay_args(&[])
                .into_iter()
                .take(2)
                .collect::<Vec<String>>()
        )
        .is_err());
    }

    #[test]
    fn shared_options_reject_out_of_range_values() {
        assert!(
            ReplayArgs::parse(&replay_args(&["--audio-segment-ms", "1"])).is_err(),
            "audio-segment-ms is rejected, never clamped"
        );
        assert!(
            ReplayArgs::parse(&replay_args(&["--handoff-listen", "0.0.0.0:9999"])).is_err(),
            "the data plane is host-local: non-loopback listeners are refused"
        );
        assert!(ReplayArgs::parse(&replay_args(&[
            "--handoff-ttl-ms",
            "1",
            "--handoff-listen",
            "127.0.0.1:9999"
        ]))
        .is_err());
    }

    #[test]
    fn evidence_options_are_opt_in_and_bounded() {
        // 默认关闭：没有 `--evidence` 就没有语义账本。
        let plain = ReplayArgs::parse(&replay_args(&[])).expect("valid replay arguments");
        assert!(plain.run.evidence.is_none());
        assert!(plain.run.ledger.is_none());

        let parsed = ReplayArgs::parse(&replay_args(&["--evidence"])).expect("evidence on");
        let policy = parsed.run.evidence.expect("policy exists once enabled");
        assert_eq!(policy.max_semantic_gap_ms, DEFAULT_MAX_SEMANTIC_GAP_MS);
        assert_eq!(policy.pre_frames, DEFAULT_CONTEXT_FRAMES);
        assert_eq!(policy.post_frames, DEFAULT_CONTEXT_FRAMES);
        assert!(
            parsed.run.ledger.is_none(),
            "no --evidence-ledger means no ledger"
        );
        assert!(parsed.run.ledger_path.is_none());

        // 越界一律拒绝，绝不夹取到"看起来合理"的范围。
        for argv in [
            vec!["--evidence", "--evidence-max-gap-ms", "50"],
            vec!["--evidence", "--evidence-max-gap-ms", "120000"],
            vec!["--evidence", "--evidence-context-before", "9"],
            vec!["--evidence", "--evidence-context-after", "9"],
            vec!["--evidence", "--evidence-change-threshold", "0"],
            vec!["--evidence", "--evidence-change-threshold", "200"],
            vec!["--evidence", "--evidence-text-change-threshold", "0"],
            vec!["--evidence", "--evidence-min-event-interval-ms", "10"],
        ] {
            assert!(
                ReplayArgs::parse(&replay_args(&argv)).is_err(),
                "{argv:?} must be rejected instead of clamped"
            );
        }
        // 给了子选项却没开开关：显式错误，而不是静默忽略。
        assert!(ReplayArgs::parse(&replay_args(&["--evidence-max-gap-ms", "5000"])).is_err());
        assert!(ReplayArgs::parse(&replay_args(&["--evidence-ledger", "ledger.jsonl"])).is_err());

        // 账本必须和策略一起给出，并真的落盘。
        let ledger_path = std::env::temp_dir().join(format!(
            "sensoryplex-evidence-ledger-{}.jsonl",
            std::process::id()
        ));
        let ledger_path = ledger_path.to_string_lossy().to_string();
        let with_ledger = ReplayArgs::parse(&replay_args(&[
            "--evidence",
            "--evidence-max-gap-ms",
            "5000",
            "--evidence-ledger",
            &ledger_path,
        ]))
        .expect("evidence with a ledger path");
        assert_eq!(
            with_ledger
                .run
                .evidence
                .expect("policy")
                .max_semantic_gap_ms,
            5000
        );
        assert!(with_ledger.run.ledger.is_some());
        assert_eq!(
            with_ledger.run.ledger_path.as_deref(),
            Some(ledger_path.as_str())
        );
        assert!(
            Path::new(&ledger_path).is_file(),
            "the ledger file is created up front"
        );
        let _ = std::fs::remove_file(&ledger_path);
    }

    #[test]
    fn observed_tracks_never_invents_a_track_without_evidence() {
        let plane = DecodedDataPlane {
            tracks: vec![
                DecodedTrackStat {
                    track_kind: "video".into(),
                    samples: 3,
                    last_end_ms: 13_770,
                    width: 854,
                    height: 480,
                    pixel_format: "RGBA".into(),
                    decoder_element: "vp9dec".into(),
                    source_codec: "video/x-vp9".into(),
                    ..Default::default()
                },
                DecodedTrackStat {
                    track_kind: "audio".into(),
                    samples: 0,
                    last_end_ms: -1,
                    ..Default::default()
                },
            ],
            ..Default::default()
        };
        let tracks = observed_tracks(&plane);
        assert_eq!(
            tracks.len(),
            1,
            "a track without samples or media end is not reported"
        );
        assert_eq!(tracks[0].track_kind, "video");
        assert!(tracks[0].timing_known);
        // 编码名只在**采集到**的时候写出来（这里是解码前的 caps 给出的 vp9），
        // 没采集到的字段仍然保持"未知"，不填猜测值。
        assert_eq!(tracks[0].codec, "video/x-vp9");
        assert_eq!(tracks[0].average_frame_rate, 0.0);
    }

    #[test]
    fn live_description_keeps_duration_and_digest_unknown() {
        let description = live_description("SRT_LIVE_URI", "stream-live-x", None);
        assert_eq!(
            description.duration_ms, 0,
            "a live stream never gets an assumed duration"
        );
        assert!(description.tracks.is_empty());
        let source = description.source.expect("source ref is always present");
        assert_eq!(
            source.content_hash, "",
            "no placeholder digest for live media"
        );
        assert_eq!(source.uri_secret_ref, "SRT_LIVE_URI");
        assert_eq!(source.kind, MediaSourceKind::Srt as i32);
    }

    #[test]
    fn live_stats_carry_the_measured_gap_and_the_reconnect_owner() {
        let live = LiveConfig {
            duration_ms: 5_000,
            stall_threshold_ms: 1_000,
            max_stalls: 4,
        };
        let stats = LiveStats {
            elapsed_ms: 5_000,
            samples: 12,
            stalls: 1,
            stalled_ms: 4_731,
            max_stall_ms: 4_731,
            pts_gap_total_ms: 4_722,
            recovered: true,
            ended_by_deadline: true,
            stall_events: vec![LiveStall {
                started_ms: 7_431,
                ended_ms: 12_162,
                gap_ms: 4_731,
                pts_jump_ms: 4_722,
                reason: "stream_gap".into(),
            }],
            ..Default::default()
        };
        let proto = live_stream_stats("SRT_LIVE_URI", &live, &stats);
        assert_eq!(proto.uri_secret_ref, "SRT_LIVE_URI");
        assert_eq!(proto.requested_duration_ms, 5_000);
        assert_eq!(proto.reconnect_owner, "srtsrc auto-reconnect");
        assert_eq!(proto.stall_events.len(), 1);
        assert_eq!(proto.stall_events[0].gap_ms, 4_731);
        assert_eq!(proto.stall_events[0].pts_jump_ms, 4_722);
        assert!(proto.recovered && proto.ended_by_deadline);
    }

    /// 直接构造收尾判据的输入，避免用真实时钟去碰一个 50ms 的循环。
    fn shutdown_control(done: bool, failed: bool, done_at_ms: i64) -> HandoffControl {
        HandoffControl {
            producer_done: std::sync::atomic::AtomicBool::new(done),
            producer_failed: std::sync::atomic::AtomicBool::new(failed),
            done_at_ms: std::sync::atomic::AtomicI64::new(done_at_ms),
        }
    }

    #[test]
    fn handoff_shutdown_stops_as_soon_as_the_consumer_emptied_the_plane() {
        let control = shutdown_control(true, false, 0);
        assert_eq!(
            handoff_shutdown(&control, true, 0, 0, 10, 60_000, 30_000),
            Some(HandoffShutdown::PlaneDrained),
            "消费者把保留表还干净之后，服务端不该再等一次空闲超时"
        );
    }

    #[test]
    fn handoff_shutdown_does_not_read_an_empty_plane_as_drained_while_producing() {
        // 生产者还在跑：保留表此刻为空只说明"还没有新帧"，不是"交接跑完了"。
        let control = shutdown_control(false, false, 0);
        assert_eq!(
            handoff_shutdown(&control, true, 0, 0, 10, 60_000, 30_000),
            None
        );
    }

    #[test]
    fn handoff_shutdown_distinguishes_a_working_consumer_from_one_that_left() {
        let control = shutdown_control(true, false, 0);
        assert_eq!(
            handoff_shutdown(&control, true, 3, 5_000, 10_000, 60_000, 30_000),
            None,
            "消费者还在调用数据面时不能收尾"
        );
        assert_eq!(
            handoff_shutdown(&control, true, 3, 5_000, 65_000, 60_000, 30_000),
            Some(HandoffShutdown::ConsumerIdle)
        );
    }

    #[test]
    fn handoff_shutdown_waits_for_a_consumer_only_after_the_producer_ended() {
        let running = shutdown_control(false, false, 0);
        assert_eq!(
            handoff_shutdown(&running, false, 5, 0, 600_000, 60_000, 30_000),
            None,
            "生产者还在解码时，没消费者也不该收尾"
        );
        let done = shutdown_control(true, false, 1_000);
        assert_eq!(
            handoff_shutdown(&done, false, 5, 0, 30_999, 60_000, 30_000),
            None
        );
        assert_eq!(
            handoff_shutdown(&done, false, 5, 0, 31_000, 60_000, 30_000),
            Some(HandoffShutdown::NoConsumerWaitExpired)
        );
    }

    #[test]
    fn handoff_shutdown_records_a_failed_producer_ahead_of_any_timeout() {
        let control = shutdown_control(false, true, 0);
        assert_eq!(
            handoff_shutdown(&control, true, 900, 0, 10, 60_000, 30_000),
            Some(HandoffShutdown::ProducerFailed)
        );
    }
}
