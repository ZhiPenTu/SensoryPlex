//! ADR-009 媒体格式准入：把承诺矩阵写成数据，把表外组合判成显式拒绝。
//!
//! 这里不做任何"降级成功"：判不出来就拒绝，并把稳定拒绝码交给调用方进报告。
//! 判定输入必须同时包含**解码前采集的源格式上下文**与解码后的格式——
//! 只看 raw caps 会把 10-bit Main10 当成 8-bit NV12（ADR-009 实测 A）。

/// 轨道类别。与 `decode::TrackKind` 分开，避免 capability 反向依赖解码实现。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrackClass {
    Video,
    Audio,
}

impl TrackClass {
    pub fn name(self) -> &'static str {
        match self {
            TrackClass::Video => "video",
            TrackClass::Audio => "audio",
        }
    }
}

/// 解码前采集到的源格式上下文。字段取不到时保持 `None`，绝不填默认值。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SourceFormatContext {
    /// 容器 caps 主名，例如 `video/quicktime`、`video/mpegts`。
    pub container: Option<String>,
    /// 轨道 caps 主名，例如 `video/x-h265`、`audio/mpeg`。
    pub codec: Option<String>,
    pub profile: Option<String>,
    /// 源亮度位深（caps 的 `bit-depth-luma`）。不是归一化后的载荷位深。
    pub bit_depth: Option<u32>,
    pub chroma: Option<String>,
    pub colorimetry: Option<String>,
    /// `audio/mpeg` 需要它区分 AAC（4）与 MP3（1）。
    pub mpegversion: Option<u32>,
    /// 源 caps 声明的帧率 `(num, den)`，缺失表示未声明。
    pub declared_frame_rate: Option<(u32, u32)>,
}

impl SourceFormatContext {
    /// 用后到的 caps 覆盖**已知**字段：解析器输出与解码器输入是同一份信息的不同阶段，
    /// 后到的更完整；而字段为 `None` 说明这一份 caps 没带它，不能把已知值抹掉。
    /// CAPS 变化（例如中途切到 10-bit）也因此能真正改写上下文并触发重判。
    pub fn adopt(&mut self, other: &SourceFormatContext) {
        if other.container.is_some() {
            self.container = other.container.clone();
        }
        if other.codec.is_some() {
            self.codec = other.codec.clone();
        }
        if other.profile.is_some() {
            self.profile = other.profile.clone();
        }
        if other.bit_depth.is_some() {
            self.bit_depth = other.bit_depth;
        }
        if other.chroma.is_some() {
            self.chroma = other.chroma.clone();
        }
        if other.colorimetry.is_some() {
            self.colorimetry = other.colorimetry.clone();
        }
        if other.mpegversion.is_some() {
            self.mpegversion = other.mpegversion;
        }
        if other.declared_frame_rate.is_some() {
            self.declared_frame_rate = other.declared_frame_rate;
        }
    }
}

/// 解码后的格式（raw caps）。只用来判"归一化之后能观察到的维度"，不能反推源位深。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DecodedFormat {
    pub pixel_format: String,
    pub width: u32,
    pub height: u32,
    pub sample_rate: u32,
    pub channels: u32,
}

/// 稳定拒绝码 + 它对应的观测值（detail 是人读的补充，码本身不含自由文本）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Rejection {
    pub code: String,
    pub detail: String,
}

impl Rejection {
    pub fn new(code: impl Into<String>, detail: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            detail: detail.into(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verdict {
    Admitted,
    Rejected(Rejection),
}

impl Verdict {
    pub fn is_admitted(&self) -> bool {
        matches!(self, Verdict::Admitted)
    }

    pub fn rejection(&self) -> Option<&Rejection> {
        match self {
            Verdict::Admitted => None,
            Verdict::Rejected(rejection) => Some(rejection),
        }
    }
}

fn rejected(code: String, detail: impl Into<String>) -> Verdict {
    Verdict::Rejected(Rejection::new(code, detail))
}

/// 把 caps 主名或取值折成拒绝码里可用的片段：只留小写字母数字与下划线。
pub fn sanitize(value: &str) -> String {
    let mut out = String::new();
    let mut last_underscore = false;
    for ch in value.chars() {
        let mapped = if ch.is_ascii_alphanumeric() {
            ch.to_ascii_lowercase()
        } else {
            '_'
        };
        if mapped == '_' {
            if last_underscore || out.is_empty() {
                continue;
            }
            last_underscore = true;
        } else {
            last_underscore = false;
        }
        out.push(mapped);
    }
    while out.ends_with('_') {
        out.pop();
    }
    if out.is_empty() {
        "unknown".to_string()
    } else {
        out
    }
}

/// V1 承诺的容器（ADR-009 §2.1）。值同时用于拒绝码与报告。
const CONTAINERS: &[(&str, &str)] = &[
    ("video/quicktime", "mov"),
    ("audio/x-m4a", "m4a"),
    ("video/x-matroska", "mkv"),
    ("audio/x-matroska", "mkv"),
    ("video/webm", "webm"),
    ("audio/webm", "webm"),
    ("video/mpegts", "mpegts"),
];

/// 已知但不承诺的容器，给它们干净的短名（拒绝码可读）。
const KNOWN_CONTAINERS: &[(&str, &str)] = &[
    ("video/x-msvideo", "avi"),
    ("video/x-flv", "flv"),
    ("application/ogg", "ogg"),
    ("audio/ogg", "ogg"),
    ("video/x-ms-asf", "asf"),
    ("video/x-ms-wmv", "asf"),
    ("video/x-wmv", "asf"),
    ("video/x-mjpeg", "mjpeg"),
    ("audio/x-wav", "wav"),
    ("video/x-dv", "dv"),
    ("video/x-msmpeg", "msmpeg"),
    ("video/x-mpeg", "mpeg_es"),
    ("audio/x-flac", "flac"),
];

pub fn container_name(caps: &str) -> String {
    if let Some((_, short)) = CONTAINERS.iter().find(|(name, _)| *name == caps) {
        return (*short).to_string();
    }
    if let Some((_, short)) = KNOWN_CONTAINERS.iter().find(|(name, _)| *name == caps) {
        return (*short).to_string();
    }
    sanitize(caps)
}

pub fn container_supported(caps: &str) -> bool {
    CONTAINERS.iter().any(|(name, _)| *name == caps)
}

/// 裸基本流（raw elementary stream）的 caps 主名。typefind 对没有容器的流会给出这些名字，
/// 它们是**编码**的名字而不是容器名，必须与"未知容器"区分开（ADR-009 §2.1 把裸 ES 列为显式拒绝）。
const ELEMENTARY_STREAMS: &[&str] = &[
    "video/x-h264",
    "video/x-h265",
    "video/x-vp8",
    "video/x-vp9",
    "video/x-av1",
    "video/x-mpeg",
    "video/mpeg",
    "audio/mpeg",
    "audio/x-ac3",
    "audio/x-eac3",
    "audio/x-dts",
    // `audio/x-flac` 与 `audio/x-wav` **不在**这里：它们各自有容器头（FLAC 的元数据块、
    // RIFF/WAVE），按容器维度命名（`unsupported_container_flac` / `unsupported_container_wav`）
    // 才与 ADR-009 §3 的命名表一致。把它们读成"没有容器"会让 §2.1 的容器短名永远走不到。
];

pub fn elementary_stream(caps: &str) -> bool {
    ELEMENTARY_STREAMS.contains(&caps)
}

/// 容器准入：没采集到容器就拒绝，绝不"默认可以放行"。
/// `container` 是 typefind 给出的 caps 主名（`video/quicktime`、`video/x-msvideo`、...）。
pub fn classify_container(container: Option<&str>) -> Verdict {
    match container {
        None => rejected(
            "unknown_source_container".into(),
            "container_caps_not_collected",
        ),
        Some(caps) if container_supported(caps) => Verdict::Admitted,
        // 裸 ES 没有容器：这不是"未知"，而是"没有容器"，两者必须是不同的码。
        Some(caps) if elementary_stream(caps) => {
            rejected("unsupported_container_raw_es".into(), caps.to_string())
        }
        Some(caps) => rejected(
            format!("unsupported_container_{}", container_name(caps)),
            caps.to_string(),
        ),
    }
}

/// pad 的 caps 主名 → 轨道类别。非音视频 pad 返回 `None`（调用方必须显式计数）。
pub fn classify_pad(caps_name: &str) -> Option<TrackClass> {
    if caps_name.starts_with("video/") {
        Some(TrackClass::Video)
    } else if caps_name.starts_with("audio/") {
        Some(TrackClass::Audio)
    } else {
        None
    }
}

/// VP8 的 caps 主名。
///
/// 它的位深与采样格式都由规范固定（RFC 6386：8-bit、4:2:0），而 caps 里**不带**这两个字段：
/// 实测（2026-09-23，`macos-aarch64`，GStreamer 1.28.7，`matroskademux ! vp8dec`）
/// `video/x-vp8` 只有 `width` / `height` / `framerate`，既没有 `profile`，
/// 也没有 `bit-depth-luma` / `chroma-format`。因此它必须走"规范固定"的判据，
/// 否则一条本来合格的 VP8 轨道会以 `unknown_source_bit_depth` 被误拒。
const VP8_CODEC: &str = "video/x-vp8";

/// 由 caps 的 profile 推导源位深。
///
/// 为什么需要它：**位深字段不是每个编码都填**。实测（`macos-aarch64`，GStreamer 1.28.7）
/// 10-bit HEVC 样本的解析器输出只有 `profile=(string)main-10`，没有 `bit-depth-luma`；
/// 而 VP9 profile 0 与 HEVC main 会带 `bit-depth-luma=8`。此时若只认字段，10-bit 源会退化成
/// `unknown_source_bit_depth`（码不对），而 8-bit 源会被正确放行——profile 是 caps 自带的、
/// 与位深等长的证据。
///
/// 只推导**规范确定**的映射：profile 本身允许 8-bit 的（H.264 high-4:2:2 / 4:4:4、
/// HEVC main-4:4:4）一律返回 `None`，宁可按未知拒绝，也不猜一个 8 出来。
pub fn implied_bit_depth(codec: &str, profile: Option<&str>) -> Option<u32> {
    // 规范固定、且 caps 里没有 profile 的编码必须排在 `profile?` 之前。
    if codec == VP8_CODEC {
        return Some(8);
    }
    let profile = profile?.trim().to_ascii_lowercase();
    let has_token = |token: &str| profile.split('-').any(|part| part == token);
    match codec {
        "video/x-h265" => {
            if has_token("10") {
                Some(10)
            } else if has_token("12") {
                Some(12)
            } else if profile.starts_with("main") && !profile.contains("4:4:4") {
                Some(8)
            } else {
                None
            }
        }
        "video/x-h264" => {
            if has_token("10") {
                Some(10)
            } else if has_token("12") {
                Some(12)
            } else if matches!(
                profile.as_str(),
                "baseline" | "constrained-baseline" | "main" | "high" | "progressive-high"
            ) {
                Some(8)
            } else {
                None
            }
        }
        // VP9 profile 0/1 是 8-bit，2/3 是 10 或 12-bit（caps 的位深字段给出确切值）。
        "video/x-vp9" => match profile.as_str() {
            "0" | "1" => Some(8),
            "2" | "3" => Some(10),
            _ => None,
        },
        _ => None,
    }
}

/// 编码规范固定了采样格式、而 caps 又不提供该字段时的映射。
///
/// 只列**规范唯一确定**的编码：VP8 恒为 4:2:0（caps 里没有 `chroma-format`，实测同上）。
/// 其它编码一律返回 `None`——采样格式取不到就按未知拒绝，绝不猜一个 4:2:0 出来。
pub fn implied_chroma_format(codec: &str) -> Option<&'static str> {
    match codec {
        VP8_CODEC => Some(PROMISED_CHROMA_FORMAT),
        _ => None,
    }
}

/// 解析后的 GstVideo 色彩学三元组（缺项为 `None`）。
///
/// **字段顺序是 `range:matrix:transfer:primaries`**，不是"直觉顺序"。这是 gst-plugins-base 的
/// 实测行为：`GstVideoColorimetry` 的结构体顺序就是 `range/matrix/transfer/primaries`，
/// `to_string` 按结构体顺序输出数字，所以 `bt709` 的数字形式是 `2:3:5:1`、
/// `bt2100-pq` 是 `2:6:14:7`（实测命令与证据见 `docs/verification.md` 的 M9 一节）。
/// 按"primaries 在最前"解析会把 transfer 读成 matrix，BT.2020 + PQ 的源会被漏放进来。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Colorimetry {
    pub primaries: Option<u32>,
    pub transfer: Option<u32>,
    pub matrix: Option<u32>,
}

/// `video-color.h` 里预定义的色彩学名字（`GST_VIDEO_COLORIMETRY_*`）与其枚举值。
/// 只列实测确认过的：这些名字对应的数值都经过 `from_string`/`to_string` 往返核对。
const NAMED_COLORIMETRY: &[(&str, u32, u32, u32)] = &[
    // (名字, primaries, transfer, matrix)
    ("bt601", 4, 16, 4),
    ("bt709", 1, 5, 3),
    ("smpte240m", 5, 6, 5),
    ("bt2020", 7, 11, 6),
    ("bt2020-10", 7, 13, 6),
    ("bt2100-pq", 7, 14, 6),
    ("bt2100-hlg", 7, 15, 6),
];

/// GstVideoTransferFunction 的 HDR 传输特性取值：14=SMPTE2084(PQ)、15=ARIB_STD_B67(HLG)。
const TRANSFER_SMPTE2084: u32 = 14;
const TRANSFER_ARIB_STD_B67: u32 = 15;
/// GstVideoColorPrimaries 的 BT.2020 取值。
const PRIMARIES_BT2020: u32 = 7;

pub fn colorimetry_fields(colorimetry: &str) -> Colorimetry {
    let lowered = colorimetry.trim().to_ascii_lowercase();
    if let Some((_, primaries, transfer, matrix)) =
        NAMED_COLORIMETRY.iter().find(|(name, ..)| *name == lowered)
    {
        return Colorimetry {
            primaries: Some(*primaries),
            transfer: Some(*transfer),
            matrix: Some(*matrix),
        };
    }
    let numbers = lowered
        .split(':')
        .map(|field| field.trim().parse::<u32>().ok())
        .collect::<Vec<_>>();
    if numbers.len() == 4 && numbers.iter().all(|number| number.is_some()) {
        return Colorimetry {
            primaries: numbers[3],
            transfer: numbers[2],
            matrix: numbers[1],
        };
    }
    Colorimetry::default()
}

/// 命中 HDR 传输特性或 BT.2020 原色即返回稳定拒绝码（ADR-009 §2.2）。
/// 无法解析的取值不构成拒绝理由：宁可把色彩学标成未知，也不凭猜测拒绝一条轨道。
fn colorimetry_rejection(colorimetry: &str) -> Option<Rejection> {
    let lowered = colorimetry.trim().to_ascii_lowercase();
    let fields = colorimetry_fields(colorimetry);
    if fields.transfer == Some(TRANSFER_SMPTE2084)
        || lowered.contains("smpte2084")
        || lowered.contains("bt2100-pq")
    {
        return Some(Rejection::new(
            "unsupported_transfer_characteristics_pq".to_string(),
            colorimetry.to_string(),
        ));
    }
    if fields.transfer == Some(TRANSFER_ARIB_STD_B67)
        || lowered.contains("arib-std-b67")
        || lowered.contains("bt2100-hlg")
        || lowered.contains("hlg")
    {
        return Some(Rejection::new(
            "unsupported_transfer_characteristics_hlg".to_string(),
            colorimetry.to_string(),
        ));
    }
    if fields.primaries == Some(PRIMARIES_BT2020)
        || lowered.contains("bt2020")
        || lowered.contains("bt2100")
    {
        return Some(Rejection::new(
            "unsupported_color_primaries_bt2020".to_string(),
            colorimetry.to_string(),
        ));
    }
    None
}

/// 已知编码 caps 主名 → 拒绝码里可读的短名。与容器同理：`video/x-prores` 的拒绝码必须是
/// `unsupported_codec_prores`，把斜杠打散成 `video_x_prores` 读起来就不再是编码名。
const KNOWN_CODECS: &[(&str, &str)] = &[
    ("video/x-h264", "h264"),
    ("video/x-h265", "h265"),
    ("video/x-vp8", "vp8"),
    ("video/x-vp9", "vp9"),
    ("video/x-av1", "av1"),
    ("video/x-prores", "prores"),
    ("video/x-dnxhd", "dnxhd"),
    ("video/x-mpeg2", "mpeg2"),
    ("video/x-msmpeg", "msmpeg"),
    ("video/x-vc1", "vc1"),
    ("image/jp2", "jpeg2000"),
    ("audio/mpeg", "mpeg"),
    ("audio/x-opus", "opus"),
    ("audio/x-vorbis", "vorbis"),
    ("audio/x-raw", "pcm"),
    ("audio/x-ac3", "ac3"),
    ("audio/x-eac3", "eac3"),
    ("audio/x-dts", "dts"),
    ("audio/x-truehd", "truehd"),
    ("audio/x-flac", "flac"),
    ("audio/x-wav", "wav"),
];

pub fn codec_name(caps: &str) -> String {
    if let Some((_, short)) = KNOWN_CODECS.iter().find(|(name, _)| *name == caps) {
        return (*short).to_string();
    }
    sanitize(caps)
}

/// V1 承诺的视频编码（ADR-009 §2.2）。AV1 在矩阵里是**实验**项，和表外编码一样显式拒绝。
const PROMISED_VIDEO_CODECS: &[&str] =
    &["video/x-h264", "video/x-h265", "video/x-vp8", "video/x-vp9"];

/// V1 承诺的音频编码（ADR-009 §2.3）。`audio/x-raw` 是"容器里存的就是 PCM"的那一行：
/// 它没有 parser/decoder，源编码只能从 caps 主名本身读出来。
const PROMISED_AUDIO_CODECS: &[&str] = &["audio/x-opus", "audio/x-vorbis", "audio/x-raw"];

/// 报告里"没有解码元素"的显式取值：源采样由 demuxer 直接给出（容器里存的就是 raw 采样，
/// 例如 MOV 里的 PCM），整条链路没有任何 parser/decoder 参与。
///
/// 它与空串必须分开：空串的含义是"本次运行没能归因到解码器"，那是未知，
/// 而这里是有结论的"本来就没有解码器"。ADR-009 §5 要求解码器证据可复核，
/// 因此这条结论也要写进报告，而不是留在日志里。
pub const NO_DECODER_ELEMENT: &str = "demuxer_passthrough";

/// v1 只承诺 8-bit；`> 8` 一律显式拒绝，而不是让归一化把位深静默截到 8（实测 A）。
const MAX_PROMISED_BIT_DEPTH: u32 = 8;
/// 承诺行（HEVC Main、VP9 Profile 0）都是 4:2:0，其它采样格式不在矩阵内。
const PROMISED_CHROMA_FORMAT: &str = "4:2:0";
/// v1 只承诺 mono/stereo（ADR-009 §2.3）。
const MAX_PROMISED_CHANNELS: u32 = 2;

/// 单条轨道的准入判定。输入必须同时包含**解码前采集的源格式上下文**与解码后的格式：
/// 只看解码后的 raw caps 会把 Main10 当成 8-bit NV12（实测 A）。
///
/// 判定顺序是"容器 → 编码 → 位深 → 色彩 → 采样格式 → 声道"：先判更粗的维度，
/// 这样报告里的拒绝码总是**最外层**那一条，而不是同一份证据衍生出的多条码。
pub fn classify_track(
    class: TrackClass,
    ctx: &SourceFormatContext,
    decoded: &DecodedFormat,
) -> Verdict {
    let container = classify_container(ctx.container.as_deref());
    if let Some(rejection) = container.rejection() {
        return Verdict::Rejected(rejection.clone());
    }
    match class {
        TrackClass::Video => classify_video(ctx, decoded),
        TrackClass::Audio => classify_audio(ctx, decoded),
    }
}

/// 视频准入。
///
/// 解码后的格式**不参与**判定：`NV12` 无法区分 8-bit 与 10-bit 源，用它当判据正是
/// "降级成功"的来源。它仍会被写进报告（`DecodedTrackStat.pixel_format`），但不是准入依据。
fn classify_video(ctx: &SourceFormatContext, _decoded: &DecodedFormat) -> Verdict {
    let Some(codec) = ctx.codec.as_deref() else {
        return rejected("unknown_source_codec".into(), "codec_caps_not_collected");
    };
    if !PROMISED_VIDEO_CODECS.contains(&codec) {
        return rejected(
            format!("unsupported_codec_{}", codec_name(codec)),
            codec.to_string(),
        );
    }
    // 位深：先看解析器给出的字段，再看 profile 能唯一确定的映射。
    // 实测（`macos-aarch64`，GStreamer 1.28.7）10-bit HEVC 只有 `profile=main-10`，
    // 没有 `bit-depth-luma`，所以只认字段会让 10-bit 源以 `unknown_source_bit_depth` 落地。
    let declared = ctx.bit_depth;
    let Some(depth) = declared.or_else(|| implied_bit_depth(codec, ctx.profile.as_deref())) else {
        let detail = ctx
            .profile
            .as_deref()
            .map(|profile| format!("profile={profile}"))
            .unwrap_or_else(|| "bit_depth_not_collected".to_string());
        return rejected("unknown_source_bit_depth".into(), detail);
    };
    if depth > MAX_PROMISED_BIT_DEPTH {
        let detail = if declared.is_some() {
            format!("bit-depth-luma={depth}")
        } else {
            format!("profile={}", ctx.profile.as_deref().unwrap_or_default())
        };
        return rejected(format!("unsupported_bit_depth_{depth}bit"), detail);
    }
    // HDR 传输特性与 BT.2020 原色：即使 8-bit 也拒绝，因为颜色语义无法保证（ADR-009 §2.2）。
    if let Some(colorimetry) = ctx.colorimetry.as_deref() {
        if let Some(rejection) = colorimetry_rejection(colorimetry) {
            return Verdict::Rejected(rejection);
        }
    }
    // 采样格式：先看 caps 实测字段，再看编码规范唯一确定的映射（VP8 恒为 4:2:0）。
    match ctx
        .chroma
        .as_deref()
        .or_else(|| implied_chroma_format(codec))
    {
        Some(PROMISED_CHROMA_FORMAT) => {}
        // 4:2:2 / 4:4:4 / 单色都不在承诺矩阵内。
        Some(chroma) => {
            return rejected(
                format!("unsupported_chroma_format_{}", sanitize(chroma)),
                chroma.to_string(),
            )
        }
        // 采样格式取不到就不放行：ADR-009 §4 要求"准入所需的源信息缺失时不得放行"。
        None => {
            return rejected(
                "unknown_source_chroma_format".into(),
                "chroma_not_collected",
            )
        }
    }
    Verdict::Admitted
}

/// 音频准入。判据是**实测**声道数，不是容器声明的声道数。
fn classify_audio(ctx: &SourceFormatContext, decoded: &DecodedFormat) -> Verdict {
    let Some(codec) = ctx.codec.as_deref() else {
        return rejected("unknown_source_codec".into(), "codec_caps_not_collected");
    };
    if codec == "audio/mpeg" {
        // `audio/mpeg` 同时承载 MPEG-1/2 Layer III 与 AAC：只看 caps 主名会把 MP3 读成 AAC。
        match ctx.mpegversion {
            Some(4) => {}
            Some(other) => {
                return rejected(
                    "unsupported_codec_mp3".into(),
                    format!("mpegversion={other}"),
                )
            }
            None => {
                return rejected(
                    "unknown_source_codec_mpegversion".into(),
                    "mpegversion_not_collected",
                )
            }
        }
        // 承诺的是 AAC-LC；对象类型取不到就不放行，取到了但不是 lc 也拒绝。
        match ctx.profile.as_deref() {
            Some(profile) if profile.eq_ignore_ascii_case("lc") => {}
            Some(profile) => {
                return rejected(
                    format!("unsupported_codec_aac_{}", sanitize(profile)),
                    profile.to_string(),
                )
            }
            None => {
                return rejected(
                    "unknown_source_codec_profile".into(),
                    "aac_profile_not_collected",
                )
            }
        }
    } else if !PROMISED_AUDIO_CODECS.contains(&codec) {
        return rejected(
            format!("unsupported_codec_{}", codec_name(codec)),
            codec.to_string(),
        );
    }
    if decoded.channels == 0 {
        return rejected("unknown_channel_layout".into(), "channels_not_measured");
    }
    if decoded.channels > MAX_PROMISED_CHANNELS {
        return rejected(
            "unsupported_channel_layout_multichannel".into(),
            decoded.channels.to_string(),
        );
    }
    Verdict::Admitted
}

/// 非音视频 pad（字幕、元数据、附件……）的稳定拒绝码。
/// ADR-009 §3（实测 B）：这类 pad 过去只写日志，报告读起来像"流里没有这条轨道"。
pub fn pad_media_rejection(caps_name: &str) -> Rejection {
    Rejection::new(
        format!("unsupported_media_type_{}", sanitize(caps_name)),
        caps_name.to_string(),
    )
}

/// 同一 kind 的第二条轨道：v1 不支持多轨（ADR-009 §3 的
/// `unsupported_track_layout_multiple_video` / `..._audio`）。
pub fn track_layout_rejection(class: TrackClass) -> Rejection {
    Rejection::new(
        format!("unsupported_track_layout_multiple_{}", class.name()),
        format!("more_than_one_{}_track", class.name()),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    pub(super) fn ctx_video(codec: &str) -> SourceFormatContext {
        SourceFormatContext {
            container: Some("video/quicktime".into()),
            codec: Some(codec.into()),
            bit_depth: Some(8),
            chroma: Some("4:2:0".into()),
            colorimetry: Some("bt709".into()),
            ..Default::default()
        }
    }

    #[test]
    fn promised_video_formats_are_admitted() {
        for codec in ["video/x-h264", "video/x-h265", "video/x-vp8", "video/x-vp9"] {
            assert_eq!(
                classify_track(
                    TrackClass::Video,
                    &ctx_video(codec),
                    &DecodedFormat::default()
                ),
                Verdict::Admitted,
                "{codec}"
            );
        }
    }

    #[test]
    fn ten_bit_source_is_rejected_even_though_raw_caps_are_8bit() {
        let mut ctx = ctx_video("video/x-h265");
        ctx.profile = Some("main-10".into());
        ctx.bit_depth = Some(10);
        let decoded = DecodedFormat {
            pixel_format: "NV12".into(),
            width: 320,
            height: 240,
            ..Default::default()
        };
        let verdict = classify_track(TrackClass::Video, &ctx, &decoded);
        assert_eq!(
            verdict.rejection().map(|r| r.code.clone()),
            Some("unsupported_bit_depth_10bit".to_string())
        );
    }

    #[test]
    fn missing_source_bit_depth_is_rejected_not_defaulted() {
        let mut ctx = ctx_video("video/x-h265");
        ctx.bit_depth = None;
        assert_eq!(
            classify_track(TrackClass::Video, &ctx, &DecodedFormat::default())
                .rejection()
                .unwrap()
                .code,
            "unknown_source_bit_depth"
        );
    }

    #[test]
    fn hdr_transfers_are_rejected() {
        let mut ctx = ctx_video("video/x-h265");
        ctx.colorimetry = Some("bt2020".into());
        assert_eq!(
            classify_track(TrackClass::Video, &ctx, &DecodedFormat::default())
                .rejection()
                .unwrap()
                .code,
            "unsupported_color_primaries_bt2020"
        );
        ctx.colorimetry = Some("smpte2084".into());
        assert_eq!(
            classify_track(TrackClass::Video, &ctx, &DecodedFormat::default())
                .rejection()
                .unwrap()
                .code,
            "unsupported_transfer_characteristics_pq"
        );
        ctx.colorimetry = Some("arib-std-b67".into());
        assert_eq!(
            classify_track(TrackClass::Video, &ctx, &DecodedFormat::default())
                .rejection()
                .unwrap()
                .code,
            "unsupported_transfer_characteristics_hlg"
        );
    }

    #[test]
    fn multichannel_audio_is_rejected_by_measured_channels() {
        let ctx = SourceFormatContext {
            container: Some("audio/x-m4a".into()),
            codec: Some("audio/mpeg".into()),
            mpegversion: Some(4),
            profile: Some("lc".into()),
            ..Default::default()
        };
        let stereo = DecodedFormat {
            channels: 2,
            ..Default::default()
        };
        assert_eq!(
            classify_track(TrackClass::Audio, &ctx, &stereo),
            Verdict::Admitted
        );
        let surround = DecodedFormat {
            channels: 6,
            ..Default::default()
        };
        assert_eq!(
            classify_track(TrackClass::Audio, &ctx, &surround)
                .rejection()
                .unwrap()
                .code,
            "unsupported_channel_layout_multichannel"
        );
    }

    #[test]
    fn mp3_is_not_read_as_aac() {
        let ctx = SourceFormatContext {
            container: Some("video/quicktime".into()),
            codec: Some("audio/mpeg".into()),
            mpegversion: Some(1),
            ..Default::default()
        };
        assert_eq!(
            classify_track(TrackClass::Audio, &ctx, &DecodedFormat::default())
                .rejection()
                .unwrap()
                .code,
            "unsupported_codec_mp3"
        );
    }

    #[test]
    fn unimplemented_containers_and_codecs_get_stable_codes() {
        let ctx = SourceFormatContext {
            container: Some("video/x-msvideo".into()),
            codec: Some("video/x-h264".into()),
            bit_depth: Some(8),
            ..Default::default()
        };
        assert_eq!(
            classify_track(TrackClass::Video, &ctx, &DecodedFormat::default())
                .rejection()
                .unwrap()
                .code,
            "unsupported_container_avi"
        );
        let ctx = SourceFormatContext {
            container: Some("video/quicktime".into()),
            codec: Some("video/x-prores".into()),
            bit_depth: Some(10),
            ..Default::default()
        };
        assert_eq!(
            classify_track(TrackClass::Video, &ctx, &DecodedFormat::default())
                .rejection()
                .unwrap()
                .code,
            "unsupported_codec_prores"
        );
    }

    #[test]
    fn missing_container_is_rejected() {
        let mut ctx = ctx_video("video/x-h265");
        ctx.container = None;
        assert_eq!(
            classify_track(TrackClass::Video, &ctx, &DecodedFormat::default())
                .rejection()
                .unwrap()
                .code,
            "unknown_source_container"
        );
    }

    #[test]
    fn adopt_overwrites_known_values_and_keeps_unknown_ones() {
        let mut ctx = SourceFormatContext::default();
        ctx.adopt(&SourceFormatContext {
            bit_depth: None,
            container: Some("video/mpegts".into()),
            ..Default::default()
        });
        ctx.adopt(&SourceFormatContext {
            bit_depth: Some(8),
            container: Some("video/mpegts".into()),
            ..Default::default()
        });
        // 只带位深的一份 caps 不会把已知道的容器抹掉，但会把位深改写（CAPS 变化）。
        ctx.adopt(&SourceFormatContext {
            bit_depth: Some(10),
            container: None,
            ..Default::default()
        });
        assert_eq!(ctx.bit_depth, Some(10));
        assert_eq!(ctx.container.as_deref(), Some("video/mpegts"));
    }
}

#[cfg(test)]
mod admission_tests {
    use super::tests::ctx_video;
    use super::*;

    fn raw_es_caps() -> Vec<&'static str> {
        vec![
            "video/x-h264",
            "video/x-h265",
            "video/x-vp9",
            "audio/mpeg",
            "audio/x-ac3",
        ]
    }

    #[test]
    fn raw_elementary_streams_are_not_read_as_unknown_containers() {
        for caps in raw_es_caps() {
            let verdict = classify_container(Some(caps));
            assert_eq!(
                verdict.rejection().map(|rejection| rejection.code.clone()),
                Some("unsupported_container_raw_es".to_string()),
                "{caps}"
            );
        }
        // 有容器头的"容器"不能被读成裸 ES：FLAC/WAV 的名字表必须走得通。
        assert_eq!(
            classify_container(Some("audio/x-wav"))
                .rejection()
                .unwrap()
                .code,
            "unsupported_container_wav"
        );
        assert_eq!(
            classify_container(Some("audio/x-flac"))
                .rejection()
                .unwrap()
                .code,
            "unsupported_container_flac"
        );
        // 真正的未知容器仍是未知容器，两者不能混。
        assert_eq!(
            classify_container(Some("application/x-mystery"))
                .rejection()
                .unwrap()
                .code,
            "unsupported_container_application_x_mystery"
        );
        assert_eq!(
            classify_container(None).rejection().unwrap().code,
            "unknown_source_container"
        );
    }

    #[test]
    fn pad_classification_only_knows_audio_and_video() {
        assert_eq!(classify_pad("video/x-raw"), Some(TrackClass::Video));
        assert_eq!(classify_pad("audio/x-raw"), Some(TrackClass::Audio));
        assert_eq!(classify_pad("text/x-raw"), None);
        assert_eq!(classify_pad("application/x-subtitle"), None);
    }

    #[test]
    fn profile_carries_the_bit_depth_when_the_caps_field_is_absent() {
        // 实测：10-bit HEVC 样本的解析器输出只有 profile=main-10。
        assert_eq!(implied_bit_depth("video/x-h265", Some("main-10")), Some(10));
        assert_eq!(
            implied_bit_depth("video/x-h265", Some("main-10-intra")),
            Some(10)
        );
        assert_eq!(
            implied_bit_depth("video/x-h265", Some("main-4:4:4-10")),
            Some(10)
        );
        assert_eq!(implied_bit_depth("video/x-h265", Some("main")), Some(8));
        // RExt 4:4:4 可以是 8/10/12-bit，只能算未知。
        assert_eq!(implied_bit_depth("video/x-h265", Some("main-4:4:4")), None);
        assert_eq!(implied_bit_depth("video/x-h264", Some("high")), Some(8));
        assert_eq!(implied_bit_depth("video/x-h264", Some("high-10")), Some(10));
        // H.264 High 4:2:2 / 4:4:4 允许 8 与 ≥10，不猜。
        assert_eq!(implied_bit_depth("video/x-h264", Some("high-4:4:4")), None);
        assert_eq!(implied_bit_depth("video/x-vp9", Some("0")), Some(8));
        assert_eq!(implied_bit_depth("video/x-vp9", Some("2")), Some(10));
        assert_eq!(implied_bit_depth("video/x-vp8", Some("0")), Some(8));
        assert_eq!(implied_bit_depth("video/x-h265", None), None);
    }

    #[test]
    fn codec_fixed_bit_depth_and_chroma_do_not_need_caps_fields() {
        // 实测：`video/x-vp8` 的 caps 里既没有 `profile`，也没有 `bit-depth-luma` /
        // `chroma-format`（`matroskademux ! vp8dec`，GStreamer 1.28.7）。VP8 的位深与采样格式
        // 由规范固定，这条轨道必须据此**准入**，而不是以 `unknown_source_bit_depth` 被误拒。
        assert_eq!(implied_bit_depth("video/x-vp8", None), Some(8));
        assert_eq!(implied_chroma_format("video/x-vp8"), Some("4:2:0"));
        // 推导只覆盖"规范唯一确定"的编码：别的编码取不到就是不猜。
        assert_eq!(implied_chroma_format("video/x-h264"), None);
        assert_eq!(implied_chroma_format("video/x-h265"), None);
        assert_eq!(implied_chroma_format("video/x-vp9"), None);

        let ctx = SourceFormatContext {
            container: Some("video/webm".into()),
            codec: Some("video/x-vp8".into()),
            ..Default::default()
        };
        assert_eq!(
            classify_track(TrackClass::Video, &ctx, &DecodedFormat::default()),
            Verdict::Admitted
        );
    }

    #[test]
    fn codec_fixed_derivation_does_not_leak_to_other_codecs() {
        // 同样"字段全缺"的上下文换成 H.264：位深/采样格式由 profile 决定而不是编码名，
        // 所以必须继续显式拒绝，不能借 VP8 的推导混进来。
        let h264 = SourceFormatContext {
            container: Some("video/quicktime".into()),
            codec: Some("video/x-h264".into()),
            ..Default::default()
        };
        assert_eq!(
            classify_track(TrackClass::Video, &h264, &DecodedFormat::default())
                .rejection()
                .map(|rejection| rejection.code.clone()),
            Some("unknown_source_bit_depth".to_string())
        );
        // 有 profile（8-bit 可确定）但采样格式取不到：仍然拒绝，而不是补一个 4:2:0。
        let h264_profile = SourceFormatContext {
            profile: Some("high".into()),
            ..h264.clone()
        };
        assert_eq!(
            classify_track(TrackClass::Video, &h264_profile, &DecodedFormat::default())
                .rejection()
                .map(|rejection| rejection.code.clone()),
            Some("unknown_source_chroma_format".to_string())
        );
    }

    #[test]
    fn ten_bit_hevc_without_a_bit_depth_field_is_rejected_by_profile() {
        let ctx = SourceFormatContext {
            container: Some("video/quicktime".into()),
            codec: Some("video/x-h265".into()),
            profile: Some("main-10".into()),
            ..Default::default()
        };
        let verdict = classify_track(TrackClass::Video, &ctx, &DecodedFormat::default());
        assert_eq!(
            verdict.rejection().map(|rejection| rejection.code.clone()),
            Some("unsupported_bit_depth_10bit".to_string())
        );
        assert_eq!(verdict.rejection().unwrap().detail, "profile=main-10");
    }

    #[test]
    fn hdr_is_detected_in_the_numeric_colorimetry_form() {
        // 数字形式按 range:matrix:transfer:primaries 排列：2:6:14:7 = BT.2020 + PQ。
        // 只匹配 `bt2020` 这个名字会漏掉数字形式的 HDR。
        let ctx = SourceFormatContext {
            container: Some("video/quicktime".into()),
            codec: Some("video/x-h265".into()),
            profile: Some("main".into()),
            bit_depth: Some(8),
            colorimetry: Some("2:6:14:7".into()),
            ..Default::default()
        };
        assert_eq!(
            classify_track(TrackClass::Video, &ctx, &DecodedFormat::default())
                .rejection()
                .unwrap()
                .code,
            "unsupported_transfer_characteristics_pq"
        );
        let mut ctx = ctx.clone();
        ctx.colorimetry = Some("2:6:15:7".into());
        assert_eq!(
            classify_track(TrackClass::Video, &ctx, &DecodedFormat::default())
                .rejection()
                .unwrap()
                .code,
            "unsupported_transfer_characteristics_hlg"
        );
        let mut ctx = ctx.clone();
        ctx.colorimetry = Some("2:6:11:7".into());
        assert_eq!(
            classify_track(TrackClass::Video, &ctx, &DecodedFormat::default())
                .rejection()
                .unwrap()
                .code,
            "unsupported_color_primaries_bt2020"
        );
        // 预定义名字形式同样命中。
        let mut ctx = ctx.clone();
        ctx.colorimetry = Some("bt2100-hlg".into());
        assert_eq!(
            classify_track(TrackClass::Video, &ctx, &DecodedFormat::default())
                .rejection()
                .unwrap()
                .code,
            "unsupported_transfer_characteristics_hlg"
        );
    }

    #[test]
    fn sdr_and_unknown_colorimetry_are_not_treated_as_hdr() {
        // VP9 实测给出 2:0:0:0（range=16_235、其余 unknown）：不是 HDR，也不得据此拒绝。
        let mut ctx = ctx_video("video/x-h265");
        ctx.colorimetry = Some("2:0:0:0".into());
        assert_eq!(
            classify_track(TrackClass::Video, &ctx, &DecodedFormat::default()),
            Verdict::Admitted
        );
        for value in [
            "bt709",
            "bt601",
            "2:3:5:1",
            "smpte240m",
            "sRGB",
            "",
            "9:16:9:9",
        ] {
            assert!(
                colorimetry_rejection(value).is_none(),
                "{value} 不该被当成 HDR"
            );
        }
    }

    #[test]
    fn colorimetry_fields_parse_names_and_numbers() {
        // 名字形式
        assert_eq!(
            colorimetry_fields("bt709"),
            Colorimetry {
                primaries: Some(1),
                transfer: Some(5),
                matrix: Some(3)
            }
        );
        // 数字形式：range:matrix:transfer:primaries
        assert_eq!(
            colorimetry_fields("2:3:5:1"),
            Colorimetry {
                primaries: Some(1),
                transfer: Some(5),
                matrix: Some(3)
            }
        );
        assert_eq!(
            colorimetry_fields("2:0:0:0"),
            Colorimetry {
                primaries: Some(0),
                transfer: Some(0),
                matrix: Some(0)
            }
        );
        // 解析不出来就保持未知，不猜。
        assert_eq!(colorimetry_fields(""), Colorimetry::default());
        assert_eq!(
            colorimetry_fields("bt2100-pq:extra"),
            Colorimetry::default()
        );
    }
}

/// ADR-009 §3 的拒绝码命名表：码必须逐字对得上，否则报告里读到的就不是同一件事。
/// 这里只证明"判定 → 稳定码"的映射；真正触发这些码的端到端负样本在
/// `make capability-check`（合成素材只验拒绝路径，不作正样本证据）。
#[cfg(test)]
mod rejection_code_tests {
    use super::tests::ctx_video;
    use super::*;

    fn audio_ctx(codec: &str) -> SourceFormatContext {
        SourceFormatContext {
            container: Some("video/quicktime".into()),
            codec: Some(codec.into()),
            ..Default::default()
        }
    }

    fn code(verdict: Verdict) -> String {
        verdict
            .rejection()
            .unwrap_or_else(|| panic!("expected a rejection, got {verdict:?}"))
            .code
            .clone()
    }

    fn aac_ctx() -> SourceFormatContext {
        SourceFormatContext {
            container: Some("video/quicktime".into()),
            codec: Some("audio/mpeg".into()),
            mpegversion: Some(4),
            profile: Some("lc".into()),
            ..Default::default()
        }
    }

    #[test]
    fn promised_audio_codecs_are_admitted() {
        for codec in ["audio/x-opus", "audio/x-vorbis", "audio/x-raw"] {
            assert_eq!(
                classify_track(
                    TrackClass::Audio,
                    &audio_ctx(codec),
                    &DecodedFormat {
                        channels: 2,
                        ..Default::default()
                    }
                ),
                Verdict::Admitted,
                "{codec}"
            );
        }
    }

    #[test]
    fn av1_stays_experimental_and_is_rejected() {
        // AV1 在矩阵里是实验项：它不得因为"元素存在"而被当作承诺格式放行。
        assert_eq!(
            code(classify_track(
                TrackClass::Video,
                &ctx_video("video/x-av1"),
                &DecodedFormat::default()
            )),
            "unsupported_codec_av1"
        );
    }

    #[test]
    fn unpromised_codecs_get_a_readable_code() {
        // 已知短名走 KNOWN_CODECS，未知 caps 退化成 sanitize（仍是稳定码，不是自由文本）。
        let mut ctx = ctx_video("video/x-prores");
        ctx.bit_depth = Some(10);
        assert_eq!(
            code(classify_track(
                TrackClass::Video,
                &ctx,
                &DecodedFormat::default()
            )),
            "unsupported_codec_prores"
        );
        assert_eq!(
            code(classify_track(
                TrackClass::Audio,
                &audio_ctx("audio/x-flac"),
                &DecodedFormat {
                    channels: 2,
                    ..Default::default()
                }
            )),
            "unsupported_codec_flac"
        );
        assert_eq!(
            code(classify_track(
                TrackClass::Video,
                &ctx_video("video/x-cinepak"),
                &DecodedFormat::default()
            )),
            "unsupported_codec_video_x_cinepak"
        );
    }

    #[test]
    fn missing_source_codec_is_rejected_not_guessed() {
        for class in [TrackClass::Video, TrackClass::Audio] {
            let ctx = SourceFormatContext {
                container: Some("video/quicktime".into()),
                ..Default::default()
            };
            assert_eq!(
                code(classify_track(class, &ctx, &DecodedFormat::default())),
                "unknown_source_codec"
            );
        }
    }

    #[test]
    fn aac_must_be_lc_and_declare_its_object_type() {
        // 承诺的是 AAC-LC：不是 lc 就是另一个对象类型，取不到就不放行。
        let mut ctx = aac_ctx();
        ctx.profile = Some("main".into());
        assert_eq!(
            code(classify_track(
                TrackClass::Audio,
                &ctx,
                &DecodedFormat::default()
            )),
            "unsupported_codec_aac_main"
        );
        let mut ctx = aac_ctx();
        ctx.profile = None;
        assert_eq!(
            code(classify_track(
                TrackClass::Audio,
                &ctx,
                &DecodedFormat::default()
            )),
            "unknown_source_codec_profile"
        );
        // `audio/mpeg` 也可能根本没说自己是哪一代：MP3 与 AAC 的 mpegversion 不同。
        let mut ctx = aac_ctx();
        ctx.mpegversion = None;
        assert_eq!(
            code(classify_track(
                TrackClass::Audio,
                &ctx,
                &DecodedFormat::default()
            )),
            "unknown_source_codec_mpegversion"
        );
    }

    #[test]
    fn chroma_must_be_420_and_measured() {
        let mut ctx = ctx_video("video/x-h265");
        ctx.chroma = Some("4:2:2".into());
        assert_eq!(
            code(classify_track(
                TrackClass::Video,
                &ctx,
                &DecodedFormat::default()
            )),
            "unsupported_chroma_format_4_2_2"
        );
        ctx.chroma = Some("4:4:4".into());
        assert_eq!(
            code(classify_track(
                TrackClass::Video,
                &ctx,
                &DecodedFormat::default()
            )),
            "unsupported_chroma_format_4_4_4"
        );
        // 取不到采样格式就不放行（宁拒不放），而不是当成 4:2:0。
        ctx.chroma = None;
        assert_eq!(
            code(classify_track(
                TrackClass::Video,
                &ctx,
                &DecodedFormat::default()
            )),
            "unknown_source_chroma_format"
        );
    }

    #[test]
    fn unmeasured_channel_count_is_rejected() {
        // 声道数来自实测 raw caps；0 表示没测到，不能当成 mono。
        assert_eq!(
            code(classify_track(
                TrackClass::Audio,
                &aac_ctx(),
                &DecodedFormat::default()
            )),
            "unknown_channel_layout"
        );
    }

    #[test]
    fn second_track_of_a_kind_gets_the_layout_code() {
        assert_eq!(
            track_layout_rejection(TrackClass::Video).code,
            "unsupported_track_layout_multiple_video"
        );
        assert_eq!(
            track_layout_rejection(TrackClass::Audio).code,
            "unsupported_track_layout_multiple_audio"
        );
    }

    #[test]
    fn non_media_pads_get_a_stable_code_instead_of_a_log_line() {
        // 实测 B：这类 pad 过去只写日志，报告读起来像"流里没有这条轨道"。
        assert_eq!(
            pad_media_rejection("text/x-raw").code,
            "unsupported_media_type_text_x_raw"
        );
        assert_eq!(
            pad_media_rejection("application/x-subtitle").code,
            "unsupported_media_type_application_x_subtitle"
        );
        assert_eq!(
            pad_media_rejection("").code,
            "unsupported_media_type_unknown"
        );
    }
}
