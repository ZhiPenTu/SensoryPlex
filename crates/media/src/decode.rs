//! 本地文件解码，基于 GStreamer（ADR-003 golden path）。
//!
//! 仅在 `gstreamer` feature 开启时编译，便于没有 GStreamer 开发文件的平台
//! 仍能构建并测试 crate 的其他部分。两个 sink 都使用有界队列，
//! 消费端停止排空时会让 pipeline 减速，而不是无界地缓冲。

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use gstreamer as gst;
use gstreamer::prelude::*;
use gstreamer_app::AppSink;
use sensoryplex_sdk::common::{
    BufferDescriptor, BufferFormat, ColorPrimaries, MatrixCoefficients, TimeRange,
    TransferCharacteristics,
};
use sensoryplex_sdk::media;

use crate::arena::{Arena, DEFAULT_ARENA_CAPACITY_BYTES};
use crate::backpressure::{BackpressurePolicy, BackpressureTracker, QueueWatermarks};
use crate::capability::{self, DecodedFormat, Rejection, SourceFormatContext, TrackClass, Verdict};
use crate::descriptor::{hand_off, BufferSpec, HandoffCounters};
use crate::evidence::{EvidencePolicy, EvidenceSelector, EvidenceStep, LedgerHandle};
use crate::handoff::{BufferHandoff, SharedHandoff};
use crate::lease::LeaseRegistry;
use crate::live::{LiveConfig, LiveStats, StallTracker};
use crate::sampler::{AdaptiveSampler, Decision, FrameSignature, SamplingPolicy, SkipReason};
use crate::segment::{AudioSegmenter, PendingSegment, AUDIO_SAMPLE_FORMAT};
use crate::MediaError;

/// typefind 与 decodebin 之间必须隔一条 queue：实测（`macos-aarch64`，GStreamer 1.28.7）
/// 直接 `typefind ! decodebin` 时 `have-type` **从不触发**，demuxer 的 sink pad 也拿不到
/// negotiated caps（`current_caps = None`），容器结论于是永远缺失；
/// 中间加一条 queue 后两条证据路径都恢复（`have-type` 概率 100，
/// `qtdemux.sink.current_caps = video/quicktime`）。这是本机实测的接线要求，不是调参。
const CONTAINER_QUEUE_MAX_BUFFERS: u32 = 100;
const CONTAINER_QUEUE_MAX_BYTES: u32 = 4 * 1024 * 1024;
/// 类型探测只需要容器头部，缓冲超过一秒对判定没有帮助，因此这条队列也按时间设上限。
const CONTAINER_QUEUE_MAX_NS: u64 = 1_000_000_000;

pub const DEFAULT_SINK_MAX_BUFFERS: u32 = 8;
pub const DEFAULT_PULL_TIMEOUT_MS: u64 = 5;
pub const DEFAULT_STATE_TIMEOUT_S: u64 = 15;
/// 直播不做 preroll：pad 是异步出现的，数据晚到不影响墙钟窗口循环，
/// 因此不等文件路径那种 15 秒状态变更超时——无源时更快暴露，而不是白等。
pub const LIVE_STATE_TIMEOUT_S: u64 = 3;
/// 连续多次 pull 都没有进展即视为卡住（stalled），而不是慢。
pub const MAX_IDLE_ROUNDS: u32 = 2_000;
/// 文件路径上等待第一条源轨道出现的上限。数据就在本地，等不到就是结论；
/// 上限固定，因此这段等待不会变成无限循环。
pub const FIRST_TRACK_WAIT_MS: u64 = 2_000;

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
    /// 相邻音频段需要保留的真实重叠音频，必须小于 `audio_segment_ms`。
    pub audio_overlap_ms: u32,
    /// 视频抽帧策略。抽帧始终启用：未抽帧的运行只能在报告里显式说明，
    /// 不能用"全保留"冒充。
    pub sampling: SamplingPolicy,
    /// 保留式交接：打开后字节留在共享内存里等第二个进程领取 lease。
    ///
    /// 这里放的是**已经打开**的共享数据面，而不是一条策略：服务端必须在解码开始之前就
    /// 拿到同一份句柄，才能与解码并发地排空保留表（见 `RetainPolicy::open`）。
    pub handoff: Option<Arc<SharedHandoff>>,
    /// 描述符之后那条有界队列的背压策略。阈值与降速倍数都在这里确定，
    /// 解码会话只负责按它作出决策并计数。
    pub backpressure: BackpressurePolicy,
    /// 语义覆盖策略。`None` 表示这次运行不做全帧判别——旧行为保持不变，报告里也不会
    /// 出现 `semantic_coverage`，因此"没有语义账本"永远不会被读成"每帧都没变"。
    pub evidence: Option<EvidencePolicy>,
    /// 逐帧账本的落盘句柄。`None` 表示只保留报告里的计数与有界预览。
    pub ledger: Option<LedgerHandle>,
    /// 逐帧账本 artifact 的路径。它只用于报告里的 `frame_ledger_path`：有账本却没写路径
    /// 会让"账本在哪"无从查起，两者必须一起设置。
    pub ledger_path: Option<String>,
}

/// 预上下文帧的字节保留环条目。帧的字节已经从解码样本里移进来，不再额外拷贝。
#[derive(Debug)]
struct PreFrame {
    frame_index: u64,
    start_ms: i64,
    end_ms: i64,
    format: BufferFormat,
    bytes: Vec<u8>,
}

/// 预上下文保留环的字节上限。超过就按最老的逐出，并显式计数，绝不无界增长。
pub const MAX_PRE_RING_BYTES: usize = 64 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrackKind {
    Video,
    Audio,
}

impl TrackKind {
    fn class(self) -> TrackClass {
        match self {
            Self::Video => TrackClass::Video,
            Self::Audio => TrackClass::Audio,
        }
    }

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
    /// 该轨道在**解码前**采集到的源格式证据与实际解码器元素（ADR-009 §4）。
    /// 采集不到时保持默认值（空串 / `None`），绝不猜一个源编码或位深出来。
    pub source: TrackSourceInfo,
    /// 源 caps 声明的采样宽高比；未声明为 `None`，不填 1/1 冒充。
    pub pixel_aspect_ratio: Option<(u32, u32)>,
    pub bytes: Vec<u8>,
}

/// 一条轨道的源侧证据：实际解码它的 GStreamer 元素，以及**解码前**采集到的格式上下文。
///
/// 证据必须来自解码前：解码后的 raw caps 会把 10-bit Main10 描述成 8-bit NV12（ADR-009 实测 A），
/// 因此这里的字段只在取不到时留空，绝不从载荷反推。
#[derive(Debug, Clone, Default)]
pub struct TrackSourceInfo {
    /// 实际选中并解码这条轨道的元素（`vtdec_hw`、`avdec_aac`……）。元素随主机与插件集变化，
    /// 因此"这个格式在本机可用"这句话离开它就无法复核（ADR-009 §5）。
    /// 空串表示本次运行没能归因到解码器，而不是"用了内置解码器"。
    /// `capability::NO_DECODER_ELEMENT` 是另一个终态：源采样由 demuxer 直接给出（例如
    /// MOV 里的 PCM），本来就没有解码元素可归因——它与"没能归因"不是同一件事。
    pub decoder_element: String,
    pub context: SourceFormatContext,
    /// 归一化阶段**实际应用过**的旋转角度。v1 未实现旋转，因此恒为 `None`（未知）：
    /// 填 0 会被下游读成"已确认画面竖直"。
    pub applied_rotation_deg: Option<u32>,
}

struct Chain {
    kind: TrackKind,
    queue: gst::Element,
    sink: AppSink,
    origin: Origin,
    /// 这条链最终链接到的源轨道标识（STREAM_START 的 stream ID）。
    /// 只有第一条轨道能拿到它：第二条同 kind 轨道会被显式拒绝，不会覆盖它。
    stream: Arc<Mutex<Option<String>>>,
    /// 源里是否出现过这种轨道（decodebin 交付过一条 pad 并接上）。
    /// 它回答的是"源里有这条轨道吗"，与"这条链的分支存在吗"是两件事：
    /// 后者一直为真，用它判断会让"源里只有音频"的源永远等一条不会来的视频轨。
    linked: Arc<AtomicBool>,
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
    stream: Arc<Mutex<Option<String>>>,
    linked: Arc<AtomicBool>,
}

pub struct GstDecoder {
    pipeline: gst::Pipeline,
    video: Option<TrackHandle>,
    audio: Option<TrackHandle>,
    config: DecodeConfig,
    /// 源级准入状态：容器结论、按 stream id 关联的源格式上下文、显式拒绝清单。
    hive: Arc<Mutex<SourceHive>>,
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
    ///
    /// 准入判定（ADR-009）挂在这一段装配上，而不是散在各个调用点：
    /// 1. 源后先接一个 `typefind`，把**容器类型**变成可读取的证据；
    /// 2. `deep-element-added` 给 demuxer/parser/decoder 的 sink pad 挂 caps 探针，
    ///    采集"解码前"的编码、profile、位深、色彩，并按 stream ID 关联到轨道；
    /// 3. 每条轨道链的入口（queue 的 sink pad）上做准入判定：判定为拒绝的样本
    ///    **不进**归一化链，也不进 arena。
    fn assemble(source: gst::Element, config: DecodeConfig) -> Result<Self, MediaError> {
        ensure_gst()?;
        let pipeline = gst::Pipeline::new();
        // decodebin3 在内部完成类型查找，既不发容器 CAPS 事件，也不在 demuxer 的 sink pad 上留下
        // caps（实测），所以容器类型必须由我们自己跑的 typefind 给出。
        let typefind = gst::ElementFactory::make("typefind")
            .name("typefind")
            .build()
            .map_err(decoder_error)?;
        let container_queue = gst::ElementFactory::make("queue")
            .name("container-queue")
            .property("max-size-buffers", CONTAINER_QUEUE_MAX_BUFFERS)
            .property("max-size-bytes", CONTAINER_QUEUE_MAX_BYTES)
            .property("max-size-time", CONTAINER_QUEUE_MAX_NS)
            .build()
            .map_err(decoder_error)?;
        let decode = gst::ElementFactory::make("decodebin")
            .name("decode")
            .build()
            .map_err(decoder_error)?;
        pipeline.add(&source).map_err(decoder_error)?;
        pipeline.add(&typefind).map_err(decoder_error)?;
        pipeline.add(&container_queue).map_err(decoder_error)?;
        pipeline.add(&decode).map_err(decoder_error)?;
        gst::Element::link(&source, &typefind).map_err(decoder_error)?;
        // 直连 `typefind ! decodebin` 会让 `have-type` 永不触发（见 CONTAINER_QUEUE_* 注释）。
        gst::Element::link(&typefind, &container_queue).map_err(decoder_error)?;
        gst::Element::link(&container_queue, &decode).map_err(decoder_error)?;

        let hive = Arc::new(Mutex::new(SourceHive::default()));
        {
            let captured = Arc::clone(&hive);
            // `have-type` 是 typefind 的结论本身；没触发就保持未知，交给 classify_container 拒绝。
            typefind.connect("have-type", false, move |values| {
                let probability: u32 = values[1].get().unwrap_or(0);
                let caps: Option<gst::Caps> = values[2].get().ok().flatten();
                if let Ok(mut hive) = captured.lock() {
                    hive.set_container(caps.as_ref(), probability);
                }
                None
            });
        }
        {
            let captured = Arc::clone(&hive);
            // 元素一被加入就挂探针：parser/decoder/capsfilter 的 sink caps 是这条轨道的源格式，
            // qtdemux/tsdemux 等 demuxer 的 sink caps 是容器。
            let decode_bin = decode
                .clone()
                .downcast::<gst::Bin>()
                .map_err(|_| MediaError::DecodeFailed("decodebin_is_not_a_bin".into()))?;
            decode_bin.connect_deep_element_added(move |_, _, element| {
                install_caps_probes(element, Arc::clone(&captured));
            });
        }

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
                    .field("format", AUDIO_SAMPLE_FORMAT)
                    .build(),
                &config,
            )?,
        ];
        for chain in &chains {
            install_admission_probe(chain, Arc::clone(&hive));
        }
        let video = chains
            .iter()
            .find(|chain| chain.kind == TrackKind::Video)
            .map(|chain| TrackHandle {
                sink: chain.sink.clone(),
                origin: Arc::clone(&chain.origin),
                stream: Arc::clone(&chain.stream),
                linked: Arc::clone(&chain.linked),
            });
        let audio = chains
            .iter()
            .find(|chain| chain.kind == TrackKind::Audio)
            .map(|chain| TrackHandle {
                sink: chain.sink.clone(),
                origin: Arc::clone(&chain.origin),
                stream: Arc::clone(&chain.stream),
                linked: Arc::clone(&chain.linked),
            });

        let linkable = chains
            .iter()
            .map(|chain| {
                (
                    chain.kind,
                    chain.queue.clone(),
                    chain.sink.clone(),
                    Arc::clone(&chain.stream),
                    Arc::clone(&chain.linked),
                )
            })
            .collect::<Vec<_>>();
        let captured = Arc::clone(&hive);
        // 被拒 pad 要能拿到 pipeline 才能接上 fakesink；用弱引用避免
        // decodebin（pipeline 的子元素）反过来强引用 pipeline 形成环。
        let pipeline_weak = pipeline.downgrade();
        decode.connect_pad_added(move |_, pad| {
            let caps = pad.current_caps().unwrap_or_else(|| pad.query_caps(None));
            let caps_name = caps
                .structure(0)
                .map(|structure| structure.name().to_string())
                .unwrap_or_default();
            let Some(class) = capability::classify_pad(&caps_name) else {
                // 非音视频 pad（字幕、元数据……）：只写日志不是契约，拒绝码必须进报告。
                let rejection = capability::pad_media_rejection(&caps_name);
                let stream_id = pad.stream_id().map(|id| id.to_string());
                tracing::warn!(
                    caps = %caps_name,
                    code = %rejection.code,
                    "decoded pad refused: unsupported media type"
                );
                if let Ok(mut hive) = captured.lock() {
                    hive.record_rejection("other", &rejection, stream_id.as_deref());
                }
                match pipeline_weak.upgrade() {
                    Some(pipeline) if terminate_rejected_pad(&pipeline, pad) => {}
                    Some(_) => {
                        tracing::error!(caps = %caps_name, "failed to terminate refused pad");
                    }
                    None => {}
                }
                return;
            };
            let kind = match class {
                TrackClass::Video => TrackKind::Video,
                TrackClass::Audio => TrackKind::Audio,
            };
            let Some((_, queue, sink, stream, linked)) =
                linkable.iter().find(|(chain_kind, ..)| *chain_kind == kind)
            else {
                return;
            };
            let Some(sink_pad) = queue.static_pad("sink") else {
                return;
            };
            if sink_pad.is_linked() {
                // 同一 kind 的第二条轨道：v1 不支持多轨，按 ADR-009 §4 显式拒绝，
                // 而不是按 pad 出现顺序配对（那会把两条轨道的信息串起来）。
                let rejection = capability::track_layout_rejection(class);
                let stream_id = pad.stream_id().map(|id| id.to_string());
                tracing::warn!(
                    track = kind.name(),
                    code = %rejection.code,
                    "decoded pad refused: more than one source track of this kind"
                );
                if let Ok(mut hive) = captured.lock() {
                    hive.record_rejection(class.name(), &rejection, stream_id.as_deref());
                }
                match pipeline_weak.upgrade() {
                    Some(pipeline) if terminate_rejected_pad(&pipeline, pad) => {}
                    Some(_) => {
                        tracing::error!(track = kind.name(), "failed to terminate refused pad");
                    }
                    None => {}
                }
                return;
            }
            if let Ok(mut slot) = stream.lock() {
                *slot = pad.stream_id().map(|id| id.to_string());
            }
            if pad.link(&sink_pad).is_err() {
                tracing::error!(track = kind.name(), "failed to link decoded pad");
                return;
            }
            // 源里确实有这条轨道：即使随后被准入拒绝，"有没有"和"放不放行"也是两件事。
            linked.store(true, Ordering::Relaxed);
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
            hive,
        })
    }

    pub fn has_track(&self, kind: TrackKind) -> bool {
        self.handle(kind).is_some()
    }

    /// 源里是否真的出现了这种轨道（decodebin 交付过一条 pad 并接上）。
    ///
    /// 与 `has_track` 的区别：后者只说明链路上有这条分支，它一直存在。
    /// "源里只有音频"的文件若用 `has_track` 判断，会永远等一条不会出现的视频轨，
    /// 最后把"有结论的没有轨道"报成 `decode_stalled`。
    pub fn track_linked(&self, kind: TrackKind) -> bool {
        self.handle(kind)
            .is_some_and(|handle| handle.linked.load(Ordering::Relaxed))
    }

    pub fn is_eos(&self, kind: TrackKind) -> bool {
        self.handle(kind).is_some_and(|handle| handle.sink.is_eos())
    }

    /// 这条轨道是否通过了准入（ADR-009）。`false` 是**终态**：轨道被显式拒绝，
    /// 拒绝码已经进了 `rejected_tracks()`。
    ///
    /// pad 还没出现、或 pad 没有 stream 标识时返回 `true`：那两种状态是"还不知道"，
    /// 不是"已拒绝"，不能混为一谈。
    pub fn track_admitted(&self, kind: TrackKind) -> bool {
        let Some(handle) = self.handle(kind) else {
            return false;
        };
        let stream = handle.stream.lock().ok().and_then(|slot| slot.clone());
        let Some(stream) = stream else {
            return true;
        };
        self.hive
            .lock()
            .map(|hive| !hive.is_rejected_stream(&stream))
            .unwrap_or(false)
    }

    /// 本次运行中被显式拒绝的轨道。驱动必须把它们写进报告：只有被拒绝轨道的报告
    /// 读起来像"源里没有这条轨道"，那正是 ADR-009 禁止的沉默。
    pub fn rejected_tracks(&self) -> Vec<media::RejectedTrack> {
        self.hive
            .lock()
            .map(|hive| hive.rejected_tracks())
            .unwrap_or_default()
    }

    /// 一条轨道都没出现时的原因串：容器结论，加上已经记录在案的非音视频/轨道级拒绝。
    /// 只写 `no_decodable_track` 会把"容器被拒绝"读成"这个文件是空的"（ADR-009 实测 B）。
    fn no_track_reason(&self) -> String {
        let mut parts = Vec::new();
        if let Some(note) = self.container_note() {
            parts.push(note);
        }
        for track in self.rejected_tracks() {
            parts.push(format!(
                "{}:{} ({})",
                track.track_kind, track.code, track.detail
            ));
        }
        if parts.is_empty() {
            "no_decodable_track".to_string()
        } else {
            format!("no_decodable_track: {}", parts.join("; "))
        }
    }

    /// 容器层结论。只在"一条轨道都没有"这种失败里用来说明原因。
    fn container_note(&self) -> Option<String> {
        self.hive.lock().ok().and_then(|hive| hive.container_note())
    }

    /// 拉取一个样本，最长等待 `pull_timeout_ms`。`None` 表示"暂无可用样本"或流已结束；
    /// 调用方通过 `is_eos` 区分这两种情况。
    pub fn pull(&self, kind: TrackKind) -> Result<Option<DecodedSample>, MediaError> {
        let Some(handle) = self.handle(kind) else {
            return Ok(None);
        };
        // 被拒轨道即使还有 buffer 在路上，也不得进入数据平面。
        if !self.track_admitted(kind) {
            return Ok(None);
        }
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
        decode_sample(kind, &sample, origin, self.source_info(handle))
    }

    /// 这条轨道在本次运行里的源侧证据。stream ID 还没确定时返回默认值（全部未知），
    /// 而不是拿别的轨道去凑。
    fn source_info(&self, handle: &TrackHandle) -> TrackSourceInfo {
        let stream = handle.stream.lock().ok().and_then(|slot| slot.clone());
        let Some(stream) = stream else {
            return TrackSourceInfo::default();
        };
        self.hive
            .lock()
            .map(|hive| hive.source_info_for(&stream))
            .unwrap_or_default()
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
    // 硬件解码器（macOS 的 `vtdec_hw`）的 src 模板把 `video/x-raw(memory:GLMemory)`
    // 排在首位，同一进程里出现两个 VideoToolbox 解码实例时，实测它会**偶尔**按
    // GLMemory 协商输出，而 `videoconvert` 不接受 GLMemory——整条 pipeline 于是以
    // `pipeline_state_change_failed` / `decode_stalled` 收场。这不是格式问题，
    // 是"源里有第二条轨道"被读成运行失败。
    //
    // `gldownload` 的 sink 同时接受 GLMemory 与系统内存，src 输出系统内存：
    // 解码器给哪种内存，归一化链都能接住（GLMemory 走一次 GPU→CPU 下载，像素内容不变）。
    // 元素缺失（无 GL 插件的宿主）时退回原有链路：GLMemory 只有 GL 解码器才可能给出，
    // 那种宿主本来也不会走到这条分支。
    let memory_download = if kind == TrackKind::Video {
        gst::ElementFactory::find("gldownload").map(|_| {
            gst::ElementFactory::make("gldownload")
                .name(format!("{}-gldownload", kind.name()))
                .build()
        })
    } else {
        None
    };
    let memory_download = match memory_download {
        None => None,
        Some(Ok(element)) => Some(element),
        Some(Err(error)) => return Err(decoder_error(error)),
    };
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
    if let Some(element) = &memory_download {
        pipeline.add(element).map_err(decoder_error)?;
    }
    pipeline.add(&converter).map_err(decoder_error)?;
    pipeline.add(&filter).map_err(decoder_error)?;
    pipeline
        .add(sink.upcast_ref::<gst::Element>())
        .map_err(decoder_error)?;
    let head = memory_download.as_ref().unwrap_or(&queue);
    gst::Element::link(head, &converter).map_err(decoder_error)?;
    if let Some(element) = &memory_download {
        gst::Element::link(&queue, element).map_err(decoder_error)?;
    }
    gst::Element::link(&converter, &filter).map_err(decoder_error)?;
    gst::Element::link(&filter, sink.upcast_ref::<gst::Element>()).map_err(decoder_error)?;
    Ok(Chain {
        kind,
        queue,
        sink,
        origin,
        stream: Arc::new(Mutex::new(None)),
        linked: Arc::new(AtomicBool::new(false)),
    })
}

/// 一条被显式拒绝的轨道。字段与 `media::RejectedTrack` 一一对应，但采集阶段不依赖 proto。
#[derive(Debug, Clone, PartialEq, Eq)]
struct RejectionRecord {
    track_kind: String,
    code: String,
    detail: String,
    container: String,
    decoder_element: String,
}

/// 源侧准入状态的唯一归属：容器结论、按 stream ID 关联的源格式上下文、实际解码器元素，
/// 以及显式拒绝清单。
///
/// 关联只认 GStreamer 的 stream ID：按 pad 出现顺序、或仅按 video/audio 类型配对，
/// 会把两条轨道的信息串起来（ADR-009 §4.3），所以第二种关联方式在这里根本不存在。
#[derive(Debug, Default)]
struct SourceHive {
    container: Option<String>,
    container_probability: u32,
    contexts: BTreeMap<String, SourceFormatContext>,
    decoders: BTreeMap<String, String>,
    /// 源采样由 demuxer 直接给出的轨道（容器里存的就是 raw 采样，例如 MOV 里的 PCM）：
    /// 这类轨道没有 parser/decoder，`decoders` 里不会有它的条目，报告里必须写明这一点，
    /// 而不是留一个空串（空串的含义是"本次运行没能归因"，不是"本来就没有"）。
    container_raw: BTreeSet<String>,
    rejected_streams: BTreeSet<String>,
    rejections: Vec<RejectionRecord>,
}

impl SourceHive {
    /// typefind 的结论。概率一起留着：0 概率的猜测不该被读成"已确认的容器"。
    fn set_container(&mut self, caps: Option<&gst::Caps>, probability: u32) {
        let Some(name) = caps
            .and_then(|caps| caps.structure(0))
            .map(|structure| structure.name().to_string())
        else {
            return;
        };
        self.container = Some(name);
        self.container_probability = probability;
    }

    /// 容器层的结论。容器本身不在承诺矩阵里时，这就是"为什么一条轨道都没有"的答案；
    /// 容器已被准入时返回 `None`，因为那时的原因确实是"源里没有可解码轨道"。
    fn container_note(&self) -> Option<String> {
        capability::classify_container(self.container.as_deref())
            .rejection()
            .map(|rejection| {
                format!(
                    "{} ({}; typefind_probability={})",
                    rejection.code, rejection.detail, self.container_probability
                )
            })
    }

    /// 一条轨道的源格式上下文。容器是流级信息，不属于任何一条轨道的 caps，
    /// 因此在这里补进每条轨道的上下文，报告里才能按轨道读到它。
    fn context_for(&self, stream_id: Option<&str>) -> SourceFormatContext {
        let mut context = stream_id
            .and_then(|stream_id| self.contexts.get(stream_id))
            .cloned()
            .unwrap_or_default();
        context.container = self.container.clone();
        context
    }

    fn source_info_for(&self, stream_id: &str) -> TrackSourceInfo {
        let decoder_element = match self.decoders.get(stream_id) {
            Some(element) => element.clone(),
            // 源采样由 demuxer 直出：没有解码元素可归因，这句话必须显式写出来（ADR-009 §5）。
            None if self.container_raw.contains(stream_id) => {
                capability::NO_DECODER_ELEMENT.to_string()
            }
            None => String::new(),
        };
        TrackSourceInfo {
            decoder_element,
            context: self.context_for(Some(stream_id)),
            applied_rotation_deg: None,
        }
    }

    /// 采集一份 sink caps。
    ///
    /// `demuxer` 为真表示这份 caps 是 demuxer 的**输入**，也就是容器 caps：它兜住
    /// typefind 没给出结论的情况（`decodebin3` 自己不发容器 CAPS，实测）。
    /// 其余元素（parser / decoder / capsfilter）的 sink caps 才是这条轨道的源格式。
    fn record_caps(
        &mut self,
        stream_id: Option<&str>,
        name: &str,
        structure: &gst::StructureRef,
        demuxer: bool,
        demux_src: bool,
        decoder: Option<&str>,
    ) {
        if let (Some(decoder), Some(stream_id)) = (decoder, stream_id) {
            self.decoders
                .entry(stream_id.to_string())
                .or_insert_with(|| decoder.to_string());
        }
        if demuxer {
            if self.container.is_none() {
                self.container = Some(name.to_string());
            }
            return;
        }
        let Some(stream_id) = stream_id else {
            // 没有 stream ID 就关联不到轨道：宁可不采，也不按出现顺序猜一个轨道。
            return;
        };
        let entry = self.contexts.entry(stream_id.to_string()).or_default();
        if decoded_payload_caps(name) {
            // 解码后的载荷 caps 不能改写已知的源编码：它描述的只是归一化前能观察到的载荷。
            // 唯一例外是这条流本来就没有 parser/decoder（容器里存的就是 raw 采样，例如 PCM），
            // 此时它是唯一的源信息，才允许补进空位。
            if entry.codec.is_none() {
                entry.codec = Some(name.to_string());
                if demux_src {
                    // 只有 demux 的输出能证明"容器里存的就是 raw 采样"：解析链上游没有
                    // 任何 parser/decoder 会给出这种 caps。
                    self.container_raw.insert(stream_id.to_string());
                }
            }
            return;
        }
        entry.adopt(&source_context_from_caps(name, structure));
    }

    /// 记录一条显式拒绝。同一 `(kind, code, detail)` 只出现一次：探针可能每帧判一次，
    /// 但报告里的清单表达的是"源里有这条轨道且被拒绝了"，不是拒绝次数。
    fn record_rejection(
        &mut self,
        track_kind: &str,
        rejection: &Rejection,
        stream_id: Option<&str>,
    ) {
        if let Some(stream_id) = stream_id {
            self.rejected_streams.insert(stream_id.to_string());
        }
        let record = RejectionRecord {
            track_kind: track_kind.to_string(),
            code: rejection.code.clone(),
            detail: rejection.detail.clone(),
            container: self.container.clone().unwrap_or_default(),
            decoder_element: stream_id
                .and_then(|stream_id| self.decoders.get(stream_id))
                .cloned()
                .unwrap_or_default(),
        };
        if !self.rejections.contains(&record) {
            self.rejections.push(record);
        }
    }

    fn is_rejected_stream(&self, stream_id: &str) -> bool {
        self.rejected_streams.contains(stream_id)
    }

    fn rejected_tracks(&self) -> Vec<media::RejectedTrack> {
        self.rejections
            .iter()
            .map(|record| media::RejectedTrack {
                track_kind: record.track_kind.clone(),
                code: record.code.clone(),
                detail: record.detail.clone(),
                container: record.container.clone(),
                decoder_element: record.decoder_element.clone(),
            })
            .collect()
    }
}

/// 解码后载荷的 caps 主名。它们描述的是载荷，不是源。
fn decoded_payload_caps(name: &str) -> bool {
    name == "video/x-raw" || name == "audio/x-raw"
}

/// caps → 源格式上下文。取不到的字段保持 `None`，不用默认值补齐。
fn source_context_from_caps(name: &str, structure: &gst::StructureRef) -> SourceFormatContext {
    SourceFormatContext {
        container: None,
        codec: Some(name.to_string()),
        profile: string_field(structure, "profile"),
        bit_depth: uint_field(structure, "bit-depth-luma"),
        chroma: string_field(structure, "chroma-format"),
        colorimetry: string_field(structure, "colorimetry"),
        mpegversion: uint_field(structure, "mpegversion").filter(|version| *version > 0),
        declared_frame_rate: structure
            .get::<gst::Fraction>("framerate")
            .ok()
            .filter(|ratio| ratio.numer() > 0 && ratio.denom() > 0)
            .map(|ratio| (ratio.numer() as u32, ratio.denom() as u32)),
    }
}

fn string_field(structure: &gst::StructureRef, field: &str) -> Option<String> {
    structure
        .get::<&str>(field)
        .ok()
        .map(|value| value.to_string())
}

/// caps 的整数字段：gst-plugins-base 对同一语义可能给 `(uint)` 或 `(int)`，
/// 只认一种会让位深悄悄变成"未知"。
fn uint_field(structure: &gst::StructureRef, field: &str) -> Option<u32> {
    structure
        .get::<u32>(field)
        .ok()
        .or_else(|| {
            structure
                .get::<i32>(field)
                .ok()
                .and_then(|value| u32::try_from(value).ok())
        })
        .filter(|value| *value > 0)
}

/// 把 pad 上**解码后**的 caps 读成准入判定需要的实测维度。
///
/// 这里读到的 `pixel_format` 是归一化前的载荷布局（例如 `NV12`）：它**不**参与源位深判定，
/// 只能用来测量声道数与几何。
fn decoded_format_from_pad(pad: &gst::Pad) -> DecodedFormat {
    let caps = pad.current_caps().or_else(|| {
        // `query_caps` 永远返回一个（可能是空的）Caps：空的就是"问不到"。
        let queried = pad.query_caps(None);
        (!queried.is_empty()).then_some(queried)
    });
    let Some(structure) = caps.as_ref().and_then(|caps| caps.structure(0)) else {
        return DecodedFormat::default();
    };
    let get = |field: &str| -> u32 {
        structure
            .get::<i32>(field)
            .ok()
            .filter(|value| *value > 0)
            .map(|value| value as u32)
            .unwrap_or(0)
    };
    DecodedFormat {
        pixel_format: string_field(structure, "format").unwrap_or_default(),
        width: get("width"),
        height: get("height"),
        sample_rate: get("rate"),
        channels: get("channels"),
    }
}

/// 给一个 deep element 的每个 sink pad 挂 caps 探针。
///
/// 只采 sink pad：demuxer 的 sink 是容器，parser / decoder / capsfilter 的 sink 是
/// "这条轨道送进该元素的 caps"。解码后的 raw caps 出现在 `pad-added`，不在这里，
/// 因此这里采到的一律是源侧证据。
fn install_caps_probes(element: &gst::Element, hive: Arc<Mutex<SourceHive>>) {
    let klass = element
        .factory()
        .map(|factory| factory.metadata("klass").unwrap_or_default().to_string())
        .unwrap_or_default();
    let demuxer = klass.contains("Demuxer");
    let decoder = klass
        .contains("Decoder")
        .then(|| element.name().to_string());
    let mut pads = element.iterate_sink_pads();
    while let Ok(Some(pad)) = pads.next() {
        let captured = Arc::clone(&hive);
        let decoder = decoder.clone();
        pad.add_probe(gst::PadProbeType::EVENT_DOWNSTREAM, move |pad, info| {
            if let Some(gst::PadProbeData::Event(event)) = &info.data {
                if let gst::EventView::Caps(caps) = event.view() {
                    if let Some(structure) = caps.caps().structure(0) {
                        let name = structure.name().to_string();
                        let stream_id = pad.stream_id().map(|id| id.to_string());
                        if let Ok(mut hive) = captured.lock() {
                            hive.record_caps(
                                stream_id.as_deref(),
                                &name,
                                structure,
                                demuxer,
                                false,
                                decoder.as_deref(),
                            );
                        }
                    }
                }
            }
            gst::PadProbeReturn::Ok
        });
    }
    if demuxer {
        // demux 输出是**第三条**证据来源（ADR-009 §4 的"demux 输出、parser 输出或 autoplug"）：
        // 容器里直接存原始采样的轨道（MOV 里的 PCM）没有任何 parser/decoder，不会产生 sink caps，
        // 只有 demux 的 src caps 会说出"源里存的是什么"。
        //
        // demuxer 的 src pad 是**解析时**才建出来的（qtdemux 的 pad 在 element-added 时并不存在），
        // 所以除了遍历现有 pad，还要在 `pad-added` 上补挂。
        let captured = Arc::clone(&hive);
        element.connect("pad-added", false, move |values| {
            let pad = values[1].get::<gst::Pad>().ok()?;
            install_demux_src_probe(&pad, Arc::clone(&captured));
            None
        });
        let mut pads = element.iterate_src_pads();
        while let Ok(Some(pad)) = pads.next() {
            install_demux_src_probe(&pad, Arc::clone(&hive));
        }
    }
}

/// demux 输出的 CAPS 探针：采到的就是"这条轨道存进容器时的样子"。
///
/// 拿不到 stream ID 就不采——宁可不采，也不按 pad 出现顺序把它配给某条轨道。
fn install_demux_src_probe(pad: &gst::Pad, hive: Arc<Mutex<SourceHive>>) {
    pad.add_probe(gst::PadProbeType::EVENT_DOWNSTREAM, move |pad, info| {
        if let Some(gst::PadProbeData::Event(event)) = &info.data {
            if let gst::EventView::Caps(caps) = event.view() {
                if let Some(structure) = caps.caps().structure(0) {
                    let name = structure.name().to_string();
                    let stream_id = pad.stream_id().map(|id| id.to_string());
                    if let Ok(mut hive) = hive.lock() {
                        hive.record_caps(stream_id.as_deref(), &name, structure, false, true, None);
                    }
                }
            }
        }
        gst::PadProbeReturn::Ok
    });
}

/// 在归一化链入口做准入判定：被判拒绝的样本不进归一化链，也不进 arena。
///
/// 判定必须在**第一个样本通过之前**发生，因此探针挂在链头 queue 的 sink pad 上：
/// 实测 6 声道样本只要放行 115 帧就会让队列反压，等到链路中段再判就已经晚了。
/// 拒绝对该链的影响是"这条轨道停摆"：后续 buffer 全部丢弃，驱动通过
/// `GstDecoder::track_admitted` 读到终态，而不是靠 idle 超时把拒绝误报成"卡住"。
fn install_admission_probe(chain: &Chain, hive: Arc<Mutex<SourceHive>>) {
    let Some(pad) = chain.queue.static_pad("sink") else {
        return;
    };
    let kind = chain.kind;
    let stream = Arc::clone(&chain.stream);
    let refused = Arc::new(AtomicBool::new(false));
    pad.add_probe(gst::PadProbeType::BUFFER, move |pad, _info| {
        // 已经拒过一次就继续丢：拒绝是这条轨道在本次运行里的终态。
        if refused.load(Ordering::Relaxed) {
            return gst::PadProbeReturn::Drop;
        }
        let class = kind.class();
        let stream_id = stream.lock().ok().and_then(|slot| slot.clone());
        let decoded = decoded_format_from_pad(pad);
        let verdict = match hive.lock() {
            Ok(hive) => {
                capability::classify_track(class, &hive.context_for(stream_id.as_deref()), &decoded)
            }
            // 拿不到采集状态就不放行：静默通过正是 ADR-009 要禁止的行为。
            Err(_) => return gst::PadProbeReturn::Drop,
        };
        match verdict {
            Verdict::Admitted => gst::PadProbeReturn::Ok,
            Verdict::Rejected(rejection) => {
                if !refused.swap(true, Ordering::Relaxed) {
                    // 先落盘拒绝记录，再让终态可见：驱动只会看到"已记录"的拒绝。
                    if let Ok(mut hive) = hive.lock() {
                        hive.record_rejection(class.name(), &rejection, stream_id.as_deref());
                    }
                    tracing::warn!(
                        track = class.name(),
                        code = %rejection.code,
                        detail = %rejection.detail,
                        "track refused by ADR-009 admission"
                    );
                }
                gst::PadProbeReturn::Drop
            }
        }
    });
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
    source: TrackSourceInfo,
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
    let pixel_aspect_ratio = structure
        .and_then(|structure| structure.get::<gst::Fraction>("pixel-aspect-ratio").ok())
        .filter(|ratio| ratio.numer() > 0 && ratio.denom() > 0)
        .map(|ratio| (ratio.numer() as u32, ratio.denom() as u32));
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
        source,
        pixel_aspect_ratio,
        bytes: mapped.as_slice().to_vec(),
    }))
}

fn decoder_error(error: gst::glib::BoolError) -> MediaError {
    MediaError::DecodeFailed(error.to_string())
}

/// 被显式拒绝的 pad 也必须有 peer。
///
/// `decodebin` 在交出 pad 之前就已经把这条轨道的解码分支建好：拒绝时只做"不链接"会让
/// 那条分支悬空，而实测（macos-aarch64 / GStreamer 1.28.7，两段视频轨的 MP4）表明悬空
/// 分支会把**准入通过的那条分支**一起拖垮。`vtdec_hw` 的 src 模板把
/// `video/x-raw(memory:GLMemory)` 排在首位，一旦它的输出按 GLMemory 协商，
/// `videoconvert` 就无法转换，整条 pipeline 以 `pipeline_state_change_failed` 或
/// `decode_stalled` 收场——这是"源里有第二条轨道"变成一次运行失败，而不是一次拒绝。
///
/// `fakesink` 只把这条分支接完：`sync=false` 不参与时钟，`async=false` 不参与 preroll，
/// 也不向 arena 交付任何样本。被拒轨道依旧不产生 descriptor，拒绝码照旧进报告。
fn terminate_rejected_pad(pipeline: &gst::Pipeline, pad: &gst::Pad) -> bool {
    // 不指定名字：同一进程里可能有多条被拒分支，重名会让 bin 拒绝加入这个 sink，
    // 分支又会退回悬空状态（实测：第二条被拒轨道会打印 "not unique in bin, not adding"）。
    let Ok(sink) = gst::ElementFactory::make("fakesink")
        .property("sync", false)
        .property("async", false)
        .build()
    else {
        return false;
    };
    if pipeline.add(&sink).is_err() {
        return false;
    }
    let Some(sink_pad) = sink.static_pad("sink") else {
        return false;
    };
    if pad.link(&sink_pad).is_err() {
        return false;
    }
    sink.sync_state_with_parent().is_ok()
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
    // descriptor 除了载荷布局，还要带上**源**位深与色彩信息：模型插件读到的就是这一份
    // `BufferDescriptor.format`，缺了它们，"10-bit 被降成 8-bit"在插件侧会再次不可见（ADR-009 §6）。
    let context = &sample.source.context;
    let colorimetry = context
        .colorimetry
        .as_deref()
        .map(capability::colorimetry_fields)
        .unwrap_or_default();
    let mut format = BufferFormat {
        // v1 不应用旋转，所以这里恒为 `None`（未确定），而不是 0。
        display_rotation_deg: sample.source.applied_rotation_deg,
        pixel_aspect_ratio_num: sample.pixel_aspect_ratio.map(|(num, _)| num),
        pixel_aspect_ratio_den: sample.pixel_aspect_ratio.map(|(_, den)| den),
        source_bit_depth: source_bit_depth_of(kind, context),
        color_primaries: primaries_field(colorimetry.primaries),
        transfer_characteristics: transfer_field(colorimetry.transfer),
        matrix_coefficients: matrix_field(colorimetry.matrix),
        ..Default::default()
    };
    match kind {
        TrackKind::Video => {
            format.pixel_format = sample.pixel_format.clone();
            format.width = sample.width;
            format.height = sample.height;
        }
        TrackKind::Audio => {
            format.sample_rate = sample.sample_rate;
            format.channels = sample.channels;
            // 样本布局也必须显式带出去：插件按字节解释音频，缺了它只能猜宽度。
            format.sample_format = sample.audio_format.clone();
        }
    }
    format
}

/// 报告与 descriptor 里的源位深：caps 字段优先，缺失时用 profile 的确定映射补齐，
/// 与准入判定同一口径。音频没有这个维度，保持未知。
fn source_bit_depth_of(kind: TrackKind, context: &SourceFormatContext) -> Option<u32> {
    if kind != TrackKind::Video {
        return None;
    }
    context.bit_depth.or_else(|| {
        capability::implied_bit_depth(
            context.codec.as_deref().unwrap_or_default(),
            context.profile.as_deref(),
        )
    })
}

/// 报告里的源采样格式：caps 字段优先，缺失时用编码规范唯一确定的映射补齐（VP8 恒为 4:2:0），
/// 与准入判定同一口径——准入用了推导，报告却不写，调用方就看不到这条轨道是凭什么被放行的。
/// 其它编码取不到就保持空（未知），绝不补一个默认的 4:2:0；音频没有这个维度。
fn source_chroma_format_of(kind: TrackKind, context: &SourceFormatContext) -> String {
    if kind != TrackKind::Video {
        return String::new();
    }
    context
        .chroma
        .clone()
        .or_else(|| {
            capability::implied_chroma_format(context.codec.as_deref().unwrap_or_default())
                .map(str::to_string)
        })
        .unwrap_or_default()
}

/// GstVideoColorPrimaries → proto。数值取自本机实测的枚举
/// （`VideoColorPrimaries.BT709=1`、`BT470BG=3`、`SMPTE170M=4`、`BT2020=7`）。
/// 只映射矩阵里出现过的取值，其余保持 UNSPECIFIED：把未知写成 BT.709 会掩盖 HDR 源。
fn primaries_field(value: Option<u32>) -> i32 {
    match value {
        Some(1) => ColorPrimaries::Bt709 as i32,
        // BT.601 在 caps 里既可能写成 BT470BG，也可能写成 SMPTE170M。
        Some(3) | Some(4) => ColorPrimaries::Bt601 as i32,
        Some(7) => ColorPrimaries::Bt2020 as i32,
        _ => ColorPrimaries::Unspecified as i32,
    }
}

/// GstVideoTransferFunction → proto（实测：BT709=5、SRGB=7、SMPTE2084=14、ARIB_STD_B67=15）。
fn transfer_field(value: Option<u32>) -> i32 {
    match value {
        Some(5) => TransferCharacteristics::Bt709 as i32,
        Some(7) => TransferCharacteristics::Srgb as i32,
        Some(14) => TransferCharacteristics::Smpte2084 as i32,
        Some(15) => TransferCharacteristics::AribStdB67 as i32,
        _ => TransferCharacteristics::Unspecified as i32,
    }
}

/// GstVideoColorMatrix → proto（实测：BT709=3、BT601=4、BT2020=6）。
fn matrix_field(value: Option<u32>) -> i32 {
    match value {
        Some(3) => MatrixCoefficients::Bt709 as i32,
        Some(4) => MatrixCoefficients::Bt601 as i32,
        Some(6) => MatrixCoefficients::Bt2020Ncl as i32,
        _ => MatrixCoefficients::Unspecified as i32,
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

/// 帧率模式判定：用 caps **声明**的帧率与整轨**实测**平均帧率比对（ADR-009 §7）。
///
/// 判定故意粗粒度——CFR 的毫秒取整本身带 ±1 ms 抖动，逐间隔比较会把 CFR 报成 VFR。
/// 声明缺失、样本不足、或声明中途改变，一律是 `UNKNOWN`：绝不把未知当 CFR。
#[derive(Debug, Clone, Default)]
struct FrameRateTracker {
    declared: Option<(u32, u32)>,
    first_pts_ms: Option<i64>,
    last_pts_ms: Option<i64>,
    samples: u64,
    /// caps 中途改写了声明值：源发生了变化，本轨帧率模式按未知处理。
    declared_conflict: bool,
}

/// 平均帧率与声明值相差超过 2% 即判 VARIABLE。
const FRAME_RATE_MODE_TOLERANCE: f64 = 0.02;
/// 少于 3 个样本时，差分算不出可复核的帧率。
const FRAME_RATE_MODE_MIN_SAMPLES: u64 = 3;

impl FrameRateTracker {
    fn observe(&mut self, pts_ms: i64, declared: Option<(u32, u32)>) {
        if let Some(declared) = declared {
            if self.declared.is_some_and(|seen| seen != declared) {
                self.declared_conflict = true;
            }
            self.declared = Some(declared);
        }
        if self.first_pts_ms.is_none() {
            self.first_pts_ms = Some(pts_ms);
        }
        self.last_pts_ms = Some(pts_ms);
        self.samples += 1;
    }

    /// 返回 (模式, 声明的帧率)。宣称过的帧率即使判不出模式也照实返回：
    /// 它是"源说过什么"，不是"实测到了什么"。
    fn mode(&self) -> (media::FrameRateMode, Option<(u32, u32)>) {
        let Some((num, den)) = self.declared else {
            return (media::FrameRateMode::Unknown, None);
        };
        let declared_fps = num as f64 / den as f64;
        let (Some(first), Some(last)) = (self.first_pts_ms, self.last_pts_ms) else {
            return (media::FrameRateMode::Unknown, self.declared);
        };
        let span_ms = last - first;
        if self.declared_conflict
            || self.samples < FRAME_RATE_MODE_MIN_SAMPLES
            || span_ms <= 0
            || declared_fps <= 0.0
        {
            return (media::FrameRateMode::Unknown, self.declared);
        }
        let measured_fps = (self.samples - 1) as f64 * 1_000.0 / span_ms as f64;
        if (measured_fps - declared_fps).abs()
            <= (declared_fps * FRAME_RATE_MODE_TOLERANCE).max(0.05)
        {
            (media::FrameRateMode::Constant, self.declared)
        } else {
            (media::FrameRateMode::Variable, self.declared)
        }
    }
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
    /// 首个被接受样本带来的源侧证据（解码器元素 + 解码前格式）。
    source: TrackSourceInfo,
    frame_rate: FrameRateTracker,
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
            source: TrackSourceInfo::default(),
            frame_rate: FrameRateTracker::default(),
        }
    }

    fn stat(&self) -> media::DecodedTrackStat {
        let context = &self.source.context;
        let (frame_rate_mode, declared_frame_rate) = self.frame_rate.mode();
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
            decoder_element: self.source.decoder_element.clone(),
            source_codec: context.codec.clone().unwrap_or_default(),
            source_bit_depth: source_bit_depth_of(self.kind, context),
            source_chroma_format: source_chroma_format_of(self.kind, context),
            colorimetry: context.colorimetry.clone().unwrap_or_default(),
            // v1 不应用旋转：写 `None`（未知），绝不写 0 冒充"已确认竖直"。
            applied_rotation_deg: self.source.applied_rotation_deg,
            frame_rate_mode: frame_rate_mode as i32,
            declared_frame_rate_num: declared_frame_rate.map(|(num, _)| num).unwrap_or(0),
            declared_frame_rate_den: declared_frame_rate.map(|(_, den)| den).unwrap_or(0),
        }
    }
}

pub(crate) struct DecodeSession<'a> {
    stream_id: &'a str,
    stream_short: &'a str,
    now_ms: i64,
    segment_ms: u32,
    audio_overlap_ms: u32,
    arena: Arena,
    leases: LeaseRegistry,
    counters: HandoffCounters,
    tracks: Vec<TrackState>,
    /// 每条轨道最多挂起一个“等下一个样本才能定时长”的样本：它不进 arena、不进统计，
    /// 差分成功才落地，差分失败则带原因计数丢弃。
    pending: [Option<DecodedSample>; 2],
    evidence: Vec<BufferDescriptor>,
    /// 源里有、但被准入拒绝的轨道。它们不产生 descriptor，也不产生 track stat：
    /// 这份清单是它们唯一的存在位置，因此是必填的。
    rejected_tracks: Vec<media::RejectedTrack>,
    sampler: AdaptiveSampler,
    /// 全帧判别与事件证据窗口选择器。`None` 表示这次运行不做语义覆盖判别。
    coverage: Option<EvidenceSelector>,
    /// 预上下文帧的字节保留环：条数由策略决定，总字节有硬上限。
    pre_frames: VecDeque<PreFrame>,
    pre_ring_bytes: usize,
    /// 因为语义证据计划而额外交接的帧数（自适应采样器本来会跳过它们）。
    evidence_added_keeps: u64,
    /// 选中但被有界保留表拒绝的帧数。
    evidence_retention_rejections: u64,
    ledger: Option<LedgerHandle>,
    ledger_path: Option<String>,
    /// 账本里已经写出的**逐帧**记录条数。窗口记录共享同一份 artifact，但它们的条数由
    /// `windows` 描述，因此这里只统计帧，报告里的 `frame_ledger_entries` 才能被逐行复核。
    ledger_entries: u64,
    segmenter: Option<AudioSegmenter>,
    segment_report: media::AudioSegmentReport,
    /// 保留式交接的数据面。`None` 表示本次运行不保留字节（进程内自校验路径）。
    ///
    /// 它由调用方在解码之前打开，并在解码期间与 gRPC 服务共享：消费者领料与生产者保留
    /// 是同一份表上的两个方向，谁也不能只在对方结束之后才开始。
    handoff: Option<Arc<SharedHandoff>>,
    /// 有界队列的压力等级与阶段一降级计数。它不做队列的权威记账：
    /// 容量/水位/峰值/丢弃都取自 `handoff` 的真实统计，见 `finish`。
    tracker: BackpressureTracker,
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
            audio_overlap_ms: run.audio_overlap_ms,
            arena: Arena::new(arena_id, DEFAULT_ARENA_CAPACITY_BYTES)?,
            leases: LeaseRegistry::default(),
            counters: HandoffCounters::default(),
            tracks: vec![
                TrackState::new(TrackKind::Video),
                TrackState::new(TrackKind::Audio),
            ],
            pending: [None, None],
            evidence: Vec::new(),
            rejected_tracks: Vec::new(),
            sampler: AdaptiveSampler::new(run.sampling),
            coverage: run.evidence.map(EvidenceSelector::new),
            pre_frames: VecDeque::new(),
            pre_ring_bytes: 0,
            evidence_added_keeps: 0,
            evidence_retention_rejections: 0,
            ledger: run.ledger.clone(),
            ledger_path: run.ledger_path.clone(),
            ledger_entries: 0,
            // 句柄由调用方在解码前打开（`RetainPolicy::open`）：段名按运行随机派生，
            // 句柄（arena id，会出现在报告里）不可反推出段名，消费者只能从 lease 服务
            // 的应答里拿到它（见 ADR-010）。这里只借用同一个 Arc，不重建数据面。
            handoff: run.handoff.clone(),
            tracker: BackpressureTracker::new(run.backpressure),
            segmenter: None,
            segment_report: media::AudioSegmentReport {
                segment_ms: run.audio_segment_ms,
                overlap_ms: run.audio_overlap_ms,
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

    /// 把源侧采集到的拒绝清单交给会话。必须在 `finish` 之前调用：被拒轨道与 track stat
    /// 是两回事，"报告里没有这条轨道"不能用来表达"这条轨道被拒绝了"。
    pub(crate) fn record_rejections(&mut self, rejected: Vec<media::RejectedTrack>) {
        self.rejected_tracks = rejected;
    }

    /// 把未交接的视频帧字节放进有限保留环，供后续事件窗口补"变化前"上下文。
    ///
    /// 环的条数由策略决定，总字节另有一个硬上限；两个上限都是逐出即计数，不静默丢失。
    /// 帧的字节是从解码样本里**移动**进来的，因此不留额外副本。
    fn hold_pre_context(
        &mut self,
        frame_index: Option<u64>,
        sample: DecodedSample,
        pts_ms: i64,
        end_ms: i64,
    ) {
        let Some(frame_index) = frame_index else {
            return;
        };
        let Some(selector) = self.coverage.as_ref() else {
            return;
        };
        let pre_frames = selector.policy().pre_frames;
        if pre_frames == 0 || sample.pixel_format != VIDEO_PIXEL_FORMAT {
            return;
        }
        let format = buffer_format(TrackKind::Video, &sample);
        let bytes = sample.bytes;
        if bytes.len() > MAX_PRE_RING_BYTES {
            if let Some(selector) = self.coverage.as_mut() {
                selector.note_pre_context_eviction();
            }
            return;
        }
        self.pre_ring_bytes += bytes.len();
        self.pre_frames.push_back(PreFrame {
            frame_index,
            start_ms: pts_ms,
            end_ms,
            format,
            bytes,
        });
        while self.pre_frames.len() > pre_frames as usize {
            self.evict_oldest_pre_frame(false);
        }
        while self.pre_ring_bytes > MAX_PRE_RING_BYTES {
            self.evict_oldest_pre_frame(true);
        }
    }

    fn evict_oldest_pre_frame(&mut self, count_as_eviction: bool) {
        let Some(evicted) = self.pre_frames.pop_front() else {
            return;
        };
        self.pre_ring_bytes -= evicted.bytes.len();
        if count_as_eviction {
            if let Some(selector) = self.coverage.as_mut() {
                selector.note_pre_context_eviction();
            }
        }
    }

    /// 把窗口要求的帧序号从保留环里取出并交接。环里已经找不到的帧是显式逐出，不是"没有这一帧"。
    fn retain_pre_context(&mut self, indexes: &[u64]) -> Result<(), MediaError> {
        if indexes.is_empty() {
            return Ok(());
        }
        let mut frames = Vec::with_capacity(indexes.len());
        for index in indexes {
            match self
                .pre_frames
                .iter()
                .position(|frame| frame.frame_index == *index)
            {
                Some(position) => {
                    if let Some(frame) = self.pre_frames.remove(position) {
                        self.pre_ring_bytes -= frame.bytes.len();
                        frames.push(frame);
                    }
                }
                None => {
                    if let Some(selector) = self.coverage.as_mut() {
                        selector.note_pre_context_eviction();
                    }
                }
            }
        }
        for frame in frames {
            self.place_pre_frame(frame)?;
        }
        Ok(())
    }

    /// 交接一个预上下文帧。它和其他视频帧走同一条 arena/descriptor/保留表路径。
    fn place_pre_frame(&mut self, frame: PreFrame) -> Result<(), MediaError> {
        let index = track_index(TrackKind::Video);
        let buffer_id = {
            let track = &mut self.tracks[index];
            track.next_index += 1;
            format!(
                "buf-{}-{}-{:08}",
                self.stream_short, VIDEO_FRAME_KIND, track.next_index
            )
        };
        let time_range = TimeRange {
            start_ms: frame.start_ms,
            end_ms: frame.end_ms,
        };
        hand_off(
            &mut self.arena,
            &mut self.leases,
            BufferSpec {
                buffer_id: buffer_id.clone(),
                kind: VIDEO_FRAME_KIND,
                stream_id: self.stream_id,
                time_range,
                format: frame.format.clone(),
            },
            &frame.bytes,
            self.now_ms,
            &mut self.counters,
        )?;
        let retained = match self.handoff.as_ref() {
            Some(handoff) => handoff
                .retain_or_reject(
                    &buffer_id,
                    VIDEO_FRAME_KIND,
                    self.stream_id,
                    time_range,
                    frame.format.clone(),
                    &frame.bytes,
                )?
                .is_some(),
            None => true,
        };
        {
            let track = &mut self.tracks[index];
            track.samples += 1;
            track.bytes += frame.bytes.len() as u64;
        }
        // 预上下文帧在它自己的时间点上被自适应采样器跳过了；现在是被语义证据计划追认交接的，
        // 所以它同样计入 `kept_evidence_window`，否则"kept + 额外保留 = 交接帧数"就不成立。
        self.evidence_added_keeps += 1;
        if let Some(selector) = self.coverage.as_mut() {
            if retained {
                selector.mark_retained(frame.frame_index, &buffer_id)?;
            } else {
                selector.mark_rejected(frame.frame_index, "data_plane_retention_rejected");
            }
        }
        if !retained {
            self.evidence_retention_rejections += 1;
        }
        Ok(())
    }

    /// 把已经按帧序号定型的判别记录写进逐帧账本。没有账本时只保留报告里的计数。
    fn flush_ledger(&mut self) -> Result<(), MediaError> {
        let Some(selector) = self.coverage.as_mut() else {
            return Ok(());
        };
        // 顺序是契约的一部分：帧按序号先写，只有当窗口里**最后一帧**也已经写出去之后，
        // 才轮到窗口记录。增量消费方因此永远看不到"引用着还不存在的行"的窗口。
        let records = selector.drain_ready();
        if let Some(ledger) = self.ledger.as_ref() {
            for record in &records {
                ledger.frame(record).map_err(MediaError::DecodeFailed)?;
                self.ledger_entries += 1;
            }
            let windows = selector.drain_emitted_windows(self.ledger_entries);
            for window in &windows {
                ledger.window(window).map_err(MediaError::DecodeFailed)?;
            }
        }
        Ok(())
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
        // 帧率模式（ADR-009 §7）用**全部**解码样本的时间戳判断，而不是抽帧之后剩下的那几帧：
        // 抽帧会人为放大间隔，拿它判 CFR/VFR 会把 CFR 误报成 VFR。
        if sample.track == TrackKind::Video {
            self.tracks[index]
                .frame_rate
                .observe(pts_ms, sample.source.context.declared_frame_rate);
        }
        // 背压观测点：在给这一帧做决定之前读一次下游队列的深度。只有在有界队列真的存在时
        // 才观测——没有队列就是没有测量，报告里必须写 `observed = false`，而不是写一组零。
        if let Some(handoff) = self.handoff.as_ref() {
            let watermarks = handoff.observe(watermarks)?;
            self.tracker.observe_queue(watermarks);
        }
        // 视频抽帧与全帧判别都发生在字节进入 arena 之前：被跳过的帧根本不交接，
        // 但一定会带原因计数，绝不静默消失。
        let mut coverage_step: Option<EvidenceStep> = None;
        if sample.track == TrackKind::Video {
            let rgba = sample.pixel_format == VIDEO_PIXEL_FORMAT;
            let signature = if rgba {
                FrameSignature::from_rgba(&sample.bytes, sample.width, sample.height)
            } else {
                None
            };
            let text_signature = if rgba {
                FrameSignature::edge_energy_from_rgba(&sample.bytes, sample.width, sample.height)
            } else {
                None
            };
            // 阶段一降级：队列降级/饱和时把最小间隔放大，先降低非关键帧的采样率。
            let throttle = self.tracker.throttle_requested().then(|| {
                self.tracker
                    .throttled_min_interval_ms(self.sampler.policy().min_interval_ms)
            });
            let decision = self
                .sampler
                .observe_with_pressure(pts_ms, signature, throttle);
            let sampler_keep = decision.kept();
            let mut sampler_skip = None;
            if let Decision::Skip { reason, .. } = decision {
                if reason == SkipReason::BackpressureThrottled {
                    self.tracker.record_throttled_keep();
                }
                sampler_skip = Some(reason);
            }
            // 全帧判别：每个可用帧先得到结论，再决定它是否进入数据面。
            let step = match self.coverage.as_mut() {
                Some(selector) => {
                    Some(selector.observe(pts_ms, end_ms, signature, text_signature)?)
                }
                None => None,
            };
            let evidence_keep = step.as_ref().is_some_and(|step| step.retain_now);
            if !sampler_keep && !evidence_keep {
                let reason = sampler_skip.unwrap_or(SkipReason::NoChangeYet);
                self.drop_sample(index, reason.name());
                // last_end_ms 描述"这条轨道解码到哪里"，而不是"交接到哪里"：
                // 抽帧只影响交接，不影响这条轨道是否已经到达流的末尾。
                self.tracks[index].last_end_ms = end_ms;
                // 未交接的帧要留在有限保留环里：后续事件窗口可以把最近几帧补成"变化前"上下文。
                self.hold_pre_context(
                    step.as_ref().map(|step| step.frame_index),
                    sample,
                    pts_ms,
                    end_ms,
                );
                self.flush_ledger()?;
                return Ok(());
            }
            if evidence_keep && !sampler_keep {
                // 事件帧不被固定间隔吃掉：这正是语义覆盖相对于固定采样的差别。
                self.evidence_added_keeps += 1;
            }
            if sampler_keep
                && step.as_ref().is_some_and(|step| {
                    step.selection == crate::evidence::FrameSelection::NotSelected
                })
            {
                // 自适应采样器单独保留了这一帧：账本必须把它标成基线锚点，
                // 否则会出现"账本说没选中、数据面里却有这个 buffer"的矛盾。
                if let (Some(selector), Some(step)) = (self.coverage.as_mut(), step.as_ref()) {
                    selector.mark_baseline_keep(step.frame_index, pts_ms);
                }
            }
            coverage_step = step;
        }

        // 这两个值在块内赋值、块外使用：语义账本要把"这一帧真的进了数据面"
        // 与"它只是拿到了 descriptor"分开记录，因此它们的生命周期必须覆盖整个校验块。
        // 两个值都在下面的校验块里被无条件赋值，因此这里不做无意义的初始化。
        let retained_in_plane: bool;
        let placed_buffer_id: String;
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
            retained_in_plane = match self.handoff.as_ref() {
                Some(handoff) => handoff
                    .retain_or_reject(
                        &buffer_id,
                        kind,
                        self.stream_id,
                        time_range,
                        format,
                        &sample.bytes,
                    )?
                    .is_some(),
                // 没有保留式数据面时不存在保留表，也就没有"被保留表拒绝"这回事。
                None => true,
            };
            placed_buffer_id = buffer_id.clone();
            if track.samples == 0 {
                // 源侧证据取首个被接受样本的那一份：同一轨道的后续样本携带同一上下文。
                track.source = sample.source.clone();
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

        if sample.track == TrackKind::Video && !retained_in_plane {
            self.evidence_retention_rejections += 1;
        }
        if let Some(step) = coverage_step {
            if let Some(selector) = self.coverage.as_mut() {
                if retained_in_plane {
                    selector.mark_retained(step.frame_index, &placed_buffer_id)?;
                } else {
                    selector.mark_rejected(step.frame_index, "data_plane_retention_rejected");
                }
            }
            // 预上下文帧在锚点之后交接：`track.samples == 0` 的源侧证据已经在锚点上采集，
            // 不会被保留环里的旧帧抢先。数据面的读取顺序仍然由半开时间区间决定。
            let pre_indexes = step.pre_context_indexes;
            if !pre_indexes.is_empty() {
                self.retain_pre_context(&pre_indexes)?;
            }
            self.flush_ledger()?;
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
            self.segmenter = Some(AudioSegmenter::with_overlap(
                self.segment_ms,
                self.audio_overlap_ms,
            )?);
        }
        let pending = match self.segmenter.as_mut() {
            Some(segmenter) => segmenter.push(
                sample.sample_rate,
                sample.channels,
                &sample.audio_format,
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
        let time_range = TimeRange {
            start_ms: segment.start_ms,
            end_ms: segment.end_ms,
        };
        let format = BufferFormat {
            sample_rate,
            channels,
            // 走到这里的样本布局已经在 segmenter 里被核实为 F32LE（其它布局被显式丢弃），
            // 所以这里写的就是实测值，不是"默认值"。
            sample_format: AUDIO_SAMPLE_FORMAT.to_string(),
            ..Default::default()
        };
        let descriptor = hand_off(
            &mut self.arena,
            &mut self.leases,
            BufferSpec {
                buffer_id: segment_id.clone(),
                kind: AUDIO_SEGMENT_KIND,
                stream_id: self.stream_id,
                time_range,
                format: format.clone(),
            },
            &segment.bytes,
            self.now_ms,
            &mut self.counters,
        )?;
        // 真实跨进程交接：段描述符必须和 `audio_pcm` 一样进保留表，否则插件端永远看不到
        // `audio_segment` 这类输入——进程内 arena 与 lease 只够本进程自证，不能替代数据面。
        // 容量类拒绝（保留表满、单类配额）是**有界行为**，已经计入 `BufferHandoff` 的统计；
        // 契约违规才让本次 decode 失败。
        if let Some(handoff) = self.handoff.as_ref() {
            handoff.retain_or_reject(
                &segment_id,
                AUDIO_SEGMENT_KIND,
                self.stream_id,
                time_range,
                format,
                &segment.bytes,
            )?;
        }
        if self.segment_report.listed.len() < MAX_LISTED_SEGMENTS {
            self.segment_report.listed.push(media::AudioSegment {
                segment_id: descriptor.buffer_id,
                time_range: descriptor.time_range,
                sample_rate,
                channels,
                bytes: segment.bytes.len() as u64,
                partial: segment.partial,
                sample_format: AUDIO_SAMPLE_FORMAT.to_string(),
            });
        }
        Ok(())
    }

    pub(crate) fn finish(
        mut self,
    ) -> Result<(media::DecodedDataPlane, Option<Arc<SharedHandoff>>), MediaError> {
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
        // 语义覆盖收尾必须在采样报告之前：`kept_evidence_window` 与账本条数描述同一批
        // 额外交接的帧，两者必须来自同一次收尾，不能各算一遍。
        let semantic_coverage = self.finalize_coverage()?;
        let sampling = self.sampling_report();
        // 收尾前再观测一次：`state` 应当描述运行**结束那一刻**的队列深度，
        // 而不是最后一个样本到来之前的那一刻。等级只在迁移时计数，多观测一次不会重复计数。
        if let Some(handoff) = self.handoff.as_ref() {
            let watermarks = handoff.observe(watermarks)?;
            self.tracker.observe_queue(watermarks);
        }
        let handoff_stats = match self.handoff.as_ref() {
            Some(handoff) => Some(handoff.stats()?),
            None => None,
        };
        let drop_kinds = match self.handoff.as_ref() {
            Some(handoff) => handoff.retain_rejections_by_kind()?,
            None => Default::default(),
        };
        let backpressure = self.tracker.report(handoff_stats.as_ref(), &drop_kinds);
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
            backpressure: Some(backpressure),
            rejected_tracks: std::mem::take(&mut self.rejected_tracks),
            semantic_coverage: semantic_coverage.into_iter().collect(),
        };
        // 保留式数据面在 decode 结束后仍然存活：字节必须留到消费者领取并释放。
        // 交出去的是同一个共享句柄（不是所有权）：服务端从这里继续服务，直到账面对上。
        Ok((plane, self.handoff.clone()))
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
            skipped_backpressure_throttled: counters.skipped_backpressure_throttled,
            max_gap_ms: counters.max_gap_ms,
            max_keeps_bound: policy.max_keeps(self.sampler.observed_span_ms()),
            max_frame_interval_ms: counters.max_frame_interval_ms,
            // 自适应采样器单独跳过了、但被语义证据计划额外交接的帧。没有语义计划时恒为 0，
            // 因此 "0" 只表示这次运行没有额外交接，不表示语义计划没有工作。
            kept_evidence_window: self.evidence_added_keeps,
        }
    }

    /// 收尾语义覆盖：把事件窗口写进逐帧账本，冲刷剩余逐帧记录，再用同一次收尾构造报告。
    ///
    /// 报告只带摘要与有界窗口预览；完整逐帧账本落盘，路径写进 `frame_ledger_path`。
    /// 返回 `None` 表示这次运行没有启用语义覆盖——那不是"每帧都没变"。
    fn finalize_coverage(&mut self) -> Result<Option<media::SemanticCoverageReport>, MediaError> {
        let Some(selector) = self.coverage.as_mut() else {
            return Ok(None);
        };
        let summary = selector.finish()?;
        let policy = selector.policy();
        let counters = summary.counters.clone();
        let selection_counts = counters.selection_counts();
        // `finish` 已把剩余候选帧定型，收尾后再刷一次，保证每一帧都恰好进账本一次；
        // 增量落盘在解码期间写过的窗口不会重复写（游标在 selector 里）。
        self.flush_ledger()?;
        // 流已结束：此刻不会再有新的帧记录，把剩下的窗口一次写完（含 `finish` 收尾的那一个）。
        let tail_windows = match self.coverage.as_mut() {
            Some(selector) => selector.drain_emitted_windows(u64::MAX),
            None => Vec::new(),
        };
        if let Some(ledger) = self.ledger.as_ref() {
            for window in &tail_windows {
                ledger.window(window).map_err(MediaError::DecodeFailed)?;
            }
        }
        // `EvidencePolicy` 里没有单独的 `context_frames` 字段：总上下文帧数就是前后之和，
        // 拆开仍保留 `pre`，避免把"变化前"的帧数读成总窗口宽度。
        let context_frames = policy.pre_frames.saturating_add(policy.post_frames);
        let listed_windows = summary
            .windows
            .iter()
            .take(crate::evidence::MAX_LISTED_WINDOWS)
            .map(|window| media::EvidenceWindow {
                window_id: window.window_id.clone(),
                time_range: Some(TimeRange {
                    start_ms: window.start_ms,
                    end_ms: window.end_ms,
                }),
                anchor_ms: window.anchor_ms,
                trigger: window.trigger.to_string(),
                frame_buffer_ids: window.frame_buffer_ids.clone(),
                context_before: window.context_before,
                context_after: window.context_after,
                handed_off_frames: window.handed_off_frames,
            })
            .collect();
        Ok(Some(media::SemanticCoverageReport {
            track_kind: TrackKind::Video.name().to_string(),
            max_semantic_gap_ms: policy.max_semantic_gap_ms,
            evidence_context_frames: context_frames,
            evidence_pre_frames: policy.pre_frames,
            change_threshold: policy.change_threshold,
            text_change_threshold: policy.text_change_threshold,
            characterized_frames: counters.characterized,
            selected_baseline_anchor: counters.baseline_anchor,
            selected_event_anchor: counters.event_anchor,
            selected_event_pre_context: counters.event_pre_context,
            selected_event_post_context: counters.event_post_context,
            covered_without_model_refresh: counters.not_selected,
            windows: counters.windows,
            windows_listed_limit: summary.listed_limit,
            listed_windows,
            max_selected_gap_ms: counters.max_selected_gap_ms,
            suppressed_event_keeps: counters.suppressed_event_keeps,
            pre_context_evictions: counters.pre_context_evictions,
            frame_ledger_path: self.ledger_path.clone().unwrap_or_default(),
            frame_ledger_entries: self.ledger_entries,
            decision_counts: summary.decision_counts.clone(),
            selection_counts,
            skip_reason_counts: summary.skip_reason_counts.clone(),
        }))
    }
}

/// 读一次保留队列的水位。它不做任何分配，因此可以在每个样本之前调用。
fn watermarks(handoff: &BufferHandoff) -> QueueWatermarks {
    QueueWatermarks {
        retained: handoff.depth(),
        retained_limit: handoff.limit(),
        retained_kind: handoff.deepest_kind(),
        arena_used_bytes: handoff.arena().used_bytes() as u64,
        arena_capacity_bytes: handoff.arena_capacity_bytes(),
    }
}

/// 一次 decode 运行的结果：报告、可选的保留式数据面，以及是否被预算截断。
pub struct DecodeOutcome {
    pub plane: media::DecodedDataPlane,
    /// `Some` 表示字节仍留在共享内存里；调用方**必须**在进程存活期间把它交给消费者，
    /// 并在退出前核对释放/过期计数。
    pub handoff: Option<Arc<SharedHandoff>>,
    /// 样本预算耗尽导致运行被截断，调用方必须如实上报。
    pub truncated: bool,
}

/// 有界等待第一条源轨道接上。返回时是否等到要看 `GstDecoder::track_linked`；
/// 本函数不替调用方下结论，也不吞掉任何拒绝记录。
fn wait_for_first_track(decoder: &GstDecoder, timeout_ms: u64) {
    let deadline = Instant::now() + Duration::from_millis(timeout_ms);
    while Instant::now() < deadline {
        if decoder.track_linked(TrackKind::Video) || decoder.track_linked(TrackKind::Audio) {
            return;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
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
    // 给 decodebin 一个有限的窗口交出第一条轨道。没有轨道就没得解码，
    // 而"容器被拒绝"与"文件本来就是空的"必须用不同的原因说清楚，不能都读成卡住。
    wait_for_first_track(&decoder, FIRST_TRACK_WAIT_MS);
    if !decoder.track_linked(TrackKind::Video) && !decoder.track_linked(TrackKind::Audio) {
        return Err(MediaError::DecodeFailed(decoder.no_track_reason()));
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
            // 源里没有这条轨道、已到 EOS、或被准入拒绝，都是终态：
            // 不再从它那里取样本，也不再算作"还在跑"。
            if !decoder.track_linked(kind) || decoder.is_eos(kind) || !decoder.track_admitted(kind)
            {
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
        if truncated {
            break;
        }
        if !running {
            // 循环开始前已经等到至少一条轨道，因此"没有活跃轨道"只可能是
            // 轨道全部被准入拒绝、或已到 EOS——两者都是有结论的终止，不是卡住。
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
    // 源里有但被拒绝的轨道必须跟着报告走，而不是只留在日志里。
    session.record_rejections(decoder.rejected_tracks());
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
    pub handoff: Option<Arc<SharedHandoff>>,
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
    // 实时源不做 preroll：起流前没有任何 pad，"一条轨道都没有"在此刻是正常状态，
    // 不能当结论。真正的空流由下面的断流预算显式失败（见 `StallTracker`）；
    // 容器级拒绝则由轨道探针写进 `rejected_tracks`。
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
            // 与文件路径同一口径：被准入拒绝的轨道不再拉样本。
            if !decoder.has_track(kind) || !decoder.track_admitted(kind) {
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
        // 源里的轨道全被准入拒绝：这不是"卡住"，而是一次有结论的拒绝，
        // 因此不能掉进断流预算里，报告以 rejected_tracks 为准。
        let any_admitted = [TrackKind::Video, TrackKind::Audio]
            .iter()
            .any(|kind| decoder.has_track(*kind) && decoder.track_admitted(*kind));
        if !any_admitted {
            ended_by_deadline = false;
            break;
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
    session.record_rejections(decoder.rejected_tracks());
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
            audio_overlap_ms: 0,
            sampling: SamplingPolicy::default(),
            handoff: None,
            backpressure: BackpressurePolicy::default(),
            evidence: None,
            ledger: None,
            ledger_path: None,
        }
    }

    fn session() -> DecodeSession<'static> {
        DecodeSession::new("stream-live-test", "live-test", "arena-live-test", &run())
            .expect("session builds")
    }

    #[test]
    fn codec_fixed_vp8_evidence_reaches_the_report_without_caps_fields() {
        // VP8 的 caps 里没有 `bit-depth-luma` / `chroma-format`；准入用规范固定值放行，
        // 报告必须给出同一份证据，否则"凭什么放行"在报告里就是空白。
        let mut context = SourceFormatContext {
            container: Some("video/webm".into()),
            codec: Some("video/x-vp8".into()),
            ..Default::default()
        };
        assert_eq!(source_bit_depth_of(TrackKind::Video, &context), Some(8));
        assert_eq!(source_chroma_format_of(TrackKind::Video, &context), "4:2:0");
        // 其它编码取不到就保持未知：绝不补一个默认位深或 4:2:0。
        context.codec = Some("video/x-h264".into());
        assert_eq!(source_bit_depth_of(TrackKind::Video, &context), None);
        assert_eq!(source_chroma_format_of(TrackKind::Video, &context), "");
        // 音频没有这两个维度。
        assert_eq!(source_bit_depth_of(TrackKind::Audio, &context), None);
        assert_eq!(source_chroma_format_of(TrackKind::Audio, &context), "");
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
            source: TrackSourceInfo::default(),
            pixel_aspect_ratio: None,
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
            source: TrackSourceInfo::default(),
            pixel_aspect_ratio: None,
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

    /// 打开保留式数据面的运行。`arena_bytes` 就是那条共享区的硬上限。
    fn retained_run(arena_bytes: usize, limit: usize, policy: BackpressurePolicy) -> DecodeRun {
        DecodeRun {
            handoff: crate::handoff::RetainPolicy::shared(arena_bytes, limit)
                .expect("retention policy is valid")
                // 等待预算为 0：这里的测试进程没有消费者，等待只会把用例拖慢。
                .open("arena-retained-test", 0)
                .expect("the shared plane opens"),
            backpressure: policy,
            ..run()
        }
    }

    /// 一帧有内容变化的视频。每帧换一个亮度，避免被普通限速先挡下，从而测到背压这条路径。
    fn changing_video_sample(pts_ms: i64, shade: u8) -> DecodedSample {
        let mut sample = video_sample(Some(pts_ms), Some(1_000));
        sample.bytes = vec![shade; 16 * 16 * 4];
        sample
    }

    #[test]
    fn a_half_full_queue_throttles_the_sampler_before_the_arena_ever_overflows() {
        // 共享区只放得下两帧：50% 的降级阈值恰好是第一帧之后的深度。
        let run = retained_run(2 * 1024, 8, BackpressurePolicy::default());
        let mut subject =
            DecodeSession::new("stream-bp", "bp", "arena-bp-throttle", &run).expect("session");
        for index in 0..5i64 {
            subject
                .push(changing_video_sample(index * 1_000, (index * 40) as u8))
                .expect("sample is admitted");
        }
        let (plane, handoff) = subject.finish().expect("session finishes");
        let report = plane.backpressure.expect("a report is always attached");
        assert!(
            report.observed,
            "the bounded queue existed and was measured"
        );
        assert_eq!(report.degraded_entries, 1, "the run entered degraded once");
        assert_eq!(
            report.saturated_entries, 1,
            "and saturated once the queue really did fill up"
        );
        assert_eq!(report.state, "saturated", "state is where the run ended");
        assert_eq!(
            report.dropped_total, 0,
            "no refusal was needed to get there"
        );
        let table = &report.queues[0];
        assert_eq!((table.current, table.peak, table.capacity), (2, 2, 8));
        let kind = &report.queues[1];
        assert_eq!(
            (kind.name.as_str(), kind.current, kind.peak, kind.capacity),
            ("handoff_retained_kind", 2, 2, 4),
            "两类共用一张表，因此按种类的上限必须单独读出来"
        );
        let arena = &report.queues[2];
        assert_eq!(
            (arena.current, arena.peak, arena.capacity),
            (2_048, 2_048, 2_048),
            "the arena is the queue that actually fills first"
        );
        let sampling = plane.sampling.first().expect("a sampling report");
        assert_eq!(sampling.skipped_backpressure_throttled, 3);
        assert_eq!(
            sampling.kept, 2,
            "the keep rate fell from one per second to one per throttled interval"
        );
        assert_eq!(
            handoff
                .expect("retention stays alive for the consumer")
                .stats()
                .expect("the plane answers its stats")
                .retained_total,
            2
        );

        // 对照实验：同一串样本在没有背压的进程内路径上，五帧全部会被保留。
        // 没有这个对照，"降速生效了"就只是一句自我判断。
        let mut control = session();
        for index in 0..5i64 {
            control
                .push(changing_video_sample(index * 1_000, (index * 40) as u8))
                .expect("sample is admitted");
        }
        let (control_plane, _) = control.finish().expect("session finishes");
        assert_eq!(
            control_plane
                .sampling
                .first()
                .expect("a sampling report")
                .kept,
            5
        );
        assert!(
            !control_plane
                .backpressure
                .expect("a report is always attached")
                .observed
        );
    }

    /// 音频段必须和 `audio_pcm` 一样落到跨进程保留表里：只有进程内的 arena + lease
    /// 自证过不了验收——插件端读的是保留表。这条测试就是"段进了数据面"的证据。
    #[test]
    fn audio_segments_reach_the_cross_process_retained_table() {
        let run = retained_run(1024 * 1024, 8, BackpressurePolicy::default());
        let mut subject =
            DecodeSession::new("stream-asr", "asr", "arena-asr-segment", &run).expect("session");
        // 五秒 1 kHz 的连续音频，每段 5 s，正好关闭一个完整段。
        for index in 0..5i64 {
            let mut sample = audio_sample(Some(index * 1_000), Some(1_000));
            sample.bytes = vec![0u8; 64];
            subject.push(sample).expect("sample is admitted");
        }
        let (plane, handoff) = subject.finish().expect("session finishes");
        let handoff = handoff.expect("retention stays alive for the consumer");
        assert_eq!(
            handoff
                .retained_by_kind()
                .expect("the plane reports its kinds")
                .get("audio_segment")
                .copied(),
            Some(1),
            "the segment descriptor must be offerable to another process"
        );
        let segment = handoff
            .list()
            .expect("the plane lists what it retains")
            .into_iter()
            .find(|held| held.kind == "audio_segment")
            .expect("the segment is in the retained table");
        assert_eq!(
            (segment.time_range.start_ms, segment.time_range.end_ms),
            (0, 5_000)
        );
        assert_eq!(segment.format.sample_rate, 48_000);
        assert_eq!(segment.format.channels, 2);
        assert_eq!(
            segment.format.sample_format, AUDIO_SAMPLE_FORMAT,
            "the consumer must not have to guess the sample layout"
        );
        assert_eq!(
            segment.length, 320,
            "five 64-byte samples are the segment payload"
        );
        let listed = &plane
            .audio_segments
            .as_ref()
            .expect("a segment report")
            .listed;
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].segment_id, segment.buffer_id);
    }

    #[test]
    fn a_full_arena_refuses_retention_and_the_refusal_carries_its_reason() {
        // 关掉阶段一降速（倍数 1），单独验证"降速也救不了的时候"会发生什么。
        let policy = BackpressurePolicy::new(50, 1, 10_000).expect("policy is valid");
        let run = retained_run(1_024, 8, policy);
        let mut subject =
            DecodeSession::new("stream-bp", "bp2", "arena-bp-full", &run).expect("session");
        for index in 0..4i64 {
            subject
                .push(changing_video_sample(index * 1_000, (index * 40) as u8))
                .expect("sample is admitted");
        }
        let (plane, _handoff) = subject.finish().expect("session finishes");
        let report = plane.backpressure.expect("a report is always attached");
        assert_eq!(report.state, "saturated");
        assert_eq!(report.dropped_total, 3, "only the first frame fits");
        let summed: u64 = report.drop_reasons.iter().map(|entry| entry.count).sum();
        assert_eq!(summed, report.dropped_total, "every drop is explained");
        assert_eq!(report.drop_reasons[0].reason, "arena_capacity_exceeded");
        let kinds: u64 = report.drop_kinds.iter().map(|entry| entry.count).sum();
        assert_eq!(kinds, report.dropped_total, "kind breakdown must add up");
        assert_eq!(
            report.drop_kinds[0].kind, "video_frame",
            "only video frames were offered in this run"
        );
        assert_eq!(report.queues[2].peak, 1_024);
        assert_eq!(report.queues[2].current, 1_024);
        assert_eq!(
            (report.queues[1].current, report.queues[1].capacity),
            (1, 4),
            "只有一帧进了窗口，按种类的上限还没到"
        );
        assert_eq!(
            report.sampling_throttled_samples, 0,
            "this policy deliberately does not change the rate"
        );
        assert_eq!(
            report.timeouts_total, 0,
            "no consumer ever held a lease, so nothing could time out"
        );
        assert!(
            report.residency_samples == 0,
            "buffers still held at the end have no measured wait time"
        );
        assert_eq!(report.saturated_entries, 1);
    }

    #[test]
    fn a_run_without_retention_reports_that_the_queue_was_never_measured() {
        // 进程内自校验路径：没有保留队列，报告必须说"没测过"，而不是给一组干净的零。
        let mut subject = session();
        subject
            .push(changing_video_sample(0, 10))
            .expect("sample is admitted");
        let (plane, handoff) = subject.finish().expect("session finishes");
        assert!(handoff.is_none());
        let report = plane.backpressure.expect("a report is always attached");
        assert!(!report.observed);
        assert!(report.queues.is_empty());
    }

    /// 带语义覆盖策略的运行。它让"全帧判别 + 事件窗口"与自适应采样同时生效。
    fn evidence_run() -> DecodeRun {
        DecodeRun {
            evidence: Some(EvidencePolicy::default()),
            ..run()
        }
    }

    #[test]
    fn semantic_coverage_accounts_for_every_video_frame_and_every_handed_off_frame() {
        let run = evidence_run();
        let mut subject = DecodeSession::new("stream-evidence", "evidence", "arena-evidence", &run)
            .expect("session builds");
        // 120 帧静态画面（走基线复查）之后接一次画面变化（走事件窗口），两条路径都要覆盖。
        let mut pts = 0i64;
        for _ in 0..120 {
            subject
                .push(video_sample(Some(pts), Some(33)))
                .expect("push");
            pts += 33;
        }
        let mut changed = video_sample(Some(pts), Some(33));
        changed.bytes = vec![240u8; 16 * 16 * 4];
        subject.push(changed).expect("push");
        pts += 33;
        for _ in 0..3 {
            subject
                .push(video_sample(Some(pts), Some(33)))
                .expect("push");
            pts += 33;
        }
        let (plane, _) = subject.finish().expect("finish");

        let video = stat(&plane, TrackKind::Video);
        let sampling = plane.sampling.first().expect("采样报告");
        let coverage = plane
            .semantic_coverage
            .first()
            .expect("启用语义覆盖后报告必须存在");
        assert_eq!(
            coverage.characterized_frames, sampling.observed,
            "判别帧数必须等于采样器观测到的帧数"
        );
        assert_eq!(
            sampling.kept + sampling.kept_evidence_window,
            video.samples,
            "采样保留加语义额外交接必须等于实际交接的视频帧数"
        );
        assert!(
            sampling.kept_evidence_window > 0,
            "事件窗口与预上下文应当带来额外交接"
        );
        assert!(coverage.characterized_frames >= 124);
        assert!(
            coverage.max_selected_gap_ms <= coverage.max_semantic_gap_ms + 33,
            "静态语义间隔不得超过上限加一个帧间隔"
        );
        assert_eq!(coverage.track_kind, "video");
        // 语义覆盖最容易被打破的一条不变量：预上下文帧先按"未选中"计数、再升级成窗口
        // 上下文，如果只撤销跳过原因而不撤销 `not_selected`，这里就会多出重叠的帧。
        let selected = coverage.selected_baseline_anchor
            + coverage.selected_event_anchor
            + coverage.selected_event_pre_context
            + coverage.selected_event_post_context;
        assert!(
            coverage.selected_event_pre_context > 0,
            "事件窗口要补上下文帧"
        );
        assert_eq!(
            selected + coverage.covered_without_model_refresh,
            coverage.characterized_frames,
            "每一帧要么被选中、要么带原因被覆盖"
        );
        let counted_skips: u64 = coverage
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
        assert_eq!(
            counted_skips, coverage.covered_without_model_refresh,
            "聚合跳过计数必须等于未选中的帧数"
        );
    }

    #[test]
    fn a_run_without_an_evidence_policy_says_so_instead_of_claiming_no_change() {
        let mut subject = session();
        subject.push(changing_video_sample(0, 10)).expect("push");
        let (plane, _) = subject.finish().expect("finish");
        assert!(
            plane.semantic_coverage.is_empty(),
            "未启用语义覆盖时报告必须为空，而不是给出'每帧都没变'"
        );
        let sampling = plane.sampling.first().expect("采样报告");
        assert_eq!(sampling.kept_evidence_window, 0);
    }
}
