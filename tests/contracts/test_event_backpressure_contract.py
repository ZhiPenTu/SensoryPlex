"""事件链路的分级背压准入契约（ADR-027，接 ADR-019 §2/§3 的口径）。

测的是**判定与形状**：读哪个变量、三种输入三种结果、越界在哪一步失败、状态行带哪些字段。
真实链路（真实 PostgreSQL + 真 JetStream + 真实 BGE + 真实 Milvus Lite，两个进程都在 compose
里常驻）由 `tools/verify_event_pipeline.py` 承担；契约测试通过**不等于**事件真的被消费成了向量。
"""

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sensoryplex_index_worker import (  # noqa: E402
    BASE_ENVIRON,
    consumer,  # noqa: E402
)
from sensoryplex_index_worker import cli as index_cli  # noqa: E402
from sensoryplex_relay import cli as relay_cli  # noqa: E402
from sensoryplex_relay import relay, residency  # noqa: E402

# 变量名只从实现里取：测试写的字符串与实现读的字符串必须是同一个（否则会一致地误判）。
CAPACITY_VAR = residency.CAPACITY_VAR
TIER_VAR = residency.TIER_VAR
# 状态行里的四个分级字段：声明值 / 上限 / 档位 / 状态。它们不是"看起来在跑"的装饰，
# 而是准入的结论；`capacity == 0` 只与 `not_injected` 同时出现。
INFLIGHT_FIELDS = {
    "inflight_state",
    "inflight_declared",
    "inflight_capacity",
    "resident_tier",
}


# ── 判定：三种输入、三种结果（ADR-019 §3 的逐字对齐） ──────────────────────


def test_no_tier_file_means_not_injected_with_an_unknown_capacity():
    state = residency.read_event_backpressure(200, environ={})
    assert (state.state, state.tier, state.declared, state.capacity) == (
        residency.NOT_INJECTED,
        residency.NOT_INJECTED,
        200,
        0,
    )
    # "没有上限可判"不等于"上限是 0 条"：状态字符串把这件事说清楚。
    assert state.document() == {
        "inflight_state": "not_injected",
        "inflight_declared": 200,
        "inflight_capacity": 0,
        "resident_tier": "not_injected",
    }


def test_a_tier_within_the_cap_is_admitted_and_keeps_the_numbers():
    state = residency.read_event_backpressure(16, environ={TIER_VAR: "small", CAPACITY_VAR: "16"})
    assert (state.state, state.tier, state.declared, state.capacity) == (
        residency.ADMITTED,
        "small",
        16,
        16,
    )


def test_a_declaration_above_the_cap_names_both_numbers():
    with pytest.raises(residency.ResidencyError) as caught:
        residency.read_event_backpressure(200, environ={TIER_VAR: "small", CAPACITY_VAR: "16"})
    assert caught.value.code == (
        "event_inflight_exceeds_tier_cap: declared=200 tier_capacity=16 tier=small"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", f"invalid_resident_limit: {CAPACITY_VAR} is set but empty"),
        ("0", f"invalid_resident_limit: {CAPACITY_VAR}=0"),
        ("-1", f"invalid_resident_limit: {CAPACITY_VAR}=-1"),
        ("16.0", f"invalid_resident_limit: {CAPACITY_VAR}=16.0"),
        ("abc", f"invalid_resident_limit: {CAPACITY_VAR}=abc"),
    ],
)
def test_a_broken_capacity_is_a_startup_failure_not_a_default(raw, expected):
    """原因码与 `crates/runtime` 的 `parse_resident_limit` 逐字对齐：同一套码，两种语言。"""

    with pytest.raises(residency.ResidencyError) as caught:
        residency.read_event_backpressure(1, environ={CAPACITY_VAR: raw})
    assert caught.value.code == expected


def test_a_set_but_empty_tier_name_is_a_broken_config():
    with pytest.raises(residency.ResidencyError) as caught:
        residency.read_event_backpressure(1, environ={TIER_VAR: "  "})
    assert caught.value.code == f"invalid_resident_limit: {TIER_VAR} is set but empty"


def test_surrounding_whitespace_is_trimmed_like_the_rust_side():
    """`parse_resident_limit` 先在 Rust 侧 trim；两侧口径不一致会让同一个 resident.env 两种结论。"""

    state = residency.read_event_backpressure(4, environ={CAPACITY_VAR: " 8 "})
    assert (state.state, state.capacity) == (residency.ADMITTED, 8)


def test_a_tier_without_a_capacity_is_still_not_injected():
    """只注入了档位名（比如手工 source 了半个 resident.env）：不猜上限，也不假装已准入。"""

    state = residency.read_event_backpressure(4, environ={TIER_VAR: "large"})
    assert state.state == residency.NOT_INJECTED
    assert state.tier == "large"
    assert state.capacity == 0


# ── 落到两个入口：越界在"连任何东西之前"失败 ───────────────────────────────


def test_relay_refuses_an_oversized_claim_depth_before_connecting(monkeypatch):
    monkeypatch.setenv(TIER_VAR, "small")
    monkeypatch.setenv(CAPACITY_VAR, "16")
    with pytest.raises(SystemExit) as failure:
        relay_cli.main(["--database-url", "postgresql://x/y", "--batch", "200"])
    assert failure.value.code == (
        "event_inflight_exceeds_tier_cap: declared=200 tier_capacity=16 tier=small"
    )


def test_relay_describe_reports_the_admission_result(monkeypatch, capsys):
    monkeypatch.setenv(TIER_VAR, "medium")
    monkeypatch.setenv(CAPACITY_VAR, "32")
    arguments = ["--database-url", "postgresql://x/y", "--batch", "32", "--describe"]
    assert relay_cli.main(arguments) == 0
    document = json.loads(capsys.readouterr().out.strip())
    assert document["event"] == "relay.describe"
    assert document["inflight_state"] == "admitted"
    assert document["inflight_capacity"] == 32


def test_serve_consume_refuses_an_oversized_inflight_depth(monkeypatch, tmp_path):
    """`serve --consume` 的批次就是 durable 的 `max_ack_pending`：超上限必须拒绝，不能夹取。"""

    # 检索面只读 import 期取好的快照（ADR-027 §10 第 4 条）：档位要注进快照，注 `os.environ` 没用。
    monkeypatch.setitem(BASE_ENVIRON, TIER_VAR, "small")
    monkeypatch.setitem(BASE_ENVIRON, CAPACITY_VAR, "16")
    uri = tmp_path / "never-created.db"
    with pytest.raises(SystemExit) as failure:
        index_cli.main(
            [
                "--uri",
                str(uri),
                "--database-url",
                "postgresql://x/y",
                "serve",
                "--vector-index-key",
                "material_text_bge_small_zh_v1_5_d512_v1",
                "--model-dir",
                "/tmp",
                "--auth-token",
                "contract-token",
                "--consume",
                "--consume-batch",
                "50",
            ]
        )
    assert failure.value.code == (
        "event_inflight_exceeds_tier_cap: declared=50 tier_capacity=16 tier=small"
    )
    # 失败发生在"抢向量库目录"之前：不能留下一个半建的库。
    assert not uri.exists()


def test_serve_without_consume_has_no_backpressure_to_report(monkeypatch):
    """只做检索面（不开 `--consume`）时没有在飞深度可言，所以是 None 而不是 0。"""

    monkeypatch.setitem(BASE_ENVIRON, CAPACITY_VAR, "16")
    arguments = index_cli.build_parser().parse_args(
        [
            "--database-url",
            "postgresql://x/y",
            "serve",
            "--vector-index-key",
            "material_text_bge_small_zh_v1_5_d512_v1",
            "--model-dir",
            "/tmp",
        ]
    )
    assert index_cli.consume_options_of(arguments) is None
    assert index_cli.consume_backpressure_of(None) is None


# ── 状态行：两个服务的准入结论可观察、且不外泄 ──────────────────────────────


def test_relay_status_line_carries_the_admission_and_nothing_sensitive(monkeypatch):
    monkeypatch.setenv(TIER_VAR, "large")
    monkeypatch.setenv(CAPACITY_VAR, "64")
    document = relay.status_document(
        cycle=1,
        options=relay.RelayOptions(),
        backpressure=residency.read_event_backpressure(64),
        claimed=1,
        published=1,
        failed=0,
        totals={"published": 1, "failed": 0},
        pending={"pending": 0, "oldest_pending_age_s": None, "max_attempt": 0},
        error_code="",
        error_detail="",
    )
    assert INFLIGHT_FIELDS <= set(document)
    text = json.dumps(document, ensure_ascii=False)
    for leaked in ("postgresql://", "/Users/", "nats://", "secret"):
        assert leaked not in text


def test_consumer_status_line_carries_the_admission(monkeypatch):
    monkeypatch.setenv(TIER_VAR, "medium")
    monkeypatch.setenv(CAPACITY_VAR, "32")
    document = consumer.status_document(
        cycle=1,
        # 在飞深度必须落在本档上限内，所以这里显式取一条等于上限的声明值。
        options=consumer.ConsumerOptions(batch=32),
        backpressure=residency.read_event_backpressure(32),
        received=0,
        consumed=0,
        skipped=0,
        failed=0,
        embedded=0,
        totals={"consumed": 0, "duplicate": 0, "skipped": 0, "failed": 0},
    )
    assert document["inflight_declared"] == 32
    assert document["inflight_capacity"] == 32
    assert document["resident_tier"] == "medium"
