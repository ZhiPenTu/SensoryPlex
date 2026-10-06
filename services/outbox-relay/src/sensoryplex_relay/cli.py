"""relay 命令行入口（ADR-024）：`run` 是常驻进程，`--once` 给脚本与验收用。

与其它 worker 一致：显式参数、显式失败、退出码可分支。

- 缺 `--database-url` / `--nats-url` 或参数越界：**在连任何东西之前**以 `SystemExit(<原因码>)`
  结束（exit 1），不留下"连上了但什么都不做"的半启动状态；
- stream 契约漂移、NATS 元数据不可用：打印一条 JSON 错误行并以 exit 1 结束；
- 常驻形态收到 SIGTERM/SIGINT：优雅收尾（当前一轮结束后退出，exit 0），不吞掉未发布的事件。
- 支持 `--archive` 执行已发布历史事件的生命周期归档与定点清理。
"""

import argparse
import asyncio
import os
import pathlib
import signal
import sys
import tempfile
import threading

import psycopg
from edge_material_sdk import get_logger

from .archival import archive_and_purge_outbox
from .relay import (
    DEFAULT_BATCH,
    DEFAULT_STREAM,
    DEFAULT_SUBJECT_PREFIX,
    MAX_BATCH,
    RelayError,
    RelayOptions,
    describe_target,
    json_line,
    run_relay,
)
from .residency import ResidencyError, read_event_backpressure

LOGGER = get_logger("sensoryplex.relay")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sensoryplex-relay", description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("SENSORYPLEX_DATABASE_URL", ""),
        help="元数据库 DSN（也可用 SENSORYPLEX_DATABASE_URL）",
    )
    parser.add_argument(
        "--nats-url",
        default=os.getenv("SENSORYPLEX_NATS_URL", "nats://127.0.0.1:24222"),
        help="NATS 地址（也可用 SENSORYPLEX_NATS_URL）",
    )
    parser.add_argument("--stream", default=DEFAULT_STREAM, help="JetStream stream 名")
    parser.add_argument(
        "--subject-prefix", default=DEFAULT_SUBJECT_PREFIX, help="subject 前缀（不含通配符）"
    )
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help=f"1..{MAX_BATCH}")
    parser.add_argument("--interval-s", type=float, default=1.0, help="两轮之间的间隔（0, 60]")
    parser.add_argument("--publish-timeout-s", type=float, default=5.0, help="单条发布超时（0, 60]")
    parser.add_argument(
        "--connect-timeout-s", type=float, default=10.0, help="启动连接超时（0, 120]；连不上即失败"
    )
    parser.add_argument("--max-batches", type=int, default=0, help="跑满 N 轮后退出；0=常驻")
    parser.add_argument("--once", action="store_true", help="等价于 --max-batches 1")
    parser.add_argument("--status-out", type=pathlib.Path, default=None, help="状态行的落盘位置")
    parser.add_argument(
        "--describe", action="store_true", help="只打印目标（凭据已抹除）与参数，然后退出"
    )
    parser.add_argument(
        "--archive", action="store_true", help="执行已确认发布的历史事件归档与清理 (TTL 策略)"
    )
    parser.add_argument(
        "--safety-window-days", type=int, default=7, help="归档安全窗口天数 (默认 7 天)"
    )
    return parser


class StatusWriter:
    """状态行同时进 stdout 与（可选的）文件；文件用临时文件 + rename 原子替换。"""

    def __init__(self, out: pathlib.Path | None):
        self._out = out

    def __call__(self, document: dict) -> None:
        line = json_line(document)
        print(line, flush=True)
        if self._out is None:
            return
        self._out.parent.mkdir(parents=True, exist_ok=True)
        handle, path = tempfile.mkstemp(dir=str(self._out.parent), prefix=".relay-status-")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(line + "\n")
        os.replace(path, self._out)


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.safety_window_days < 1:
        raise SystemExit("invalid_safety_window_days")

    if arguments.archive:
        if not arguments.database_url:
            raise SystemExit("database_url_required")
        with psycopg.connect(arguments.database_url) as conn:
            result = archive_and_purge_outbox(conn, arguments.safety_window_days)
            print(json_line({"event": "outbox.archived", **result}))
        return 0

    options = RelayOptions(
        stream=arguments.stream,
        subject_prefix=arguments.subject_prefix,
        batch=arguments.batch,
        interval_s=arguments.interval_s,
        publish_timeout_s=arguments.publish_timeout_s,
        connect_timeout_s=arguments.connect_timeout_s,
        max_batches=1 if arguments.once else arguments.max_batches,
    )
    try:
        options.validate()
    except RelayError as error:
        # 参数越界在连数据库/NATS 之前就失败：给的是原因码，不是 traceback。
        raise SystemExit(error.code) from error
    if not arguments.database_url:
        raise SystemExit("database_url_required")
    try:
        # 分级背压准入（ADR-027）：每轮认领深度超本档上限就**不启动**——与参数越界同一位置，
        # 都在连数据库/NATS 之前；夹取会让配置里的数与真实在飞的数长期不一致。
        backpressure = read_event_backpressure(options.batch)
    except ResidencyError as error:
        raise SystemExit(error.code) from error
    if arguments.describe:
        print(
            json_line(
                {
                    "event": "relay.describe",
                    "target": describe_target(arguments.database_url),
                    "stream": options.stream,
                    "subject_prefix": options.subject_prefix,
                    "batch": options.batch,
                    "max_batches": options.max_batches,
                    **backpressure.document(),
                }
            )
        )
        return 0

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    LOGGER.info(
        "Starting outbox relay",
        stream=options.stream,
        subject_prefix=options.subject_prefix,
        batch=options.batch,
    )
    emit = StatusWriter(arguments.status_out)
    try:
        return asyncio.run(
            run_relay(
                database_url=arguments.database_url,
                nats_url=arguments.nats_url,
                options=options,
                backpressure=backpressure,
                emit=emit,
                stop=stop,
            )
        )
    except RelayError as error:
        LOGGER.error(
            "Outbox relay error", stream=options.stream, error_code=error.code, detail=error.detail
        )
        emit(
            {
                "event": "relay.error",
                "stream": options.stream,
                "subject_prefix": options.subject_prefix,
                "error_code": error.code,
                "error_detail": error.detail,
            }
        )
        return 1
    except KeyboardInterrupt:  # pragma: no cover - 信号处理器已覆盖
        return 0


if __name__ == "__main__":
    sys.exit(main())
