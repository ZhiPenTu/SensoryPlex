"""outbox relay 的契约测试：subject 准入、envelope 对账、stream 契约比较与状态行形状。

测的是**纯判定与形状**：不连 NATS、不连 PostgreSQL、不发布任何事件。真实发布、确认后才写
`published_at`、失败不写、`attempt` 计数、重放去重与 JetStream 的真实往返由
`tools/verify_outbox_relay.py`（真实 PostgreSQL + 真实 NATS JetStream）承担——契约测试通过
**不等于** outbox 已经能发布出去，更不等于下游已经消费。
"""

import asyncio
import json
import time

import pytest
import sensoryplex_relay
from edge_material_sdk.generated.common.v1.common_pb2 import EventEnvelope
from sensoryplex_relay import cli, relay

DIGEST = "sha256:" + "a" * 64


def envelope(event_id: str = "material:m1:1", event_type: str = "material.upserted") -> bytes:
    return EventEnvelope(
        event_id=event_id,
        event_type=event_type,
        stream_id="stream_contract",
        trace_id="trace_contract",
        payload_ref=event_id,
        created_at_unix_ms=1_700_000_000_000,
        schema_version=1,
    ).SerializeToString(deterministic=True)


# ── subject 准入 ────────────────────────────────────────────────────────────


def test_event_type_maps_to_a_subject_without_rewriting_it():
    assert relay.subject_for("material.upserted", "sensoryplex.events") == (
        "sensoryplex.events.material.upserted"
    )
    assert relay.subject_for("a", "x") == "x.a"
    assert relay.subject_for("a.b.c.d", "x") == "x.a.b.c.d"


@pytest.mark.parametrize(
    "event_type",
    [
        "",
        "Material.upserted",
        "material..upserted",
        ".material",
        "material.",
        "material upserted",
        "material.upserted.*",
        "material.upserted.>",
        "*",
        ">",
        "material-upserted",
        "1material",
        "m" * 200,
        "a.b.c.d.e",
    ],
)
def test_illegal_event_types_are_rejected_instead_of_published(event_type):
    """通配符/空格/超出层级不是"换了个名字"，是投成通配订阅或非法 subject——必须拒发。"""
    with pytest.raises(relay.RelayError) as failure:
        relay.subject_for(event_type, "sensoryplex.events")
    assert failure.value.code == "invalid_event_type"


def test_subject_prefix_rejects_wildcards_and_dangling_dot():
    relay.RelayOptions(subject_prefix="sensoryplex.events").validate()
    for prefix in ("", "sensoryplex.events.", "sensoryplex.*", "sensoryplex.>", "x" * 200):
        with pytest.raises(relay.RelayError) as failure:
            relay.RelayOptions(subject_prefix=prefix).validate()
        assert failure.value.code == "invalid_subject_prefix"


def test_stream_filter_is_the_prefix_wildcard_only():
    assert relay.RelayOptions().subjects == ["sensoryplex.events.>"]
    assert relay.RelayOptions(subject_prefix="a.b").subjects == ["a.b.>"]


# ── 选项准入 ────────────────────────────────────────────────────────────────


def test_option_bounds_are_rejected_one_by_one():
    relay.RelayOptions().validate()
    cases = [
        ({"stream": ""}, "invalid_stream_name"),
        ({"stream": "s" * 200}, "invalid_stream_name"),
        ({"batch": 0}, "invalid_batch"),
        ({"batch": relay.MAX_BATCH + 1}, "invalid_batch"),
        ({"interval_s": 0}, "invalid_interval"),
        ({"interval_s": 61}, "invalid_interval"),
        ({"publish_timeout_s": 0}, "invalid_publish_timeout"),
        ({"publish_timeout_s": 61}, "invalid_publish_timeout"),
        ({"connect_timeout_s": 0}, "invalid_connect_timeout"),
        ({"connect_timeout_s": 121}, "invalid_connect_timeout"),
        ({"max_batches": -1}, "invalid_max_batches"),
    ]
    for overrides, code in cases:
        with pytest.raises(relay.RelayError) as failure:
            relay.RelayOptions(**overrides).validate()
        assert failure.value.code == code, overrides


# ── envelope 对账 ───────────────────────────────────────────────────────────


def test_envelope_must_agree_with_the_row_it_was_stored_in():
    parsed = relay.envelope_of(envelope(), expected_event_id="material:m1:1")
    assert parsed.event_type == "material.upserted"
    with pytest.raises(relay.RelayError) as failure:
        relay.envelope_of(envelope(event_id="material:m1:2"), expected_event_id="material:m1:1")
    assert failure.value.code == "event_envelope_mismatch"


def test_unusable_contract_bytes_are_rejected_with_a_stable_code():
    with pytest.raises(relay.RelayError) as failure:
        relay.envelope_of(b"not-a-protobuf", expected_event_id="material:m1:1")
    assert failure.value.code == "invalid_event_contract"
    with pytest.raises(relay.RelayError) as failure:
        relay.envelope_of(envelope(event_type=""), expected_event_id="material:m1:1")
    assert failure.value.code == "invalid_event_contract"
    assert failure.value.detail == "empty_event_type"


# ── stream 契约比较 ─────────────────────────────────────────────────────────


def matching_stream(**overrides) -> dict:
    snapshot = {
        "subjects": ["sensoryplex.events.>"],
        "storage": "file",
        "max_msgs": relay.STREAM_MAX_MSGS,
        "max_bytes": relay.STREAM_MAX_BYTES,
        # 单位是**秒**：nats-py 的 StreamConfig 用秒，纳秒由库自己换算（见 relay.py 的注释）。
        "max_age": relay.STREAM_MAX_AGE_S,
        "duplicate_window": relay.STREAM_DUPLICATE_WINDOW_S,
    }
    snapshot.update(overrides)
    return snapshot


def test_matching_stream_has_no_diff():
    assert relay.stream_contract_diff(matching_stream(), relay.RelayOptions()) == []
    # `StorageType.FILE` 在往返里可能回成 `filestorage`，两种写法都接受。
    assert (
        relay.stream_contract_diff(matching_stream(storage="FileStorage"), relay.RelayOptions())
        == []
    )
    assert relay.stream_contract_diff(matching_stream(storage="FILE"), relay.RelayOptions()) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"subjects": ["sensoryplex.events.material.>"]},
        {"subjects": []},
        {"storage": "memory"},
        {"max_msgs": 0},
        {"max_bytes": 1 << 20},
        {"max_age": 60},
        {"duplicate_window": 60},
    ],
)
def test_any_stream_drift_is_reported_rather_than_repaired(overrides):
    """漂移就报错、不自动改保留策略：静默改小 `max_age` 会丢掉还没被消费的事件。"""
    diff = relay.stream_contract_diff(matching_stream(**overrides), relay.RelayOptions())
    assert diff, overrides


def test_stream_max_age_is_compared_in_seconds_not_nanoseconds():
    """这条钉的是真机踩过的坑：把 7 天写成纳秒会被 nats-py **再乘一次 1e9** 而被服务端拒收。"""
    assert relay.STREAM_MAX_AGE_S == 604_800
    assert relay.STREAM_DUPLICATE_WINDOW_S == 7_200
    nanosecond_snapshot = matching_stream(max_age=relay.STREAM_MAX_AGE_S * 1_000_000_000)
    assert relay.stream_contract_diff(nanosecond_snapshot, relay.RelayOptions())


def test_stream_diff_names_the_field_and_the_expected_value():
    diff = relay.stream_contract_diff(matching_stream(max_age=60), relay.RelayOptions())
    assert len(diff) == 1
    assert diff[0].startswith("max_age=") and "604800" in diff[0]


# ── 状态行形状（AGENTS.md 的日志边界） ──────────────────────────────────────


def test_status_line_carries_counts_and_identifiers_only():
    document = relay.status_document(
        cycle=3,
        options=relay.RelayOptions(),
        claimed=2,
        published=1,
        failed=1,
        totals={"published": 5, "failed": 2},
        pending={"pending": 7, "oldest_pending_age_s": 12.5, "max_attempt": 4},
        error_code="event_publish_failed",
        error_detail="TimeoutError",
    )
    assert set(document) == {
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
    }
    assert document["event"] == "relay.status"
    serialized = json.dumps(document)
    # 载荷、向量、令牌与主机路径一律不进状态行；detail 只放异常**类名**。
    for leaked in ("/Users/", "contract_bytes", "password", "postgresql://", "vector"):
        assert leaked not in serialized


def test_describe_target_drops_the_credentials():
    described = relay.describe_target("postgresql://user:secret@db.example:5432/sensoryplex")
    assert described == "db.example:5432/sensoryplex"
    assert "secret" not in described
    assert relay.describe_target("not a dsn") == "unparsable"


def test_json_line_is_stable_and_keeps_unicode_readable():
    line = relay.json_line({"b": 1, "a": "华东区"})
    assert line == '{"a": "华东区", "b": 1}'


# ── CLI 参数层 ──────────────────────────────────────────────────────────────


def test_out_of_range_arguments_exit_before_touching_any_endpoint(capsys):
    """参数越界给的是一条原因码，不是 traceback，也不是"连上了但什么都不做"的半启动状态。"""
    with pytest.raises(SystemExit) as failure:
        cli.main(["--database-url", "postgresql://x/y", "--batch", "0"])
    assert failure.value.code == "invalid_batch"


def test_missing_database_url_is_an_explicit_exit_code(monkeypatch):
    # 容器里本来就设着 SENSORYPLEX_DATABASE_URL；不摘掉它就不是在测"没给 DSN"，
    # 而是让这条用例真的去连一次 NATS（曾经因此把测试挂死在这里）。
    monkeypatch.delenv("SENSORYPLEX_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as failure:
        cli.main(["--nats-url", "nats://127.0.0.1:24222"])
    assert failure.value.code == "database_url_required"


# ── 启动连接必须**有界**（真机踩过的坑） ────────────────────────────────────


class NeverConnects:
    """假的 nats 模块：`connect` 永不返回，模拟"地址填错 / NATS 没起"。"""

    @staticmethod
    async def connect(*args, **kwargs):
        await asyncio.Event().wait()


class RefusingNats:
    @staticmethod
    async def connect(url, **kwargs):
        raise ConnectionRefusedError("connection refused")


class CapturingNats:
    calls: list = []

    @classmethod
    async def connect(cls, url, **kwargs):
        cls.calls.append((url, kwargs))
        return "fake-client"


async def test_unreachable_nats_fails_bounded_instead_of_retrying_forever():
    """`max_reconnect_attempts=-1` 连**首次**连接都是无限重试，必须由我们加上限。"""
    options = relay.RelayOptions(connect_timeout_s=0.05)
    started = time.monotonic()
    with pytest.raises(relay.RelayError) as failure:
        await relay.connect_bounded(NeverConnects, "nats://127.0.0.1:1", options)
    assert failure.value.code == "nats_unreachable"
    assert failure.value.detail == "TimeoutError"
    assert time.monotonic() - started < 5


async def test_connection_refused_is_reported_as_unreachable():
    options = relay.RelayOptions(connect_timeout_s=1.0)
    with pytest.raises(relay.RelayError) as failure:
        await relay.connect_bounded(RefusingNats, "nats://127.0.0.1:1", options)
    assert failure.value.code == "nats_unreachable"
    assert failure.value.detail == "ConnectionRefusedError"


async def test_connected_client_keeps_infinite_reconnect_for_steady_state():
    """启动有界，稳态无限重连：NATS 重启不该让常驻 relay 退出。"""
    CapturingNats.calls.clear()
    options = relay.RelayOptions(connect_timeout_s=3.0)
    client = await relay.connect_bounded(CapturingNats, "nats://nats:4222", options)
    assert client == "fake-client"
    url, kwargs = CapturingNats.calls[0]
    assert url == "nats://nats:4222"
    assert kwargs["max_reconnect_attempts"] == -1
    assert kwargs["connect_timeout"] == 3.0
    assert kwargs["name"] == "sensoryplex-relay"


def test_once_is_exactly_one_batch():
    parser = cli.build_parser()
    assert parser.parse_args(["--once"]).once is True
    assert parser.parse_args([]).max_batches == 0


def test_package_exposes_no_publish_entry_point_by_accident():
    """relay 只做"发布"这一跳：包名与对外符号必须一致，不能顺手长出消费循环。"""
    assert sensoryplex_relay.__file__.endswith("sensoryplex_relay/__init__.py")
    assert not hasattr(sensoryplex_relay, "consume")
