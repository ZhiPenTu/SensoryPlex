"""ADR-029 P1 集成测试：真实 PostgreSQL 验证 Run/Task 持久化、调度、取消、重试与租约恢复。"""

import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sensoryplex_api.app import create_app
from sensoryplex_api.auth import password_hash
from sensoryplex_api.settings import Settings

from tools.migrate import migrate

pytestmark = pytest.mark.integration
PASSWORD = "test-orchestration-pass-2026"


@pytest.fixture
def orch_database():
    url = os.environ["SENSORYPLEX_TEST_DATABASE_URL"]
    schema = "orch_test_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated = make_conninfo(url, options=f"-c search_path={schema}")
    try:
        migrate(isolated)
        with psycopg.connect(isolated) as conn:
            conn.execute(
                "INSERT INTO console_user(username,display_name,password_hash,roles) "
                "VALUES (%s,%s,%s,%s)",
                ("admin", "Admin", password_hash(PASSWORD), ["admin", "operator"]),
            )
        yield isolated
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def client(orch_database):
    settings = Settings(database_url=orch_database)
    app = create_app(settings)
    with TestClient(app) as test_client:
        login = test_client.post(
            "/auth/v1/session", json={"username": "admin", "password": PASSWORD}
        )
        assert login.status_code == 200
        test_client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        yield test_client


def test_pipeline_validation_and_immutability(client, orch_database):
    # 1. 环检测
    cyclic_res = client.post(
        "/v1/orchestration/pipelines:validate",
        json={
            "nodes": [
                {
                    "id": "a",
                    "plugin_id": "p.a",
                    "consumes": ["y"],
                    "produces": ["x"],
                    "deadline_ms": 1000,
                },
                {
                    "id": "b",
                    "plugin_id": "p.b",
                    "consumes": ["x"],
                    "produces": ["y"],
                    "deadline_ms": 1000,
                },
            ],
            "edges": [
                {"from": "a", "to": "b", "modality": "x", "join": "same_stream_window"},
                {"from": "b", "to": "a", "modality": "y", "join": "same_stream_window"},
            ],
        },
    )
    assert cyclic_res.status_code == 200
    assert cyclic_res.json()["valid"] is False
    assert "orchestration_cycle_detected" in cyclic_res.json()["errors"]

    # 2. 合法图发布
    pub_res = client.post(
        "/v1/orchestration/pipelines",
        json={
            "pipeline_id": "ocr-pipe",
            "name": "OCR Pipeline",
            "nodes": [
                {
                    "id": "src",
                    "plugin_id": "p.src",
                    "consumes": [],
                    "produces": ["media.video_frame"],
                    "placement": "data_plane_local",
                    "deadline_ms": 5000,
                    "max_attempts": 2,
                },
                {
                    "id": "ocr",
                    "plugin_id": "p.ocr",
                    "consumes": ["media.video_frame"],
                    "produces": ["obs.text"],
                    "placement": "data_plane_local",
                    "deadline_ms": 5000,
                    "max_attempts": 2,
                },
            ],
            "edges": [
                {
                    "from": "src",
                    "to": "ocr",
                    "modality": "media.video_frame",
                    "join": "same_item",
                    "required": True,
                },
            ],
        },
    )
    assert pub_res.status_code == 200
    assert pub_res.json()["revision"]["revision"] == 1
    assert pub_res.json()["revision"]["graph_digest"].startswith("sha256:")

    # 3. 数据库触发器阻止修改 revision
    with psycopg.connect(orch_database) as conn:
        with pytest.raises(psycopg.Error) as exc_info:
            conn.execute(
                "UPDATE pipeline_revision SET graph_digest='sha256:hack' "
                "WHERE pipeline_id='ocr-pipe'"
            )
        assert "immutable_fact_requires_new_revision" in str(exc_info.value)


def test_run_lifecycle_and_downstream_unlock(client):
    # 发布
    client.post(
        "/v1/orchestration/pipelines",
        json={
            "pipeline_id": "flow-1",
            "nodes": [
                {
                    "id": "n1",
                    "plugin_id": "p1",
                    "consumes": [],
                    "produces": ["m1"],
                    "placement": "data_plane_local",
                    "deadline_ms": 5000,
                },
                {
                    "id": "n2",
                    "plugin_id": "p2",
                    "consumes": ["m1"],
                    "produces": ["m2"],
                    "placement": "data_plane_local",
                    "deadline_ms": 5000,
                },
            ],
            "edges": [
                {"from": "n1", "to": "n2", "modality": "m1", "join": "same_item", "required": True},
            ],
        },
    )

    # 提交
    sub_res = client.post(
        "/v1/orchestration/runs",
        json={
            "pipeline_id": "flow-1",
            "revision": 1,
            "input_ref": "video://1",
            "idempotency_key": "k1",
        },
    )
    assert sub_res.status_code == 201
    run_id = sub_res.json()["run"]["run_id"]
    tasks = {t["node_id"]: t for t in sub_res.json()["tasks"]}
    assert tasks["n1"]["state"] == "PIPELINE_TASK_STATE_READY"
    assert tasks["n2"]["state"] == "PIPELINE_TASK_STATE_PENDING"

    # 幂等提交
    dup_res = client.post(
        "/v1/orchestration/runs",
        json={
            "pipeline_id": "flow-1",
            "revision": 1,
            "input_ref": "video://1",
            "idempotency_key": "k1",
        },
    )
    assert dup_res.json()["is_duplicate"] is True
    assert dup_res.json()["run"]["run_id"] == run_id

    # 远程节点不可调度 data_plane_local
    step_rej = client.post(
        "/v1/orchestration/scheduler:step", json={"node_id": "remote", "is_co_located": False}
    )
    assert len([a for a in step_rej.json()["assigned_tasks"] if a["run_id"] == run_id]) == 0

    # 本地节点调度
    step_ok = client.post(
        "/v1/orchestration/scheduler:step", json={"node_id": "local", "is_co_located": True}
    )
    n1_asgn = next(
        (
            a
            for a in step_ok.json()["assigned_tasks"]
            if a["run_id"] == run_id and a["node_id"] == "n1"
        ),
        None,
    )
    assert n1_asgn is not None
    assert n1_asgn["attempt"] == 1

    # 完成 n1，解锁 n2
    rep_res = client.post(
        f"/v1/orchestration/tasks/{tasks['n1']['task_id']}:result",
        json={
            "run_id": run_id,
            "attempt": 1,
            "assignment_id": n1_asgn["assignment_id"],
            "success": True,
            "output_ref": "buf://1",
        },
    )
    assert rep_res.status_code == 200
    assert len(rep_res.json()["unlocked_task_ids"]) == 1

    # 再次调度 n2
    step_n2 = client.post(
        "/v1/orchestration/scheduler:step", json={"node_id": "local", "is_co_located": True}
    )
    n2_asgn = next(
        (
            a
            for a in step_n2.json()["assigned_tasks"]
            if a["run_id"] == run_id and a["node_id"] == "n2"
        ),
        None,
    )
    assert n2_asgn is not None

    # 完成 n2，整体成功
    rep_n2 = client.post(
        f"/v1/orchestration/tasks/{tasks['n2']['task_id']}:result",
        json={
            "run_id": run_id,
            "attempt": 1,
            "assignment_id": n2_asgn["assignment_id"],
            "success": True,
            "output_ref": "buf://2",
        },
    )
    assert rep_n2.json()["run_completed"] is True
    run_state = client.get(f"/v1/orchestration/runs/{run_id}").json()
    assert run_state["run"]["state"] == "PIPELINE_RUN_STATE_SUCCEEDED"


def test_cancellation_and_late_result_guard(client):
    client.post(
        "/v1/orchestration/pipelines",
        json={
            "pipeline_id": "flow-cancel",
            "nodes": [
                {
                    "id": "c1",
                    "plugin_id": "p1",
                    "consumes": [],
                    "produces": ["m1"],
                    "deadline_ms": 5000,
                },
                {
                    "id": "c2",
                    "plugin_id": "p2",
                    "consumes": ["m1"],
                    "produces": ["m2"],
                    "deadline_ms": 5000,
                },
            ],
            "edges": [
                {"from": "c1", "to": "c2", "modality": "m1", "join": "same_item"},
            ],
        },
    )
    sub = client.post(
        "/v1/orchestration/runs",
        json={
            "pipeline_id": "flow-cancel",
            "revision": 1,
            "input_ref": "video://c",
            "idempotency_key": "kc",
        },
    ).json()
    run_id = sub["run"]["run_id"]
    t1_id = next(t["task_id"] for t in sub["tasks"] if t["node_id"] == "c1")

    # 调度 c1
    sched = client.post("/v1/orchestration/scheduler:step", json={"is_co_located": True}).json()
    asgn = next(a for a in sched["assigned_tasks"] if a["run_id"] == run_id)

    # 取消 Run
    cancel_resp = client.post(
        f"/v1/orchestration/runs/{run_id}:cancel", json={"reason": "cancelled_by_test"}
    ).json()
    assert cancel_resp["run"]["state"] == "PIPELINE_RUN_STATE_CANCELLED"

    # 上报迟到结果：应当被安全丢弃，不解锁下游
    late_resp = client.post(
        f"/v1/orchestration/tasks/{t1_id}:result",
        json={
            "run_id": run_id,
            "attempt": 1,
            "assignment_id": asgn["assignment_id"],
            "success": True,
        },
    ).json()
    assert late_resp["discarded"] is True

    run_state = client.get(f"/v1/orchestration/runs/{run_id}").json()
    assert run_state["run"]["state"] == "PIPELINE_RUN_STATE_CANCELLED"
    for t in run_state["tasks"]:
        assert t["state"] == "PIPELINE_TASK_STATE_CANCELLED"


def test_bounded_retry_and_crash_recovery(client, orch_database):
    client.post(
        "/v1/orchestration/pipelines",
        json={
            "pipeline_id": "flow-retry",
            "nodes": [
                {
                    "id": "r1",
                    "plugin_id": "p1",
                    "consumes": [],
                    "produces": ["m1"],
                    "deadline_ms": 5000,
                    "max_attempts": 2,
                },
                {
                    "id": "r2",
                    "plugin_id": "p2",
                    "consumes": ["m1"],
                    "produces": ["m2"],
                    "deadline_ms": 5000,
                    "max_attempts": 2,
                },
            ],
            "edges": [
                {"from": "r1", "to": "r2", "modality": "m1", "join": "same_item"},
            ],
        },
    )
    sub = client.post(
        "/v1/orchestration/runs",
        json={
            "pipeline_id": "flow-retry",
            "revision": 1,
            "input_ref": "video://r",
            "idempotency_key": "kr",
        },
    ).json()
    run_id = sub["run"]["run_id"]
    t1_id = next(t["task_id"] for t in sub["tasks"] if t["node_id"] == "r1")

    # 调度 attempt 1
    sched1 = client.post("/v1/orchestration/scheduler:step", json={"is_co_located": True}).json()
    asgn1 = next(a for a in sched1["assigned_tasks"] if a["run_id"] == run_id)

    # 模拟租约超时（崩溃）
    with psycopg.connect(orch_database) as conn:
        conn.execute(
            "UPDATE scheduler_assignment "
            "SET lease_expires_at = now() - interval '1 minute' WHERE assignment_id=%s",
            (asgn1["assignment_id"],),
        )

    # 恢复器自动回收并重派为 attempt 2
    step_rec = client.post("/v1/orchestration/scheduler:step", json={"is_co_located": True}).json()
    assert step_rec["recovery"]["expired_assignments_count"] >= 1
    assert t1_id in step_rec["recovery"]["recovered_tasks"]
    asgn2 = next(a for a in step_rec["assigned_tasks"] if a["run_id"] == run_id)
    assert asgn2["attempt"] == 2

    # attempt 2 失败且耗尽预算 (max_attempts = 2)
    fail_res = client.post(
        f"/v1/orchestration/tasks/{t1_id}:result",
        json={
            "run_id": run_id,
            "attempt": 2,
            "assignment_id": asgn2["assignment_id"],
            "success": False,
            "retryable": True,
            "reason_code": "hw_fail",
        },
    ).json()
    assert fail_res["run_failed"] is True
    assert "retry_exhausted:hw_fail" in fail_res["reason_code"]

    run_state = client.get(f"/v1/orchestration/runs/{run_id}").json()
    tasks_map = {t["node_id"]: t for t in run_state["tasks"]}
    assert tasks_map["r1"]["state"] == "PIPELINE_TASK_STATE_FAILED"
    assert tasks_map["r2"]["state"] == "PIPELINE_TASK_STATE_BLOCKED"
    assert run_state["run"]["state"] == "PIPELINE_RUN_STATE_FAILED"
