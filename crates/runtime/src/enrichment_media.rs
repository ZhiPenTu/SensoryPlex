//! 同机补全的按锚点解码：FFmpeg 只解一帧/一个有界音频段，Rust 持有共享内存与租约。
use std::path::Path;
use std::process::Stdio;
use std::time::Duration;

use prost::Message;
use sensoryplex_media::handoff::RetainPolicy;
use sensoryplex_media::source::{FileSource, MediaSource};
use sensoryplex_sdk::common::{BufferDescriptor, BufferFormat, BufferLocator, TimeRange};
use sensoryplex_sdk::orchestration::EnrichmentTask;
use tokio::io::AsyncReadExt;
use tokio::process::Command;

use crate::handoff_service::HandoffService;

const MAX_INPUT_BYTES: usize = 64 << 20;

pub async fn run(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    if args.len() != 3 {
        return Err("enrichment_media_arguments_invalid".into());
    }
    let data = std::fs::read(&args[0])?;
    if data.len() > 1 << 20 {
        return Err("enrichment_media_task_limit_exceeded".into());
    }
    let task = EnrichmentTask::decode(data.as_slice())?;
    let span = task
        .time_range
        .as_ref()
        .ok_or("enrichment_media_range_missing")?;
    if span.start_ms < 0 || span.end_ms <= span.start_ms || span.end_ms - span.start_ms > 60_000 {
        return Err("enrichment_media_range_invalid".into());
    }
    let mut source = FileSource::open("AGENT_CONTROLLED_ASSET", Path::new(&args[1]))?;
    let description = source.describe().clone();
    let identity = description
        .source
        .as_ref()
        .ok_or("enrichment_media_source_missing")?;
    if identity.content_hash != task.content_hash
        || identity.stream_id != task.stream_id
        || identity.source_id != task.source_id
        || span.end_ms > description.duration_ms
        || !task.observation_refs.is_empty()
    {
        return Err("enrichment_media_identity_mismatch".into());
    }
    let mut actual = *span;
    let (kind, format, extra, expected) = match task.input_modality.as_str() {
        "media.video_frame" => {
            let track = description
                .tracks
                .iter()
                .find(|t| t.track_kind == "video")
                .ok_or("enrichment_video_track_missing")?;
            let size = u64::from(track.width) * u64::from(track.height) * 4;
            if size == 0 || size > MAX_INPUT_BYTES as u64 {
                return Err("enrichment_media_input_limit_exceeded".into());
            }
            let mut selected = None;
            while let Some(point) = source.next_anchor()? {
                if point.track_kind != "video" {
                    continue;
                }
                if selected.is_none() && point.pts_ms >= span.start_ms && point.pts_ms < span.end_ms
                {
                    selected = Some(point.pts_ms);
                } else if let Some(start) = selected {
                    if point.pts_ms > start {
                        actual = TimeRange {
                            start_ms: start,
                            end_ms: point.pts_ms.min(span.end_ms),
                        };
                        break;
                    }
                }
            }
            let start = selected.ok_or("enrichment_anchor_has_no_video_frame")?;
            if actual.start_ms != start {
                actual = TimeRange {
                    start_ms: start,
                    end_ms: span.end_ms,
                };
            }
            (
                "video_frame",
                BufferFormat {
                    pixel_format: "RGBA".into(),
                    width: track.width,
                    height: track.height,
                    strides: vec![track.width * 4],
                    ..Default::default()
                },
                vec![
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgba",
                ],
                size as usize,
            )
        }
        "media.audio_segment" => {
            if !description.tracks.iter().any(|t| t.track_kind == "audio") {
                return Err("enrichment_audio_track_missing".into());
            }
            (
                "audio_segment",
                BufferFormat {
                    sample_format: "F32LE".into(),
                    sample_rate: 16000,
                    channels: 1,
                    ..Default::default()
                },
                vec![
                    "-map", "0:a:0", "-vn", "-f", "f32le", "-ar", "16000", "-ac", "1",
                ],
                0,
            )
        }
        _ => return Err("enrichment_media_modality_invalid".into()),
    };
    let mut command = Command::new("ffmpeg");
    command
        .args([
            "-v",
            "error",
            "-nostdin",
            "-ss",
            &format!("{:.3}", actual.start_ms as f64 / 1000.0),
        ])
        .arg("-i")
        .arg(&args[1])
        .args([
            "-t",
            &format!("{:.3}", (actual.end_ms - actual.start_ms) as f64 / 1000.0),
        ])
        .args(extra)
        .arg("pipe:1")
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let mut decoder = command.spawn()?;
    let stdout = decoder
        .stdout
        .take()
        .ok_or("enrichment_decode_pipe_missing")?;
    let mut pixels = Vec::new();
    tokio::time::timeout(Duration::from_secs(30), async {
        stdout
            .take((MAX_INPUT_BYTES + 1) as u64)
            .read_to_end(&mut pixels)
            .await?;
        let status = decoder.wait().await?;
        if !status.success() {
            return Err(std::io::Error::other("enrichment_anchor_decode_failed"));
        }
        Ok::<(), std::io::Error>(())
    })
    .await
    .map_err(|_| "enrichment_anchor_decode_timeout")??;
    if pixels.is_empty()
        || pixels.len() > MAX_INPUT_BYTES
        || (expected > 0 && pixels.len() != expected)
    {
        return Err("enrichment_decoded_layout_invalid".into());
    }
    if kind == "audio_segment" {
        if pixels.len() % 4 != 0 {
            return Err("enrichment_audio_layout_invalid".into());
        }
        let duration = (pixels.len() / 4) as i64 * 1000 / 16000;
        actual.end_ms = (actual.start_ms + duration).min(actual.end_ms);
        if actual.end_ms <= actual.start_ms {
            return Err("enrichment_audio_range_empty".into());
        }
    }
    let plane = RetainPolicy::shared(MAX_INPUT_BYTES, 2)?
        .open("enrichment", 0)?
        .ok_or("enrichment_handoff_missing")?;
    let buffer_id = format!("buf-{}", task.task_id);
    let held = plane
        .plane()?
        .retain(&buffer_id, kind, &task.stream_id, actual, format, &pixels)?;
    drop(pixels);
    let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
    let address = listener.local_addr()?;
    // 私有单任务进程只在本机监听；端点文件以 protobuf 原子落盘，不从日志猜端口。
    listener.set_nonblocking(true)?;
    let incoming = tokio_stream::wrappers::TcpListenerStream::new(
        tokio::net::TcpListener::from_std(listener)?,
    );
    let descriptor = BufferDescriptor {
        buffer_id,
        kind: kind.into(),
        stream_id: task.stream_id,
        time_range: Some(held.time_range),
        format: Some(held.format),
        content_hash: held.content_hash,
        memory_kind: "cpu_shared_memory".into(),
        locator: Some(BufferLocator {
            offset: held.offset,
            length: held.length,
            handoff_endpoint: address.to_string(),
            ..Default::default()
        }),
        ..Default::default()
    };
    let encoded = descriptor.encode_to_vec();
    let output = Path::new(&args[2]);
    let temp = output.with_extension("tmp");
    let server = tonic::transport::Server::builder()
        .add_service(HandoffService::new(plane.clone(), 30000).server());
    std::fs::write(&temp, encoded)?;
    std::fs::rename(temp, output)?;
    server
        .serve_with_incoming_shutdown(incoming, async move {
            let until = tokio::time::Instant::now() + Duration::from_secs(90);
            loop {
                if plane.stats().is_ok_and(|s| s.retained == 0)
                    || tokio::time::Instant::now() >= until
                {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        })
        .await?;
    Ok(())
}
