//! 本地文件解码，基于 GStreamer（ADR-003 golden path）。
//!
//! 仅在 `gstreamer` feature 开启时编译，便于没有 GStreamer 开发文件的平台
//! 仍能构建并测试 crate 的其他部分。两个 sink 都使用有界队列，
//! 消费端停止排空时会让 pipeline 减速，而不是无界地缓冲。

use std::collections::BTreeSet;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use gstreamer as gst;
use gstreamer::prelude::*;
use gstreamer_app::AppSink;
use sensoryplex_sdk::common::{BufferDescriptor, BufferFormat, TimeRange};
use sensoryplex_sdk::media;

use crate::arena::{Arena, DEFAULT_ARENA_CAPACITY_BYTES};
use crate::descriptor::{hand_off, BufferSpec, HandoffCounters};
use crate::handoff::{BufferHandoff, RetainPolicy};
use crate::lease::LeaseRegistry;
use crate::live::{LiveConfig, LiveStats, StallTracker};
use crate::sampler::{AdaptiveSampler, Decision, FrameSignature, SamplingPolicy};
use crate::segment::{AudioSegmenter, PendingSegment};
use crate::MediaError;

pub const DEFAULT_SINK_MAX_BUFFERS: u32 = 8;
pub const DEFAULT_PULL_TIMEOUT_MS: u64 = 5;
pub const DEFAULT_STATE_TIMEOUT_S: u64 = 15;
/// 直播不做 preroll：pad 是异步出现的，数据晚到不影响墙钟窗口循环，
/// 因此不等文件路径那种 15 秒状态变更超时——无源时更快暴露，而不是白等。
pub const LIVE_STATE_TIMEOUT_S: u64 = 3;
/// 连续多次 pull 都没有进展即视为卡住（stalled），而不是慢。
pub const MAX_IDLE_ROUNDS: u32 = 2_000;
/// 由 PTS 差分推导出的时长上限。更大的间隔说明中间丢了样本或者换了段，
/// 把整段空白当成一帧的时间长度会失真，因此按“不可用”处理并显式计数。
pub const MAX_DERIVED_DURATION_MS: i64 = 5_000;
/// 证据保持精简：报告只证明交接契约，不是 buffer 转储。
pub const MAX_EVIDENCE_DESCRIPTORS: usize = 4;
pub const MAX_LISTED_SEGMENTS: usize = 64;
pub const VIDEO_FRAME_KIND: &str = "video_frame";
pub const AUDIO_PCM_KIND: &str = "audio_pcm";
pub const AUDIO_SEGMENT_KIND: &str = "audio_segment";
/// 视频链由 capsfilter 固定为 RGBA；抽帧签名只对该布局成立，其他布局按未知处理。
pub const VIDEO_PIXEL_FORMAT: &str = "RGBA";

/// 单次 decode 运行的上限。本模块中的所有循环都受其中一项约束。
#[derive(Debug, Clone)]
pub struct DecodeRun {
    pub decode: DecodeConfig,
    pub max_samples: usize,
    pub audio_segment_ms: u32,
    /// 视频抽帧策略。抽帧始终启用：未抽帧的运行只能在报告里显式说明，
    /// 不能用"全保留"冒充。
    pub sampling: SamplingPolicy,
    /// 保留式交接：打开后字节留在共享内存里等第二个进程领取 lease。
    pub handoff: RetainPolicy,
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

/// 一个已解码的样本，加上描述它所需的布局信息。
#[derive(Debug, Clone)]
pub struct DecodedSample {
    pub track: TrackKind,
    /// 已从 `pts_ms` 中减去 presentation origin（直接取自 pipeline 的 segment event）。
    /// 为 0 表示容器没有平移这条轨道。
    pub origin_ms: i64,
    /// element 未产生 presentation 时间戳时为 `None`，绝不会被伪造。
    pub pts_ms: Option<i64>,
    /// buffer 不携带 duration 时为 `None`；驱动随后需要下一个样本才能确定时长。
    pub duration_ms: Option<i64>,
    /// `true` 表示上面的时长不是 buffer 自带的，而是由同轨下一个样本的 PTS 差分得来。
    /// 报告按此计数，绝不把推导值伪装成容器声明的时长。
    pub duration_derived: bool,
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

/// 单条轨道的 presentation 时间轴起点。
///
/// 容器可能携带一个裁掉流头部的 edit list（本样本的视频轨道起始媒体时间为
/// 15000/90000 s）。GStreamer 报告的是原始媒体时间戳，并把 presentation origin
/// 通过 segment event 告知。如果 descriptor 直接用原始时间戳构造，
/// 与其他工具（以及与音频轨道）的结果会差 166 ms。
type Origin = Arc<Mutex<Option<gst::ClockTime>>>;

struct TrackHandle {
    sink: AppSink,
    origin: Origin,
}

pub struct GstDecoder {
    pipeline: gst::Pipeline,
    video: Option<TrackHandle>,
    audio: Option<TrackHandle>,
    config: DecodeConfig,
}

/// `ElementFactory::make` 要求 GStreamer 已经初始化：核心元素能靠静态注册表解析，
/// 但外部插件（例如 `srtsrc`）要经插件注册表，未初始化时会直接 panic。
fn ensure_gst() -> Result<(), MediaError> {
    gst::init().map_err(|error| MediaError::DecodeFailed(format!("gst_init: {error}")))
}

impl GstDecoder {
    /// 构建并启动离线文件 pipeline。缺失的轨道保持 `None`，不会被伪造。
    pub fn open_file(path: &Path, config: DecodeConfig) -> Result<Self, MediaError> {
        ensure_gst()?;
        if !path.is_file() {
            return Err(MediaError::IoFailed("media_path_not_a_file".into()));
        }
        let source = gst::ElementFactory::make("filesrc")
            .name("source")
            .build()
            .map_err(decoder_error)?;
        source.set_property("location", path.to_string_lossy().to_string());
        Self::assemble(source, config)
    }

    /// 构建并启动 SRT 实时 pipeline。
    ///
    /// 直播没有 EOF：`automatic-eos` 关闭。断流由元素按 `auto-reconnect` 自行重连，
    /// 本进程只**测量**断流与恢复（见 `crate::live`），不假装控制重连，
    /// 也不把"元素内部重试了几次"写成已知事实。
    pub fn open_live(uri: &str, config: DecodeConfig) -> Result<Self, MediaError> {
        ensure_gst()?;
        let source = gst::ElementFactory::make("srtsrc")
            .name("source")
            .build()
            .map_err(decoder_error)?;
        source.set_property("uri", uri);
        source.set_property("auto-reconnect", true);
        source.set_property("automatic-eos", false);
        Self::assemble(source, config)
    }

    /// 离线与实时共用的装配部分：只在这里决定"源"是什么。
    fn assemble(source: gst::Element, config: DecodeConfig) -> Result<Self, MediaError> {
        ensure_gst()?;
        let pipeline = gst::Pipeline::new();
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
                    .field("format", VIDEO_PIXEL_FORMAT)
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

    /// 拉取一个样本，最长等待 `pull_timeout_ms`。`None` 表示"暂无可用样本"或流已结束；
    /// 调用方通过 `is_eos` 区分这两种情况。
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

impl Drop for GstDecoder {
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

/// 容器时间戳是纳秒精度的，但契约按毫秒对齐，所以要四舍五入而不是截断：
/// 截断会让每个样本损失最多一毫秒。
fn to_ms(value: gst::ClockTime) -> i64 {
    ((value.nseconds() as i128 + 500_000) / 1_000_000) as i64
}

fn presentation_ms(value: Option<gst::ClockTime>, origin: gst::ClockTime) -> Option<i64> {
    // 先做减法再四舍五入，位移保持精确；如果两端分别四舍五入后再相减，
    // 会重新引入 origin 原本要去除的漂移。
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
        duration_derived: false,
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

/// 布局未知的样本无法交接：descriptor 描述不出任何信息。这种样本会连同原因一起被丢弃。
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

/// 一条轨道的布局信息，从首个被接受的样本中取得，绝不猜测。
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
    /// 该轨最后一个被解码的样本的右端点。抽帧不会让它缩短。
    last_end_ms: i64,
    dropped_samples: u64,
    overlapping_samples: u64,
    duration_derived_samples: u64,
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
            duration_derived_samples: 0,
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
            duration_derived_samples: self.duration_derived_samples,
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

pub(crate) struct DecodeSession<'a> {
    stream_id: &'a str,
    stream_short: &'a str,
    now_ms: i64,
    segment_ms: u32,
    arena: Arena,
    leases: LeaseRegistry,
    counters: HandoffCounters,
    tracks: Vec<TrackState>,
    /// 每条轨道最多挂起一个“等下一个样本才能定时长”的样本：它不进 arena、不进统计，
    /// 差分成功才落地，差分失败则带原因计数丢弃。
    pending: [Option<DecodedSample>; 2],
    evidence: Vec<BufferDescriptor>,
    sampler: AdaptiveSampler,
    segmenter: Option<AudioSegmenter>,
    segment_report: media::AudioSegmentReport,
    /// 保留式交接的数据面。`None` 表示本次运行不保留字节（进程内自校验路径）。
    handoff: Option<BufferHandoff>,
}

impl<'a> DecodeSession<'a> {
    pub(crate) fn new(
        stream_id: &'a str,
        stream_short: &'a str,
        arena_id: &str,
        run: &DecodeRun,
    ) -> Result<Self, MediaError> {
        Ok(Self {
            stream_id,
            stream_short,
            now_ms: crate::now_unix_ms(),
            segment_ms: run.audio_segment_ms,
            arena: Arena::new(arena_id, DEFAULT_ARENA_CAPACITY_BYTES)?,
            leases: LeaseRegistry::default(),
            counters: HandoffCounters::default(),
            tracks: vec![
                TrackState::new(TrackKind::Video),
                TrackState::new(TrackKind::Audio),
            ],
            pending: [None, None],
            evidence: Vec::new(),
            sampler: AdaptiveSampler::new(run.sampling),
            handoff: match run.handoff.enabled {
                // 段名按运行随机派生：句柄（arena id，会出现在报告里）不可反推出段名，
                // 消费者只能从 lease 服务的应答里拿到它（见 ADR-010）。
                true => Some(BufferHandoff::new(
                    arena_id,
                    run.handoff.arena_capacity_bytes,
                    Some(&crate::shm::run_seed()),
                    run.handoff.retained_limit,
                )?),
                false => None,
            },
            segmenter: None,
            segment_report: media::AudioSegmentReport {
                segment_ms: run.audio_segment_ms,
                listed_limit: MAX_LISTED_SEGMENTS as u32,
                ..Default::default()
            },
        })
    }

    /// 当前该流的媒体时间终点（各轨道 `last_end_ms` 的最大值）；还没样本时为 `None`。
    /// 实时路径用它把墙钟断流换算成"媒体时间缺了多少"。
    pub(crate) fn media_end_ms(&self) -> Option<i64> {
        self.tracks
            .iter()
            .map(|track| track.last_end_ms)
            .filter(|end| *end >= 0)
            .max()
    }

    fn drop_sample(&mut self, index: usize, reason: &'static str) {
        self.tracks[index].dropped_samples += 1;
        self.tracks[index].drop_reasons.insert(reason);
    }

    /// 驱动入口。buffer 自带时长的样本立即落地；时长缺失的样本按轨道挂起一个，
    /// 由同一轨下一个样本的 PTS 差分补齐（真实测量值，不是估计）。
    /// 补不出来时显式丢弃并记原因，绝不静默。
    pub(crate) fn push(&mut self, sample: DecodedSample) -> Result<(), MediaError> {
        let index = track_index(sample.track);
        if let Some(pending) = self.pending[index].take() {
            self.resolve_pending(index, pending, sample.pts_ms)?;
        }
        // 时长未知但时间戳已知的样本要等下一个样本；连时间戳都没有的样本
        // 走 accept 的既有路径，在那里记 `pts_unavailable`。
        if sample
            .duration_ms
            .filter(|duration| *duration > 0)
            .is_none()
            && sample.pts_ms.filter(|pts| *pts >= 0).is_some()
        {
            self.pending[index] = Some(sample);
            return Ok(());
        }
        self.accept(sample)
    }

    /// 用下一个样本的 PTS 给挂起的样本补时长；补不出来就带原因丢弃。
    fn resolve_pending(
        &mut self,
        index: usize,
        mut pending: DecodedSample,
        next_pts_ms: Option<i64>,
    ) -> Result<(), MediaError> {
        let previous_pts_ms = pending.pts_ms.unwrap_or(-1);
        let Some(next_pts_ms) = next_pts_ms.filter(|pts| *pts >= 0) else {
            // 下一个样本没有时间戳，差分无从谈起。
            self.drop_sample(index, "duration_unavailable");
            return Ok(());
        };
        let delta_ms = next_pts_ms - previous_pts_ms;
        if delta_ms <= 0 {
            self.drop_sample(index, "duration_delta_nonpositive");
            return Ok(());
        }
        if delta_ms > MAX_DERIVED_DURATION_MS {
            // 间隔过大说明中间丢了样本或换了段，不能把空白算成一帧的长度。
            self.drop_sample(index, "duration_delta_out_of_range");
            return Ok(());
        }
        pending.duration_ms = Some(delta_ms);
        pending.duration_derived = true;
        self.accept(pending)
    }

    /// 把一个解码后的样本转换为已校验的 descriptor、一条轨道统计，
    /// 以及（音频场景下）一段 segment 贡献。字节不会离开 arena。
    pub(crate) fn accept(&mut self, sample: DecodedSample) -> Result<(), MediaError> {
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
        // 视频抽帧发生在这里，也就是在字节进入 arena 之前：被跳过的帧根本不交接，
        // 但一定会带原因计数，绝不静默消失。
        if sample.track == TrackKind::Video {
            let signature = if sample.pixel_format == VIDEO_PIXEL_FORMAT {
                FrameSignature::from_rgba(&sample.bytes, sample.width, sample.height)
            } else {
                None
            };
            if let Decision::Skip { reason, .. } = self.sampler.observe(pts_ms, signature) {
                self.drop_sample(index, reason.name());
                // last_end_ms 描述"这条轨道解码到哪里"，而不是"交接到哪里"：
                // 抽帧只影响交接，不影响这条轨道是否已经到达流的末尾。
                self.tracks[index].last_end_ms = end_ms;
                return Ok(());
            }
        }

        {
            let track = &mut self.tracks[index];
            // origin 是轨道的属性，而不是 buffer 的属性。流中途变化意味着
            // 开始了新的 segment，本 replay 路径不建模这种情况。
            match track.origin_ms {
                Some(seen) if seen != sample.origin_ms => {
                    return Err(MediaError::DecodeFailed(
                        "presentation_origin_changed_mid_stream".into(),
                    ));
                }
                Some(_) => {}
                None => track.origin_ms = Some(sample.origin_ms),
            }
            // 重复的 interval 会被保留——payload 是真实的解码音频——但会被计数。
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
            let kind = buffer_kind(sample.track);
            let time_range = TimeRange {
                start_ms: pts_ms,
                end_ms,
            };
            let format = buffer_format(sample.track, &sample);
            let descriptor = hand_off(
                &mut self.arena,
                &mut self.leases,
                BufferSpec {
                    buffer_id: buffer_id.clone(),
                    kind,
                    stream_id: self.stream_id,
                    time_range,
                    format: format.clone(),
                },
                &sample.bytes,
                self.now_ms,
                &mut self.counters,
            )?;
            // 真实跨进程交接：字节留在共享内存里等消费者领取 lease。容量类拒绝（保留表满、
            // 段满）是**有界行为**，已经计入 `BufferHandoff` 的统计；契约违规才让本次 decode 失败。
            if let Some(handoff) = self.handoff.as_mut() {
                handoff.retain_or_reject(
                    &buffer_id,
                    kind,
                    self.stream_id,
                    time_range,
                    format,
                    &sample.bytes,
                )?;
            }
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
            if sample.duration_derived {
                track.duration_derived_samples += 1;
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

    pub(crate) fn finish(
        mut self,
    ) -> Result<(media::DecodedDataPlane, Option<BufferHandoff>), MediaError> {
        // 流/窗口结束时仍挂起的样本补不出时长：显式计入丢弃，不让它静默消失。
        for index in 0..self.pending.len() {
            if self.pending[index].take().is_some() {
                self.drop_sample(index, "duration_unresolved_at_end");
            }
        }
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
        let sampling = self.sampling_report();
        let plane = media::DecodedDataPlane {
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
            sampling: vec![sampling],
        };
        // 保留式数据面在 decode 结束后仍然存活：字节必须留到消费者领取并释放。
        Ok((plane, self.handoff.take()))
    }

    /// 抽帧口径。被跳过的帧在这里有完整明细，同时也计入轨道的 `dropped_samples`：
    /// 两者描述同一批帧，不可相加。
    fn sampling_report(&self) -> media::SamplingReport {
        let counters = self.sampler.counters();
        let policy = self.sampler.policy();
        media::SamplingReport {
            track_kind: TrackKind::Video.name().to_string(),
            min_interval_ms: policy.min_interval_ms,
            static_hold_ms: policy.static_hold_ms,
            change_threshold: policy.change_threshold,
            observed: counters.observed,
            kept: counters.kept,
            kept_first_frame: counters.kept_first_frame,
            kept_content_change: counters.kept_content_change,
            kept_static_heartbeat: counters.kept_static_heartbeat,
            skipped_rate_limited: counters.skipped_rate_limited,
            skipped_no_change: counters.skipped_no_change,
            skipped_non_monotonic: counters.skipped_non_monotonic,
            skipped_missing_signature: counters.skipped_missing_signature,
            max_gap_ms: counters.max_gap_ms,
            max_keeps_bound: policy.max_keeps(self.sampler.observed_span_ms()),
            max_frame_interval_ms: counters.max_frame_interval_ms,
        }
    }
}

/// 一次 decode 运行的结果：报告、可选的保留式数据面，以及是否被预算截断。
pub struct DecodeOutcome {
    pub plane: media::DecodedDataPlane,
    /// `Some` 表示字节仍留在共享内存里；调用方**必须**在进程存活期间把它交给消费者，
    /// 并在退出前核对释放/过期计数。
    pub handoff: Option<BufferHandoff>,
    /// 样本预算耗尽导致运行被截断，调用方必须如实上报。
    pub truncated: bool,
}

/// 把本地文件解码为已校验的 descriptor。
///
/// 每个样本都会被拷贝到有界 arena 中，转换为只读、带 lease 的 descriptor，
/// 完成校验后释放；返回的 plane 携带计数器与少量证据集。
/// 打开 `run.handoff` 时，同一批字节还会被保留到共享内存，供第二个进程按 lease 领取。
pub fn decode_file(
    path: &Path,
    stream_id: &str,
    arena_id: &str,
    run: &DecodeRun,
) -> Result<DecodeOutcome, MediaError> {
    if run.max_samples == 0 {
        return Err(MediaError::DecodeFailed(
            "decode_sample_budget_is_zero".into(),
        ));
    }
    let decoder = GstDecoder::open_file(path, run.decode.clone())?;
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
            session.push(sample)?;
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
    let (plane, handoff) = session.finish()?;
    Ok(DecodeOutcome {
        plane,
        handoff,
        truncated,
    })
}

/// 一次实时 ingest 的结果。
pub struct LiveOutcome {
    pub plane: media::DecodedDataPlane,
    /// 与 `DecodeOutcome` 同义：`Some` 表示字节留在共享内存里等消费者领 lease。
    pub handoff: Option<BufferHandoff>,
    /// 窗口内的断流/恢复记录。没有样本的窗口不是一个成功的 ingest。
    pub stats: LiveStats,
    /// 样本预算耗尽导致窗口提前结束。
    pub truncated: bool,
}

/// 从 SRT 实时接入：在 `live.duration_ms` 的墙钟窗口内把样本送进与文件路径**同一条**
/// arena / descriptor / lease /（可选）交接路径，并测量断流与恢复。
///
/// 直播没有已知时长，因此本路径不产出 anchor 区间，也不写 `ReplayReport`：
/// 它产出 `LiveIngestReport`（`proto/media/v1/live.proto`）。
pub fn decode_live(
    uri: &str,
    stream_id: &str,
    arena_id: &str,
    run: &DecodeRun,
    live: &LiveConfig,
) -> Result<LiveOutcome, MediaError> {
    live.validate()?;
    if run.max_samples == 0 {
        return Err(MediaError::DecodeFailed(
            "decode_sample_budget_is_zero".into(),
        ));
    }
    let mut decode_config = run.decode.clone();
    decode_config.state_timeout_s = LIVE_STATE_TIMEOUT_S;
    let decoder = GstDecoder::open_live(uri, decode_config)?;
    if !decoder.has_track(TrackKind::Video) && !decoder.has_track(TrackKind::Audio) {
        return Err(MediaError::DecodeFailed("no_decodable_track".into()));
    }
    let stream_short = stream_id.strip_prefix("stream-").unwrap_or(stream_id);
    let mut session = DecodeSession::new(stream_id, stream_short, arena_id, run)?;
    let mut tracker = StallTracker::new(live);

    let started = Instant::now();
    let mut consumed = 0usize;
    let mut truncated = false;
    let mut ended_by_deadline = true;
    loop {
        if started.elapsed().as_millis() as u64 >= live.duration_ms {
            break;
        }
        let mut round_samples = 0u64;
        let mut first_pts: Option<i64> = None;
        for kind in [TrackKind::Video, TrackKind::Audio] {
            if !decoder.has_track(kind) {
                continue;
            }
            while let Some(sample) = decoder.pull(kind)? {
                if consumed >= run.max_samples {
                    truncated = true;
                    break;
                }
                consumed += 1;
                round_samples += 1;
                if first_pts.is_none() {
                    first_pts = sample.pts_ms;
                }
                session.push(sample)?;
            }
        }
        let now_ms = started.elapsed().as_millis() as u64;
        if round_samples > 0 {
            tracker.on_progress(now_ms, round_samples, first_pts, session.media_end_ms());
        } else {
            // 卡顿预算超限会显式失败，而不是无限等下去。
            tracker.on_idle(now_ms, session.media_end_ms())?;
        }
        if truncated {
            // 样本预算先到：窗口是被预算截断的，不是按 deadline 正常结束。
            ended_by_deadline = false;
            break;
        }
    }
    let elapsed_ms = started.elapsed().as_millis() as u64;
    let stats = tracker.finish(elapsed_ms, ended_by_deadline);
    let (plane, handoff) = session.finish()?;
    Ok(LiveOutcome {
        plane,
        handoff,
        stats,
        truncated,
    })
}

#[cfg(all(test, feature = "gstreamer"))]
mod tests {
    use super::*;

    fn run() -> DecodeRun {
        DecodeRun {
            decode: DecodeConfig::default(),
            max_samples: 16,
            audio_segment_ms: 5_000,
            sampling: SamplingPolicy::default(),
            handoff: RetainPolicy::default(),
        }
    }

    fn session() -> DecodeSession<'static> {
        DecodeSession::new("stream-live-test", "live-test", "arena-live-test", &run())
            .expect("session builds")
    }

    fn audio_sample(pts_ms: Option<i64>, duration_ms: Option<i64>) -> DecodedSample {
        DecodedSample {
            track: TrackKind::Audio,
            origin_ms: 0,
            pts_ms,
            duration_ms,
            duration_derived: false,
            width: 0,
            height: 0,
            pixel_format: String::new(),
            sample_rate: 48_000,
            channels: 2,
            audio_format: "F32LE".into(),
            bytes: vec![0u8; 64],
        }
    }

    fn video_sample(pts_ms: Option<i64>, duration_ms: Option<i64>) -> DecodedSample {
        DecodedSample {
            track: TrackKind::Video,
            origin_ms: 0,
            pts_ms,
            duration_ms,
            duration_derived: false,
            width: 16,
            height: 16,
            pixel_format: VIDEO_PIXEL_FORMAT.into(),
            sample_rate: 0,
            channels: 0,
            audio_format: String::new(),
            bytes: vec![7u8; 16 * 16 * 4],
        }
    }

    fn stat(plane: &media::DecodedDataPlane, kind: TrackKind) -> &media::DecodedTrackStat {
        plane
            .tracks
            .iter()
            .find(|track| track.track_kind == kind.name())
            .expect("track stat is always present")
    }

    /// 复现真实 OBS 输入：码流不写 VUI timing，h264parse 因此不给 buffer duration。
    /// 时长必须由同一轨下一个样本的 PTS 差分补上，且这个推导要显式计算。
    #[test]
    fn video_without_buffer_duration_is_timed_by_the_next_sample() {
        let mut session = session();
        session.push(video_sample(Some(0), None)).expect("push");
        session.push(video_sample(Some(33), None)).expect("push");
        let (plane, _) = session.finish().expect("finish");

        let video = stat(&plane, TrackKind::Video);
        assert_eq!(video.samples, 1, "首帧由 PTS 差分定时后被交接");
        assert_eq!(video.duration_derived_samples, 1);
        assert_eq!(video.dropped_samples, 1, "窗口末尾那一帧补不出时长");
        assert!(video
            .drop_reasons
            .contains(&"duration_unresolved_at_end".to_string()));
        assert_eq!(video.width, 16);
        assert_eq!(video.height, 16);
        let evidence = &plane.evidence_descriptors[0];
        let range = evidence.time_range.as_ref().expect("range");
        assert_eq!((range.start_ms, range.end_ms), (0, 33));
    }

    /// 时长由 buffer 自带时不许计入推导数，否则报告会把容器声明的时长和推导值混为一谈。
    #[test]
    fn declared_duration_is_never_counted_as_derived() {
        let mut session = session();
        session.push(audio_sample(Some(0), Some(21))).expect("push");
        let (plane, _) = session.finish().expect("finish");

        let audio = stat(&plane, TrackKind::Audio);
        assert_eq!(audio.samples, 1);
        assert_eq!(audio.duration_derived_samples, 0);
        assert_eq!(audio.dropped_samples, 0);
        assert!(audio.drop_reasons.is_empty());
    }

    /// 间隔过大说明中间丢了样本或换了段：不推导，显式丢弃并记原因。
    #[test]
    fn oversized_gap_is_dropped_instead_of_becoming_a_frame_duration() {
        let mut session = session();
        session.push(audio_sample(Some(0), None)).expect("push");
        session
            .push(audio_sample(Some(MAX_DERIVED_DURATION_MS + 1), Some(21)))
            .expect("push");
        let (plane, _) = session.finish().expect("finish");

        let audio = stat(&plane, TrackKind::Audio);
        assert_eq!(audio.samples, 1, "带自带时长的那一个照常落地");
        assert_eq!(audio.duration_derived_samples, 0);
        assert_eq!(audio.dropped_samples, 1);
        assert!(audio
            .drop_reasons
            .contains(&"duration_delta_out_of_range".to_string()));
    }

    /// 非正间隔不是时长，同样按不可用处理。
    #[test]
    fn nonpositive_gap_is_dropped_with_a_reason() {
        let mut session = session();
        session.push(audio_sample(Some(100), None)).expect("push");
        session
            .push(audio_sample(Some(100), Some(21)))
            .expect("push");
        let (plane, _) = session.finish().expect("finish");

        let audio = stat(&plane, TrackKind::Audio);
        assert_eq!(audio.dropped_samples, 1);
        assert!(audio
            .drop_reasons
            .contains(&"duration_delta_nonpositive".to_string()));
    }

    /// 没有下一个样本就没有差分：窗口结束时仍挂起的样本必须显式计数。
    #[test]
    fn pending_sample_at_window_end_is_reported_not_silently_lost() {
        let mut session = session();
        session.push(audio_sample(Some(0), None)).expect("push");
        let (plane, _) = session.finish().expect("finish");

        let audio = stat(&plane, TrackKind::Audio);
        assert_eq!(audio.samples, 0);
        assert_eq!(audio.dropped_samples, 1);
        assert!(audio
            .drop_reasons
            .contains(&"duration_unresolved_at_end".to_string()));
    }

    /// 下一个样本连时间戳都没有时差分无从谈起：挂起的样本按不可用丢弃。
    #[test]
    fn pending_sample_is_dropped_when_the_next_one_has_no_timestamp() {
        let mut session = session();
        session.push(audio_sample(Some(0), None)).expect("push");
        session.push(audio_sample(None, Some(21))).expect("push");
        let (plane, _) = session.finish().expect("finish");

        let audio = stat(&plane, TrackKind::Audio);
        assert_eq!(audio.samples, 0);
        assert_eq!(audio.dropped_samples, 2);
        assert!(audio
            .drop_reasons
            .contains(&"duration_unavailable".to_string()));
        assert!(audio.drop_reasons.contains(&"pts_unavailable".to_string()));
    }
}
