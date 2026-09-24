"""outbox relay 端到端验收（ADR-024）：真实 PostgreSQL → 真实 NATS JetStream。

角色（全部是真实服务，没有替身）：

1. 本脚本（编排 + 对账），在 **api 容器内**执行：它要的是真实 PostgreSQL 与真实
   JetStream，这两样都在 compose 里（宿主侧只在 127.0.0.1:24222 暴露 NATS），而
   Milvus Lite / HF 权重 / CoreML 这些"主机专属"资源**一条都不用**——所以这个目标
   没有理由退回主机跑（与 index-check / semantic-check 的理由不同）；
2. `python -m sensoryplex_relay.cli`（一条短命进程，就是镜像里装好的那个入口）；
3. 真实 PostgreSQL（本次新建隔离 schema，跑真实迁移）；
4. 真实 NATS JetStream（本脚本自建一个**独立** stream 与 subject 前缀，用完删掉，
   不碰开发用的 `sensoryplex-events`）。

判定标准（全部来自真实执行）：

- 发布确认后才记账：`published_at` 只在 JetStream 确认之后写；发布失败/契约缺陷时它必须
  保持 NULL——否则"压根没发出去"会被后来的进程读成"已经发过"，事件永久消失；
- 真消息可对账：从 JetStream 拉回来的 subject、`Nats-Msg-Id`、载荷逐字节等于 outbox 行；
- 幂等：以同一 `Nats-Msg-Id` 重发，JetStream 的 duplicate window 必须把它吸收（消息数不变）；
- 漂移不静默：已存在的 stream 与契约不符时**报错并保持原样**，不自动改保留策略；
- NATS 不可达时不糊弄：relay 必须显式失败（exit 1 + `nats_unreachable`），且**一行都不写**；
- sink 侧去重键是 `(event_id, consumer_name)`，重复完成是幂等的 `False`；
- 状态行里没有 DSN、密码、载荷、向量或主机路径。
"""

import argparse
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

import nats  # noqa: E402
import nats.js.api as jsapi  # noqa: E402
import psycopg  # noqa: E402
from edge_material_sdk.generated.common.v1.common_pb2 import EventEnvelope  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402
from sensoryplex_index_worker import records  # noqa: E402
from sensoryplex_relay import relay  # noqa: E402

from tools.migrate import migrate  # noqa: E402

RELAY_MODULE = "sensoryplex_relay.cli"
# 与开发用的 `sensoryplex-events` 完全分开：验收不许动别人的 stream。
STREAM = "sensoryplex-events-acceptance"
PREFIX = "sensoryplex.acceptance.events"
CONSUMER = "sensoryplex-index-worker-acceptance"
UNREACHABLE_NATS = "nats://127.0.0.1:1"
# 漂移用的 max_age：半个契约值，既与原值不同，又仍大于 duplicate_window。
DRIFTED_MAX_AGE_S = relay.STREAM_MAX_AGE_S // 2
CREATED_AT = ("2026-09-24T03:00:01Z", "2026-09-24T03:00:02Z", "2026-09-24T03:00:03Z")


def check(condition, message: str, failures: list[str]) -> bool:
    if not condition:
        failures.append(message)
    return bool(condition)


def run_relay(arguments: list[str], expected_exit: int, failures: list[str]) -> list[dict]:
    """按真实进程跑一次 relay，把它打的 JSON 行解析回来（ready / status / error）。"""
    completed = subprocess.run(
        [sys.executable, "-m", RELAY_MODULE, *arguments],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    documents: list[dict] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        try:
            documents.append(json.loads(line))
        except json.JSONDecodeError:
            failures.append(f"relay printed a non-JSON line: {line[:200]!r}")
    if completed.returncode != expected_exit:
        failures.append(
            f"relay exited {completed.returncode} (expected {expected_exit}): "
            f"stdout={completed.stdout[-300:]!r} stderr={completed.stderr.strip()[-400:]!r}"
        )
    return documents


def document_of(documents: list[dict], event: str) -> dict:
    for document in documents:
        if document.get("event") == event:
            return document
    return {}


def add_event(conn, index: int, *, event_id: str | None = None) -> str:
    """写一条真实的 outbox 行（与 `append_material` 写的是同一种 EventEnvelope）。"""
    identifier = event_id or f"material:acceptance-{index}:1"
    envelope = EventEnvelope(
        event_id=identifier,
        event_type="material.upserted",
        stream_id="stream_outbox_acceptance",
        trace_id="trace_outbox_acceptance",
        payload_ref=identifier,
        created_at_unix_ms=1_700_000_000_000,
        schema_version=1,
    )
    conn.execute(
        "INSERT INTO event_outbox(event_id,event_type,contract_bytes,created_at) "
        "VALUES (%s,%s,%s,%s)",
        (
            identifier,
            "material.upserted",
            envelope.SerializeToString(deterministic=True),
            CREATED_AT[index],
        ),
    )
    return identifier


def outbox_row(conn, event_id: str) -> tuple[bool, int]:
    """(还没发布, 尝试次数)。"""
    return conn.execute(
        "SELECT published_at IS NULL, attempt FROM event_outbox WHERE event_id=%s", (event_id,)
    ).fetchone()


async def verify(database_url: str, nats_url: str, failures: list[str]) -> None:
    admin = psycopg.connect(database_url, autocommit=True)
    schema = "verify_" + uuid.uuid4().hex
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated = make_conninfo(database_url, options=f"-c search_path={schema},public")
    migrate(isolated)

    # 本脚本自己只做短操作：连接**有界**（默认的 60 次重连会让"NATS 没起"这种失败
    # 拖成两分钟才报出来，验收要的是马上、明确地 FAIL）。
    client = await nats.connect(
        nats_url, name="sensoryplex-outbox-acceptance", connect_timeout=5, max_reconnect_attempts=1
    )
    js = client.jetstream()
    try:
        # 先把验收 stream 清干净（上一次崩溃留下的残骸不许干扰本次判定）。
        try:
            await js.delete_stream(STREAM)
        except Exception:  # noqa: BLE001 - 不存在就是干净的
            pass

        # ── 场景 1：目标描述不外泄凭据 ─────────────────────────────────────
        described = run_relay(["--database-url", isolated, "--describe"], 0, failures)
        describe_line = document_of(described, "relay.describe")
        check(
            describe_line.get("target") == relay.describe_target(isolated),
            f"describe did not report the redacted target: {describe_line}",
            failures,
        )
        check("postgresql://" not in json.dumps(describe_line), "describe leaked a DSN", failures)

        arguments = [
            "--database-url",
            isolated,
            "--nats-url",
            nats_url,
            "--stream",
            STREAM,
            "--subject-prefix",
            PREFIX,
        ]

        # ── 场景 2：真实建 stream，并把配置读回来核对契约 ──────────────────
        documents = run_relay([*arguments, "--once"], 0, failures)
        ready = document_of(documents, "relay.ready")
        check(
            ready.get("stream") == STREAM and ready.get("storage") == "file",
            f"relay did not report a file-backed stream: {ready}",
            failures,
        )
        info = await js.stream_info(STREAM)
        config = info.config
        check(
            list(config.subjects or []) == [f"{PREFIX}.>"],
            f"stream subjects drifted: {config.subjects}",
            failures,
        )
        check(
            int(config.max_age or 0) == relay.STREAM_MAX_AGE_S,
            f"stream max_age is not the contract value: {config.max_age}",
            failures,
        )
        check(
            int(config.duplicate_window or 0) == relay.STREAM_DUPLICATE_WINDOW_S,
            f"stream duplicate_window is not the contract value: {config.duplicate_window}",
            failures,
        )
        check(
            int(config.max_msgs or 0) == relay.STREAM_MAX_MSGS
            and int(config.max_bytes or 0) == relay.STREAM_MAX_BYTES,
            f"stream bounds drifted: {config.max_msgs}/{config.max_bytes}",
            failures,
        )

        # ── 场景 3：真实发布，并从 JetStream 拉回来逐字节对账 ──────────────
        subscription = await js.pull_subscribe(f"{PREFIX}.>", stream=STREAM)
        with psycopg.connect(isolated) as conn:
            ids = [add_event(conn, index) for index in (0, 1)]
            rows = {
                event_id: conn.execute(
                    "SELECT contract_bytes FROM event_outbox WHERE event_id=%s", (event_id,)
                ).fetchone()[0]
                for event_id in ids
            }
        documents = run_relay([*arguments, "--once"], 0, failures)
        status = document_of(documents, "relay.status")
        check(
            status.get("claimed") == 2
            and status.get("published") == 2
            and status.get("failed") == 0,
            f"relay did not publish the two real events: {status}",
            failures,
        )
        messages = await subscription.fetch(2, timeout=5)
        check(
            len(messages) == 2, f"JetStream returned {len(messages)} messages, expected 2", failures
        )
        for message in messages:
            event_id = (message.headers or {}).get("Nats-Msg-Id", "")
            check(event_id in rows, f"unexpected Nats-Msg-Id in the stream: {event_id!r}", failures)
            if event_id not in rows:
                continue
            check(
                message.subject == f"{PREFIX}.material.upserted",
                f"wrong subject: {message.subject}",
                failures,
            )
            check(
                message.data == rows[event_id],
                "the published payload is not byte-identical to the outbox row",
                failures,
            )
            await message.ack()

        # ── 场景 4：确认之后才记账 ─────────────────────────────────────────
        with psycopg.connect(isolated) as conn:
            for event_id in ids:
                unpublished, attempt = outbox_row(conn, event_id)
                check(
                    unpublished is False and attempt == 1,
                    f"{event_id} is not recorded as published once: {(unpublished, attempt)}",
                    failures,
                )
            stats = relay.pending_stats(conn)
            check(stats["pending"] == 0, f"pending did not drain: {stats}", failures)

        # ── 场景 5：发不出去就不写 published_at（契约缺陷 + NATS 不可达） ──
        with psycopg.connect(isolated) as conn:
            defect = "material:acceptance-defect:1"
            add_event(conn, 2, event_id=defect)
            mismatched = EventEnvelope(
                event_id="material:someone-else:1",
                event_type="material.upserted",
                stream_id="stream_outbox_acceptance",
                created_at_unix_ms=1_700_000_000_000,
                schema_version=1,
            )
            conn.execute(
                "UPDATE event_outbox SET contract_bytes=%s WHERE event_id=%s",
                (mismatched.SerializeToString(deterministic=True), defect),
            )
        documents = run_relay([*arguments, "--once"], 0, failures)
        status = document_of(documents, "relay.status")
        check(
            status.get("failed") == 1 and status.get("error_code") == "event_envelope_mismatch",
            f"a mismatched envelope was not refused: {status}",
            failures,
        )
        with psycopg.connect(isolated) as conn:
            unpublished, attempt = outbox_row(conn, defect)
            check(
                unpublished is True and attempt == 1,
                f"a refused event was recorded as published: {(unpublished, attempt)}",
                failures,
            )
            check(
                relay.pending_stats(conn)["pending"] == 1,
                "the refused event did not stay pending",
                failures,
            )
        # NATS 连不上：整条命令必须显式失败，而且**什么都不记**。
        documents = run_relay(
            [
                *arguments[:1],
                isolated,
                "--nats-url",
                UNREACHABLE_NATS,
                "--stream",
                STREAM,
                "--subject-prefix",
                PREFIX,
                "--connect-timeout-s",
                "1",
                "--once",
            ],
            1,
            failures,
        )
        error = document_of(documents, "relay.error")
        check(
            error.get("error_code") == "nats_unreachable",
            f"an unreachable NATS was not reported explicitly: {documents}",
            failures,
        )
        check(
            document_of(documents, "relay.status") == {},
            "relay reported a status line even though it never connected",
            failures,
        )
        with psycopg.connect(isolated) as conn:
            check(
                outbox_row(conn, defect) == (True, 1),
                "an unreachable NATS still changed the accounting",
                failures,
            )

        # ── 场景 6：同一 Nats-Msg-Id 重放被 JetStream 吸收 ─────────────────
        replay = await js.publish(
            f"{PREFIX}.material.upserted",
            rows[ids[0]],
            headers={"Nats-Msg-Id": ids[0]},
        )
        check(
            replay.duplicate is True,
            f"JetStream did not deduplicate the replay: {replay}",
            failures,
        )
        info = await js.stream_info(STREAM)
        check(
            info.state.messages == 2, f"the replay added a message: {info.state.messages}", failures
        )

        # ── 场景 7：stream 漂移必须报错且**不**被修好 ──────────────────────
        await js.delete_stream(STREAM)
        await js.add_stream(
            config=jsapi.StreamConfig(
                name=STREAM,
                subjects=[f"{PREFIX}.>"],
                storage=jsapi.StorageType.FILE,
                max_msgs=relay.STREAM_MAX_MSGS,
                max_bytes=relay.STREAM_MAX_BYTES,
                # 只漂移 max_age，且仍大于 duplicate_window：JetStream 拒绝
                # `duplicate_window > max_age`，那种 stream 根本建不出来。
                max_age=DRIFTED_MAX_AGE_S,
                duplicate_window=relay.STREAM_DUPLICATE_WINDOW_S,
            )
        )
        documents = run_relay([*arguments, "--once"], 1, failures)
        error = document_of(documents, "relay.error")
        check(
            error.get("error_code") == "event_stream_contract_mismatch",
            f"a drifted stream was not refused: {documents}",
            failures,
        )
        check(
            "max_age" in str(error.get("error_detail", "")),
            f"the drift report does not name the field: {error}",
            failures,
        )
        info = await js.stream_info(STREAM)
        check(
            int(info.config.max_age or 0) == DRIFTED_MAX_AGE_S,
            "relay silently repaired the retention policy instead of refusing",
            failures,
        )
        await js.delete_stream(STREAM)
        documents = run_relay([*arguments, "--once"], 0, failures)
        check(
            document_of(documents, "relay.ready") != {},
            "relay did not recreate a missing stream",
            failures,
        )
        info = await js.stream_info(STREAM)
        check(
            int(info.config.max_age or 0) == relay.STREAM_MAX_AGE_S,
            f"the recreated stream does not carry the contract: {info.config.max_age}",
            failures,
        )

        # ── 场景 8：sink 侧去重（真 PostgreSQL，真事件 id） ────────────────
        with psycopg.connect(isolated) as conn:
            sink_event = ids[1]
            check(
                records.is_consumed(conn, event_id=sink_event, consumer_name=CONSUMER) is False,
                "a fresh event was already reported as consumed",
                failures,
            )
            first = records.record_consumed(conn, event_id=sink_event, consumer_name=CONSUMER)
            second = records.record_consumed(conn, event_id=sink_event, consumer_name=CONSUMER)
            check(
                first is True and second is False,
                f"consumption is not idempotent: {first}/{second}",
                failures,
            )
            check(
                records.is_consumed(conn, event_id=sink_event, consumer_name=CONSUMER) is True
                and records.consumed_state(conn, event_id=sink_event, consumer_name=CONSUMER)[
                    "consumed_at"
                ],
                "the consumption record is not observable",
                failures,
            )
            check(
                conn.execute("SELECT count(*) FROM consumed_event").fetchone()[0] == 1,
                "a duplicate consumption row was written",
                failures,
            )

        # ── 场景 9：状态行不外泄 ───────────────────────────────────────────
        emitted = json.dumps(documents)
        for leaked in ("postgresql://", schema, "/Users/", "contract_bytes", "password"):
            check(leaked not in emitted, f"the relay status line leaked {leaked!r}", failures)
        check(
            set(status)
            == {
                "event",
                "cycle",
                "stream",
                "subject_prefix",
                "claimed",
                "published",
                "failed",
                "published_total",
                "failed_total",
                "pending",
                "oldest_pending_age_s",
                "max_attempt",
                "error_code",
                "error_detail",
            },
            f"the status line shape drifted: {sorted(status)}",
            failures,
        )
    finally:
        try:
            await js.delete_stream(STREAM)
        except Exception:  # noqa: BLE001 - 收尾尽力而为
            pass
        await client.close()
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("SENSORYPLEX_DATABASE_URL", ""),
        help="元数据库 DSN（容器内用 compose 的 SENSORYPLEX_DATABASE_URL）",
    )
    parser.add_argument(
        "--nats-url",
        default=os.getenv("SENSORYPLEX_NATS_URL", ""),
        help="NATS 地址（容器内是 nats://nats:4222，宿主是 nats://127.0.0.1:24222）",
    )
    arguments = parser.parse_args()
    if not arguments.database_url:
        raise SystemExit("database_url_required")
    if not arguments.nats_url:
        raise SystemExit("nats_url_required")

    failures: list[str] = []
    print(f"database: {relay.describe_target(arguments.database_url)}")
    print(f"nats: {arguments.nats_url}")
    try:
        asyncio.run(verify(arguments.database_url, arguments.nats_url, failures))
    except Exception as error:  # noqa: BLE001 - 连不上就是验收失败，不跳过
        failures.append(f"{type(error).__name__}: {str(error)[:200]}")
    if failures:
        print("\noutbox relay acceptance FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\noutbox relay acceptance: real outbox -> JetStream -> replay dedupe passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
