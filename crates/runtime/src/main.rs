use std::collections::BTreeSet;
use std::path::Path;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use prost::Message;
use sensoryplex_media::handoff::{BufferHandoff, HandoffStats, RetainPolicy};
use sensoryplex_media::sampler::SamplingPolicy;
use sensoryplex_media::segment::{MAX_AUDIO_SEGMENT_MS, MIN_AUDIO_SEGMENT_MS};
use sensoryplex_media::source::{drain_source, FileSource, MediaSource, UnavailableSource};
use sensoryplex_media::MediaError;
use sensoryplex_runtime::{capability, Pipeline};
use sensoryplex_sdk::media::{DecodedDataPlane, MediaSourceDescription, ReplayReport};
use sensoryplex_sdk::runtime::{
    runtime_service_server::{RuntimeService, RuntimeServiceServer},
    DescribeCapabilitiesRequest, DescribeCapabilitiesResponse, HealthRequest, HealthResponse,
};
use tonic::{Request, Response, Status};

use crate::handoff_service::{HandoffService, DEFAULT_HANDOFF_TTL_MS};

mod handoff_service;

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
#[cfg(feature = "gstreamer")]
#[allow(clippy::type_complexity)]
fn decode_pass(
    media: &str,
    stream_id: &str,
    arena_id: &str,
    max_samples: usize,
    audio_segment_ms: u32,
    sampling: SamplingPolicy,
    handoff: RetainPolicy,
) -> Result<(Option<DecodedDataPlane>, Option<BufferHandoff>, bool), String> {
    let run = sensoryplex_media::decode::DecodeRun {
        decode: sensoryplex_media::decode::DecodeConfig::default(),
        max_samples,
        audio_segment_ms,
        sampling,
        handoff,
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
    _max_samples: usize,
    _audio_segment_ms: u32,
    _sampling: SamplingPolicy,
    _handoff: RetainPolicy,
) -> Result<(Option<DecodedDataPlane>, Option<BufferHandoff>, bool), String> {
    Ok((None, None, false))
}

const REPLAY_USAGE: &str = "usage: sensoryplex-runtime replay <pipeline.yaml> <media-path> --report <report.pb> [--max-points N] [--audio-segment-ms N] [--sampling-min-interval-ms N] [--sampling-static-hold-ms N] [--sampling-change-threshold N] [--handoff-listen 127.0.0.1:PORT] [--handoff-arena-bytes N] [--handoff-retained-limit N] [--handoff-ttl-ms N] [--handoff-wait-timeout-ms N] [--handoff-idle-timeout-ms N]";

/// 跨进程交接的服务端配置。只有在显式给出 `--handoff-listen` 时才存在：
/// 不保留字节的运行仍然是合法运行，但它不算"消费方已验证"。
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

struct ReplayArgs {
    pipeline: String,
    media: String,
    report: String,
    /// 时间轴锚点与解码样本数量的上限；不存在无界模式。
    max_points: usize,
    audio_segment_ms: u32,
    sampling: SamplingPolicy,
    /// `None` 表示本次运行不暴露数据面（也不声称有消费者）。
    handoff: Option<HandoffArgs>,
}

impl ReplayArgs {
    fn parse(args: &[String]) -> Result<Self, String> {
        let mut positional = Vec::new();
        let mut report = None;
        let mut max_points = DEFAULT_MAX_POINTS;
        let mut audio_segment_ms = DEFAULT_AUDIO_SEGMENT_MS;
        let mut sampling_min_interval_ms = sensoryplex_media::sampler::DEFAULT_MIN_INTERVAL_MS;
        let mut sampling_static_hold_ms = sensoryplex_media::sampler::DEFAULT_STATIC_HOLD_MS;
        let mut sampling_change_threshold = sensoryplex_media::sampler::DEFAULT_CHANGE_THRESHOLD;
        let mut handoff_listen: Option<std::net::SocketAddr> = None;
        let mut handoff_arena_bytes = sensoryplex_media::handoff::DEFAULT_RETAIN_ARENA_BYTES;
        let mut handoff_retained_limit = sensoryplex_media::handoff::DEFAULT_RETAINED_LIMIT;
        let mut handoff_ttl_ms = DEFAULT_HANDOFF_TTL_MS;
        let mut handoff_wait_timeout_ms = DEFAULT_HANDOFF_WAIT_TIMEOUT_MS;
        let mut handoff_idle_timeout_ms = DEFAULT_HANDOFF_IDLE_TIMEOUT_MS;
        let mut index = 0;
        while index < args.len() {
            match args[index].as_str() {
                "--report" => {
                    report = args.get(index + 1).cloned();
                    index += 2;
                }
                "--max-points" => {
                    let value = args
                        .get(index + 1)
                        .ok_or("missing value for --max-points")?;
                    max_points = value.parse().map_err(|_| "invalid --max-points")?;
                    index += 2;
                }
                "--audio-segment-ms" => {
                    let value = args
                        .get(index + 1)
                        .ok_or("missing value for --audio-segment-ms")?;
                    audio_segment_ms = value.parse().map_err(|_| "invalid --audio-segment-ms")?;
                    index += 2;
                }
                "--sampling-min-interval-ms" => {
                    let value = args
                        .get(index + 1)
                        .ok_or("missing value for --sampling-min-interval-ms")?;
                    sampling_min_interval_ms = value
                        .parse()
                        .map_err(|_| "invalid --sampling-min-interval-ms")?;
                    index += 2;
                }
                "--sampling-static-hold-ms" => {
                    let value = args
                        .get(index + 1)
                        .ok_or("missing value for --sampling-static-hold-ms")?;
                    sampling_static_hold_ms = value
                        .parse()
                        .map_err(|_| "invalid --sampling-static-hold-ms")?;
                    index += 2;
                }
                "--sampling-change-threshold" => {
                    let value = args
                        .get(index + 1)
                        .ok_or("missing value for --sampling-change-threshold")?;
                    sampling_change_threshold = value
                        .parse()
                        .map_err(|_| "invalid --sampling-change-threshold")?;
                    index += 2;
                }
                "--handoff-listen" => {
                    let value = args
                        .get(index + 1)
                        .ok_or("missing value for --handoff-listen")?;
                    handoff_listen = Some(HandoffArgs::loopback(value)?);
                    index += 2;
                }
                "--handoff-arena-bytes" => {
                    let value = args
                        .get(index + 1)
                        .ok_or("missing value for --handoff-arena-bytes")?;
                    handoff_arena_bytes =
                        value.parse().map_err(|_| "invalid --handoff-arena-bytes")?;
                    index += 2;
                }
                "--handoff-retained-limit" => {
                    let value = args
                        .get(index + 1)
                        .ok_or("missing value for --handoff-retained-limit")?;
                    handoff_retained_limit = value
                        .parse()
                        .map_err(|_| "invalid --handoff-retained-limit")?;
                    index += 2;
                }
                "--handoff-ttl-ms" => {
                    let value = args
                        .get(index + 1)
                        .ok_or("missing value for --handoff-ttl-ms")?;
                    handoff_ttl_ms = value.parse().map_err(|_| "invalid --handoff-ttl-ms")?;
                    index += 2;
                }
                "--handoff-wait-timeout-ms" => {
                    let value = args
                        .get(index + 1)
                        .ok_or("missing value for --handoff-wait-timeout-ms")?;
                    handoff_wait_timeout_ms = value
                        .parse()
                        .map_err(|_| "invalid --handoff-wait-timeout-ms")?;
                    index += 2;
                }
                "--handoff-idle-timeout-ms" => {
                    let value = args
                        .get(index + 1)
                        .ok_or("missing value for --handoff-idle-timeout-ms")?;
                    handoff_idle_timeout_ms = value
                        .parse()
                        .map_err(|_| "invalid --handoff-idle-timeout-ms")?;
                    index += 2;
                }
                value => {
                    positional.push(value.to_string());
                    index += 1;
                }
            }
        }
        if positional.len() != 2 || max_points == 0 {
            return Err(REPLAY_USAGE.into());
        }
        if !(MIN_AUDIO_SEGMENT_MS..=MAX_AUDIO_SEGMENT_MS).contains(&audio_segment_ms) {
            return Err(format!(
                "audio-segment-ms must be between {MIN_AUDIO_SEGMENT_MS} and {MAX_AUDIO_SEGMENT_MS}"
            ));
        }
        // 越界策略是被拒绝，而不是被夹取到合法范围：夹取会悄悄改变抽帧语义。
        let sampling = SamplingPolicy::new(
            sampling_min_interval_ms,
            sampling_static_hold_ms,
            sampling_change_threshold,
        )
        .map_err(|error| error.to_string())?;
        let handoff = match handoff_listen {
            None => None,
            Some(listen) => {
                if handoff_ttl_ms == 0 {
                    handoff_ttl_ms = DEFAULT_HANDOFF_TTL_MS;
                }
                // TTL 与两个上限同样按"越界即拒绝"处理，不做夹取。
                if !(sensoryplex_media::handoff::MIN_LEASE_TTL_MS
                    ..=sensoryplex_media::handoff::MAX_LEASE_TTL_MS)
                    .contains(&handoff_ttl_ms)
                {
                    return Err(format!(
                        "handoff-ttl-ms must be between {} and {}",
                        sensoryplex_media::handoff::MIN_LEASE_TTL_MS,
                        sensoryplex_media::handoff::MAX_LEASE_TTL_MS
                    ));
                }
                if handoff_idle_timeout_ms == 0 || handoff_wait_timeout_ms == 0 {
                    return Err("handoff timeouts must be positive".into());
                }
                RetainPolicy::shared(handoff_arena_bytes, handoff_retained_limit)
                    .map_err(|error| error.to_string())?;
                Some(HandoffArgs {
                    listen,
                    ttl_ms: handoff_ttl_ms,
                    arena_bytes: handoff_arena_bytes,
                    retained_limit: handoff_retained_limit,
                    wait_timeout_ms: handoff_wait_timeout_ms,
                    idle_timeout_ms: handoff_idle_timeout_ms,
                })
            }
        };
        Ok(Self {
            pipeline: positional[0].clone(),
            media: positional[1].clone(),
            report: report.ok_or("replay requires --report <report.pb>")?,
            max_points,
            audio_segment_ms,
            sampling,
            handoff,
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
    let mut source = open_source(&pipeline, &args.media).map_err(std::io::Error::other)?;
    let description = source.describe().clone();
    let duration_ms = description.duration_ms;

    let mut report = ReplayReport {
        source: Some(description.clone()),
        platform: capability::platform(),
        blockers: replay_blockers(),
        // 只有真正通过媒体验收的运行才允许设置该标志；本次构建无法达成。
        golden_path_verified: false,
        ..Default::default()
    };

    let drained = drain_source(
        source.as_mut(),
        duration_ms,
        GAP_THRESHOLD_MS,
        args.max_points,
    );
    let (intervals, truncated) = match drained {
        Ok(result) => result,
        Err(MediaError::UnsupportedSource(reason)) => {
            report.blockers.push(reason);
            std::fs::write(&args.report, report.encode_to_vec())?;
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
    report.handoff_state = match &args.handoff {
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
    let handoff_policy = match &args.handoff {
        Some(config) => RetainPolicy::shared(config.arena_bytes, config.retained_limit)?,
        None => RetainPolicy::default(),
    };
    let retained;
    match decode_pass(
        &args.media,
        &stream_id,
        &arena_id,
        args.max_points,
        args.audio_segment_ms,
        args.sampling,
        handoff_policy,
    ) {
        Ok((plane, handoff, decode_truncated)) => {
            report.decoded = plane;
            retained = handoff;
            if decode_truncated {
                report.blockers.push("decode_truncated".into());
            }
        }
        Err(reason) => {
            // 解码尝试失败本身就是证据，不是沉默：写入报告中，
            // 然后让命令失败，避免任何调用方把它误判为通过。
            report.decoded = Some(DecodedDataPlane {
                failure_reasons: vec![reason.clone()],
                ..Default::default()
            });
            report.blockers.push("decode_failed".into());
            std::fs::write(&args.report, report.encode_to_vec())?;
            return Err(std::io::Error::other(format!("decode_failed: {reason}")).into());
        }
    }

    std::fs::write(&args.report, report.encode_to_vec())?;
    let plane = report.decoded.as_ref();
    // 交接是否真的跑过，必须在同一行里说清楚：没跑过就不能被读成"消费方已验证"。
    let handoff_note = match (&retained, &args.handoff) {
        (Some(_), Some(config)) => format!("exposed_on={}", config.listen),
        (_, Some(_)) => "unavailable_this_build".to_string(),
        (_, None) => report.handoff_state.clone(),
    };
    println!(
        "replay report written: platform={} anchors={} decoded_items={} dropped={} gaps={} out_of_order={} descriptors={} leases_issued={} leases_released={} segments={} arena_peak_bytes={} sampling_kept={}/{} handoff={} blockers={}",
        report.platform,
        report.emitted_anchors,
        report.decoded_items,
        report.dropped_items,
        report.gap_items,
        report.out_of_order_items,
        plane.map_or(0, |plane| plane.descriptors_validated),
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
    match (retained, &args.handoff) {
        (Some(handoff), Some(config)) => serve_handoff(handoff, config).await?,
        // 声明要暴露数据面，却拿不到保留的字节：这是失败，不是"跳过"。
        (None, Some(config)) => {
            return Err(format!(
                "handoff_requested_but_no_decoded_bytes: this build cannot serve {}",
                config.listen
            )
            .into())
        }
        (_, None) => {}
    }
    Ok(())
}

/// 把保留的字节交给独立进程。服务期间数据面一直存活；服务结束后必须能对上账：
/// 每条保留的 buffer 要么被消费者释放，要么因 TTL 过期被回收。
async fn serve_handoff(
    plane: BufferHandoff,
    config: &HandoffArgs,
) -> Result<(), Box<dyn std::error::Error>> {
    let retained = plane.stats();
    let segment = plane.segment_name().unwrap_or_default().to_string();
    let capacity = plane.arena_capacity_bytes();
    let plane = Arc::new(Mutex::new(plane));
    let service = HandoffService::new(Arc::clone(&plane), config.ttl_ms);
    let last_activity = service.last_activity_ms();
    let connected = service.connected();
    println!(
        "handoff_ready listen={} segment={} arena_capacity_bytes={} retained={} retained_limit={} offered={} retain_rejected={} rejection_reasons={}",
        config.listen,
        segment,
        capacity,
        retained.retained,
        retained.retained_limit,
        retained.offered_total,
        retained.retain_rejections,
        describe_rejections(&retained)
    );
    // 两种收尾条件必须分开：等不到消费者（从启动算起）与消费者已离开（从最后一次调用算起）。
    let started_ms = sensoryplex_media::now_unix_ms();
    let wait_timeout_ms = config.wait_timeout_ms as i64;
    let idle_timeout_ms = config.idle_timeout_ms as i64;
    let watch_connected = Arc::clone(&connected);
    let shutdown = async move {
        loop {
            tokio::time::sleep(Duration::from_millis(50)).await;
            let now = sensoryplex_media::now_unix_ms();
            if watch_connected.load(Ordering::Relaxed) {
                if now - last_activity.load(Ordering::Relaxed) >= idle_timeout_ms {
                    break;
                }
            } else if now - started_ms >= wait_timeout_ms {
                break;
            }
        }
    };
    tonic::transport::Server::builder()
        .add_service(service.server())
        .serve_with_shutdown(config.listen, shutdown)
        .await?;

    let consumer_seen = connected.load(Ordering::Relaxed);
    let mut plane = plane
        .lock()
        .map_err(|_| "handoff_plane_poisoned".to_string())?;
    // 关闭前先结算过期，否则"迟到的消费者"会让账面对不上。
    plane.expire(sensoryplex_media::now_unix_ms());
    let stats = plane.stats();
    println!(
        "handoff_stats consumer_seen={} retained={} retained_total={} offered={} released={} expired={} retain_rejected={} request_rejected={} rejection_reasons={} arena_live_slabs={} arena_used_bytes={} arena_capacity_bytes={}",
        consumer_seen,
        stats.retained,
        stats.retained_total,
        stats.offered_total,
        stats.released_total,
        stats.expired_total,
        stats.retain_rejections,
        stats.request_rejections,
        describe_rejections(&stats),
        stats.arena_live_slabs,
        stats.arena_used_bytes,
        plane.arena_capacity_bytes(),
    );
    let accounted = stats
        .released_total
        .saturating_add(stats.expired_total)
        .saturating_add(stats.retained);
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
    if stats.rejection_reasons.is_empty() {
        return "none".to_string();
    }
    stats
        .rejection_reasons
        .iter()
        .map(|(reason, count)| format!("{reason}:{count}"))
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

struct Runtime;
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
        Ok(Response::new(capability::describe()))
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
    if args.first().map(String::as_str) == Some("replay") {
        return replay(&args[1..]).await;
    }
    if !args.is_empty() && args != ["serve"] {
        return Err(
            "usage: sensoryplex-runtime [serve | check <pipeline.yaml> | replay <pipeline.yaml> <media-path> --report <report.pb>]"
                .into(),
        );
    }
    let address = std::env::var("SENSORYPLEX_RUNTIME_ADDR")
        .unwrap_or_else(|_| "127.0.0.1:50051".into())
        .parse()?;
    tracing::info!(
        %address,
        platform = %capability::platform(),
        state = capability::state(),
        "runtime control endpoint started"
    );
    tonic::transport::Server::builder()
        .add_service(RuntimeServiceServer::new(Runtime))
        .serve_with_shutdown(address, async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}
