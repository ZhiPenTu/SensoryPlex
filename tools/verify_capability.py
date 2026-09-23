"""ADR-009 媒体格式准入验收：承诺矩阵内的样本不被误拒，矩阵外一律拿到稳定拒绝码。

这个脚本只做两件事，两件都必须有真实执行结果：

1. **正样本对照**：仓库登记的授权样本必须 `rejected=0`，并且每条被解码轨道都要给出
   源侧证据（`source_codec` / `source_bit_depth` / `source_chroma_format` /
   `decoder_element` / `frame_rate_mode`）。缺哪一项，"这个格式在这台机器可用"就不成立。
2. **拒绝路径**：矩阵外的输入必须显式拒绝，且拒绝码逐字等于 ADR-009 §3 的命名表。
   负样本由 FFmpeg 现场合成（10-bit、5.1、AVI、裸 ES、字幕、双视频轨、4:2:2、MP3），
   **只证明拒绝路径**，不得当作任何正样本证据（见 AGENTS.md 与 ADR-009 §4）。

判定标准同样是实现真正给出的语义，而不是脚本的期望值：拒绝码出现次数、命令行
`rejected=` 与报告 `rejected_tracks` 必须自洽，被拒轨道必须带上容器上下文，
而且**绝不能**以 `decode_stalled` 收尾——"有结论的拒绝"与"卡住"是两件事。
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from edge_material_sdk.generated.media.v1 import media_pb2

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "config/pipelines/file-material.yaml"
SAMPLES = ROOT / "video/samples"


@dataclass(frozen=True)
class PositiveSample:
    """登记在册的授权正样本 + 它自己的证据口径（见 tests/fixtures/media/OPEN-SAMPLES.md）。"""

    path: Path
    # 容器是否声明了帧率。逐样本声明，而不是把断言放宽：2012 年的 WebM 原始上传没有
    # `DefaultDuration`，报告里的帧率模式必然未知——那是源的实测事实，不是缺失的证据；
    # 但它也绝不能被写成"25/1 恒定"。
    declares_frame_rate: bool
    note: str
    # 源里的音频是不是"容器直接存 raw 采样"（MOV 里的 PCM）：这类轨道没有 parser/decoder，
    # 报告必须写明 `demuxer_passthrough`，不能留空串（空串的含义是"没能归因到解码器"）。
    container_raw_audio: bool = False


# 仓库已登记的授权正样本（见 tests/fixtures/media/OPEN-SAMPLES.md）。
POSITIVE_SAMPLES = (
    PositiveSample(ROOT / "video/1.mp4", True, "HEVC/AAC MP4"),
    PositiveSample(SAMPLES / "screencast-watchlist.480p.vp9.webm", True, "VP9/Opus WebM"),
    PositiveSample(SAMPLES / "sintel-trailer.480p.h264.mp4", True, "H.264/AAC MP4"),
    PositiveSample(
        SAMPLES / "editing-basics-sandboxes.vp8.webm",
        False,
        "VP8/Vorbis WebM（容器未声明帧率）",
    ),
    PositiveSample(
        SAMPLES / "mpegts-h264-aac.live-recording.ts",
        True,
        "H.264/AAC 文件形态 MPEG-TS",
    ),
    PositiveSample(
        SAMPLES / "conger-conger.h264-pcm.mov",
        True,
        "H.264/容器内 PCM（pcm_s16le）MOV",
        container_raw_audio=True,
    ),
)
# 报告里的拒绝码不允许出现在这里：它们说明的是"没跑通"，不是"拒绝了"。
STALL_CODE = "decode_stalled"
# 源采样由 demuxer 直出时，报告里"没有解码元素"的显式取值（`capability::NO_DECODER_ELEMENT`）。
CONTAINER_RAW_DECODER = "demuxer_passthrough"
RUN_TIMEOUT_S = 600.0
POSITIVE_MAX_POINTS = 400
NEGATIVE_MAX_POINTS = 60
# 合成负样本需要的编码器。缺任何一个都显式失败：不允许把"没测"读成"通过"。
# `vorbis` 是 FFmpeg 自带的实验性编码器（调用时需要 `-strict -2`），它一直都在，
# 这里仍然显式要求：缺了就必须报错，而不是静默少测一条拒绝路径。
REQUIRED_ENCODERS = (
    "libx264",
    "libx265",
    "libvpx",
    "libvpx-vp9",
    "aac",
    "ac3",
    "libmp3lame",
    "vorbis",
)
FRAME_RATE_MODE_UNKNOWN = media_pb2.FRAME_RATE_MODE_UNKNOWN


def runtime_binary() -> Path:
    for profile in ("release", "debug"):
        candidate = ROOT / f"target/{profile}/sensoryplex-runtime"
        if candidate.is_file():
            return candidate
    raise SystemExit("build the runtime first: make capability-check builds it for you")


def require_ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise SystemExit("ffmpeg is required to synthesize the rejection-path samples")
    listing = subprocess.run(
        [binary, "-hide_banner", "-encoders"], capture_output=True, text=True, check=True
    ).stdout
    missing = [name for name in REQUIRED_ENCODERS if f" {name} " not in listing]
    if missing:
        raise SystemExit(f"ffmpeg is missing encoders needed for the negative samples: {missing}")
    return binary


def sample(path: Path) -> Path:
    if not path.is_file():
        raise SystemExit(
            f"registered sample missing (see tests/fixtures/media/OPEN-SAMPLES.md): {path}"
        )
    return path


def registered(entry: PositiveSample) -> Path:
    return sample(entry.path)


class Checks:
    """收集断言结果：每一项都打印出来，不做静默跳过。"""

    def __init__(self, name: str):
        self.name = name
        self.failures: list[str] = []
        self.checks: list[str] = []

    def check(self, condition: bool, description: str) -> bool:
        if condition:
            self.checks.append(description)
        else:
            self.failures.append(description)
        print(f"  [{'ok' if condition else 'FAIL'}] {description}")
        return bool(condition)

    def finish(self) -> bool:
        status = "PASS" if not self.failures else f"FAIL ({len(self.failures)})"
        print(f"== {self.name}: {status} ==")
        return not self.failures


def load_report(path: Path) -> media_pb2.ReplayReport:
    report = media_pb2.ReplayReport()
    report.ParseFromString(path.read_bytes())
    return report


def parse_fields(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in line.strip().split():
        key, separator, value = token.partition("=")
        if separator:
            fields[key] = value
    return fields


def report_line(stdout: str) -> dict[str, str]:
    for line in stdout.splitlines():
        if line.startswith("replay report written:"):
            return parse_fields(line.split(":", 1)[1])
    raise SystemExit(f"no replay report line in runtime output:\n{stdout}")


def replay(
    media: Path, report_path: Path, max_points: int
) -> tuple[subprocess.CompletedProcess, dict]:
    completed = subprocess.run(
        [
            str(runtime_binary()),
            "replay",
            str(PIPELINE),
            str(media),
            "--report",
            str(report_path),
            "--max-points",
            str(max_points),
        ],
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_S,
    )
    # 报告行缺失时，退出码与 stderr 是唯一能说明"到底发生了什么"的证据：
    # 静默的失败必须留下可诊断的痕迹，而不是只报"没看到那一行"。
    if "replay report written:" not in completed.stdout:
        raise SystemExit(
            f"runtime produced no replay report for {media}: "
            f"returncode={completed.returncode}\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        )
    return completed, report_line(completed.stdout)


def ffmpeg_run(ffmpeg: str, args: list[str]) -> None:
    completed = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", *args], capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise SystemExit(f"ffmpeg failed: {' '.join(args)}\n{completed.stderr}")


def synthesize(
    ffmpeg: str, workspace: Path, authorized: Path
) -> list[tuple[str, Path, list[tuple[str, str]], set[str]]]:
    """现场合成拒绝路径负样本。

    返回 `(场景名, 文件, 期望的 (track_kind, code) 集合, 期望仍然被解码的轨道种类)`。
    全部由 FFmpeg 合成：它们只用于验证拒绝路径，不产生任何"格式可用"的结论。
    """
    base = workspace / "base.mp4"
    sub = workspace / "sub.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nsynthetic subtitle\n")
    # 基础片段来自已授权样本（只重封装，不重编码），作为"承诺容器 + 承诺编码"的对照组。
    ffmpeg_run(ffmpeg, ["-i", str(authorized), "-t", "2", "-c", "copy", str(base)])

    cases: list[tuple[str, Path, list[tuple[str, str]], set[str]]] = []

    ten_bit = workspace / "ten-bit-hevc.mp4"
    ffmpeg_run(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=10:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx265",
            "-pix_fmt",
            "yuv420p10le",
            "-tag:v",
            "hvc1",
            "-c:a",
            "aac",
            "-ac",
            "2",
            "-shortest",
            str(ten_bit),
        ],
    )
    cases.append(
        (
            "ten_bit_source_is_rejected_before_the_8bit_raw_caps",
            ten_bit,
            [("video", "unsupported_bit_depth_10bit")],
            {"audio"},
        )
    )

    surround = workspace / "five-one.m4a"
    ffmpeg_run(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-af",
            "pan=5.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0",
            "-c:a",
            "aac",
            "-ac",
            "6",
            str(surround),
        ],
    )
    cases.append(
        (
            "multichannel_audio_is_rejected_by_measured_channels",
            surround,
            [("audio", "unsupported_channel_layout_multichannel")],
            set(),
        )
    )

    avi = workspace / "legacy.avi"
    ffmpeg_run(ffmpeg, ["-i", str(base), "-c", "copy", str(avi)])
    cases.append(
        (
            "avi_container_is_rejected_for_both_tracks",
            avi,
            [("video", "unsupported_container_avi"), ("audio", "unsupported_container_avi")],
            set(),
        )
    )

    raw = workspace / "raw.h265"
    ffmpeg_run(
        ffmpeg,
        [
            "-i",
            str(authorized),
            "-t",
            "2",
            "-c:v",
            "libx265",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-f",
            "hevc",
            str(raw),
        ],
    )
    cases.append(
        (
            "raw_elementary_stream_is_not_read_as_an_unknown_container",
            raw,
            [("video", "unsupported_container_raw_es")],
            set(),
        )
    )

    movie = workspace / "with-subtitle.mp4"
    ffmpeg_run(
        ffmpeg, ["-i", str(base), "-i", str(sub), "-c", "copy", "-c:s", "mov_text", str(movie)]
    )
    cases.append(
        (
            "non_media_pad_is_counted_instead_of_logged",
            movie,
            [("other", "unsupported_media_type_text_x_raw")],
            {"video", "audio"},
        )
    )

    two_video = workspace / "two-video.mp4"
    ffmpeg_run(
        ffmpeg,
        [
            "-i",
            str(base),
            "-i",
            str(base),
            "-map",
            "0:v",
            "-map",
            "1:v",
            "-map",
            "0:a",
            "-c",
            "copy",
            str(two_video),
        ],
    )
    cases.append(
        (
            "second_video_track_is_a_track_layout_rejection",
            two_video,
            [("video", "unsupported_track_layout_multiple_video")],
            {"video", "audio"},
        )
    )

    chroma = workspace / "chroma-422.mp4"
    ffmpeg_run(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=10:duration=2",
            "-c:v",
            "libx265",
            "-pix_fmt",
            "yuv422p",
            "-tag:v",
            "hvc1",
            "-an",
            str(chroma),
        ],
    )
    cases.append(
        (
            "chroma_other_than_420_is_rejected",
            chroma,
            [("video", "unsupported_chroma_format_4_2_2")],
            set(),
        )
    )

    mp3 = workspace / "mp3-in-mp4.mp4"
    ffmpeg_run(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            "-f",
            "mp4",
            str(mp3),
        ],
    )
    cases.append(("mp3_is_not_read_as_aac", mp3, [("audio", "unsupported_codec_mp3")], set()))

    # H.264 行的相邻负样本：同编码的 10-bit 变体必须被位深判据挡下，而不是降成 8-bit。
    h264_10bit = workspace / "h264-high10.mp4"
    ffmpeg_run(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=10:duration=2",
            "-c:v",
            "libx264",
            "-profile:v",
            "high10",
            "-pix_fmt",
            "yuv420p10le",
            "-an",
            str(h264_10bit),
        ],
    )
    cases.append(
        (
            "h264_10bit_is_rejected_by_profile",
            h264_10bit,
            [("video", "unsupported_bit_depth_10bit")],
            set(),
        )
    )

    # VP8 行的相邻负样本：VP8 落在不承诺的容器里，仍然由容器判据挡下。
    vp8_avi = workspace / "vp8-in-avi.avi"
    ffmpeg_run(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=10:duration=2",
            "-c:v",
            "libvpx",
            "-b:v",
            "300k",
            "-an",
            "-f",
            "avi",
            str(vp8_avi),
        ],
    )
    cases.append(
        (
            "vp8_in_an_unsupported_container_is_rejected",
            vp8_avi,
            [("video", "unsupported_container_avi")],
            set(),
        )
    )

    # Vorbis 行的相邻负样本：Vorbis 的母容器 Ogg 不在承诺矩阵内（容器判据先于编码判据）。
    # 这里用 FFmpeg 现场编码（`vorbis` 编码器是实验性的，需要 `-strict -2`），而不是把 WebM 里的
    # Vorbis **重封装**进 Ogg：重封装会保留源的 pre-skip，首帧 PTS 变成 -0.0005，撞上参考探针
    # "拒绝而不 clamp"的既有策略（`invalid_probe_output`，见 docs/verification.md 的 M9 边界）。
    # 那是报告之前的失败，不是准入拒绝，不能拿它当这条负样本。
    vorbis_ogg = workspace / "vorbis-in-ogg.oga"
    ffmpeg_run(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:a",
            "vorbis",
            "-strict",
            "-2",
            "-ac",
            "2",
            str(vorbis_ogg),
        ],
    )
    cases.append(
        (
            "vorbis_in_ogg_is_rejected_by_container",
            vorbis_ogg,
            [("audio", "unsupported_container_ogg")],
            set(),
        )
    )

    # MPEG-TS 行的相邻负样本：容器承诺不等于编码承诺——TS 里的 AC-3 必须被编码判据挡下，
    # 而同一条流里的 H.264 视频轨照常解码。
    ts_ac3 = workspace / "mpegts-ac3.ts"
    ffmpeg_run(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=10:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "ac3",
            "-ac",
            "2",
            "-shortest",
            "-f",
            "mpegts",
            str(ts_ac3),
        ],
    )
    cases.append(
        (
            "mpegts_container_does_not_promise_ac3",
            ts_ac3,
            [("audio", "unsupported_codec_ac3")],
            {"video"},
        )
    )

    # PCM 行的边界：矩阵承诺的是"容器里的 PCM"，不是 WAV 这个容器本身。
    # 正样本是 `video/samples/conger-conger.h264-pcm.mov`（H.264 + pcm_s16le，见 OPEN-SAMPLES.md），
    # 这条只证明边界的方向：换了容器就不再承诺。
    pcm_wav = workspace / "pcm-in-wav.wav"
    ffmpeg_run(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:a",
            "pcm_s16le",
            "-ac",
            "2",
            str(pcm_wav),
        ],
    )
    cases.append(
        (
            "pcm_outside_a_promised_container_is_rejected",
            pcm_wav,
            [("audio", "unsupported_container_wav")],
            set(),
        )
    )

    return cases


def check_rejection_consistency(
    checks: Checks, fields: dict, report: media_pb2.ReplayReport
) -> None:
    rejected = list(report.decoded.rejected_tracks)
    checks.check(
        fields.get("rejected") == str(len(rejected)),
        f"命令行与报告给出同一个拒绝条数：cli={fields.get('rejected')} report={len(rejected)}",
    )
    seen = [(track.track_kind, track.code) for track in rejected]
    checks.check(len(seen) == len(set(seen)), f"每条被拒轨道只出现一次：{seen}")
    for track in rejected:
        checks.check(
            bool(track.detail),
            f"{track.track_kind}/{track.code} 带上观测值：detail={track.detail!r}",
        )
        checks.check(
            bool(track.container),
            f"{track.track_kind}/{track.code} 带上容器上下文：container={track.container!r}",
        )


def scenario_positive(entry: PositiveSample, workspace: Path) -> bool:
    name = f"positive:{entry.path.name}"
    media = registered(entry)
    checks = Checks(f"{name} [{entry.note}]")
    report_path = workspace / f"{name}.pb"
    completed, fields = replay(media, report_path, POSITIVE_MAX_POINTS)
    report = load_report(report_path)
    checks.check(completed.returncode == 0, "承诺矩阵内的样本正常收尾")
    checks.check(STALL_CODE not in completed.stdout, "没有把正常回放读成卡住")
    check_rejection_consistency(checks, fields, report)
    checks.check(not report.decoded.rejected_tracks, "承诺矩阵内的样本没有被拒绝的轨道")
    checks.check(report.decoded.descriptors_validated > 0, "真的解码并校验了 descriptor")
    tracks = list(report.decoded.tracks)
    video = [track for track in tracks if track.track_kind == "video" and track.samples > 0]
    checks.check(bool(video), "至少一条视频轨道真的产出了样本")
    for track in tracks:
        if track.samples == 0:
            # 源里没有这种轨道时不会产出样本，这里不替它编造结论。
            continue
        checks.check(
            bool(track.source_codec), f"{track.track_kind} 记录了源编码：{track.source_codec}"
        )
        if track.track_kind == "audio" and entry.container_raw_audio:
            # 容器里存的就是 raw 采样：报告必须**写明**没有解码元素。留空串会被读成
            # "这次没能归因"，与"本来就没有解码器"是两件事。
            checks.check(
                track.decoder_element == CONTAINER_RAW_DECODER,
                f"{track.track_kind} 的源采样由 demuxer 直出，写明没有解码元素："
                f"{track.decoder_element!r}",
            )
        else:
            checks.check(
                bool(track.decoder_element),
                f"{track.track_kind} 记录了实际解码元素：{track.decoder_element}",
            )
    for track in video:
        checks.check(
            track.HasField("source_bit_depth") and track.source_bit_depth == 8,
            f"记录的是源位深 8-bit：{track.source_bit_depth}",
        )
        checks.check(
            track.source_chroma_format == "4:2:0",
            f"记录的是源采样格式：{track.source_chroma_format!r}",
        )
        if entry.declares_frame_rate:
            checks.check(
                track.frame_rate_mode != FRAME_RATE_MODE_UNKNOWN
                and track.declared_frame_rate_num > 0,
                f"帧率模式不是未知：mode={track.frame_rate_mode} declared="
                f"{track.declared_frame_rate_num}/{track.declared_frame_rate_den}",
            )
        else:
            # 容器没声明帧率：报告必须如实写"未知"，而不是补一个看起来确定的恒定帧率。
            checks.check(
                track.frame_rate_mode == FRAME_RATE_MODE_UNKNOWN
                and track.declared_frame_rate_num == 0,
                f"源没有声明帧率时如实报告未知（不补恒定值）：mode={track.frame_rate_mode} "
                f"declared={track.declared_frame_rate_num}/{track.declared_frame_rate_den}",
            )
        checks.check(
            not track.HasField("applied_rotation_deg"),
            "v1 不声明已应用旋转：applied_rotation_deg 必须缺省",
        )
    for track in tracks:
        checks.check(
            not track.HasField("applied_rotation_deg"),
            f"{track.track_kind} 没有伪造 0 度旋转",
        )
    return checks.finish()


def scenario_negative(
    name: str,
    media: Path,
    expected: list[tuple[str, str]],
    still_decoded: set[str],
    workspace: Path,
) -> bool:
    checks = Checks(name)
    report_path = workspace / f"{name}.pb"
    completed, fields = replay(media, report_path, NEGATIVE_MAX_POINTS)
    report = load_report(report_path)
    checks.check(completed.returncode == 0, "显式拒绝不是运行失败")
    checks.check(STALL_CODE not in completed.stdout, "拒绝不是卡住：没有 decode_stalled")
    check_rejection_consistency(checks, fields, report)
    observed = sorted((track.track_kind, track.code) for track in report.decoded.rejected_tracks)
    checks.check(observed == sorted(expected), f"拒绝码逐字等于 ADR-009 §3 的命名表：{observed}")
    decoded_kinds = {track.track_kind for track in report.decoded.tracks if track.samples > 0}
    checks.check(
        decoded_kinds == still_decoded or not still_decoded,
        f"同一条流里没被拒的轨道照常解码：decoded={sorted(decoded_kinds)}",
    )
    if not still_decoded:
        checks.check(
            report.decoded.descriptors_validated == 0,
            "全部轨道被拒时没有 descriptor 被放行",
        )
    return checks.finish()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--media",
        default=str(POSITIVE_SAMPLES[0].path),
        help="授权正样本（默认仓库登记的第一个）",
    )
    parser.add_argument(
        "--max-points", type=int, default=POSITIVE_MAX_POINTS, help="正样本的样本预算"
    )
    args = parser.parse_args()

    ffmpeg = require_ffmpeg()
    authorized = sample(Path(args.media))
    # 命令行指定的样本若不在登记表里，就顶掉第一个登记样本（口径按"容器声明帧率"从严），
    # 其余登记样本照跑：换一个样本不会把已有证据一起顶掉。
    entries = [entry for entry in POSITIVE_SAMPLES if entry.path == authorized] or [
        PositiveSample(authorized, True, "命令行指定，未登记")
    ]
    entries += [entry for entry in POSITIVE_SAMPLES if entry.path != authorized]

    results: list[bool] = []
    with tempfile.TemporaryDirectory(prefix="sensoryplex-capability-") as raw:
        workspace = Path(raw)
        for entry in entries:
            results.append(scenario_positive(entry, workspace))
        print("\n以下负样本由 FFmpeg 现场合成：它们只验证拒绝路径，不是任何格式的可用性证据。")
        for name, media, expected, still_decoded in synthesize(ffmpeg, workspace, authorized):
            results.append(scenario_negative(name, media, expected, still_decoded, workspace))

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
