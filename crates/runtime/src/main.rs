use std::collections::BTreeSet;
use std::path::Path;

use prost::Message;
use sensoryplex_media::source::{drain_source, FileSource, MediaSource, UnavailableSource};
use sensoryplex_media::MediaError;
use sensoryplex_runtime::{capability, Pipeline};
use sensoryplex_sdk::media::ReplayReport;
use sensoryplex_sdk::runtime::{
    runtime_service_server::{RuntimeService, RuntimeServiceServer},
    DescribeCapabilitiesRequest, DescribeCapabilitiesResponse, HealthRequest, HealthResponse,
};
use tonic::{Request, Response, Status};

/// A gap larger than this is counted as a timeline discontinuity rather than smoothed over.
const GAP_THRESHOLD_MS: i64 = 1_000;
const DEFAULT_MAX_POINTS: usize = 1_000_000;
/// This build only anchors timelines; it does not decode buffers or hand out leases yet.
const REPLAY_BLOCKERS: [&str; 2] = [
    "gstreamer_decode_not_implemented",
    "buffer_lease_handoff_not_implemented",
];

struct ReplayArgs {
    pipeline: String,
    media: String,
    report: String,
    max_points: usize,
}

impl ReplayArgs {
    fn parse(args: &[String]) -> Result<Self, String> {
        let mut positional = Vec::new();
        let mut report = None;
        let mut max_points = DEFAULT_MAX_POINTS;
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
                value => {
                    positional.push(value.to_string());
                    index += 1;
                }
            }
        }
        if positional.len() != 2 || max_points == 0 {
            return Err(
                "usage: sensoryplex-runtime replay <pipeline.yaml> <media-path> --report <report.pb> [--max-points N]"
                    .into(),
            );
        }
        Ok(Self {
            pipeline: positional[0].clone(),
            media: positional[1].clone(),
            report: report.ok_or("replay requires --report <report.pb>")?,
            max_points,
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
        source: Some(description),
        platform: capability::platform(),
        blockers: REPLAY_BLOCKERS
            .iter()
            .map(|name| (*name).to_string())
            .collect(),
        // Only a real media acceptance run may ever set this; this build cannot.
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
    std::fs::write(&args.report, report.encode_to_vec())?;
    println!(
        "replay report written: platform={} anchors={} decoded={} dropped={} gaps={} out_of_order={} blockers={}",
        report.platform,
        report.emitted_anchors,
        report.decoded_items,
        report.dropped_items,
        report.gap_items,
        report.out_of_order_items,
        report.blockers.join(",")
    );
    Ok(())
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
