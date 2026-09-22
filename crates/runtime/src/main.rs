use std::collections::BTreeSet;
use std::path::Path;

use prost::Message;
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

/// 大于该阈值的间隔会被计入时间轴断层（discontinuity），不会被平滑掉。
const GAP_THRESHOLD_MS: i64 = 1_000;
const DEFAULT_MAX_POINTS: usize = 1_000_000;
const DEFAULT_AUDIO_SEGMENT_MS: u32 = sensoryplex_media::segment::DEFAULT_AUDIO_SEGMENT_MS;

/// 列出本次构建仍无法完成的能力，按构建显式声明。报告中绝不声明二进制不具备的能力。
fn replay_blockers() -> Vec<String> {
    let mut blockers = vec!["adaptive_sampling_not_implemented".to_string()];
    if !cfg!(feature = "gstreamer") {
        blockers.push("gstreamer_decode_not_implemented".to_string());
    }
    // 目前已经签发、校验并释放 lease，但还没有独立的 worker 进程消费它。
    blockers.push("lease_consumer_not_implemented".to_string());
    blockers
}

/// 将文件解码为已校验的 descriptor。未启用 GStreamer feature 的构建没有解码器，
/// 会通过 `replay_blockers` 显式说明，而不是返回空 data plane。
#[cfg(feature = "gstreamer")]
fn decode_pass(
    media: &str,
    stream_id: &str,
    arena_id: &str,
    max_samples: usize,
    audio_segment_ms: u32,
) -> Result<(Option<DecodedDataPlane>, bool), String> {
    let run = sensoryplex_media::decode::DecodeRun {
        decode: sensoryplex_media::decode::DecodeConfig::default(),
        max_samples,
        audio_segment_ms,
    };
    sensoryplex_media::decode::decode_file(Path::new(media), stream_id, arena_id, &run)
        .map(|(plane, truncated)| (Some(plane), truncated))
        .map_err(|error| error.to_string())
}

#[cfg(not(feature = "gstreamer"))]
fn decode_pass(
    _media: &str,
    _stream_id: &str,
    _arena_id: &str,
    _max_samples: usize,
    _audio_segment_ms: u32,
) -> Result<(Option<DecodedDataPlane>, bool), String> {
    Ok((None, false))
}

const REPLAY_USAGE: &str = "usage: sensoryplex-runtime replay <pipeline.yaml> <media-path> --report <report.pb> [--max-points N] [--audio-segment-ms N]";

struct ReplayArgs {
    pipeline: String,
    media: String,
    report: String,
    /// 时间轴锚点与解码样本数量的上限；不存在无界模式。
    max_points: usize,
    audio_segment_ms: u32,
}

impl ReplayArgs {
    fn parse(args: &[String]) -> Result<Self, String> {
        let mut positional = Vec::new();
        let mut report = None;
        let mut max_points = DEFAULT_MAX_POINTS;
        let mut audio_segment_ms = DEFAULT_AUDIO_SEGMENT_MS;
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
        Ok(Self {
            pipeline: positional[0].clone(),
            media: positional[1].clone(),
            report: report.ok_or("replay requires --report <report.pb>")?,
            max_points,
            audio_segment_ms,
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

fn replay(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
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
    match decode_pass(
        &args.media,
        &stream_id,
        &arena_id,
        args.max_points,
        args.audio_segment_ms,
    ) {
        Ok((plane, decode_truncated)) => {
            report.decoded = plane;
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
    println!(
        "replay report written: platform={} anchors={} decoded_items={} dropped={} gaps={} out_of_order={} descriptors={} leases_issued={} leases_released={} segments={} arena_peak_bytes={} blockers={}",
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
        report.blockers.join(",")
    );
    Ok(())
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
        return replay(&args[1..]);
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
