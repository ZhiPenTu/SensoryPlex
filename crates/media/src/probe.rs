//! 离线媒体探测，基于 ffprobe（ADR-003：实时路径用 GStreamer，离线路径用 FFmpeg）。
//! 媒体字节不会离开本模块，摘要不会被合成。

use std::path::Path;
use std::process::Command;

use sensoryplex_sdk::media::{MediaSourceDescription, MediaSourceKind, MediaSourceRef, MediaTrack};
use sha2::{Digest, Sha256};

use crate::MediaError;

/// 单条轨道上的一个探测时间点。当容器报告 N/A 时 `pts_ms` 保持 `None`，
/// 调用方按原因丢弃，而不是猜测一个时间戳。
#[derive(Debug, Clone, PartialEq)]
pub struct ProbePoint {
    pub pts_ms: Option<i64>,
    pub keyframe: bool,
}

#[derive(Debug, Clone)]
pub struct ProbedTrack {
    pub track_kind: String,
    pub points: Vec<ProbePoint>,
}

#[derive(Debug, Clone)]
pub struct ProbedFile {
    pub source: MediaSourceRef,
    pub description: MediaSourceDescription,
    pub tracks: Vec<ProbedTrack>,
}

pub fn file_digest(path: &Path) -> Result<String, MediaError> {
    let mut file =
        std::fs::File::open(path).map_err(|error| MediaError::IoFailed(error.to_string()))?;
    let mut hasher = Sha256::new();
    std::io::copy(&mut file, &mut hasher)
        .map_err(|error| MediaError::IoFailed(error.to_string()))?;
    Ok(format!("sha256:{:x}", hasher.finalize()))
}

/// 探测一个本地文件。返回的引用携带摘要，绝不携带 URI。
///
/// Stream 与 source 的身份由内容摘要派生，因此对同一文件的 replay 是幂等的，
/// 不会把不同文件误判为同一个。
pub fn probe_file(uri_secret_ref: &str, path: &Path) -> Result<ProbedFile, MediaError> {
    if !path.is_file() {
        return Err(MediaError::IoFailed("media_path_not_a_file".into()));
    }
    let content_hash = file_digest(path)?;
    let short = content_hash.get(7..19).unwrap_or_default();
    let source = MediaSourceRef {
        stream_id: format!("stream-{short}"),
        source_id: format!("file-{short}"),
        kind: MediaSourceKind::File as i32,
        uri_secret_ref: uri_secret_ref.to_string(),
        content_hash,
    };
    let probe_tool = probe_tool_version()?;
    let duration_ms = duration_ms(path)?;
    let mut tracks = Vec::new();
    let mut probed = Vec::new();
    if let Some(track) = video_track(path)? {
        tracks.push(track);
        probed.push(ProbedTrack {
            track_kind: "video".into(),
            points: track_points(path, "v:0")?,
        });
    }
    if let Some(track) = audio_track(path)? {
        tracks.push(track);
        probed.push(ProbedTrack {
            track_kind: "audio".into(),
            points: track_points(path, "a:0")?,
        });
    }
    if tracks.is_empty() {
        return Err(MediaError::InvalidProbeOutput(
            "no_audio_or_video_track".into(),
        ));
    }
    Ok(ProbedFile {
        source: source.clone(),
        description: MediaSourceDescription {
            source: Some(source),
            tracks,
            duration_ms,
            probe_tool,
        },
        tracks: probed,
    })
}

fn probe_tool_version() -> Result<String, MediaError> {
    let output = ffprobe(&["-version"], None)?;
    let version = output
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(2))
        .ok_or_else(|| MediaError::InvalidProbeOutput("ffprobe_version".into()))?;
    Ok(format!("ffprobe {version}"))
}

fn duration_ms(path: &Path) -> Result<i64, MediaError> {
    let output = ffprobe(
        &[
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
        ],
        Some(path),
    )?;
    match output.trim() {
        "" | "N/A" => Ok(0), // 未知时长刻意保持为 0。
        value => seconds_to_ms(value),
    }
}

fn video_track(path: &Path) -> Result<Option<MediaTrack>, MediaError> {
    let output = ffprobe(
        &[
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate",
            "-of",
            "csv=p=0",
        ],
        Some(path),
    )?;
    let line = output.trim();
    if line.is_empty() {
        return Ok(None);
    }
    let fields = line.split(',').collect::<Vec<_>>();
    Ok(Some(MediaTrack {
        track_kind: "video".into(),
        codec: known(fields.first()),
        width: fields
            .get(1)
            .and_then(|value| value.parse().ok())
            .unwrap_or(0),
        height: fields
            .get(2)
            .and_then(|value| value.parse().ok())
            .unwrap_or(0),
        sample_rate: 0,
        channels: 0,
        average_frame_rate: fields.get(3).map(|value| frame_rate(value)).unwrap_or(0.0),
        timing_known: true,
    }))
}

fn audio_track(path: &Path) -> Result<Option<MediaTrack>, MediaError> {
    let output = ffprobe(
        &[
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels",
            "-of",
            "csv=p=0",
        ],
        Some(path),
    )?;
    let line = output.trim();
    if line.is_empty() {
        return Ok(None);
    }
    let fields = line.split(',').collect::<Vec<_>>();
    Ok(Some(MediaTrack {
        track_kind: "audio".into(),
        codec: known(fields.first()),
        width: 0,
        height: 0,
        sample_rate: fields
            .get(1)
            .and_then(|value| value.parse().ok())
            .unwrap_or(0),
        channels: fields
            .get(2)
            .and_then(|value| value.parse().ok())
            .unwrap_or(0),
        average_frame_rate: 0.0,
        timing_known: true,
    }))
}

fn track_points(path: &Path, selector: &str) -> Result<Vec<ProbePoint>, MediaError> {
    let output = ffprobe(
        &[
            "-v",
            "error",
            "-select_streams",
            selector,
            "-show_entries",
            "frame=key_frame,pts_time",
            "-of",
            "csv=p=0",
        ],
        Some(path),
    )?;
    let mut points = Vec::new();
    for line in output.lines().filter(|line| !line.trim().is_empty()) {
        let mut fields = line.split(',');
        let keyframe = fields
            .next()
            .map(|value| value.trim() == "1")
            .unwrap_or(false);
        let raw = fields.next().map(str::trim).filter(|value| *value != "N/A");
        let pts_ms = match raw {
            Some(value) if !value.is_empty() => Some(seconds_to_ms(value)?),
            _ => None,
        };
        points.push(ProbePoint { pts_ms, keyframe });
    }
    Ok(points)
}

fn ffprobe(args: &[&str], path: Option<&Path>) -> Result<String, MediaError> {
    let mut command = Command::new("ffprobe");
    command.args(args);
    if let Some(path) = path {
        command.arg(path);
    }
    let output = command.output().map_err(|error| match error.kind() {
        std::io::ErrorKind::NotFound => MediaError::ToolMissing("ffprobe".into()),
        _ => MediaError::ProbeFailed(error.to_string()),
    })?;
    if !output.status.success() {
        return Err(MediaError::ProbeFailed(
            String::from_utf8_lossy(&output.stderr).trim().to_string(),
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

/// 空的 codec 表示"探测未能识别"，绝不是猜测得到的名称。
fn known(value: Option<&&str>) -> String {
    match value.map(|value| value.trim()) {
        Some(value) if value.is_empty() || value == "N/A" => String::new(),
        Some(value) => value.to_string(),
        None => String::new(),
    }
}

fn frame_rate(value: &str) -> f64 {
    let (numerator, denominator) = match value.trim().split_once('/') {
        Some(parts) => parts,
        None => return value.trim().parse().unwrap_or(0.0),
    };
    let numerator: f64 = numerator.parse().unwrap_or(0.0);
    let denominator: f64 = denominator.parse().unwrap_or(0.0);
    if denominator == 0.0 {
        return 0.0; // "0/0" 表示未知帧率。
    }
    numerator / denominator
}

fn seconds_to_ms(value: &str) -> Result<i64, MediaError> {
    let seconds: f64 = value
        .trim()
        .parse()
        .map_err(|_| MediaError::InvalidProbeOutput(format!("timestamp={value}")))?;
    if !seconds.is_finite() || seconds < 0.0 {
        return Err(MediaError::InvalidProbeOutput(format!("timestamp={value}")));
    }
    Ok((seconds * 1000.0).round() as i64)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_rate_handles_rationals_and_unknowns() {
        assert!((frame_rate("30000/1001") - 29.970_029).abs() < 0.001);
        assert_eq!(frame_rate("0/0"), 0.0);
        assert_eq!(frame_rate("25"), 25.0);
    }

    #[test]
    fn timestamps_are_rejected_instead_of_clamped() {
        assert_eq!(seconds_to_ms("1.500000").unwrap(), 1_500);
        for value in ["N/A", "-0.5", "abc", "inf"] {
            assert!(seconds_to_ms(value).is_err(), "{value} must be rejected");
        }
    }

    #[test]
    fn missing_codec_is_reported_as_unknown() {
        assert_eq!(known(Some(&"N/A")), "");
        assert_eq!(known(Some(&"")), "");
        assert_eq!(known(None), "");
        assert_eq!(known(Some(&"h264")), "h264");
    }
}
