"""ADR-032 容器常驻发布与结果融合；队列不取代 PostgreSQL 状态。"""

import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path

import nats
import psycopg
from edge_material_sdk.enrichments import require_stream, subscription
from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2 as pb
from google.protobuf.message import DecodeError

from .infrastructure import enrichments as store
from .settings import Settings


async def fuse_delivery(message, url):
    """非法通知终止投递，暂时性存储失败仍由有界队列重投。"""
    try:
        if len(message.data) > 1024:
            raise ValueError("enrichment_result_notification_invalid")
        reference = pb.EnrichmentResultReference.FromString(message.data)
        if not re.fullmatch(r"enr_[a-f0-9]{32}", reference.task_id) or not re.fullmatch(
            r"sha256:[a-f0-9]{64}", reference.result_digest
        ):
            raise ValueError("enrichment_result_notification_invalid")
        with psycopg.connect(url, connect_timeout=5) as conn:
            conn.execute("SET LOCAL statement_timeout='15s'")
            state = store.fuse(conn, reference)
    except (DecodeError, ValueError) as error:
        reason = (
            str(error)
            if isinstance(error, ValueError)
            else "enrichment_result_notification_invalid"
        )
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,119}", reason):
            reason = "enrichment_result_notification_invalid"
        if isinstance(error, ValueError) and reason != "enrichment_result_notification_invalid":
            with psycopg.connect(url, connect_timeout=5) as conn:
                conn.execute("SET LOCAL statement_timeout='15s'")
                store.reject_staged_result(conn, reference, reason)
        await message.term()
        print(
            json.dumps({"event": "enrichment.notification.rejected", "reason": reason}), flush=True
        )
        return
    except Exception:
        await message.nak(delay=5)
        raise
    await message.ack()
    print(
        json.dumps({"event": "enrichment.fused", "task_id": reference.task_id, "state": state}),
        flush=True,
    )


async def publish_tick(js, url):
    for table, subject in [
        ("enrichment_outbox", None),
        ("enrichment_result_outbox", store.RESULT_SUBJECT),
    ]:
        with psycopg.connect(url, connect_timeout=5) as conn:
            conn.execute("SET LOCAL statement_timeout='15s'")
            # 先提交有界发布租约，再进行网络 IO；不在行锁事务中等待 NATS。
            rows = conn.execute(
                f"WITH pending AS (SELECT task_id FROM {table} WHERE published_at IS NULL "
                "AND (claim_until IS NULL OR claim_until<now()) ORDER BY task_id "
                "LIMIT 10 FOR UPDATE SKIP LOCKED) "
                f"UPDATE {table} o SET claim_until=now()+interval '60 seconds',"
                "attempts=attempts+1 FROM pending p WHERE o.task_id=p.task_id "
                "RETURNING o.task_id,o.contract_bytes,o.claim_until"
                + (",o.subject" if subject is None else "")
            ).fetchall()
        for record in rows:
            task_id, data = record[:2]
            claim_until = record[2]
            destination = subject or record[3]
            await js.publish(
                destination, data, headers={"Nats-Msg-Id": table + ":" + task_id}, timeout=5
            )
            with psycopg.connect(url, connect_timeout=5) as conn:
                conn.execute("SET LOCAL statement_timeout='15s'")
                conn.execute(
                    f"UPDATE {table} SET published_at=now(),claim_until=NULL "
                    "WHERE task_id=%s AND claim_until=%s AND published_at IS NULL",
                    (task_id, claim_until),
                )
    with psycopg.connect(url, connect_timeout=5) as conn:
        conn.execute("SET LOCAL statement_timeout='15s'")
        store.expire(conn)


async def run(mode):
    settings = Settings()
    url = settings.database_url.get_secret_value()
    bus = await nats.connect(
        os.environ.get("SENSORYPLEX_NATS_URL", "nats://nats:4222"),
        name="sensoryplex-enrichment-" + mode,
        connect_timeout=5,
        max_reconnect_attempts=32,
    )
    js = bus.jetstream()
    await require_stream(js, create=mode == "publisher")
    pull = (
        await subscription(js, store.RESULT_SUBJECT, "enrichment-fuser-v1")
        if mode == "fuser"
        else None
    )
    print(json.dumps({"event": "enrichment." + mode + ".ready"}), flush=True)
    status_path = Path("/workspace/.data/events/enrichment-" + mode + "-status.json")
    status_path.parent.mkdir(parents=True, exist_ok=True)

    def heartbeat(state, reason=""):
        temporary = status_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "updated_at_unix_ms": int(time.time() * 1000),
                    "state": state,
                    "reason_code": reason,
                }
            )
        )
        temporary.replace(status_path)

    heartbeat("ready")
    try:
        while True:
            try:
                if mode == "publisher":
                    await publish_tick(js, url)
                    heartbeat("ready")
                    await asyncio.sleep(1)
                    continue
                try:
                    messages = await pull.fetch(1, timeout=1)
                except TimeoutError:
                    heartbeat("ready")
                    continue
                for message in messages:
                    await fuse_delivery(message, url)
                    heartbeat("ready")
            except Exception as error:
                reason = (
                    str(error) if isinstance(error, ValueError) else "enrichment_service_failed"
                )
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,119}", reason):
                    reason = "enrichment_service_failed"
                heartbeat("degraded", reason)
                print(
                    json.dumps({"event": "enrichment." + mode + ".failed", "reason": reason}),
                    flush=True,
                )
                await asyncio.sleep(1)
    finally:
        await bus.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["publisher", "fuser"])
    asyncio.run(run(parser.parse_args().mode))


if __name__ == "__main__":
    main()
