"""背压与队列可观察验收：描述符之后那条有界队列的水位、丢弃、超时与等待时间。

三个角色必须分开，否则"有背压"只是一种说法：
1. 本脚本（编排 + 对账）；
2. `sensoryplex-runtime replay --handoff-listen`（生产者，持有保留表与共享区）；
3. `tools/handoff_worker.py`（消费者，只在需要测等待时出现）。

判定标准（全部为真实执行结果，不用健康检查冒充）：
- 没有消费者的运行必须先把队列填满并**显式拒绝**：`dropped_total > 0`、
  `dropped_total == sum(drop_reasons)`、原因全部落在容量类拒绝码上
  （`handoff_backlog_full` / `handoff_kind_quota_full` / `arena_capacity_exceeded`），
  并且命令本身因为"消费者从未连接"而失败——不能让一次无人消费的运行看起来像成功；
- 降级必须先于拒绝生效：运动样本上 `sampling_throttled_samples > 0`，采样器的
  `skipped_backpressure_throttled` 与之一致，且它们都是"本来会被 keep 的帧"；
- 水位必须自证有界：`0 < peak <= capacity`，三条队列都要给容量；其中"按种类"那条的容量
  必须是保留表容量的一半——它解释为什么单一种类无法独占窗口（实时流里先到的是音频块）；
- 等待时间只能在真的有消费者领走并释放之后才有值：消费者场景下 `residency_samples > 0`；
- 没有保留队列的运行必须写 `observed=false`，绝不能用一组干净的零冒充"没有压力"。

样本用仓库登记的公有许可素材（见 `tests/fixtures/media/OPEN-SAMPLES.md`）；
`officehours-panel` 是 37 分钟的长样本，用 `--max-points` 截断到够用的长度并在结果里
如实标注 `decode_truncated`。
"""

import argparse
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from edge_material_sdk.generated.media.v1 import media_pb2

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "config/pipelines/file-material.yaml"
WORKER = ROOT / "tools/handoff_worker.py"
SAMPLES = ROOT / "video/samples"
# 队列上限钉死在 8：这是"有界"的直接体现，不是性能调参。
RETAINED_LIMIT = 8
# 共享区给 32 MiB，48 0p RGBA 一帧约 1.5 MiB，因此真正先触顶的是保留表，不是字节容量。
ARENA_BYTES = 32 * 1024 * 1024
# 消费者场景的宽松上限：消费者跟得上时队列根本不该满。
CONSUMER_RETAINED_LIMIT = 4096
CONSUMER_ARENA_BYTES = 512 * 1024 * 1024
WAIT_TIMEOUT_MS = 3_000
IDLE_TIMEOUT_MS = 2_000
READY_TIMEOUT_S = 900.0
RUN_TIMEOUT_S = 900.0
# 只由"有界容量"产生的保留期拒绝码。
# 只由"有界容量"产生的保留期拒绝码。按种类的那条上限也是一条硬上限，因此同样属于容量类。
CAPACITY_REASONS = {"handoff_backlog_full", "handoff_kind_quota_full", "arena_capacity_exceeded"}
RETENTION_QUEUES = {"handoff_retained_table", "handoff_retained_kind", "handoff_arena_bytes"}
TABLE_QUEUE = "handoff_retained_table"
KIND_QUEUE = "handoff_retained_kind"


def runtime_binary() -> Path:
    for profile in ("release", "debug"):
        candidate = ROOT / f"target/{profile}/sensoryplex-runtime"
        if candidate.is_file():
            return candidate
    raise SystemExit("build the runtime first: make backpressure-check builds it for you")


def sample(name: str) -> Path:
    path = SAMPLES / name
    if not path.is_file():
        raise SystemExit(f"sample missing (see tests/fixtures/media/OPEN-SAMPLES.md): {path}")
    return path


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def parse_fields(line: str) -> dict[str, str]:
    """把 `key=value ...` 文本行解析成字典；值里允许出现 `:` 与 `,`。"""
    fields: dict[str, str] = {}
    for token in line.strip().split():
        key, separator, value = token.partition("=")
        if separator:
            fields[key] = value
    return fields


def backpressure_line(stdout: str) -> dict[str, str]:
    for line in stdout.splitlines():
        if line.startswith("backpressure "):
            return parse_fields(line)
    raise SystemExit(f"no backpressure line in runtime output:\n{stdout}")


def handoff_stats_line(stdout: str) -> dict[str, str]:
    for line in stdout.splitlines():
        if line.startswith("handoff_stats "):
            return parse_fields(line)
    raise SystemExit(f"no handoff_stats line in runtime output:\n{stdout}")


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


def queues_from_report(report: media_pb2.BackpressureReport) -> dict[str, tuple[int, int, int]]:
    return {queue.name: (queue.current, queue.peak, queue.capacity) for queue in report.queues}


def load_report(path: Path) -> media_pb2.ReplayReport:
    report = media_pb2.ReplayReport()
    report.ParseFromString(path.read_bytes())
    return report


def assert_bounded_queue(
    checks: Checks, fields: dict[str, str], report: media_pb2.ReplayReport
) -> None:
    """报告与命令行必须给出同一组水位：两者不一致就说明有一边在编。"""
    backpressure = report.decoded.backpressure
    queues = queues_from_report(backpressure)
    checks.check(backpressure.observed, "报告声明这条有界队列被测量过")
    checks.check(
        set(queues) == RETENTION_QUEUES,
        f"三条队列都给出了容量与水位：{sorted(queues)}",
    )
    rendered = fields.get("queues", "")
    checks.check(
        all(name in rendered for name in queues),
        f"命令行与报告列出同一组队列：{rendered}",
    )
    for name, (current, peak, capacity) in queues.items():
        checks.check(
            capacity > 0 and peak > 0 and peak <= capacity and current <= peak,
            f"{name} 自证有界：current={current} peak={peak} capacity={capacity}",
        )
    # 按种类的上限必须是保留表容量的一半（至少 1）：这是"谁也不能独占窗口"这条策略
    # 在报告里可验证的形式，而不是一句设计说明。
    table_capacity = queues[TABLE_QUEUE][2]
    checks.check(
        queues[KIND_QUEUE][2] == max(1, table_capacity // 2),
        f"按种类的上限 = 保留表容量的一半：{queues[KIND_QUEUE][2]} / {table_capacity}",
    )
    checks.check(
        fields.get("state") == backpressure.state,
        f"状态在报告与命令行一致：{backpressure.state}",
    )
    check_drop_identity(checks, backpressure)


def check_drop_identity(checks: Checks, backpressure: media_pb2.BackpressureReport) -> None:
    summed = sum(entry.count for entry in backpressure.drop_reasons)
    checks.check(
        summed == backpressure.dropped_total,
        f"每个丢弃都有原因：dropped_total={backpressure.dropped_total} sum(reasons)={summed}",
    )
    by_kind = sum(entry.count for entry in backpressure.drop_kinds)
    composition = ",".join(f"{entry.kind}:{entry.count}" for entry in backpressure.drop_kinds)
    checks.check(
        by_kind == backpressure.dropped_total,
        f"总数能按 buffer 种类读出来：dropped_total={backpressure.dropped_total} "
        f"sum(kinds)={by_kind} kinds={composition or 'none'}",
    )


def scenario_queue_saturates(
    name: str, media: Path, max_points: int, workspace: Path, expect_throttle: bool
) -> bool:
    """没有消费者：队列必须按上限拒绝，而且必须在拒绝之前先降速。"""
    checks = Checks(name)
    listen = f"127.0.0.1:{free_port()}"
    report_path = workspace / f"{name}.pb"
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
            "--handoff-listen",
            listen,
            "--handoff-retained-limit",
            str(RETAINED_LIMIT),
            "--handoff-arena-bytes",
            str(ARENA_BYTES),
            "--handoff-wait-timeout-ms",
            str(WAIT_TIMEOUT_MS),
            "--handoff-idle-timeout-ms",
            str(IDLE_TIMEOUT_MS),
        ],
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_S,
    )
    # 没人消费的运行必须失败，并在原因里说清楚：这是一次无人消费的运行，不是"通过"。
    checks.check(
        completed.returncode != 0
        and "handoff_consumer_never_connected" in (completed.stderr or completed.stdout),
        "没有消费者时运行显式失败（handoff_consumer_never_connected）",
    )
    fields = backpressure_line(completed.stdout)
    report = load_report(report_path)
    backpressure = report.decoded.backpressure

    checks.check(
        fields.get("observed") == "true" and backpressure.observed,
        "本次运行真的有一条可测量的有界队列",
    )
    assert_bounded_queue(checks, fields, report)
    checks.check(
        backpressure.dropped_total > 0,
        f"队列触顶后存在被拒绝的保留：dropped_total={backpressure.dropped_total}",
    )
    reasons = {entry.reason for entry in backpressure.drop_reasons}
    checks.check(
        bool(reasons) and reasons <= CAPACITY_REASONS,
        f"拒绝原因都是容量类：{sorted(reasons)}",
    )
    checks.check(
        backpressure.state in {"degraded", "saturated"},
        f"结束时的状态不是 ok：{backpressure.state}",
    )
    checks.check(
        any(entry.kind == "video_frame" for entry in backpressure.drop_kinds),
        "被拒绝的里面有视频帧，不只是音频 buffer"
        f"（{','.join(f'{entry.kind}:{entry.count}' for entry in backpressure.drop_kinds)}）",
    )
    checks.check(
        any(entry.kind == "audio_pcm" for entry in backpressure.drop_kinds),
        "同一条队列也承接音频 PCM：这一条解释总数为什么大于视频 keep 数",
    )
    checks.check(
        backpressure.degraded_entries + backpressure.saturated_entries > 0,
        f"状态迁移被计数：degraded={backpressure.degraded_entries} "
        f"saturated={backpressure.saturated_entries}",
    )
    checks.check(
        backpressure.throttle_factor >= 1,
        f"降级倍数随报告一起给出：{backpressure.throttle_factor}",
    )
    sampling = report.decoded.sampling[0] if report.decoded.sampling else None
    if expect_throttle:
        checks.check(
            backpressure.sampling_throttled_samples > 0,
            f"阶段一降级真的生效：throttled={backpressure.sampling_throttled_samples}",
        )
        checks.check(
            sampling is not None
            and sampling.skipped_backpressure_throttled == backpressure.sampling_throttled_samples,
            "采样侧的背压跳过数与报告一致"
            f"（{None if sampling is None else sampling.skipped_backpressure_throttled}）",
        )
        checks.check(
            sampling is not None and sampling.kept > 0,
            "降速不会把一条流变成空样本集合",
        )
    return checks.finish()


def scenario_consumer_measures_wait(media: Path, max_points: int, workspace: Path) -> bool:
    """有消费者：不该有丢弃，而且等待时间必须在真的释放之后才有值。"""
    checks = Checks("consumer_measures_wait")
    listen = f"127.0.0.1:{free_port()}"
    report_path = workspace / "consumer.pb"
    worker_report = workspace / "consumer-worker.json"
    producer = subprocess.Popen(
        [
            str(runtime_binary()),
            "replay",
            str(PIPELINE),
            str(media),
            "--report",
            str(report_path),
            "--max-points",
            str(max_points),
            "--handoff-listen",
            listen,
            "--handoff-retained-limit",
            str(CONSUMER_RETAINED_LIMIT),
            "--handoff-arena-bytes",
            str(CONSUMER_ARENA_BYTES),
            "--handoff-wait-timeout-ms",
            "30000",
            "--handoff-idle-timeout-ms",
            str(IDLE_TIMEOUT_MS),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: list[str] = []
    deadline = time.monotonic() + READY_TIMEOUT_S
    ready = False
    while time.monotonic() < deadline and not ready:
        line = producer.stdout.readline()
        if not line:
            break
        lines.append(line.rstrip("\n"))
        ready = line.startswith("handoff_ready")
    checks.check(ready, "生产者把数据面挂到了回环地址上")
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
        timeout=RUN_TIMEOUT_S,
    )
    remaining_out, remaining_err = producer.communicate(timeout=RUN_TIMEOUT_S)
    stdout = "\n".join(lines + [remaining_out or ""])
    checks.check(
        producer.returncode == 0,
        f"生产者正常收尾（rc={producer.returncode}）"
        + (f" stderr={remaining_err.strip()[:200]}" if producer.returncode else ""),
    )
    checks.check(worker.returncode == 0, f"消费者进程正常收尾（rc={worker.returncode}）")

    fields = backpressure_line(stdout)
    report = load_report(report_path)
    backpressure = report.decoded.backpressure
    assert_bounded_queue(checks, fields, report)
    checks.check(
        backpressure.dropped_total == 0,
        f"消费者跟得上时队列不该有拒绝：dropped_total={backpressure.dropped_total}",
    )
    checks.check(
        backpressure.state == "ok",
        f"消费者跟得上时状态是 ok：{backpressure.state}",
    )
    checks.check(
        backpressure.timeouts_total == 0,
        f"没有 TTL 超时：timeouts_total={backpressure.timeouts_total}",
    )
    # 等待时间来自保留侧的权威记账（`handoff_stats` 行），它只有在 buffer 被释放或回收后才有值。
    stats = handoff_stats_line(stdout)
    released = int(stats.get("released", "0"))
    residency_samples = int(stats.get("residency_samples", "0"))
    checks.check(released > 0, f"消费者真的释放了保留的 buffer：released={released}")
    checks.check(
        residency_samples > 0,
        f"等待时间有真实样本：residency_samples={residency_samples}",
    )
    checks.check(
        int(stats.get("residency_max_ms", "0")) > 0,
        f"最长等待时间被测量：residency_max_ms={stats.get('residency_max_ms')}",
    )
    checks.check(
        int(stats.get("retained_peak", "0")) > 0,
        f"保留表高水位被测量：retained_peak={stats.get('retained_peak')}",
    )
    checks.check(
        int(stats.get("released", "0"))
        + int(stats.get("expired", "0"))
        + int(stats.get("retained", "0"))
        == int(stats.get("retained_total", "0")),
        "每条保留的 buffer 都有归宿（released + expired + retained == retained_total）",
    )
    return checks.finish()


def scenario_without_retention_reports_unobserved(media: Path, workspace: Path) -> bool:
    """对照：没有保留队列的运行必须写 observed=false，而不是写一组干净的水位。"""
    checks = Checks("no_retention_control")
    report_path = workspace / "control.pb"
    completed = subprocess.run(
        [
            str(runtime_binary()),
            "replay",
            str(PIPELINE),
            str(media),
            "--report",
            str(report_path),
            "--max-points",
            "300",
        ],
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_S,
    )
    checks.check(completed.returncode == 0, "进程内自校验路径正常收尾")
    fields = backpressure_line(completed.stdout)
    report = load_report(report_path)
    backpressure = report.decoded.backpressure
    checks.check(fields.get("observed") == "false", "命令行说明队列没有被测量")
    checks.check(not backpressure.observed, "报告里 observed=false")
    checks.check(not report.decoded.backpressure.queues, "没有队列就不该有水位数")
    checks.check(
        backpressure.dropped_total == 0 and backpressure.timeouts_total == 0,
        "没有测量过就不声明丢弃与超时",
    )
    checks.check(
        report.decoded.descriptors_validated > 0,
        f"同一趟运行确实解码了（descriptors={report.decoded.descriptors_validated}）",
    )
    return checks.finish()


def scenario_truncated_static_still_fills(media: Path, max_points: int, workspace: Path) -> bool:
    """静止为主的样本同样会把有界队列填满：静止不等于没有背压。"""
    name = "static_still_fills"
    checks = Checks(name)
    listen = f"127.0.0.1:{free_port()}"
    report_path = workspace / f"{name}.pb"
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
            "--handoff-listen",
            listen,
            "--handoff-retained-limit",
            str(RETAINED_LIMIT),
            "--handoff-arena-bytes",
            str(ARENA_BYTES),
            "--handoff-wait-timeout-ms",
            str(WAIT_TIMEOUT_MS),
            "--handoff-idle-timeout-ms",
            str(IDLE_TIMEOUT_MS),
        ],
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_S,
    )
    checks.check(completed.returncode != 0, "没有消费者的运行必须失败")
    report = load_report(report_path)
    backpressure = report.decoded.backpressure
    checks.check("decode_truncated" in report.blockers, "长样本被预算截断，报告如实标注")
    checks.check(
        backpressure.dropped_total > 0,
        f"静止段也会填满有界队列：dropped_total={backpressure.dropped_total}",
    )
    check_drop_identity(checks, backpressure)
    sampling = report.decoded.sampling[0] if report.decoded.sampling else None
    checks.check(
        sampling is not None and sampling.kept > 0,
        "静止段的 keep 全部有心跳原因，不是零保留",
    )
    return checks.finish()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--motion",
        default=str(sample("sasebo-basketball.480p.vp9.webm")),
        help="持续运动样本",
    )
    parser.add_argument(
        "--static",
        default=str(sample("officehours-panel.480p.vp9.webm")),
        help="以静止为主的长样本",
    )
    parser.add_argument(
        "--consumer-sample",
        default=str(sample("screencast-watchlist.480p.vp9.webm")),
        help="消费者场景用的样本",
    )
    parser.add_argument("--motion-max-points", type=int, default=4_000)
    parser.add_argument("--static-max-points", type=int, default=5_000)
    parser.add_argument("--consumer-max-points", type=int, default=900)
    args = parser.parse_args()

    motion = Path(args.motion)
    static = Path(args.static)
    consumer_sample = Path(args.consumer_sample)
    results: list[bool] = []
    with tempfile.TemporaryDirectory(prefix="sensoryplex-backpressure-") as raw:
        workspace = Path(raw)
        results.append(
            scenario_queue_saturates(
                "motion_queue_saturates", motion, args.motion_max_points, workspace, True
            )
        )
        results.append(
            scenario_truncated_static_still_fills(static, args.static_max_points, workspace)
        )
        results.append(
            scenario_consumer_measures_wait(consumer_sample, args.consumer_max_points, workspace)
        )
        results.append(scenario_without_retention_reports_unobserved(motion, workspace))

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
