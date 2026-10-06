"""SRT 实时接入验收：真实授权样本 → GStreamer `srtsink` 直推 → Runtime `ingest`。

发布端由本脚本自己拉起（GStreamer 直接出 SRT，不是 RTMP 转封装），因此验收不依赖
OBS 是否空闲。若 MediaMTX 上已经有别的发布者（例如正在直播的 OBS），脚本会拒绝运行，
不去挤掉别人的会话（配置里 `overridePublisher: false` 也不允许）。

判定标准（全部是真实执行结果，不用健康检查冒充）：
- 稳定窗口：`samples > 0`、`stalls == 0`、`blockers` 为空、`golden_path_verified` 恒为 false，
  并且 `source.duration_ms == 0`、`content_hash` 为空——直播没有已知时长与内容摘要；
- 断流恢复：发布端被 SIGINT 后再拉起，必须测到 `stalls >= 1`、`stalled_ms > 0`、`recovered`，
  且明细里至少一条 `stream_gap`；重连归属必须写成解码元素；
- 无源：没有任何发布者时必须非 0 退出并写出显式原因，绝不"成功但为空"；
- URI 只从环境变量读：命令行、stdout 与报告里都不出现 URI 或 streamid；
- 同一趟数据面：`--handoff-listen` 下由**独立进程**按 lease 读取并释放，账目对得上；
- 背压：有保留队列的直播窗口必须报出 `observed=true`、三条队列水位、按种类拆分的拒绝，
  且拒绝只落在容量类原因码上；没有保留队列的窗口必须写 `observed=false`（没测过，
  不是"零压力"）。降级（先降低非关键帧采样率）必须早于拒绝出现。
"""

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from edge_material_sdk.generated.media.v1 import live_pb2

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "config/pipelines/srt-live.yaml"
WORKER = ROOT / "tools/handoff_worker.py"
# 覆盖类别为"翻页/界面突变"的 552 秒公有许可样本：够长，可以覆盖断流后重启发布端。
DEFAULT_SAMPLE = ROOT / "video/samples/screencast-video2commons.480p.vp9.webm"
# 与仓库登记一致的读取 URI（`read:` 不需要凭据；`publish:` 才需要）。
DEFAULT_URI = "srt://127.0.0.1:8890?streamid=read:live/obs"
DEFAULT_PUBLISH_URI = "srt://127.0.0.1:8890"
DEFAULT_PUBLISH_STREAMID = "publish:live/obs"
METRICS_URL = "http://127.0.0.1:9998/metrics"
PATH_NAME = "live/obs"
READY_TIMEOUT_S = 45.0
STOP_TIMEOUT_S = 10.0
# 发布端视频编码器。`videotoolbox` 用的是 OBS 同类的 Apple 硬件编码器：
# 现场 OBS 直推暴露的失败模式（码流不带 timing → 接收端 buffer 没有 duration →
# 旧实现把整条视频轨丢掉）必须在脚本里能复现，否则只能靠手工回归。
VIDEO_ENCODERS = {
    "x264": [
        "x264enc",
        "tune=zerolatency",
        "speed-preset=ultrafast",
        "key-int-max=60",
        "bitrate=2500",
    ],
    "videotoolbox": [
        "vtenc_h264",
        "realtime=true",
        "allow-frame-reordering=false",
        "max-keyframe-interval=60",
        "bitrate=2500",
    ],
}
# 报告里绝不允许出现的字符串：URI 可能带凭据，只能以引用名出现。
FORBIDDEN_IN_REPORT = ("srt://", "127.0.0.1", "streamid", ":8890")

STATE_PATTERN = re.compile(r'^paths\{name="' + re.escape(PATH_NAME) + r'",state="([^"]+)"\}', re.M)
BYTES_PATTERN = re.compile(
    r'^paths_inbound_bytes\{name="' + re.escape(PATH_NAME) + r'",state="[^"]+"\} (\d+)', re.M
)
# 保留期的有界容量拒绝码：保留表满、单一 buffer 种类到配额、共享段满。
CAPACITY_REASONS = {"handoff_backlog_full", "handoff_kind_quota_full", "arena_capacity_exceeded"}
RETENTION_QUEUES = {"handoff_retained_table", "handoff_retained_kind", "handoff_arena_bytes"}
TABLE_QUEUE = "handoff_retained_table"
KIND_QUEUE = "handoff_retained_kind"


def runtime_binary() -> Path:
    for profile in ("release", "debug"):
        candidate = ROOT / f"target/{profile}/sensoryplex-runtime"
        if candidate.is_file():
            return candidate
    raise SystemExit("build the runtime first: make live-check builds it for you")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


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


def metrics() -> tuple[str, int]:
    """读取 MediaMTX 的路径状态与入站字节数。拿不到指标就是硬失败，不做默认值兜底。"""
    try:
        with urllib.request.urlopen(METRICS_URL, timeout=3) as response:
            text = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as error:
        raise SystemExit(
            f"MediaMTX metrics unreachable ({METRICS_URL}): {error}"
            "\n前端接入层没起来？先执行 make stream-up。"
        ) from error
    state = STATE_PATTERN.search(text)
    inbound = BYTES_PATTERN.search(text)
    if state is None or inbound is None:
        raise SystemExit(f"metrics for path {PATH_NAME} missing; is the config current?")
    return state.group(1), int(inbound.group(1))


class Publisher:
    """用 GStreamer `srtsink` 把授权样本**直推** SRT（不经 RTMP、不经 MediaMTX 转码）。"""

    def __init__(self, sample: Path, log_path: Path, encoder: str = "x264"):
        self.sample = sample
        self.log_path = log_path
        self.encoder = encoder
        self.process: subprocess.Popen | None = None
        self.log = None

    def start(self) -> None:
        environment = dict(os.environ)
        # VP9 硬解在 macOS 上出 GLMemory，`videoconvert` 接不上；这里固定用软件解码，
        # 只影响发布端的测试编码，与 Runtime 的读取链路无关。
        environment["GST_PLUGIN_FEATURE_RANK"] = "vtdec_hw:0,vtdec:0"
        self.log = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [
                "gst-launch-1.0",
                "-e",
                "filesrc",
                f"location={self.sample}",
                "!",
                "decodebin",
                "name=d",
                "d.",
                "!",
                "queue",
                "max-size-buffers=60",
                "!",
                "videoconvert",
                "!",
                "videoscale",
                "!",
                "video/x-raw,format=I420",
                "!",
                *VIDEO_ENCODERS[self.encoder],
                "!",
                "h264parse",
                "!",
                "queue",
                "!",
                "mux.",
                "d.",
                "!",
                "queue",
                "max-size-buffers=200",
                "!",
                "audioconvert",
                "!",
                "audioresample",
                "!",
                "audio/x-raw,rate=48000,channels=2",
                "!",
                "avenc_aac",
                "bitrate=128000",
                "!",
                "aacparse",
                "!",
                "queue",
                "!",
                "mux.",
                "mpegtsmux",
                "name=mux",
                "!",
                "srtsink",
                f"uri={DEFAULT_PUBLISH_URI}",
                f"streamid={DEFAULT_PUBLISH_STREAMID}",
                "max-bitrate=4000000",
            ],
            env=environment,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )

    def wait_ready(self, timeout_s: float = READY_TIMEOUT_S) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise SystemExit(self.failure("publisher exited before the path became ready"))
            state, inbound = metrics()
            if state == "ready" and inbound > 0:
                return
            time.sleep(0.5)
        raise SystemExit(self.failure(f"path {PATH_NAME} not ready within {timeout_s}s"))

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=STOP_TIMEOUT_S)
        self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def failure(self, message: str) -> str:
        tail = ""
        if self.log_path.is_file():
            tail = self.log_path.read_text(errors="replace")[-1500:]
        return f"{message}\n--- publisher log tail ---\n{tail}"


def run_ingest(
    report: Path,
    duration_ms: int,
    uri: str,
    extra: list[str] | None = None,
    stall_threshold_ms: int = 1000,
) -> subprocess.CompletedProcess:
    command = [
        str(runtime_binary()),
        "ingest",
        str(PIPELINE),
        "--report",
        str(report),
        "--duration-ms",
        str(duration_ms),
        "--stall-threshold-ms",
        str(stall_threshold_ms),
        *(extra or []),
    ]
    # URI 只经环境变量传入：进程参数里不得出现它。
    assert uri not in " ".join(command), "the URI must never travel on the command line"
    environment = dict(os.environ)
    environment["SENSORYPLEX_SRT_LIVE_URI"] = uri
    return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=300)


def read_report(path: Path) -> tuple[live_pb2.LiveIngestReport, bytes]:
    raw = path.read_bytes()
    report = live_pb2.LiveIngestReport()
    report.ParseFromString(raw)
    return report, raw


def parse_fields(line: str) -> dict[str, str]:
    """把运行时打印的一行 `key=value ...` 拆成字典；`=` 之后的内容原样保留。"""
    return dict(token.split("=", 1) for token in line.split(" ") if "=" in token)


def find_line(lines: list[str], prefix: str) -> str | None:
    return next((line for line in lines if line.startswith(prefix)), None)


def read_queues(rendered: str) -> dict[str, tuple[int, int, int]]:
    """解析 `name[unit]=current/peak/capacity,...`。"""
    queues: dict[str, tuple[int, int, int]] = {}
    for entry in filter(None, rendered.split(",")):
        name, _, watermarks = entry.partition("=")
        current, peak, capacity = (int(value) for value in watermarks.split("/"))
        queues[name.split("[")[0]] = (current, peak, capacity)
    return queues


def assert_retention_backpressure(
    checks: Checks, report: live_pb2.LiveIngestReport
) -> dict[str, tuple[int, int, int]]:
    """保留队列的背压可观察量：三条队列、自证有界、拒绝只落在容量类原因码上。"""
    backpressure = report.decoded.backpressure
    checks.check(backpressure.observed, "这次直播窗口真的有可测量的有界队列")
    queues = {
        queue.name: (queue.current, queue.peak, queue.capacity) for queue in backpressure.queues
    }
    checks.check(set(queues) == RETENTION_QUEUES, f"三条队列都给容量与水位：{sorted(queues)}")
    for name, (current, peak, capacity) in queues.items():
        checks.check(
            capacity > 0 and peak > 0 and peak <= capacity and current <= peak,
            f"{name} 自证有界：current={current} peak={peak} capacity={capacity}",
        )
    if TABLE_QUEUE in queues and KIND_QUEUE in queues:
        checks.check(
            queues[KIND_QUEUE][2] == max(1, queues[TABLE_QUEUE][2] // 2),
            "按种类的上限 = 保留表容量的一半：谁也不能独占窗口",
        )
    summed = sum(entry.count for entry in backpressure.drop_reasons)
    by_kind = sum(entry.count for entry in backpressure.drop_kinds)
    checks.check(
        summed == backpressure.dropped_total == by_kind,
        f"丢弃按原因与种类都对得上：dropped={backpressure.dropped_total} "
        f"reasons={summed} kinds={by_kind}",
    )
    reasons = {entry.reason for entry in backpressure.drop_reasons}
    checks.check(
        bool(reasons) and reasons <= CAPACITY_REASONS, f"拒绝原因都是容量类：{sorted(reasons)}"
    )
    checks.check(
        backpressure.state in {"degraded", "saturated"},
        f"结束时的状态不是 ok：{backpressure.state}",
    )
    checks.check(
        backpressure.degraded_entries + backpressure.saturated_entries > 0,
        f"状态迁移被计数：degraded={backpressure.degraded_entries} "
        f"saturated={backpressure.saturated_entries}",
    )
    return queues


def scenario_steady(sample: Path, workspace: Path, duration_ms: int, uri: str) -> bool:
    checks = Checks("steady")
    publisher = Publisher(sample, workspace / "steady-publisher.log")
    publisher.start()
    try:
        publisher.wait_ready()
        report_path = workspace / "steady-report.pb"
        result = run_ingest(report_path, duration_ms, uri)
        checks.check(
            result.returncode == 0,
            f"ingest exit 0 (got {result.returncode}): {result.stderr[-400:]}",
        )
        report, raw = read_report(report_path)
        checks.check(report.platform == "macos-aarch64", f"platform={report.platform}")
        checks.check(report.stream.samples > 0, f"samples={report.stream.samples} > 0")
        checks.check(
            report.stream.stalls == 0, f"stalls={report.stream.stalls} == 0 in a steady window"
        )
        checks.check(report.stream.stalled_ms == 0, f"stalled_ms={report.stream.stalled_ms} == 0")
        checks.check(report.stream.recovered is False, "recovered stays false when nothing stalled")
        checks.check(
            report.stream.ended_by_deadline is True, "window ended by deadline, not by failure"
        )
        checks.check(
            report.stream.reconnect_owner == "srtsrc auto-reconnect",
            f"reconnect_owner={report.stream.reconnect_owner!r} "
            "(measured, not controlled, by this process)",
        )
        checks.check(
            abs(report.stream.elapsed_ms - duration_ms) < 3000,
            f"elapsed_ms={report.stream.elapsed_ms} tracks the requested {duration_ms} ms",
        )
        checks.check(
            report.stream.uri_secret_ref == "SRT_LIVE_URI",
            "report carries the reference name, not the URI",
        )
        checks.check(
            report.decoded.descriptors_validated > 0,
            f"descriptors_validated={report.decoded.descriptors_validated}",
        )
        checks.check(
            report.decoded.descriptor_failures == 0,
            f"descriptor_failures={report.decoded.descriptor_failures}",
        )
        checks.check(
            report.decoded.leases_issued == report.decoded.leases_released,
            "every issued lease was released",
        )
        track_kinds = {track.track_kind for track in report.decoded.tracks if track.samples > 0}
        checks.check(
            track_kinds == {"video", "audio"},
            f"decoded tracks with samples: {sorted(track_kinds)}",
        )
        checks.check(
            len(report.source.tracks) >= 1, f"observed source tracks={len(report.source.tracks)}"
        )
        checks.check(
            all(track.timing_known for track in report.source.tracks),
            "observed tracks report known timing",
        )
        checks.check(
            report.source.duration_ms == 0,
            "a live stream has no known duration (0, never an assumed value)",
        )
        checks.check(
            report.source.source.content_hash == "",
            "content_hash stays empty: a live stream has no reproducible digest",
        )
        checks.check(report.source.source.kind == 2, "source kind is SRT")
        checks.check(report.golden_path_verified is False, "golden_path_verified stays false")
        checks.check(
            report.handoff_state == "not_exercised", f"handoff_state={report.handoff_state!r}"
        )
        # 没有保留队列的窗口必须写"没测过"：一组干净的零会被读成"没有压力"。
        checks.check(
            report.decoded.backpressure.observed is False
            and not report.decoded.backpressure.queues,
            "没有保留队列时写 observed=false，而不是零压力"
            f"（state={report.decoded.backpressure.state}）",
        )
        checks.check(list(report.blockers) == [], f"blockers={list(report.blockers)}")
        leaked = [token for token in FORBIDDEN_IN_REPORT if token.encode() in raw]
        checks.check(not leaked, f"report leaks no URI material (found {leaked})")
        checks.check(uri not in result.stdout, "stdout leaks no URI")
    finally:
        publisher.stop()
    return checks.finish()


def scenario_videotoolbox_video(sample: Path, workspace: Path, duration_ms: int, uri: str) -> bool:
    """OBS 同类的 Apple 硬件编码器直推：没有容器 timing 的视频轨也必须进入数据面。

    现场那次 OBS SRT 直推里 video buffer 不带 duration，旧实现按 `duration_unavailable`
    丢掉了整条视频轨（20 秒窗口 0 帧）。本场景把"整条轨消失"钉成回归断言，并要求
    时长推导被显式计数，而不是冒充容器声明的时长。
    """
    checks = Checks("videotoolbox_video")
    publisher = Publisher(sample, workspace / "videotoolbox-publisher.log", encoder="videotoolbox")
    publisher.start()
    try:
        publisher.wait_ready()
        report_path = workspace / "videotoolbox-report.pb"
        result = run_ingest(report_path, duration_ms, uri)
        checks.check(
            result.returncode == 0,
            f"ingest exit 0 (got {result.returncode}): {result.stderr[-400:]}",
        )
        report, raw = read_report(report_path)
        video = next(
            (track for track in report.decoded.tracks if track.track_kind == "video"), None
        )
        checks.check(video is not None, "the report carries a video track stat")
        if video is not None:
            checks.check(
                video.samples > 0,
                f"video samples={video.samples} > 0: decoded frames reach the data plane",
            )
            checks.check(
                "duration_unavailable" not in video.drop_reasons,
                f"video drop_reasons={list(video.drop_reasons)}: missing buffer durations "
                "are resolved from the PTS delta, not dropped",
            )
            checks.check(
                video.duration_derived_samples <= video.samples,
                f"duration_derived_samples={video.duration_derived_samples} "
                f"<= samples={video.samples}",
            )
            checks.check(
                video.width > 0 and video.height > 0,
                f"video layout={video.width}x{video.height} is measured, not assumed",
            )
            print(
                f"[videotoolbox_video] duration_derived_samples={video.duration_derived_samples} "
                f"video_samples={video.samples} drops={list(video.drop_reasons)}"
            )
        audio = next(
            (track for track in report.decoded.tracks if track.track_kind == "audio"), None
        )
        checks.check(
            audio is not None and audio.samples > 0,
            "the audio track keeps flowing next to the video one",
        )
        described = [
            track
            for track in report.source.tracks
            if track.track_kind == "video" and track.width > 0
        ]
        checks.check(
            len(described) == 1,
            f"observed video source tracks carrying a real size: {len(described)}",
        )
        checks.check(report.golden_path_verified is False, "golden_path_verified stays false")
        checks.check(list(report.blockers) == [], f"blockers={list(report.blockers)}")
        leaked = [token for token in FORBIDDEN_IN_REPORT if token.encode() in raw]
        checks.check(not leaked, f"report leaks no URI material (found {leaked})")
        checks.check(uri not in result.stdout, "stdout leaks no URI")
    finally:
        publisher.stop()
    return checks.finish()


def scenario_stall_recovery(sample: Path, workspace: Path, uri: str) -> bool:
    checks = Checks("stall_recovery")
    window_ms = 20_000
    publisher = Publisher(sample, workspace / "stall-publisher.log")
    publisher.start()
    ingest: subprocess.Popen | None = None
    try:
        publisher.wait_ready()
        report_path = workspace / "stall-report.pb"
        command = [
            str(runtime_binary()),
            "ingest",
            str(PIPELINE),
            "--report",
            str(report_path),
            "--duration-ms",
            str(window_ms),
            "--stall-threshold-ms",
            "1000",
        ]
        environment = dict(os.environ)
        environment["SENSORYPLEX_SRT_LIVE_URI"] = uri
        ingest = subprocess.Popen(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        time.sleep(6)
        publisher.stop()
        state, _ = metrics()
        checks.check(state != "ready", f"publisher gone: path state={state!r}")
        time.sleep(3)
        publisher.start()
        publisher.wait_ready()
        stdout, stderr = ingest.communicate(timeout=120)
        checks.check(
            ingest.returncode == 0, f"ingest exit 0 (got {ingest.returncode}): {stderr[-400:]}"
        )
        report, _ = read_report(report_path)
        stream = report.stream
        checks.check(stream.samples > 0, f"samples={stream.samples} > 0 across the gap")
        checks.check(stream.stalls >= 1, f"stalls={stream.stalls} >= 1")
        checks.check(stream.stalled_ms > 0, f"stalled_ms={stream.stalled_ms} > 0")
        checks.check(stream.max_stall_ms > 0, f"max_stall_ms={stream.max_stall_ms} > 0")
        checks.check(stream.recovered is True, "recovered: samples arrived again after the gap")
        checks.check(
            stream.stalls + 1 > 0 and len(stream.stall_events) >= 1,
            f"stall_events listed={len(stream.stall_events)} of {stream.stalls}",
        )
        if stream.stall_events:
            event = stream.stall_events[0]
            checks.check(event.reason == "stream_gap", f"first event reason={event.reason!r}")
            checks.check(
                event.ended_ms > event.started_ms and event.gap_ms >= 1000,
                f"first event gap: started={event.started_ms} "
                f"ended={event.ended_ms} gap={event.gap_ms}",
            )
            # 媒体时间缺口可以是负数：重启的发布端从 0 重新计时。-1 才是"未知"。
            checks.check(
                event.pts_jump_ms != -1,
                f"media-time jump was observable: pts_jump_ms={event.pts_jump_ms}",
            )
        else:
            checks.check(False, "no stall detail was recorded")
        checks.check(stream.ended_by_deadline is True, "the window still ran to its deadline")
        checks.check(report.golden_path_verified is False, "golden_path_verified stays false")
    finally:
        if ingest is not None and ingest.poll() is None:
            ingest.kill()
            ingest.wait(timeout=10)
        publisher.stop()
    return checks.finish()


def scenario_no_source(workspace: Path, uri: str) -> bool:
    checks = Checks("no_source")
    state, _ = metrics()
    checks.check(state != "ready", f"no publisher is running: state={state!r}")
    report_path = workspace / "no-source-report.pb"
    result = run_ingest(report_path, 1500, uri)
    checks.check(
        result.returncode != 0, f"ingest must fail without a source (exit={result.returncode})"
    )
    if report_path.is_file():
        report, _ = read_report(report_path)
        blockers = list(report.blockers)
        checks.check(
            any(
                blocker in blockers
                for blocker in ("live_ingest_failed", "live_window_produced_no_samples")
            ),
            f"explicit blocker instead of a silent empty success: {blockers}",
        )
        checks.check(report.golden_path_verified is False, "golden_path_verified stays false")
        checks.check(
            report.stream.samples == 0, f"no sample was invented: samples={report.stream.samples}"
        )
    else:
        checks.check(False, "a report was still written on failure")
    return checks.finish()


def scenario_live_handoff(sample: Path, workspace: Path, duration_ms: int, uri: str) -> bool:
    checks = Checks("live_handoff")
    listen = f"127.0.0.1:{free_port()}"
    publisher = Publisher(sample, workspace / "handoff-publisher.log")
    publisher.start()
    ingest: subprocess.Popen | None = None
    try:
        publisher.wait_ready()
        report_path = workspace / "live-handoff-report.pb"
        worker_report = workspace / "live-handoff-worker.json"
        environment = dict(os.environ)
        environment["SENSORYPLEX_SRT_LIVE_URI"] = uri
        ingest = subprocess.Popen(
            [
                str(runtime_binary()),
                "ingest",
                str(PIPELINE),
                "--report",
                str(report_path),
                "--duration-ms",
                str(duration_ms),
                "--handoff-listen",
                listen,
                "--handoff-wait-timeout-ms",
                "30000",
                "--handoff-idle-timeout-ms",
                "2000",
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # 消费者必须在数据面就绪之后、decode 结束时才算数：等 runtime 打印 handoff_ready。
        lines: list[str] = []
        deadline = time.monotonic() + 120
        ready = False
        while time.monotonic() < deadline and not ready:
            line = ingest.stdout.readline()
            if not line:
                break
            lines.append(line.rstrip("\n"))
            ready = line.startswith("handoff_ready")
        checks.check(ready, "the runtime exposed the live data plane on loopback")
        worker = subprocess.run(
            [
                "uv",
                "run",
                "python",
                str(WORKER),
                "--listen",
                listen,
                "--report",
                str(worker_report),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        remaining_out, remaining_err = ingest.communicate(timeout=180)
        lines.extend(remaining_out.splitlines())
        checks.check(
            worker.returncode == 0, f"independent consumer exited 0: {worker.stderr[-400:]}"
        )
        # 消费者自己的报告说明它真的读到了什么：直播窗口里必须**同时**有视频帧与音频块，
        # 否则"接进下游"只是音频接进去了（保留表是所有种类共用的一张表）。
        consumed = json.loads(worker_report.read_text())
        checks.check(
            int(consumed.get("video_buffers", 0)) > 0,
            f"live video frames reached the consumer: video_buffers={consumed.get('video_buffers')}"
            f" audio_buffers={consumed.get('audio_buffers')}",
        )
        checks.check(
            int(consumed.get("audio_buffers", 0)) > 0,
            "audio buffers reached the consumer next to the video ones",
        )
        checks.check(
            ingest.returncode == 0,
            f"ingest exit 0 (got {ingest.returncode}): {remaining_err[-400:]}",
        )
        report, _ = read_report(report_path)
        checks.check(
            report.handoff_state == f"exposed_on={listen}",
            f"handoff_state={report.handoff_state!r}",
        )
        checks.check(
            report.stream.samples > 0,
            f"live samples reached the data plane: {report.stream.samples}",
        )
        queues = assert_retention_backpressure(checks, report)
        # 命令行与报告必须给出同一组水位：两边不一致就说明有一边在编。
        printed = find_line(lines, "backpressure ")
        checks.check(printed is not None, "the runtime printed its backpressure line")
        if printed is not None:
            fields = parse_fields(printed)
            checks.check(
                fields.get("state") == report.decoded.backpressure.state,
                f"state 在报告与命令行一致：{report.decoded.backpressure.state}",
            )
            checks.check(
                set(read_queues(fields.get("queues", ""))) == RETENTION_QUEUES,
                f"命令行与报告列出同一组队列：{fields.get('queues')}",
            )
            checks.check(
                int(fields.get("throttled", 0)) > 0,
                f"阶段一降级（降低非关键帧采样率）真的生效：throttled={fields.get('throttled')}",
            )
        stats_line = next((line for line in lines if line.startswith("handoff_stats")), None)
        checks.check(
            stats_line is not None, "the runtime reconciled the lease ledger before exiting"
        )
        if stats_line:
            fields = parse_fields(stats_line)
            checks.check(fields.get("consumer_seen") == "true", "consumer_seen=true")
            checks.check(
                fields.get("retained") == "0",
                f"no buffer left retained: retained={fields.get('retained')}",
            )
            checks.check(
                fields.get("arena_live_slabs") == "0",
                f"arena has no dangling slab: arena_live_slabs={fields.get('arena_live_slabs')}",
            )
            checks.check(
                int(fields.get("retain_rejected", 0)) > 0,
                "有界窗口真的触顶过并显式拒绝："
                f"retain_rejected={fields.get('retain_rejected')} "
                f"reasons={fields.get('rejection_reasons')}",
            )
            checks.check(
                int(fields.get("released", 0)) > 0,
                f"消费者真的释放了 buffer：released={fields.get('released')}",
            )
            checks.check(
                int(fields.get("residency_samples", 0)) > 0,
                "等待时间只在消费者真的领走并释放之后才有样本："
                f"residency_samples={fields.get('residency_samples')} "
                f"max_ms={fields.get('residency_max_ms')}",
            )
            checks.check(
                int(fields.get("retained_kind_limit", -1)) == queues[KIND_QUEUE][2],
                "按种类的上限在账目与报告里一致："
                f"retained_kind_limit={fields.get('retained_kind_limit')} "
                f"report={queues[KIND_QUEUE][2]} retained_limit={fields.get('retained_limit')}",
            )
            checks.check(
                int(fields.get("retained_kind_peak", 0))
                <= int(fields.get("retained_kind_limit", -1)),
                "单一种类的水位没有越过它自己的上限："
                f"retained_kind_peak={fields.get('retained_kind_peak')}",
            )
            accounted = (
                int(fields.get("released", 0))
                + int(fields.get("expired", 0))
                + int(fields.get("retained", 0))
            )
            checks.check(
                accounted == int(fields.get("retained_total", -1)),
                f"released+expired+retained={accounted} "
                f"== retained_total={fields.get('retained_total')}",
            )
    finally:
        if ingest is not None and ingest.poll() is None:
            ingest.kill()
            ingest.wait(timeout=10)
        publisher.stop()
    return checks.finish()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample",
        type=Path,
        default=DEFAULT_SAMPLE,
        help="发布端使用的授权样本（默认：公有许可的 552 秒录屏）",
    )
    parser.add_argument("--uri", default=DEFAULT_URI, help="读取 URI（只经环境变量传给 Runtime）")
    parser.add_argument("--steady-duration-ms", type=int, default=12_000)
    parser.add_argument("--handoff-duration-ms", type=int, default=8_000)
    parser.add_argument(
        "--scenario", action="append", default=None, help="只跑指定场景，可重复；默认全跑"
    )
    args = parser.parse_args()

    if not args.sample.is_file():
        raise SystemExit(
            f"sample not found: {args.sample}（见 tests/fixtures/media/OPEN-SAMPLES.md）"
        )
    if subprocess.run(["which", "gst-launch-1.0"], capture_output=True).returncode != 0:
        raise SystemExit("gst-launch-1.0 not found; the SRT publisher needs GStreamer")
    state, _ = metrics()
    if state == "ready":
        raise SystemExit(
            f"path {PATH_NAME} already has a publisher (OBS 在直播？)。"
            "移除它或等它结束后再跑本验收。"
        )

    scenarios = {
        "steady": lambda workspace: scenario_steady(
            args.sample, workspace, args.steady_duration_ms, args.uri
        ),
        "videotoolbox_video": lambda workspace: scenario_videotoolbox_video(
            args.sample, workspace, args.steady_duration_ms, args.uri
        ),
        "stall_recovery": lambda workspace: scenario_stall_recovery(
            args.sample, workspace, args.uri
        ),
        "no_source": lambda workspace: scenario_no_source(workspace, args.uri),
        "live_handoff": lambda workspace: scenario_live_handoff(
            args.sample, workspace, args.handoff_duration_ms, args.uri
        ),
    }
    selected = args.scenario or list(scenarios)
    unknown = [name for name in selected if name not in scenarios]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {unknown}; known: {list(scenarios)}")

    workspace = Path(tempfile.mkdtemp(prefix="sensoryplex-live-"))
    print(f"workspace: {workspace}")
    results = {}
    for name in selected:
        results[name] = scenarios[name](workspace)

    state, _ = metrics()
    print(f"\npath state after the run: {state}")
    if state == "ready":
        print(
            "WARNING: a publisher is still live on the path; check for leftover processes",
            file=sys.stderr,
        )

    print("\nscenario summary:")
    for name, passed in results.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    if not all(results.values()):
        print(f"\nartifacts kept for inspection: {workspace}", file=sys.stderr)
        return 1
    print("live ingest acceptance: all scenarios passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
