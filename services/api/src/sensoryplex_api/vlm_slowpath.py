"""VLM 延迟满足常驻进程：任务 Outbox 发布与结果融合。

运行在控制面容器：`publisher` 唯一负责把 Postgres outbox 确认发到 WorkQueue；`fuser`
唯一负责消费结果并以数据库事务写 Observation/Material/Outbox。两个角色都不解码媒体。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib

import psycopg
from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2
from edge_material_sdk.vlm_tasks import RESULT_DURABLE

from .infrastructure import vlm_delayed


def _document(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def _write_status(path: pathlib.Path | None, event: str, **fields) -> None:
    """原子刷新仅含控制面状态的健康凭据，不能把消息正文或 DSN 写入 bind mount。"""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps({"event": event, **fields}, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _report(path: pathlib.Path | None, event: str, **fields) -> None:
    _document(event, **fields)
    _write_status(path, event, **fields)


def _database_url(value: str) -> str:
    if not value:
        raise SystemExit("database_url_required")
    return value


def _nats_url(value: str) -> str:
    if not value:
        raise SystemExit("nats_url_required")
    return value


def _consumer_snapshot(info) -> dict:
    config = info.config
    return {
        "durable_name": config.durable_name or "",
        "filter_subject": config.filter_subject or "",
        "ack_policy": str(getattr(config.ack_policy, "value", config.ack_policy)).lower(),
        "ack_wait": int(config.ack_wait or 0),
        "max_deliver": int(config.max_deliver or 0),
    }


async def _ensure_pull_consumer(
    js, *, durable: str, subject: str, options: vlm_delayed.VlmQueueOptions
):
    import nats.js.api as jsapi

    expected = {
        "durable_name": durable,
        "filter_subject": subject,
        "ack_policy": "explicit",
        "ack_wait": options.ack_wait_s,
        "max_deliver": options.max_deliver,
    }
    try:
        info = await js.consumer_info(options.stream, durable)
    except Exception as error:  # noqa: BLE001
        if type(error).__name__ != "NotFoundError":
            raise vlm_delayed.VlmDelayedError(
                "vlm_task_consumer_unavailable", type(error).__name__
            ) from error
        return await js.add_consumer(
            options.stream,
            config=jsapi.ConsumerConfig(
                durable_name=durable,
                filter_subject=subject,
                ack_policy=jsapi.AckPolicy.EXPLICIT,
                ack_wait=options.ack_wait_s,
                max_deliver=options.max_deliver,
            ),
        )
    actual = _consumer_snapshot(info)
    if actual != expected:
        raise vlm_delayed.VlmDelayedError("vlm_task_consumer_contract_mismatch")
    return info


async def run_publisher(
    *,
    database_url: str,
    nats_url: str,
    batch: int,
    interval_s: float,
    once: bool,
    status_out: pathlib.Path | None,
) -> int:
    options = vlm_delayed.VlmQueueOptions()
    options.validate()
    if not 1 <= batch <= 1_000 or not 0 < interval_s <= 60:
        raise SystemExit("vlm_task_publisher_arguments_invalid")
    client = await vlm_delayed.connect_bounded(nats_url, name="sensoryplex-vlm-publisher")
    try:
        js = client.jetstream()
        await vlm_delayed.ensure_task_stream(js)
        _report(
            status_out,
            "vlm.publisher.ready",
            stream=options.stream,
            subject=options.task_subject,
        )
        with psycopg.connect(database_url) as conn:
            while True:
                outcome = await vlm_delayed.publish_task_outbox_cycle(conn, js, batch=batch)
                _report(status_out, "vlm.publisher.status", **outcome)
                if once:
                    return 0
                await asyncio.sleep(interval_s)
    finally:
        await client.close()


async def run_fuser(*, database_url: str, nats_url: str, status_out: pathlib.Path | None) -> int:
    options = vlm_delayed.VlmQueueOptions()
    options.validate()
    client = await vlm_delayed.connect_bounded(nats_url, name="sensoryplex-vlm-result-fuser")
    try:
        js = client.jetstream()
        await vlm_delayed.require_task_stream(js)
        await _ensure_pull_consumer(
            js,
            durable=RESULT_DURABLE,
            subject=vlm_delayed.RESULT_SUBJECT,
            options=options,
        )
        subscription = await js.pull_subscribe(vlm_delayed.RESULT_SUBJECT, durable=RESULT_DURABLE)
        _report(
            status_out,
            "vlm.fuser.ready",
            stream=options.stream,
            subject=options.result_subject,
            durable=RESULT_DURABLE,
        )
        with psycopg.connect(database_url) as conn:
            while True:
                try:
                    messages = await subscription.fetch(1, timeout=1)
                except TimeoutError:
                    _write_status(status_out, "vlm.fuser.idle")
                    continue
                for message in messages:
                    try:
                        result = orchestration_pb2.VlmTaskResult.FromString(message.data)
                        outcome = vlm_delayed.apply_vlm_result(conn, result)
                    except Exception as error:  # noqa: BLE001 - 不 ACK，让 WorkQueue 重投
                        conn.rollback()
                        _report(
                            status_out,
                            "vlm.fuser.retry",
                            error_code=getattr(error, "code", type(error).__name__),
                        )
                        await message.nak(delay=options.ack_wait_s)
                        continue
                    await message.ack()
                    _report(status_out, "vlm.fuser.status", **outcome)
    finally:
        await client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url", default=os.getenv("SENSORYPLEX_DATABASE_URL", ""), help="PostgreSQL DSN"
    )
    parser.add_argument(
        "--nats-url", default=os.getenv("SENSORYPLEX_NATS_URL", ""), help="NATS URL"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    publisher = commands.add_parser("publisher")
    publisher.add_argument("--batch", type=int, default=64)
    publisher.add_argument("--interval-s", type=float, default=1.0)
    publisher.add_argument("--once", action="store_true")
    publisher.add_argument("--status-out", type=pathlib.Path)
    fuser = commands.add_parser("fuser")
    fuser.add_argument("--status-out", type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_url = _database_url(args.database_url)
    nats_url = _nats_url(args.nats_url)
    if args.command == "publisher":
        return asyncio.run(
            run_publisher(
                database_url=database_url,
                nats_url=nats_url,
                batch=args.batch,
                interval_s=args.interval_s,
                once=args.once,
                status_out=args.status_out,
            )
        )
    return asyncio.run(
        run_fuser(database_url=database_url, nats_url=nats_url, status_out=args.status_out)
    )


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
