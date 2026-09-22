//! Local file decoding through GStreamer (ADR-003 golden path).
//!
//! Compiled only with the `gstreamer` feature so platforms without GStreamer development
//! files can still build and test the rest of the crate. Both sinks run with bounded queues,
//! so a consumer that stops draining slows the pipeline instead of buffering without limit.

use std::collections::BTreeSet;
use std::path::Path;
use std::sync::{Arc, Mutex};

use gstreamer as gst;
use gstreamer::prelude::*;
use gstreamer_app::AppSink;
use sensoryplex_sdk::common::{BufferDescriptor, BufferFormat, TimeRange};
use sensoryplex_sdk::media;

use crate::arena::{Arena, DEFAULT_ARENA_CAPACITY_BYTES};
use crate::descriptor::{hand_off, BufferSpec, HandoffCounters};
use crate::lease::LeaseRegistry;
use crate::segment::{AudioSegmenter, PendingSegment};
use crate::MediaError;

pub const DEFAULT_SINK_MAX_BUFFERS: u32 = 8;
pub const DEFAULT_PULL_TIMEOUT_MS: u64 = 5;
pub const DEFAULT_STATE_TIMEOUT_S: u64 = 15;
/// A decode that makes no progress for this many pulls is stalled, not slow.
pub const MAX_IDLE_ROUNDS: u32 = 2_000;
/// Evidence stays small: the report proves the handoff contract, it is not a buffer dump.
pub const MAX_EVIDENCE_DESCRIPTORS: usize = 4;
pub const MAX_LISTED_SEGMENTS: usize = 64;
pub const VIDEO_FRAME_KIND: &str = "video_frame";
pub const AUDIO_PCM_KIND: &str = "audio_pcm";
pub const AUDIO_SEGMENT_KIND: &str = "audio_segment";

/// One decode run's bounds. Every loop in this module is governed by one of these.
#[derive(Debug, Clone)]
pub struct DecodeRun {
    pub decode: DecodeConfig,
    pub max_samples: usize,
    pub audio_segment_ms: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrackKind {
    Video,
    Audio,
}

impl TrackKind {
    pub fn name(self) -> &'static str {
        match self {
            Self::Video => "video",
            Self::Audio => "audio",
        }
    }
}

#[derive(Debug, Clone)]
pub struct DecodeConfig {
    pub sink_max_buffers: u32,
    pub pull_timeout_ms: u64,
    pub state_timeout_s: u64,
}

impl Default for DecodeConfig {
    fn default() -> Self {
        Self {
            sink_max_buffers: DEFAULT_SINK_MAX_BUFFERS,
            pull_timeout_ms: DEFAULT_PULL_TIMEOUT_MS,
            state_timeout_s: DEFAULT_STATE_TIMEOUT_S,
        }
    }
}

/// One decoded sample plus the layout needed to describe it.
#[derive(Debug, Clone)]
pub struct DecodedSample {
    pub track: TrackKind,
    /// Presentation origin already subtracted from `pts_ms`, straight from the pipeline's
    /// segment event. Zero means the container did not shift this track.
    pub origin_ms: i64,
    /// `None` when the element produced no presentation timestamp; never invented.
    pub pts_ms: Option<i64>,
    /// `None` when the buffer carries no duration; the driver then needs the next sample.
    pub duration_ms: Option<i64>,
    pub width: u32,
    pub height: u32,
    pub pixel_format: String,
    pub sample_rate: u32,
    pub channels: u32,
    pub audio_format: String,
    pub bytes: Vec<u8>,
}

struct Chain {
    kind: TrackKind,
    queue: gst::Element,
    sink: AppSink,
    origin: Origin,
}

/// Where the presentation timeline starts for one track.
///
/// Containers can carry an edit list that trims the head of a stream (this sample's video
/// track starts at media time 15000/90000 s). GStreamer reports the raw media timestamp and
/// announces the presentation origin in the segment event, so a descriptor built from the raw
/// timestamp would sit 166 ms away from every other tool's answer - and from the audio track.
type Origin = Arc<Mutex<Option<gst::ClockTime>>>;

struct TrackHandle {
    sink: AppSink,
    origin: Origin,
}

pub struct GstFileDecoder {
    pipeline: gst::Pipeline,
    video: Option<TrackHandle>,
    audio: Option<TrackHandle>,
    config: DecodeConfig,
}

impl GstFileDecoder {
    /// Builds and starts the pipeline. Missing tracks stay `None` instead of being faked.
    pub fn open(path: &Path, config: DecodeConfig) -> Result<Self, MediaError> {
        if !path.is_file() {
            return Err(MediaError::IoFailed("media_path_not_a_file".into()));
        }
        gst::init().map_err(|error| MediaError::DecodeFailed(format!("gst_init: {error}")))?;
        let pipeline = gst::Pipeline::new();
        let source = gst::ElementFactory::make("filesrc")
            .name("source")
            .build()
            .map_err(decoder_error)?;
        source.set_property("location", path.to_string_lossy().to_string());
        let decode = gst::ElementFactory::make("decodebin")
            .name("decode")
            .build()
            .map_err(decoder_error)?;
        pipeline.add(&source).map_err(decoder_error)?;
        pipeline.add(&decode).map_err(decoder_error)?;
        gst::Element::link(&source, &decode).map_err(decoder_error)?;

        let chains = [
            build_chain(
                &pipeline,
                TrackKind::Video,
                "videoconvert",
                gst::Caps::builder("video/x-raw")
                    .field("format", "RGBA")
                    .build(),
                &config,
            )?,
            build_chain(
                &pipeline,
                TrackKind::Audio,
                "audioconvert",
                gst::Caps::builder("audio/x-raw")
                    .field("format", "F32LE")
                    .build(),
                &config,
            )?,
        ];
        let video = chains
            .iter()
            .find(|chain| chain.kind == TrackKind::Video)
            .map(|chain| TrackHandle {
                sink: chain.sink.clone(),
                origin: Arc::clone(&chain.origin),
            });
        let audio = chains
            .iter()
            .find(|chain| chain.kind == TrackKind::Audio)
            .map(|chain| TrackHandle {
                sink: chain.sink.clone(),
                origin: Arc::clone(&chain.origin),
            });

        let linkable = chains
            .iter()
            .map(|chain| (chain.kind, chain.queue.clone(), chain.sink.clone()))
            .collect::<Vec<_>>();
        decode.connect_pad_added(move |_, pad| {
            let caps = pad.current_caps().unwrap_or_else(|| pad.query_caps(None));
            let kind = classify(&caps);
            let Some(kind) = kind else {
                tracing::warn!(
                    caps = %caps.to_string(),
                    "decoded pad ignored: unsupported media type"
                );
                return;
            };
            let Some((_, queue, sink)) =
                linkable.iter().find(|(chain_kind, ..)| *chain_kind == kind)
            else {
                return;
            };
            let Some(sink_pad) = queue.static_pad("sink") else {
                return;
            };
            if sink_pad.is_linked() {
                return;
            }
            if pad.link(&sink_pad).is_err() {
                tracing::error!(track = kind.name(), "failed to link decoded pad");
                return;
            }
            sink.set_state(gst::State::Playing).ok();
        });

        pipeline
            .set_state(gst::State::Playing)
            .map_err(state_change_error)?;
        let (state, _, _) = pipeline.state(gst::ClockTime::from_seconds(config.state_timeout_s));
        let state = state.map_err(state_change_error)?;
        if state != gst::StateChangeSuccess::Success {
            tracing::warn!(
                state = %gst::StateChangeReturn::from_ok(state).name(),
                timeout_s = config.state_timeout_s,
                "pipeline did not finish its state change within the timeout"
            );
        }
        Ok(Self {
            pipeline,
            video,
            audio,
            config,
        })
    }

    pub fn has_track(&self, kind: TrackKind) -> bool {
        self.handle(kind).is_some()
    }

    pub fn is_eos(&self, kind: TrackKind) -> bool {
        self.handle(kind).is_some_and(|handle| handle.sink.is_eos())
    }

    /// Pulls one sample, waiting at most `pull_timeout_ms`. `None` means "nothing available
    /// yet" or end of stream; callers distinguish the two with `is_eos`.
    pub fn pull(&self, kind: TrackKind) -> Result<Option<DecodedSample>, MediaError> {
        let Some(handle) = self.handle(kind) else {
            return Ok(None);
        };
        let timeout = gst::ClockTime::from_mseconds(self.config.pull_timeout_ms);
        let Some(sample) = handle.sink.try_pull_sample(timeout) else {
            return Ok(None);
        };
        let origin = handle
            .origin
            .lock()
            .ok()
            .and_then(|origin| *origin)
            .ok_or_else(|| MediaError::DecodeFailed("presentation_origin_unavailable".into()))?;
        decode_sample(kind, &sample, origin)
    }

    fn handle(&self, kind: TrackKind) -> Option<&TrackHandle> {
        match kind {
            TrackKind::Video => self.video.as_ref(),
            TrackKind::Audio => self.audio.as_ref(),
        }
    }
}

impl Drop for GstFileDecoder {
    fn drop(&mut self) {
        let _ = self.pipeline.set_state(gst::State::Null);
    }
}

fn build_chain(
    pipeline: &gst::Pipeline,
    kind: TrackKind,
    convert: &str,
    caps: gst::Caps,
    config: &DecodeConfig,
) -> Result<Chain, MediaError> {
    let queue = gst::ElementFactory::make("queue")
        .name(format!("{}-queue", kind.name()))
        .property("max-size-buffers", config.sink_max_buffers)
        .property("max-size-bytes", 0u32)
        .property("max-size-time", 0u64)
        .build()
        .map_err(decoder_error)?;
    let converter = gst::ElementFactory::make(convert)
        .name(format!("{}-convert", kind.name()))
        .build()
        .map_err(decoder_error)?;
    let filter = gst::ElementFactory::make("capsfilter")
        .name(format!("{}-caps", kind.name()))
        .property("caps", caps)
        .build()
        .map_err(decoder_error)?;
    let sink = AppSink::builder()
        .name(format!("{}-sink", kind.name()))
        .max_buffers(config.sink_max_buffers)
        .drop(false)
        .sync(false)
        .build();
    let origin: Origin = Arc::new(Mutex::new(None));
    if let Some(pad) = queue.static_pad("sink") {
        let captured = Arc::clone(&origin);
        pad.add_probe(gst::PadProbeType::EVENT_DOWNSTREAM, move |_, info| {
            if let Some(gst::PadProbeData::Event(event)) = &info.data {
                if let gst::EventView::Segment(segment) = event.view() {
                    if let Some(start) = segment
                        .segment()
                        .downcast_ref::<gst::ClockTime>()
                        .and_then(|segment| segment.start())
                    {
                        if let Ok(mut slot) = captured.lock() {
                            *slot = Some(start);
                        }
                    }
                }
            }
            gst::PadProbeReturn::Ok
        });
    }
    pipeline.add(&queue).map_err(decoder_error)?;
    pipeline.add(&converter).map_err(decoder_error)?;
    pipeline.add(&filter).map_err(decoder_error)?;
    pipeline
        .add(sink.upcast_ref::<gst::Element>())
        .map_err(decoder_error)?;
    gst::Element::link(&queue, &converter).map_err(decoder_error)?;
    gst::Element::link(&converter, &filter).map_err(decoder_error)?;
    gst::Element::link(&filter, sink.upcast_ref::<gst::Element>()).map_err(decoder_error)?;
    Ok(Chain {
        kind,
        queue,
        sink,
        origin,
    })
}

fn classify(caps: &gst::Caps) -> Option<TrackKind> {
    let structure = caps.structure(0)?;
    let name = structure.name();
    if name.starts_with("video/") {
        Some(TrackKind::Video)
    } else if name.starts_with("audio/") {
        Some(TrackKind::Audio)
    } else {
        None
    }
}

/// Container timestamps are nanosecond-exact and the contract is whole milliseconds, so
/// round rather than truncate: truncation loses up to a millisecond per sample.
fn to_ms(value: gst::ClockTime) -> i64 {
    ((value.nseconds() as i128 + 500_000) / 1_000_000) as i64
}

fn presentation_ms(value: Option<gst::ClockTime>, origin: gst::ClockTime) -> Option<i64> {
    // Subtracting first and rounding second keeps the shift exact; rounding both ends and then
    // subtracting would re-introduce the drift the origin is there to remove.
    value.map(|value| to_ms(value.checked_sub(origin).unwrap_or(value)))
}

fn decode_sample(
    kind: TrackKind,
    sample: &gst::Sample,
    origin: gst::ClockTime,
) -> Result<Option<DecodedSample>, MediaError> {
    let Some(buffer) = sample.buffer() else {
        return Ok(None);
    };
    let mapped = buffer.map_readable().map_err(decoder_error)?;
    let pts_ms = presentation_ms(buffer.pts(), origin);
    let duration_ms = buffer.duration().map(to_ms);
    let origin_ms = to_ms(origin);
    let caps = sample.caps_owned();
    let structure = caps.as_ref().and_then(|caps| caps.structure(0));
    let get_i32 = |field: &str| -> u32 {
        structure
            .and_then(|structure| structure.get::<i32>(field).ok())
            .filter(|value| *value > 0)
            .map(|value| value as u32)
            .unwrap_or(0)
    };
    let get_string = |field: &str| -> String {
        structure
            .and_then(|structure| structure.get::<&str>(field).ok())
            .map(|value| value.to_string())
            .unwrap_or_default()
    };
    Ok(Some(DecodedSample {
        track: kind,
        origin_ms,
        pts_ms,
        duration_ms,
        width: get_i32("width"),
        height: get_i32("height"),
        pixel_format: get_string("format"),
        sample_rate: get_i32("rate"),
        channels: get_i32("channels"),
        audio_format: get_string("format"),
        bytes: mapped.as_slice().to_vec(),
    }))
}

fn decoder_error(error: gst::glib::BoolError) -> MediaError {
    MediaError::DecodeFailed(error.to_string())
}

fn state_change_error(error: gst::StateChangeError) -> MediaError {
    MediaError::DecodeFailed(format!("pipeline_state_change_failed: {error}"))
}

fn kind_name(kind: TrackKind) -> &'static str {
    kind.name()
}

fn track_index(kind: TrackKind) -> usize {
    match kind {
        TrackKind::Video => 0,
        TrackKind::Audio => 1,
    }
}

fn buffer_kind(kind: TrackKind) -> &'static str {
    match kind {
        TrackKind::Video => VIDEO_FRAME_KIND,
        TrackKind::Audio => AUDIO_PCM_KIND,
    }
}

fn now_unix_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis() as i64)
        .unwrap_or_default()
}

fn buffer_format(kind: TrackKind, sample: &DecodedSample) -> BufferFormat {
    match kind {
        TrackKind::Video => BufferFormat {
            pixel_format: sample.pixel_format.clone(),
            width: sample.width,
            height: sample.height,
            ..Default::default()
        },
        TrackKind::Audio => BufferFormat {
            sample_rate: sample.sample_rate,
            channels: sample.channels,
            ..Default::default()
        },
    }
}

/// A sample whose layout is unknown cannot be handed off: the descriptor would describe
/// nothing. It is dropped with a reason instead.
fn layout_problem(kind: TrackKind, sample: &DecodedSample) -> Option<&'static str> {
    match kind {
        TrackKind::Video if sample.width == 0 || sample.height == 0 => Some("missing_frame_size"),
        TrackKind::Video if sample.pixel_format.is_empty() => Some("missing_pixel_format"),
        TrackKind::Audio if sample.sample_rate == 0 || sample.channels == 0 => {
            Some("missing_audio_layout")
        }
        _ => None,
    }
}

/// The layout of a track, taken from its first accepted sample. Never guessed.
#[derive(Debug, Clone, Default)]
struct SampleLayout {
    width: u32,
    height: u32,
    pixel_format: String,
    sample_rate: u32,
    channels: u32,
    audio_format: String,
}

struct TrackState {
    kind: TrackKind,
    layout: SampleLayout,
    samples: u64,
    bytes: u64,
    first_pts_ms: Option<i64>,
    last_end_ms: i64,
    dropped_samples: u64,
    overlapping_samples: u64,
    origin_ms: Option<i64>,
    drop_reasons: BTreeSet<&'static str>,
    next_index: u64,
    evidence_taken: bool,
}

impl TrackState {
    fn new(kind: TrackKind) -> Self {
        Self {
            kind,
            layout: SampleLayout::default(),
            samples: 0,
            bytes: 0,
            first_pts_ms: None,
            last_end_ms: -1,
            dropped_samples: 0,
            overlapping_samples: 0,
            origin_ms: None,
            drop_reasons: BTreeSet::new(),
            next_index: 0,
            evidence_taken: false,
        }
    }

    fn stat(&self) -> media::DecodedTrackStat {
        media::DecodedTrackStat {
            track_kind: kind_name(self.kind).to_string(),
            samples: self.samples,
            bytes: self.bytes,
            first_pts_ms: self.first_pts_ms.unwrap_or(-1),
            last_end_ms: self.last_end_ms,
            dropped_samples: self.dropped_samples,
            overlapping_samples: self.overlapping_samples,
            timeline_offset_ms: self.origin_ms.unwrap_or(0),
            width: self.layout.width,
            height: self.layout.height,
            pixel_format: self.layout.pixel_format.clone(),
            sample_rate: self.layout.sample_rate,
            channels: self.layout.channels,
            audio_format: self.layout.audio_format.clone(),
            drop_reasons: self
                .drop_reasons
                .iter()
                .map(|reason| (*reason).to_string())
                .collect(),
        }
    }
}

struct DecodeSession<'a> {
    stream_id: &'a str,
    stream_short: &'a str,
    now_ms: i64,
    segment_ms: u32,
    arena: Arena,
    leases: LeaseRegistry,
    counters: HandoffCounters,
    tracks: Vec<TrackState>,
    evidence: Vec<BufferDescriptor>,
    segmenter: Option<AudioSegmenter>,
    segment_report: media::AudioSegmentReport,
}

impl<'a> DecodeSession<'a> {
    fn new(
        stream_id: &'a str,
        stream_short: &'a str,
        arena_id: &str,
        run: &DecodeRun,
    ) -> Result<Self, MediaError> {
        Ok(Self {
            stream_id,
            stream_short,
            now_ms: now_unix_ms(),
            segment_ms: run.audio_segment_ms,
            arena: Arena::new(arena_id, DEFAULT_ARENA_CAPACITY_BYTES)?,
            leases: LeaseRegistry::default(),
            counters: HandoffCounters::default(),
            tracks: vec![
                TrackState::new(TrackKind::Video),
                TrackState::new(TrackKind::Audio),
            ],
            evidence: Vec::new(),
            segmenter: None,
            segment_report: media::AudioSegmentReport {
                segment_ms: run.audio_segment_ms,
                listed_limit: MAX_LISTED_SEGMENTS as u32,
                ..Default::default()
            },
        })
    }

    fn drop_sample(&mut self, index: usize, reason: &'static str) {
        self.tracks[index].dropped_samples += 1;
        self.tracks[index].drop_reasons.insert(reason);
    }

    /// Turns one decoded sample into a verified descriptor, a track statistic and - for
    /// audio - a segment contribution. Bytes never leave the arena.
    fn accept(&mut self, sample: DecodedSample) -> Result<(), MediaError> {
        let index = track_index(sample.track);
        let Some(pts_ms) = sample.pts_ms.filter(|pts| *pts >= 0) else {
            self.drop_sample(index, "pts_unavailable");
            return Ok(());
        };
        let Some(duration_ms) = sample.duration_ms.filter(|duration| *duration > 0) else {
            self.drop_sample(index, "duration_unavailable");
            return Ok(());
        };
        if sample.bytes.is_empty() {
            self.drop_sample(index, "empty_payload");
            return Ok(());
        }
        if let Some(reason) = layout_problem(sample.track, &sample) {
            self.drop_sample(index, reason);
            return Ok(());
        }

        let end_ms = pts_ms + duration_ms;
        {
            let track = &mut self.tracks[index];
            // The origin is a property of the track, not of a buffer. A change mid-stream
            // means a new segment started, which this replay path does not model.
            match track.origin_ms {
                Some(seen) if seen != sample.origin_ms => {
                    return Err(MediaError::DecodeFailed(
                        "presentation_origin_changed_mid_stream".into(),
                    ));
                }
                Some(_) => {}
                None => track.origin_ms = Some(sample.origin_ms),
            }
            // A repeated interval is kept - the payload is real decoded audio - but counted.
            if track.last_end_ms >= 0 && pts_ms < track.last_end_ms {
                track.overlapping_samples += 1;
            }
            track.next_index += 1;
            let buffer_id = format!(
                "buf-{}-{}-{:08}",
                self.stream_short,
                buffer_kind(sample.track),
                track.next_index
            );
            let descriptor = hand_off(
                &mut self.arena,
                &mut self.leases,
                BufferSpec {
                    buffer_id,
                    kind: buffer_kind(sample.track),
                    stream_id: self.stream_id,
                    time_range: TimeRange {
                        start_ms: pts_ms,
                        end_ms,
                    },
                    format: buffer_format(sample.track, &sample),
                },
                &sample.bytes,
                self.now_ms,
                &mut self.counters,
            )?;
            if track.samples == 0 {
                track.layout = SampleLayout {
                    width: sample.width,
                    height: sample.height,
                    pixel_format: sample.pixel_format.clone(),
                    sample_rate: sample.sample_rate,
                    channels: sample.channels,
                    audio_format: sample.audio_format.clone(),
                };
            }
            track.samples += 1;
            track.bytes += sample.bytes.len() as u64;
            track.last_end_ms = end_ms;
            if track.first_pts_ms.is_none() {
                track.first_pts_ms = Some(pts_ms);
            }
            if !track.evidence_taken && self.evidence.len() < MAX_EVIDENCE_DESCRIPTORS {
                track.evidence_taken = true;
                self.evidence.push(descriptor);
            }
        }

        if sample.track == TrackKind::Audio {
            self.segment(&sample, pts_ms, duration_ms)?;
        }
        Ok(())
    }

    fn segment(
        &mut self,
        sample: &DecodedSample,
        pts_ms: i64,
        duration_ms: i64,
    ) -> Result<(), MediaError> {
        if self.segmenter.is_none() {
            self.segmenter = Some(AudioSegmenter::new(self.segment_ms)?);
        }
        let pending = match self.segmenter.as_mut() {
            Some(segmenter) => segmenter.push(
                sample.sample_rate,
                sample.channels,
                pts_ms,
                duration_ms,
                &sample.bytes,
            )?,
            None => None,
        };
        if let Some(segment) = pending {
            self.emit_segment(sample.sample_rate, sample.channels, &segment)?;
        }
        Ok(())
    }

    fn emit_segment(
        &mut self,
        sample_rate: u32,
        channels: u32,
        segment: &PendingSegment,
    ) -> Result<(), MediaError> {
        self.segment_report.segments += 1;
        if segment.partial {
            self.segment_report.partial_segments += 1;
        }
        self.segment_report.bytes += segment.bytes.len() as u64;
        let segment_id = format!(
            "seg-{}-{:08}",
            self.stream_short, self.segment_report.segments
        );
        let descriptor = hand_off(
            &mut self.arena,
            &mut self.leases,
            BufferSpec {
                buffer_id: segment_id.clone(),
                kind: AUDIO_SEGMENT_KIND,
                stream_id: self.stream_id,
                time_range: TimeRange {
                    start_ms: segment.start_ms,
                    end_ms: segment.end_ms,
                },
                format: BufferFormat {
                    sample_rate,
                    channels,
                    ..Default::default()
                },
            },
            &segment.bytes,
            self.now_ms,
            &mut self.counters,
        )?;
        if self.segment_report.listed.len() < MAX_LISTED_SEGMENTS {
            self.segment_report.listed.push(media::AudioSegment {
                segment_id: descriptor.buffer_id,
                time_range: descriptor.time_range,
                sample_rate,
                channels,
                bytes: segment.bytes.len() as u64,
                partial: segment.partial,
            });
        }
        Ok(())
    }

    fn finish(mut self) -> Result<media::DecodedDataPlane, MediaError> {
        let flushed = self.segmenter.as_mut().map(|segmenter| {
            (
                segmenter.sample_rate(),
                segmenter.channels(),
                segmenter.finish(),
                segmenter.dropped_samples(),
                segmenter.discontinuities(),
                segmenter.drop_reasons(),
            )
        });
        if let Some((sample_rate, channels, tail, dropped, discontinuities, reasons)) = flushed {
            self.segment_report.dropped_samples = dropped;
            self.segment_report.discontinuities = discontinuities;
            self.segment_report.drop_reasons = reasons;
            if let Some(segment) = tail {
                self.emit_segment(sample_rate, channels, &segment)?;
            }
        }
        Ok(media::DecodedDataPlane {
            arena_id: self.arena.id().to_string(),
            arena_capacity_bytes: self.arena.capacity() as u64,
            arena_peak_bytes: self.arena.peak_bytes() as u64,
            decoded_bytes: self.tracks.iter().map(|track| track.bytes).sum(),
            tracks: self.tracks.iter().map(TrackState::stat).collect(),
            descriptors_built: self.counters.built,
            descriptors_validated: self.counters.validated,
            descriptor_failures: self.counters.failures,
            failure_reasons: self.counters.failure_reasons(),
            leases_issued: self.counters.leases_issued,
            leases_released: self.counters.leases_released,
            evidence_descriptors: self.evidence,
            audio_segments: Some(self.segment_report),
        })
    }
}

/// Decodes a local file into verified descriptors.
///
/// Every sample is copied into a bounded arena, converted into a read-only leased descriptor,
/// verified and released; the returned plane carries the counters and a small evidence set.
/// `truncated` means the sample budget stopped the run, which the caller must report.
pub fn decode_file(
    path: &Path,
    stream_id: &str,
    arena_id: &str,
    run: &DecodeRun,
) -> Result<(media::DecodedDataPlane, bool), MediaError> {
    if run.max_samples == 0 {
        return Err(MediaError::DecodeFailed(
            "decode_sample_budget_is_zero".into(),
        ));
    }
    let decoder = GstFileDecoder::open(path, run.decode.clone())?;
    if !decoder.has_track(TrackKind::Video) && !decoder.has_track(TrackKind::Audio) {
        return Err(MediaError::DecodeFailed("no_decodable_track".into()));
    }
    let stream_short = stream_id.strip_prefix("stream-").unwrap_or(stream_id);
    let mut session = DecodeSession::new(stream_id, stream_short, arena_id, run)?;

    let mut consumed = 0usize;
    let mut idle_rounds = 0u32;
    let mut truncated = false;
    loop {
        let mut running = false;
        let mut progressed = false;
        for kind in [TrackKind::Video, TrackKind::Audio] {
            if !decoder.has_track(kind) || decoder.is_eos(kind) {
                continue;
            }
            running = true;
            if consumed >= run.max_samples {
                truncated = true;
                break;
            }
            let Some(sample) = decoder.pull(kind)? else {
                continue;
            };
            progressed = true;
            consumed += 1;
            session.accept(sample)?;
        }
        if truncated || !running {
            break;
        }
        if progressed {
            idle_rounds = 0;
        } else {
            idle_rounds += 1;
            if idle_rounds > MAX_IDLE_ROUNDS {
                return Err(MediaError::DecodeFailed("decode_stalled".into()));
            }
        }
    }
    Ok((session.finish()?, truncated))
}
