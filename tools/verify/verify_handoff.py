"""跨进程数据面验收：Runtime 保留字节 → **独立进程**按 lease 读取、校验并释放。

三个进程角色必须分开，否则验收没有意义：
1. 本脚本（编排 + 对账）；
2. `sensoryplex-runtime replay --handoff-listen`（生产者，持有共享内存与保留表）；
3. `tools/handoff_worker.py`（消费者，只能通过 gRPC 拿到段名与窗口）。

判定标准（全部为真实执行结果，不做"健康检查即通过"）：
- 消费者进程与生产者进程不同 PID；
- 视频/音频 buffer 的摘要与 mmap 读到的字节一致；子窗口摘要与整条 buffer 不同；
- 越界、未知 buffer、非法 TTL、重复领取、迟到释放都得到显式拒绝码；
- 保留表有界：触顶时拒绝码落在容量类（`handoff_backlog_full` / `handoff_kind_quota_full` /
  `arena_capacity_exceeded`），且保留 + 拒绝 = 已交接样本数；
- 结束时每条保留的 buffer 都被释放或过期回收（`released + expired + retained == retained_total`），
  arena 没有悬挂 slab。
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from edge_material_sdk.generated.media.v1 import media_pb2

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "config/pipelines/file-material.yaml"
WORKER = ROOT / "tools/handoff_worker.py"

# 默认 consumer 完成后 2 s 关闭数据面；等第一个消费者最多 30 s。
IDLE_TIMEOUT_MS = 2_000
WAIT_TIMEOUT_MS = 30_000
# 服务端默认 lease TTL：必须覆盖消费者读完一整批 buffer 的时间。
SERVER_TTL_MS = 30_000
# 只由"有界容量"产生的保留期拒绝码。
CAPACITY_REASONS = {"handoff_backlog_full", "handoff_kind_quota_full", "arena_capacity_exceeded"}
# 长样本（如 officehours-panel 五万五千帧）单次 replay 要数分钟，等待窗口必须给足。
READY_TIMEOUT_S = 900.0
RUN_TIMEOUT_S = 900.0

SCENARIOS = [
    {
        # 上限尽量放大：观察"能不能把整批交接样本都保留下来"。结果由素材大小决定，
        # 因此不做无条件断言：全保留要求 rejections=0，否则每条拒绝都必须是显式的容量原因。
        "name": "large_bounds",
        "retained_limit": 4096,
        "arena_bytes": 256 * 1024 * 1024,
        "expect_backlog_rejection": False,
    },
    {
        # 保留表钉死在 6：有界性必须表现为显式拒绝，而不是无限堆积。
        "name": "bounded_backlog",
        "retained_limit": 6,
        "arena_bytes": 128 * 1024 * 1024,
        "expect_backlog_rejection": True,
    },
]


def runtime_binary() -> Path:
    for profile in ("release", "debug"):
        candidate = ROOT / f"target/{profile}/sensoryplex-runtime"
        if candidate.is_file():
            return candidate
    raise SystemExit("build the runtime first: make handoff-check builds it for you")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def parse_fields(line: str) -> dict[str, str]:
    """把 `key=value ...` 文本行解析成字典；值里允许出现 `:` 与 `,`。"""
    fields: dict[str, str] = {}
    for token in line.split(" "):
        key, separator, value = token.partition("=")
        if separator:
            fields[key] = value
    return fields


def parse_reasons(raw: str) -> dict[str, int]:
    if raw in {"", "none"}:
        return {}
    reasons: dict[str, int] = {}
    for item in raw.split(","):
        name, _, count = item.partition(":")
        reasons[name] = int(count)
    return reasons


class RuntimeProcess:
    """生产者进程：按行观察它的 stdout，等它报告数据面就绪与最终对账。"""

    def __init__(self, media: Path, listen: str, scenario: dict, report_path: Path):
        self.arena_bytes = scenario["arena_bytes"]
        self.media = media
        self.listen = listen
        self.scenario = scenario
        self.report_path = report_path
        self.lines: list[str] = []
        self.stderr: list[str] = []
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        command = [
            str(runtime_binary()),
            "replay",
            str(PIPELINE),
            str(self.media),
            "--report",
            str(self.report_path),
            "--handoff-listen",
            self.listen,
            "--handoff-retained-limit",
            str(self.scenario["retained_limit"]),
            "--handoff-arena-bytes",
            str(self.arena_bytes),
            "--handoff-ttl-ms",
            str(SERVER_TTL_MS),
            "--handoff-wait-timeout-ms",
            str(WAIT_TIMEOUT_MS),
            "--handoff-idle-timeout-ms",
            str(IDLE_TIMEOUT_MS),
        ]
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(
            target=self._pump, args=(self.process.stdout, self.lines), daemon=True
        ).start()
        threading.Thread(
            target=self._pump, args=(self.process.stderr, self.stderr), daemon=True
        ).start()

    @staticmethod
    def _pump(stream, sink: list[str]) -> None:
        for line in stream:
            sink.append(line.rstrip("\n"))

    def wait_for(self, prefix: str, timeout: float) -> dict[str, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in list(self.lines):
                if line.startswith(prefix):
                    return parse_fields(line)
            if self.process is not None and self.process.poll() is not None:
                raise AssertionError(
                    f"runtime exited before reporting {prefix!r}: "
                    f"stdout={self.lines} stderr={self.stderr}"
                )
            time.sleep(0.05)
        raise AssertionError(f"runtime never reported {prefix!r} within {timeout}s: {self.lines}")

    def finish(self, timeout: float) -> int:
        assert self.process is not None
        code = self.process.wait(timeout=timeout)
        return code


def check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def verify_scenario(media: Path, scenario: dict, workspace: Path) -> list[str]:
    failures: list[str] = []
    port = free_port()
    listen = f"127.0.0.1:{port}"
    report_path = workspace / f"{scenario['name']}.pb"
    worker_report = workspace / f"{scenario['name']}-worker.json"
    producer = RuntimeProcess(media, listen, scenario, report_path)
    producer.start()

    ready = producer.wait_for("handoff_ready", timeout=READY_TIMEOUT_S)
    check(ready.get("listen") == listen, f"unexpected listen address: {ready}", failures)
    arena_bytes = scenario["arena_bytes"]
    check(
        ready.get("arena_capacity_bytes") == str(arena_bytes),
        f"runtime arena capacity {ready.get('arena_capacity_bytes')} != requested {arena_bytes}",
        failures,
    )
    segment = ready.get("segment", "")
    check(
        segment.startswith("/sp.") and segment.count("/") == 1,
        f"segment name is not a bounded opaque handle: {segment!r}",
        failures,
    )
    retained = int(ready.get("retained", "0"))
    retained_limit = int(ready.get("retained_limit", "0"))
    offered = int(ready.get("offered", "0"))
    rejected = int(ready.get("retain_rejected", "0"))
    check(retained > 0, "no buffer was retained at all", failures)
    check(
        retained <= retained_limit and retained_limit == scenario["retained_limit"],
        f"retained={retained} exceeds the configured limit {retained_limit}",
        failures,
    )
    check(
        offered == retained + rejected,
        f"retention accounting is off: {retained} + {rejected} != {offered}",
        failures,
    )
    reasons = parse_reasons(ready.get("rejection_reasons", ""))
    check(
        set(reasons) <= CAPACITY_REASONS,
        f"retention was rejected for a non-capacity reason: {reasons}",
        failures,
    )
    if scenario["expect_backlog_rejection"]:
        check(
            sum(reasons.get(reason, 0) for reason in CAPACITY_REASONS) == rejected > 0,
            f"expected explicit backlog rejections, got {ready.get('rejection_reasons')!r}",
            failures,
        )
    full_retention = rejected == 0

    worker = subprocess.run(
        [
            sys.executable,
            str(WORKER),
            "--listen",
            listen,
            "--report",
            str(worker_report),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=RUN_TIMEOUT_S,
    )
    check(
        worker.returncode == 0,
        f"consumer process failed ({worker.returncode}): {worker.stderr[-2000:]}",
        failures,
    )
    payload = json.loads(worker_report.read_text()) if worker_report.is_file() else {}
    check(
        payload.get("checks_failed") == 0,
        f"consumer reported failing checks: {payload.get('failures')}",
        failures,
    )
    # 已知限制必须被显式上报，不能靠"没提"来暗示已经做了内存隔离。
    check(
        isinstance(payload.get("same_uid_segment_visibility"), str)
        and payload["same_uid_segment_visibility"].strip() != "",
        "the consumer must report the same-UID visibility limitation, "
        "not imply per-buffer isolation",
        failures,
    )
    # 三者必须是三个进程：消费者自己报 pid，另外两个由本脚本持有。否则"跨进程"是口号。
    producer_pid = producer.process.pid if producer.process is not None else None
    worker_pid = payload.get("worker_pid")
    check(
        isinstance(worker_pid, int) and worker_pid not in {os.getpid(), producer_pid},
        f"consumer must be a separate process: worker={worker_pid!r} "
        f"orchestrator={os.getpid()} producer={producer_pid!r}",
        failures,
    )
    check(
        payload.get("segment_name") == segment,
        f"consumer read a different segment: {payload.get('segment_name')} != {segment}",
        failures,
    )
    # 至少两条视频帧被保留时，子窗口摘要必须真的测过；否则如实记为未测。
    requires_sub_window = (payload.get("video_buffers") or 0) >= 2
    check(
        (payload.get("sub_window_tested") is True) or not requires_sub_window,
        f"the sub-window digest check did not run: {payload.get('video_buffers')} video buffers",
        failures,
    )

    exit_code = producer.finish(timeout=300)
    stats = producer.wait_for("handoff_stats", timeout=5)
    report_line = next(
        (line for line in producer.lines if line.startswith("replay report written:")), ""
    )
    check(exit_code == 0, f"producer exited {exit_code}: {producer.stderr[-2000:]}", failures)
    check(
        "handoff=exposed_on" in report_line,
        f"replay line does not state the data plane: {report_line}",
        failures,
    )
    check(stats.get("consumer_seen") == "true", f"no consumer was seen: {stats}", failures)
    total = int(stats.get("retained_total", "0"))
    released = int(stats.get("released", "0"))
    expired = int(stats.get("expired", "0"))
    still_retained = int(stats.get("retained", "0"))
    check(
        still_retained == 0 and released + expired == total,
        f"buffers left unreleased: {stats}",
        failures,
    )
    check(
        expired == 1,
        f"exactly one deliberate lease expiry was expected, got {expired}",
        failures,
    )
    check(
        released == payload.get("releases"),
        f"runtime released {released} leases, consumer released {payload.get('releases')}",
        failures,
    )
    check(
        stats.get("arena_live_slabs") == "0",
        f"arena still holds slabs: {stats.get('arena_live_slabs')}",
        failures,
    )
    check(
        int(stats.get("request_rejected", "0")) > 0,
        "the consumer's refusals were not counted on the producer side",
        failures,
    )
    check(
        payload.get("runtime_stats", {}).get("retained_total") == total,
        "consumer and producer disagree on retained_total",
        failures,
    )

    # 数据面必须能对上 replay 报告。对照的基准是**报告亲手交接过的 descriptor 数**：
    # 从 M10 起，音频段描述符和逐样本 buffer 一样进保留表，所以"样本数"不再等于
    # "被 offer 的 buffer 数"，差的就是段数；这个差必须被报告显式解释，不能当成误差抹掉。
    replay = media_pb2.ReplayReport()
    replay.ParseFromString(report_path.read_bytes())
    decoded = replay.decoded
    samples = sum(track.samples for track in decoded.tracks)
    segments = decoded.audio_segments.segments
    handed_off = decoded.descriptors_built
    check(
        handed_off == samples + segments,
        f"report descriptor accounting is off: built={handed_off} samples={samples} "
        f"segments={segments}",
        failures,
    )
    check(
        total + rejected == handed_off and offered == handed_off,
        f"data plane offered {offered} (retained {total} + rejected {rejected}) buffers but the "
        f"report handed off {handed_off} descriptors "
        f"({samples} samples + {segments} audio segments)",
        failures,
    )
    check(
        not replay.golden_path_verified,
        "this run must not claim the golden path",
        failures,
    )
    check(
        replay.handoff_state == f"exposed_on={listen}",
        f"the report must state where the data plane was served, got {replay.handoff_state!r}",
        failures,
    )
    print(
        f"[{scenario['name']}] offered={offered} retained={total} rejected={rejected} "
        f"released={released} expired={expired} checks={payload.get('checks_total')} "
        f"drained={payload.get('drained_buffers')} full_retention={full_retention} "
        f"segment={segment}"
    )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument(
        "--scenario",
        action="append",
        choices=[scenario["name"] for scenario in SCENARIOS],
        help="run only the named scenario (repeatable); default runs all",
    )
    args = parser.parse_args()
    media = args.media.resolve()
    if not media.is_file():
        raise SystemExit(f"not a media file: {media}")
    selected = [
        scenario for scenario in SCENARIOS if not args.scenario or scenario["name"] in args.scenario
    ]

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as workspace:
        for scenario in selected:
            failures.extend(verify_scenario(media, scenario, Path(workspace)))

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        print(f"handoff verification failed: {len(failures)} problem(s)", file=sys.stderr)
        return 1
    print(f"Handoff verified: {len(selected)} scenario(s) on {media.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
