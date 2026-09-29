"""ADR-030 集成测试：真实 PostgreSQL 验证插件热部署控制面（release 导入、意图通路、状态机、
fencing、蓝绿切换、排空、回滚、下载授权、节点清理）。

测的是**控制面契约**：用虚拟 Agent 回报驱动状态机，不起真实插件进程、不下载真实 bundle。
真实平台服务托管、真实候选进程 describe/validate/start/health、真实 endpoint 文件与真实首方
插件蓝绿由 `tools/verify_plugin_hot_deploy.py --scope native` 在宿主上验收；契约层通过
**不等于**热部署已在节点上真实执行。
"""

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sensoryplex_api.app import create_app
from sensoryplex_api.auth import password_hash
from sensoryplex_api.settings import Settings

from tools.migrate import migrate

pytestmark = pytest.mark.integration

PASSWORD = "test-plugin-hot-deploy-pass-2026"
CANARY = "org.sensoryplex.deploy-canary"
VLM = "org.sensoryplex.vlm-moondream"
MIB = 1024 * 1024
CANARY_BYTES = 512 * MIB

# 热部署意图的字段白名单：只允许受控引用，不得出现任意 URL、命令、宿主路径或密钥。
INTENT_FIELDS = {
    "intent_id",
    "instance_id",
    "node_id",
    "plugin_id",
    "plugin_version",
    "action",
    "artifact_digest",
    "rollback_digest",
    "config",
    "created_at",
    "deadline_unix_ms",
    "operation_id",
    "generation",
    "release_id",
    "bundle_digest",
    "runtime_instance_id",
    "grace_period_ms",
    "config_hash",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_release(
    repository,
    *,
    plugin_id,
    version,
    memory_bytes,
    platform="macos",
    arch="aarch64",
    deadline_ms=5000,
):
    """写一个**合成** release 描述符：只用于控制面契约，不是可执行制品，也不联网。"""
    slug = f"{plugin_id}/{version}/{platform}-{arch}"
    directory = repository / slug
    directory.mkdir(parents=True, exist_ok=True)
    marker = f"{plugin_id}:{version}:{platform}-{arch}".encode()
    manifest = yaml.safe_load(
        (
            Path(__file__).parents[2]
            / "plugins/python/processors"
            / plugin_id.removeprefix("org.sensoryplex.")
            / "plugin.yaml"
        ).read_text()
    )
    bundle = directory / "bundle.tar.gz"
    bundle.write_bytes(b"synthetic-bundle:" + marker)
    descriptor = {
        "release_id": "rel_" + _sha256(marker)[:32],
        "plugin_id": plugin_id,
        "plugin_version": version,
        "platform": platform,
        "arch": arch,
        "form": "local_native",
        "artifact_digest": manifest["spec"]["artifacts"]["digest"],
        "bundle_digest": "sha256:" + _sha256(bundle.read_bytes()),
        "manifest_digest": _sha256(b"manifest:" + marker),
        "config_schema_digest": _sha256(b"schema:" + marker),
        "sbom_digest": _sha256(b"sbom:" + marker),
        "bundle_bytes": bundle.stat().st_size,
        "entrypoint": {"transport": "grpc", "python_module": "deploy_canary"},
        "runtime_requirements": {"python": ">=3.12"},
        "bundle_path": f"{slug}/bundle.tar.gz",
        "trust": "first_party",
        "authenticated": True,
        "authentication_method": "first_party_repository",
        "signature_status": "unsigned_local_native_v1",
        "sbom_components": 3,
        "declared_memory_bytes": memory_bytes,
        "declared_cpu_millicores": 1000,
        "default_deadline_ms": deadline_ms,
    }
    (directory / "release.json").write_text(json.dumps(descriptor))
    return descriptor


@pytest.fixture(scope="module")
def repository(tmp_path_factory):
    root = tmp_path_factory.mktemp("plugin-release-repository")
    return {
        "path": root,
        "releases": {
            "0.1.0": write_release(
                root, plugin_id=CANARY, version="0.1.0", memory_bytes=CANARY_BYTES
            ),
            "0.2.0": write_release(
                root, plugin_id=CANARY, version="0.2.0", memory_bytes=CANARY_BYTES
            ),
            # 0.3.0 声明 768MiB：验证"余量不足时不停止旧版本、也不隐式降级为停机更新"。
            "0.3.0": write_release(root, plugin_id=CANARY, version="0.3.0", memory_bytes=768 * MIB),
            "vlm": write_release(
                root,
                plugin_id=VLM,
                version=yaml.safe_load(
                    (
                        Path(__file__).parents[2]
                        / "plugins/python/processors/vlm-moondream/plugin.yaml"
                    ).read_text()
                )["metadata"]["version"],
                memory_bytes=1024 * MIB,
            ),
            **{
                modality: write_release(
                    root,
                    plugin_id=f"org.sensoryplex.{modality}",
                    version=yaml.safe_load(
                        (
                            Path(__file__).parents[2]
                            / "plugins/python/processors"
                            / modality
                            / "plugin.yaml"
                        ).read_text()
                    )["metadata"]["version"],
                    memory_bytes=1024 * MIB,
                )
                for modality in ("ocr-rapidocr", "asr-whisper-mlx")
            },
        },
    }


@pytest.fixture(scope="module")
def deployment_database(repository):
    url = os.environ["SENSORYPLEX_TEST_DATABASE_URL"]
    schema = "hot_deploy_test_" + uuid.uuid4().hex
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
        yield {
            "url": isolated,
            "repository": repository["path"],
            "releases": repository["releases"],
        }
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def client(deployment_database):
    settings = Settings(
        database_url=deployment_database["url"],
        release_repository=deployment_database["repository"],
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        login = test_client.post(
            "/auth/v1/session", json={"username": "admin", "password": PASSWORD}
        )
        assert login.status_code == 200
        test_client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        yield test_client


@pytest.fixture
def releases(deployment_database):
    return deployment_database["releases"]


@pytest.fixture
def db(deployment_database):
    with psycopg.connect(deployment_database["url"], autocommit=True) as conn:
        yield conn


# ── 通用助手 ──────────────────────────────────────────────────────────────


def sync(client) -> dict:
    response = client.post("/admin/v1/plugin-releases:sync", json={})
    assert response.status_code == 200, response.text
    return response.json()


def enroll(client, node_id, *, memory_bytes, platform="macos", arch="aarch64", co_located=True):
    token = client.post(
        "/admin/v1/nodes/enrollment-tokens",
        json={"node_id": node_id, "expires_in_minutes": 30},
    ).json()["token"]
    response = client.post(
        "/v1/agent/enroll",
        json={
            "enrollment_token": token,
            "node_id": node_id,
            "display_name": node_id,
            "is_co_located": co_located,
            "capabilities": {
                "platform": platform,
                "arch": arch,
                "cpu_cores": 10,
                "memory_bytes": str(memory_bytes),
                "supported_artifacts": ["local_native"],
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["session_token"]


def heartbeat(client, node_id, token, observations=None) -> dict:
    response = client.post(
        "/v1/agent/heartbeat",
        json={
            "node_id": node_id,
            "session_token": token,
            "timestamp_unix_ms": 1_757_000_000_000,
            "available_memory_bytes": "0",
            "current_concurrency": 0,
            "running_instance_ids": [],
            "runtime_observations": observations or [],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def pending_intent(client, node_id, token, action):
    for intent in heartbeat(client, node_id, token)["pending_intents"]:
        if intent["action"] == action:
            return intent
    raise AssertionError(f"没有收到 {action} 意图")


def report(client, token, intent, **fields):
    payload = {
        "intent_id": intent["intent_id"],
        "instance_id": intent["instance_id"],
        "node_id": intent["node_id"],
        "action": intent["action"],
        "operation_id": intent["operation_id"],
        "generation": int(intent["generation"]),
        "release_id": intent["release_id"],
        "runtime_instance_id": intent["runtime_instance_id"],
        # 与真实执行器的 `_report` 一致：默认是"这一步成功"，失败必须显式传 success=False。
        "success": True,
    }
    payload.update({key: value for key, value in fields.items() if value not in ("", None)})
    return client.post(
        "/v1/agent/report", json=payload, headers={"Authorization": f"Bearer {token}"}
    )


def provision(client, node_id, plugin_id, release_id, kind="provision"):
    return client.post(
        f"/admin/v1/nodes/{node_id}/plugins/{plugin_id}:{kind}",
        json={"release_id": release_id, "config": {}},
    )


def operation(client, operation_id) -> dict:
    response = client.get(f"/admin/v1/plugin-deployments/{operation_id}")
    assert response.status_code == 200, response.text
    return response.json()


def stage_report(client, token, intent, stage, **fields):
    return report(client, token, intent, stage=stage, **fields)


def cutover(client, node_id, token, operation_id, release, *, expected="succeeded", intent=None):
    """按真实执行器顺序把候选推到切换完成：staging → starting → validating → candidate_ready。

    只有 `candidate_ready` 请求切换；`validating` 之后必须仍然没有切换 active 指针。
    """
    if intent is None:
        intent = pending_intent(client, node_id, token, "DEPLOYMENT_ACTION_STAGE_RELEASE")
    staging = stage_report(client, token, intent, "PLUGIN_OPERATION_STAGE_STAGING", staging_ms=1200)
    assert staging.status_code == 200, staging.text
    starting = stage_report(
        client, token, intent, "PLUGIN_OPERATION_STAGE_STARTING", staging_ms=1200, starting_ms=900
    )
    assert starting.status_code == 200, starting.text
    validating = stage_report(
        client, token, intent, "PLUGIN_OPERATION_STAGE_VALIDATING", validating_ms=3000
    )
    assert validating.status_code == 200, validating.text
    assert operation(client, operation_id)["stage"] == "PLUGIN_OPERATION_STAGE_VALIDATING"
    ready = stage_report(
        client,
        token,
        intent,
        "PLUGIN_OPERATION_STAGE_CANDIDATE_READY",
        validating_ms=3000,
        endpoint="127.0.0.1:9",
        supervisor_id="org.sensoryplex.plugin.verify",
        verified_plugin_id=release["plugin_id"],
        verified_artifact_digest=release["artifact_digest"],
    )
    assert ready.status_code == 200, ready.text
    payload = operation(client, operation_id)
    assert payload["stage"] == f"PLUGIN_OPERATION_STAGE_{expected.upper()}", payload["error_code"]
    return payload


def drain_previous(client, node_id, token):
    intent = pending_intent(client, node_id, token, "DEPLOYMENT_ACTION_DRAIN")
    response = report(client, token, intent, success=True, draining_ms=4200)
    assert response.status_code == 200, response.text
    return response.json()


def slot_row(db, node_id, plugin_id=CANARY) -> dict:
    with db.cursor(row_factory=dict_row) as cursor:
        return cursor.execute(
            "SELECT * FROM console_plugin_instance WHERE node_id=%s AND plugin_id=%s",
            (node_id, plugin_id),
        ).fetchone()


def runtime_rows(db, node_id) -> list[dict]:
    with db.cursor(row_factory=dict_row) as cursor:
        return cursor.execute(
            "SELECT * FROM plugin_runtime_instance WHERE node_id=%s ORDER BY created_at",
            (node_id,),
        ).fetchall()


# ── 制品仓：同步与拒收 ─────────────────────────────────────────────────────


def test_release_repository_sync_is_idempotent_and_rejects_tampering(client, releases, repository):
    first = sync(client)
    assert sorted(first["imported"]) == sorted(item["release_id"] for item in releases.values())

    second = sync(client)
    assert second["imported"] == []
    assert second["rejected"] == []
    assert len(second["unchanged"]) == len(releases)
    assert second["total"] == len(releases)

    listed = client.get("/admin/v1/plugin-releases").json()["items"]
    assert all(item["trust"] == "first_party" and item["authenticated"] for item in listed)

    # 不带 body 的同步按默认选项执行，不得炸成未处理异常。
    bodyless = client.post("/admin/v1/plugin-releases:sync")
    assert bodyless.status_code == 200, bodyless.text

    root = repository["path"]
    vlm = releases["vlm"]
    fixtures = {
        "bytes": {"bundle_bytes": int(vlm["bundle_bytes"]) + 1},
        # 第三方/未认证制品不得进入受控 release 台账。
        "thirdparty": {"trust": "third_party", "authenticated": False},
        # 容器形态不在首期范围。
        "container": {"form": "container"},
        # 制品仓之外的路径必须拒收（不允许 Agent 从任意位置取包）。
        "escape": {"bundle_path": "../escape.tar.gz"},
        # 摘要不是 sha256:<64 hex> 一律拒收。
        "digest": {"bundle_digest": "sha256:" + "0" * 64},
    }
    expected = {
        "bytes": "bundle_bytes_mismatch",
        "thirdparty": "release_not_first_party_authenticated",
        "container": "unsupported_form",
        "escape": "bundle_path_outside_repository",
        "digest": "bundle_digest_mismatch",
    }
    tampered = root / "_tampered"
    try:
        for case, changes in fixtures.items():
            descriptor = dict(vlm, **changes)
            target = tampered / case / "macos-aarch64"
            target.mkdir(parents=True, exist_ok=True)
            (target / "release.json").write_text(json.dumps(descriptor))

        third = sync(client)
        rejected = {item.split(":", 1)[0]: item.split(":", 1)[1] for item in third["rejected"]}
        for case, reason in expected.items():
            label = f"_tampered/{case}/macos-aarch64/release.json"
            assert rejected.get(label) == reason, f"{case}: {rejected.get(label)!r}"
        assert third["imported"] == []
    finally:
        shutil.rmtree(tampered, ignore_errors=True)


# ── 部署意图通路 ───────────────────────────────────────────────────────────


def test_intent_carries_hot_deploy_identity_without_urls_or_paths(client, releases, db):
    sync(client)
    node = "hot-node-intent"
    token = enroll(client, node, memory_bytes=8 * 1024 * MIB)

    # 缺 release_id 的部署意图必须显式失败，不能落到未处理异常。
    missing = client.post(
        f"/admin/v1/nodes/{node}/plugins/{CANARY}:provision",
        json={},
    )
    assert missing.status_code == 422, missing.text
    assert missing.json()["reason_code"] == "invalid_text_field"

    created = provision(client, node, CANARY, releases["0.1.0"]["release_id"])
    assert created.status_code == 201, created.text
    operation_id = created.json()["operation_id"]
    candidate = created.json()["candidate_runtime_instance_id"]

    # 心跳必须能带着插件身份把热部署意图发出去（此前会 500：意图表没有 plugin_id 列）。
    payload = heartbeat(client, node, token)
    assert payload["status"] == "NODE_STATUS_READY"
    intents = payload["pending_intents"]
    assert len(intents) == 1
    intent = intents[0]

    assert set(intent) == INTENT_FIELDS
    assert intent["plugin_id"] == CANARY
    assert intent["plugin_version"] == "0.1.0"
    assert intent["action"] == "DEPLOYMENT_ACTION_STAGE_RELEASE"
    assert intent["operation_id"] == operation_id
    assert intent["runtime_instance_id"] == candidate
    assert int(intent["generation"]) == 1
    assert intent["release_id"] == releases["0.1.0"]["release_id"]
    assert intent["bundle_digest"] == releases["0.1.0"]["bundle_digest"]
    assert intent["config_hash"] == "sha256:" + hashlib.sha256(b"{}").hexdigest()
    # grace period 取插件声明的最大请求 deadline（夹在兜底值与上限之间）。
    assert int(intent["grace_period_ms"]) == 5000
    assert int(intent["deadline_unix_ms"]) > 0
    # 契约：意图只携带受控引用，不携带任意 URL、命令、宿主路径或密钥。
    blob = json.dumps(intent)
    for forbidden in ("http://", "https://", "/Users/", "/tmp/", "token", "secret", "--port"):
        assert forbidden not in blob, forbidden

    # 意图已被领取（dispatched），且槽位的 active 指针还没有被写过。
    assert slot_row(db, node)["active_runtime_instance_id"] is None


def test_stage_reports_advance_then_cutover_switches_active_pointer(client, releases, db):
    sync(client)
    node = "hot-node-cutover"
    token = enroll(client, node, memory_bytes=8 * 1024 * MIB)
    created = provision(client, node, CANARY, releases["0.1.0"]["release_id"]).json()
    operation_id = created["operation_id"]
    candidate = created["candidate_runtime_instance_id"]

    intent = pending_intent(client, node, token, "DEPLOYMENT_ACTION_STAGE_RELEASE")
    staging = stage_report(client, token, intent, "PLUGIN_OPERATION_STAGE_STAGING", staging_ms=1200)
    assert staging.status_code == 200, staging.text
    assert staging.json()["reason_code"] == "stage_staging"
    assert operation(client, operation_id)["stage"] == "PLUGIN_OPERATION_STAGE_STAGING"
    assert slot_row(db, node)["active_runtime_instance_id"] is None

    starting = stage_report(
        client, token, intent, "PLUGIN_OPERATION_STAGE_STARTING", staging_ms=1200, starting_ms=900
    )
    assert starting.status_code == 200, starting.text
    assert operation(client, operation_id)["stage"] == "PLUGIN_OPERATION_STAGE_STARTING"

    # validating 只推进状态，绝不切换 active。
    validating = stage_report(
        client, token, intent, "PLUGIN_OPERATION_STAGE_VALIDATING", validating_ms=3000
    )
    assert validating.status_code == 200, validating.text
    assert operation(client, operation_id)["stage"] == "PLUGIN_OPERATION_STAGE_VALIDATING"
    assert slot_row(db, node)["active_runtime_instance_id"] is None

    ready = stage_report(
        client,
        token,
        intent,
        "PLUGIN_OPERATION_STAGE_CANDIDATE_READY",
        endpoint="127.0.0.1:51291",
        supervisor_id="org.sensoryplex.plugin.hot-node-cutover",
        verified_plugin_id=CANARY,
        verified_artifact_digest=releases["0.1.0"]["artifact_digest"],
    )
    assert ready.status_code == 200, ready.text
    payload = operation(client, operation_id)
    # 首个实例没有"旧实例"需要排空，直接成功。
    assert payload["stage"] == "PLUGIN_OPERATION_STAGE_SUCCEEDED"
    assert payload["candidate"]["state"] == "PLUGIN_RUNTIME_STATE_ACTIVE"
    assert payload["candidate"]["endpoint"] == "127.0.0.1:51291"
    assert payload["candidate"]["verified_plugin_id"] == CANARY
    # active 读的是槽位实时指针：首次部署切换成功后它就是本次 candidate。
    assert payload["active"]["runtime_instance_id"] == candidate
    assert payload["active"]["state"] == "PLUGIN_RUNTIME_STATE_ACTIVE"
    # 首次部署没有被替换的实例，from 保持空，不会被误当成"当前 active"。
    assert payload["from_runtime_instance_id"] == ""

    slot = slot_row(db, node)
    # 槽位 generation 从 1 起（为 0 时 CAS 永远失败），active 指针指向候选。
    assert int(slot["generation"]) == 1
    assert slot["active_runtime_instance_id"] == candidate
    assert slot["active_release_id"] == releases["0.1.0"]["release_id"]
    assert slot["endpoint"] == "127.0.0.1:51291"
    assert slot["previous_runtime_instance_id"] is None

    # 重复投递同一个已完成意图 = 拒绝，而不是"再执行一次"。
    duplicate = stage_report(
        client, token, intent, "PLUGIN_OPERATION_STAGE_STAGING", staging_ms=1200
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["reason_code"] == "duplicate_deployment_report"

    # 已终态的操作不能再被取消（切换窗口已关闭）。
    closed = client.post(f"/admin/v1/plugin-deployments/{operation_id}:cancel")
    assert closed.status_code == 409
    assert closed.json()["reason_code"] == "plugin_deployment_operation_already_closed"


def test_fencing_rejects_stale_and_wrong_generation_reports(client, releases, db):
    sync(client)
    node = "hot-node-fence"
    token = enroll(client, node, memory_bytes=8 * 1024 * MIB)
    created = provision(client, node, CANARY, releases["0.1.0"]["release_id"]).json()
    intent = pending_intent(client, node, token, "DEPLOYMENT_ACTION_STAGE_RELEASE")

    wrong_generation = report(
        client,
        token,
        intent,
        generation=int(intent["generation"]) + 1,
        stage="PLUGIN_OPERATION_STAGE_STAGING",
    )
    assert wrong_generation.status_code == 409
    assert wrong_generation.json()["reason_code"] == "fencing_token_mismatch"

    stale = report(client, token, intent, operation_id="op_other_generation")
    assert stale.status_code == 409
    assert stale.json()["reason_code"] == "stale_deployment_report"

    stale_runtime = report(client, token, intent, runtime_instance_id="rti_someone_else")
    assert stale_runtime.status_code == 409
    assert stale_runtime.json()["reason_code"] == "stale_deployment_report"

    # 被拒绝的回报不得改坏状态：意图仍在飞行中，操作仍停在 accepted。
    with db.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute(
            "SELECT * FROM console_deployment_intent WHERE id=%s", (intent["intent_id"],)
        ).fetchone()
    assert row["state"] == "dispatched"
    assert operation(client, created["operation_id"])["stage"] == "PLUGIN_OPERATION_STAGE_ACCEPTED"

    # 正确的回报仍然能被接受（拒绝路径没有留下半成品）。
    ok = stage_report(client, token, intent, "PLUGIN_OPERATION_STAGE_STAGING", staging_ms=1200)
    assert ok.status_code == 200, ok.text


def test_deadline_expiry_fails_operation_without_touching_active(client, releases, db):
    sync(client)
    node = "hot-node-deadline"
    token = enroll(client, node, memory_bytes=8 * 1024 * MIB)
    created = provision(client, node, CANARY, releases["0.1.0"]["release_id"]).json()
    operation_id = created["operation_id"]
    candidate = created["candidate_runtime_instance_id"]
    intent = pending_intent(client, node, token, "DEPLOYMENT_ACTION_STAGE_RELEASE")

    db.execute(
        "UPDATE plugin_deployment_operation SET deadline_unix_ms=1 WHERE operation_id=%s",
        (operation_id,),
    )
    expired = stage_report(client, token, intent, "PLUGIN_OPERATION_STAGE_STAGING", staging_ms=1200)
    assert expired.status_code == 200, expired.text
    assert expired.json() == {"status": "rejected", "reason_code": "operation_deadline_exceeded"}

    payload = operation(client, operation_id)
    assert payload["stage"] == "PLUGIN_OPERATION_STAGE_FAILED"
    assert payload["error_code"] == "operation_deadline_exceeded"
    assert slot_row(db, node)["active_runtime_instance_id"] is None
    candidate_row = next(
        row for row in runtime_rows(db, node) if row["runtime_instance_id"] == candidate
    )
    assert candidate_row["state"] == "failed"


def test_cancel_before_cutover_keeps_old_state_and_closes_window(client, releases, db):
    sync(client)
    node = "hot-node-cancel"
    token = enroll(client, node, memory_bytes=8 * 1024 * MIB)
    created = provision(client, node, CANARY, releases["0.1.0"]["release_id"]).json()
    operation_id = created["operation_id"]
    candidate = created["candidate_runtime_instance_id"]

    cancelled = client.post(f"/admin/v1/plugin-deployments/{operation_id}:cancel")
    assert cancelled.status_code == 200, cancelled.text
    payload = cancelled.json()
    assert payload["stage"] == "PLUGIN_OPERATION_STAGE_CANCELLED"
    assert payload["cancellable"] is False

    slot = slot_row(db, node)
    assert slot["active_runtime_instance_id"] is None
    candidate_row = next(
        row for row in runtime_rows(db, node) if row["runtime_instance_id"] == candidate
    )
    assert candidate_row["state"] == "failed"
    assert candidate_row["error_code"] == "cancelled_by_administrator"

    # 取消必须下发"切换前清理"意图，并且它能被收尾（否则操作行与事实永久不一致）。
    stop = pending_intent(client, node, token, "DEPLOYMENT_ACTION_STOP")
    finished = report(
        client, token, stop, success=True, actual_state="PLUGIN_INSTANCE_STATE_STOPPED"
    )
    assert finished.status_code == 200, finished.text

    again = client.post(f"/admin/v1/plugin-deployments/{operation_id}:cancel")
    assert again.status_code == 409
    assert again.json()["reason_code"] == "plugin_deployment_operation_already_closed"


# ── 余量、蓝绿、回滚 ───────────────────────────────────────────────────────


def test_upgrade_headroom_is_not_double_counted_and_insufficient_keeps_old_active(
    client, releases, db
):
    sync(client)
    node = "hot-node-headroom"
    # 节点内存 = 2 × 512MiB：蓝绿要求"旧实例 + 候选"同时放得下，刚好够升级。
    token = enroll(client, node, memory_bytes=2 * CANARY_BYTES)
    first = provision(client, node, CANARY, releases["0.1.0"]["release_id"]).json()
    cutover(client, node, token, first["operation_id"], releases["0.1.0"])
    assert slot_row(db, node)["active_release_id"] == releases["0.1.0"]["release_id"]

    # 同一槽位升级到同样声明 512MiB 的 0.2.0：旧实例不计入"其它槽位占用"，
    # 否则这份声明被减两次，合法升级会被误拒 upgrade_headroom_insufficient。
    upgrade = provision(client, node, CANARY, releases["0.2.0"]["release_id"], kind="upgrade")
    assert upgrade.status_code == 201, upgrade.text
    upgraded = upgrade.json()
    assert int(upgraded["generation"]) == 2
    slot = slot_row(db, node)
    assert slot["active_runtime_instance_id"] == first["candidate_runtime_instance_id"]
    assert slot["previous_runtime_instance_id"] is None

    # 余量不足（候选声明 768MiB > 512MiB 余量）必须显式失败，且不停止旧版本。
    insufficient = provision(client, node, CANARY, releases["0.3.0"]["release_id"], kind="upgrade")
    assert insufficient.status_code == 422, insufficient.text
    assert insufficient.json()["reason_code"] == "upgrade_headroom_insufficient"
    after = slot_row(db, node)
    assert after["active_runtime_instance_id"] == slot["active_runtime_instance_id"]
    assert after["endpoint"] == slot["endpoint"]
    assert int(after["generation"]) == 2
    assert not [
        row
        for row in runtime_rows(db, node)
        if row["release_id"] == releases["0.3.0"]["release_id"]
    ]

    # 其它槽位的占用必须如实计入：canary 已占 512MiB，VLM 要 1GiB 放不下。
    other = provision(client, node, VLM, releases["vlm"]["release_id"])
    assert other.status_code == 422, other.text
    assert other.json()["reason_code"] == "upgrade_headroom_insufficient"

    # 蓝绿：升级后的候选切换成功，旧实例进入 previous/draining，随后被排空停止。
    payload = cutover(
        client, node, token, upgraded["operation_id"], releases["0.2.0"], expected="draining_old"
    )
    assert payload["previous"]["state"] == "PLUGIN_RUNTIME_STATE_DRAINING"
    # 切换后"当前 active"必须指向新候选；被替换的旧实例只由 from_runtime_instance_id 表达。
    assert payload["active"]["runtime_instance_id"] == upgraded["candidate_runtime_instance_id"]
    assert payload["active"]["release_id"] == releases["0.2.0"]["release_id"]
    assert payload["from_runtime_instance_id"] == first["candidate_runtime_instance_id"]
    drain_previous(client, node, token)
    final = operation(client, upgraded["operation_id"])
    assert final["stage"] == "PLUGIN_OPERATION_STAGE_SUCCEEDED"
    assert int(final["draining_ms"]) == 4200
    stopped = next(
        row
        for row in runtime_rows(db, node)
        if row["runtime_instance_id"] == first["candidate_runtime_instance_id"]
    )
    assert stopped["state"] == "stopped"
    assert (
        slot_row(db, node)["previous_runtime_instance_id"] == first["candidate_runtime_instance_id"]
    )


def test_rollback_creates_reverse_operation_without_rewriting_history(client, releases, db):
    sync(client)
    node = "hot-node-rollback"
    token = enroll(client, node, memory_bytes=8 * 1024 * MIB)
    first = provision(client, node, CANARY, releases["0.1.0"]["release_id"]).json()
    cutover(client, node, token, first["operation_id"], releases["0.1.0"])

    second = provision(client, node, CANARY, releases["0.2.0"]["release_id"], kind="upgrade").json()
    cutover(client, node, token, second["operation_id"], releases["0.2.0"], expected="draining_old")
    drain_previous(client, node, token)
    assert operation(client, second["operation_id"])["stage"] == "PLUGIN_OPERATION_STAGE_SUCCEEDED"

    rolled = client.post(f"/admin/v1/plugin-deployments/{second['operation_id']}:rollback")
    assert rolled.status_code == 201, rolled.text
    payload = rolled.json()
    assert payload["kind"] == "rollback"
    assert payload["rollback_of_operation_id"] == second["operation_id"]
    assert payload["release_id"] == releases["0.1.0"]["release_id"]
    assert int(payload["generation"]) == 3
    assert payload["stage"] == "PLUGIN_OPERATION_STAGE_ACCEPTED"
    assert payload["candidate"]["release_id"] == releases["0.1.0"]["release_id"]

    # 历史操作行不被改写：旧操作仍是 succeeded，回滚只是新增一行反向操作。
    history = operation(client, second["operation_id"])
    assert history["stage"] == "PLUGIN_OPERATION_STAGE_SUCCEEDED"
    assert history["kind"] == "upgrade"

    # 回滚操作还没结算（accepted），再次回滚必须被拒绝。
    unsettled = client.post(f"/admin/v1/plugin-deployments/{payload['operation_id']}:rollback")
    assert unsettled.status_code == 409
    assert unsettled.json()["reason_code"] == "plugin_deployment_operation_not_settled"

    # 回滚本身也是一次完整蓝绿：切换完成后 active 回到 0.1.0，旧版本留作 previous。
    cutover(
        client, node, token, payload["operation_id"], releases["0.1.0"], expected="draining_old"
    )
    drain_previous(client, node, token)
    slot = slot_row(db, node)
    assert slot["active_release_id"] == releases["0.1.0"]["release_id"]
    rolled_back = operation(client, payload["operation_id"])
    assert rolled_back["stage"] == "PLUGIN_OPERATION_STAGE_SUCCEEDED"
    assert rolled_back["active"]["runtime_instance_id"] == payload["candidate_runtime_instance_id"]
    assert rolled_back["from_runtime_instance_id"] == second["candidate_runtime_instance_id"]


# ── Agent 制品下载授权 ─────────────────────────────────────────────────────


def test_bundle_download_requires_matching_intent(client, releases, repository):
    sync(client)
    node = "hot-node-download"
    token = enroll(client, node, memory_bytes=8 * 1024 * MIB)
    other_token = enroll(client, "hot-node-download-outsider", memory_bytes=8 * 1024 * MIB)
    release = releases["0.1.0"]
    created = provision(client, node, CANARY, release["release_id"])
    assert created.status_code == 201, created.text
    path = f"/v1/agent/releases/{release['release_id']}/bundle"

    assert client.get(path).status_code == 401
    assert (
        client.get(path, headers={"Authorization": "Bearer sp_node_bogus"}).json()["reason_code"]
        == "invalid_node_credentials"
    )

    # 没有该 release 意图的节点不得下载（防止横向取包）。
    outsider = client.get(path, headers={"Authorization": f"Bearer {other_token}"})
    assert outsider.status_code == 403
    assert outsider.json()["reason_code"] == "release_not_entitled_for_this_agent"

    entitled = client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert entitled.status_code == 200, entitled.text
    assert entitled.headers["X-Bundle-Digest"] == release["bundle_digest"]
    expected = (repository["path"] / release["bundle_path"]).read_bytes()
    assert entitled.content == expected

    # 意图结算后授权随之失效。
    intent = pending_intent(client, node, token, "DEPLOYMENT_ACTION_STAGE_RELEASE")
    failed = report(client, token, intent, success=False, error_code="candidate_start_failed")
    assert failed.status_code == 200, failed.text
    after = client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert after.status_code == 403
    assert after.json()["reason_code"] == "release_not_entitled_for_this_agent"


# ── Agent 重启对账 ─────────────────────────────────────────────────────────


def test_reconciliation_never_guesses_success_and_keeps_active_pointer(client, releases, db):
    sync(client)
    node = "hot-node-reconcile"
    token = enroll(client, node, memory_bytes=8 * 1024 * MIB)
    created = provision(client, node, CANARY, releases["0.1.0"]["release_id"]).json()
    cutover(client, node, token, created["operation_id"], releases["0.1.0"])
    candidate = created["candidate_runtime_instance_id"]

    unknown = heartbeat(
        client,
        node,
        token,
        observations=[
            {
                "runtime_instance_id": candidate,
                "operation_id": created["operation_id"],
                "generation": 1,
                "observed_state": "unknown",
                "supervisor_managed": False,
                "unit_loaded": False,
                "reconciliation": "unknown",
                "detail": "launchagent_unloaded",
            }
        ],
    )
    assert unknown["reconciliation_required"] == [candidate]

    # 未知状态只如实记录，不改 role/state、不删 active 制品。
    row = next(r for r in runtime_rows(db, node) if r["runtime_instance_id"] == candidate)
    assert row["state"] == "active"
    assert row["role"] == "active"
    assert row["error_code"] == "reconciliation_required"
    assert slot_row(db, node)["active_runtime_instance_id"] == candidate

    matched = heartbeat(
        client,
        node,
        token,
        observations=[
            {
                "runtime_instance_id": candidate,
                "operation_id": created["operation_id"],
                "generation": 1,
                "observed_state": "active",
                "endpoint": "127.0.0.1:50123",
                "supervisor_id": "org.sensoryplex.plugin.hot-node-reconcile",
                "supervisor_managed": True,
                "unit_loaded": True,
                "reconciliation": "matched",
            }
        ],
    )
    assert matched["reconciliation_required"] == []
    refreshed = next(r for r in runtime_rows(db, node) if r["runtime_instance_id"] == candidate)
    assert refreshed["endpoint"] == "127.0.0.1:50123"
    assert refreshed["supervisor_id"] == "org.sensoryplex.plugin.hot-node-reconcile"


# ── 节点下线与台账清理 ─────────────────────────────────────────────────────


def test_revoked_node_with_deployment_ledger_can_be_deleted(client, releases, db):
    sync(client)
    node = "hot-node-revoke"
    enroll(client, node, memory_bytes=8 * 1024 * MIB)
    created = provision(client, node, CANARY, releases["0.1.0"]["release_id"])
    assert created.status_code == 201, created.text
    releases_before = db.execute("SELECT count(*) FROM plugin_release").fetchone()[0]

    revoked = client.post(f"/admin/v1/nodes/{node}:revoke")
    assert revoked.status_code == 200, revoked.text

    # 有热部署台账（operation/runtime 都带 console_node 外键）的节点必须能删干净。
    deleted = client.delete(f"/admin/v1/nodes/{node}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"status": "deleted", "node_id": node}
    assert client.get(f"/admin/v1/nodes/{node}").status_code == 404

    for table in (
        "console_deployment_intent",
        "plugin_deployment_operation",
        "plugin_runtime_instance",
        "console_plugin_instance",
    ):
        remaining = db.execute(
            f"SELECT count(*) FROM {table} WHERE node_id=%s", (node,)
        ).fetchone()[0]
        assert remaining == 0, table
    # 不可变制品仓不受节点清理影响。
    assert db.execute("SELECT count(*) FROM plugin_release").fetchone()[0] == releases_before


# ── 可观测性：指标聚合 ─────────────────────────────────────────────────────


def _prometheus(text: str) -> dict[tuple[str, frozenset], float]:
    """极简 Prometheus 文本解析：只解析本端点产出的 `name{label="v"} value` 行。

    故意不引入 Prometheus 客户端库——这里要验证的正是 exposition 文本本身。
    """
    series: dict[tuple[str, frozenset], float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        metric, _, value = line.rpartition(" ")
        labels: set[tuple[str, str]] = set()
        if "{" in metric:
            metric, _, tail = metric.partition("{")
            for part in tail.rstrip("}").split(","):
                key, _, raw = part.partition("=")
                labels.add((key, raw.strip('"')))
        series[(metric, frozenset(labels))] = float(value)
    return series


def _metric_buckets(series, metric, **match):
    """筛出指定 metric 下 label 与 match 全部相等的 series。"""
    found = []
    for (name, labels), value in series.items():
        if name != metric:
            continue
        pairs = dict(labels)
        if all(pairs.get(key) == want for key, want in match.items()):
            found.append((pairs, value))
    return found


def test_metrics_aggregate_ledger_by_dimensions_without_operation_labels(client, releases, db):
    sync(client)
    node = "hot-node-metrics"
    token = enroll(client, node, memory_bytes=4 * CANARY_BYTES)
    # 首次部署（无旧实例，不排空）→ 一次成功桶。
    first = provision(client, node, CANARY, releases["0.1.0"]["release_id"]).json()
    cutover(client, node, token, first["operation_id"], releases["0.1.0"])
    # 升级到 0.2.0：staging / starting / validating / draining 全部落库。
    upgrade = provision(
        client, node, CANARY, releases["0.2.0"]["release_id"], kind="upgrade"
    ).json()
    cutover(
        client, node, token, upgrade["operation_id"], releases["0.2.0"], expected="draining_old"
    )
    drain_previous(client, node, token)
    assert operation(client, upgrade["operation_id"])["stage"] == (
        "PLUGIN_OPERATION_STAGE_SUCCEEDED"
    )
    # 一次切换前失败：留下显式 reason，供聚合出第二个桶。
    failed = provision(client, node, CANARY, releases["0.3.0"]["release_id"], kind="upgrade").json()
    intent = pending_intent(client, node, token, "DEPLOYMENT_ACTION_STAGE_RELEASE")
    db.execute(
        "UPDATE plugin_deployment_operation SET deadline_unix_ms=1 WHERE operation_id=%s",
        (failed["operation_id"],),
    )
    expired = stage_report(client, token, intent, "PLUGIN_OPERATION_STAGE_STAGING", staging_ms=7)
    assert expired.status_code == 200, expired.text
    assert expired.json()["reason_code"] == "operation_deadline_exceeded"

    response = client.get(f"/admin/v1/plugin-deployments/metrics?node_id={node}")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    series = _prometheus(text)

    succeeded = _metric_buckets(
        series,
        "sensoryplex_plugin_deployment_operations",
        node_id=node,
        plugin_id=CANARY,
        release_id=releases["0.2.0"]["release_id"],
        stage="PLUGIN_OPERATION_STAGE_SUCCEEDED",
        reason="",
    )
    assert len(succeeded) == 1, succeeded
    ok_labels, ok_count = succeeded[0]
    assert ok_count == 1
    # kind 让"按操作聚合"能区分升级与首次部署，而不是只按阶段看。
    assert ok_labels["kind"] == "upgrade"

    # 首次部署也自成一个成功桶，但它没有 draining 耗时（真实台账如此，不补零）。
    first_bucket = _metric_buckets(
        series,
        "sensoryplex_plugin_deployment_operations",
        node_id=node,
        plugin_id=CANARY,
        release_id=releases["0.1.0"]["release_id"],
        stage="PLUGIN_OPERATION_STAGE_SUCCEEDED",
        reason="",
    )
    assert len(first_bucket) == 1 and first_bucket[0][1] == 1
    assert first_bucket[0][0]["kind"] == "provision"
    assert (
        series[
            (
                "sensoryplex_plugin_deployment_phase_milliseconds_sum",
                frozenset(set(first_bucket[0][0].items()) | {("phase", "draining")}),
            )
        ]
        == 0
    )

    failed_bucket = _metric_buckets(
        series,
        "sensoryplex_plugin_deployment_operations",
        node_id=node,
        plugin_id=CANARY,
        release_id=releases["0.3.0"]["release_id"],
        stage="PLUGIN_OPERATION_STAGE_FAILED",
        reason="operation_deadline_exceeded",
    )
    assert len(failed_bucket) == 1, failed_bucket
    assert failed_bucket[0][1] == 1
    assert failed_bucket[0][0]["kind"] == "upgrade"
    failed_labels = failed_bucket[0][0]
    # 被拒的回报（deadline 已过）不写任何耗时：失败桶的 staging 必须是 0，不能被污染。
    assert (
        series[
            (
                "sensoryplex_plugin_deployment_phase_milliseconds_sum",
                frozenset(set(failed_labels.items()) | {("phase", "staging")}),
            )
        ]
        == 0
    )

    # 阶段耗时系列的 label = 桶 label + phase，数值必须与台账逐字段一致。
    def phase(stage_labels, name, kind):
        return series[
            (
                f"sensoryplex_plugin_deployment_phase_milliseconds_{kind}",
                frozenset(set(stage_labels.items()) | {("phase", name)}),
            )
        ]

    assert phase(ok_labels, "staging", "sum") == 1200
    assert phase(ok_labels, "starting", "sum") == 900
    assert phase(ok_labels, "validating", "sum") == 3000
    assert phase(ok_labels, "draining", "sum") == 4200
    assert phase(ok_labels, "staging", "max") == 1200
    assert phase(ok_labels, "draining", "max") == 4200

    info = _metric_buckets(series, "sensoryplex_plugin_deployment_metrics_info", window_hours="24")
    assert len(info) == 1 and info[0][1] == 1

    # 高基数与敏感内容：operation_id 不进 label，正文里也不出现凭据或操作号。
    assert "operation_id" not in text
    assert first["operation_id"] not in text
    assert PASSWORD not in text


def test_metrics_window_and_node_filters_do_not_synthesize_rows(client, releases, db):
    sync(client)
    node = "hot-node-metrics-window"
    token = enroll(client, node, memory_bytes=2 * CANARY_BYTES)
    created = provision(client, node, CANARY, releases["0.1.0"]["release_id"]).json()
    cutover(client, node, token, created["operation_id"], releases["0.1.0"])

    inside = client.get(f"/admin/v1/plugin-deployments/metrics?node_id={node}&window_hours=24")
    assert inside.status_code == 200, inside.text
    assert f'node_id="{node}"' in inside.text

    db.execute(
        "UPDATE plugin_deployment_operation SET created_at = now() - interval '30 days' "
        "WHERE node_id=%s",
        (node,),
    )
    outside = client.get(f"/admin/v1/plugin-deployments/metrics?node_id={node}&window_hours=24")
    assert outside.status_code == 200, outside.text
    # 台账被推出窗口后，该节点的 series 必须消失——不合成、不补零。
    assert f'node_id="{node}"' not in outside.text
    assert 'sensoryplex_plugin_deployment_metrics_info{window_hours="24"} 1' in outside.text

    # 窗口参数越界必须显式拒绝，不得被夹取。
    assert client.get("/admin/v1/plugin-deployments/metrics?window_hours=0").status_code == 422
    assert client.get("/admin/v1/plugin-deployments/metrics?window_hours=721").status_code == 422


def test_metrics_endpoint_requires_plugin_manage_permission(deployment_database):
    settings = Settings(
        database_url=deployment_database["url"],
        release_repository=deployment_database["repository"],
    )
    with TestClient(create_app(settings)) as anonymous:
        assert anonymous.get("/admin/v1/plugin-deployments/metrics").status_code == 401


def test_batch_deploy_creates_real_operations_and_is_idempotent(client, db, releases):
    sync(client)
    node_id = "batch-" + uuid.uuid4().hex[:12]
    token = enroll(client, node_id, memory_bytes=16 * 1024 * MIB)
    url = f"/admin/v1/nodes/{node_id}/plugins:batch-deploy"
    response = client.post(url, json={"plugin_ids": [CANARY, CANARY, "missing.plugin"]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["rejected"] == [{"plugin_id": "missing.plugin", "reason": "plugin_not_found"}]
    assert len(result["operations"]) == 1
    op = result["operations"][0]
    assert op["release_id"] == releases["0.1.0"]["release_id"]
    assert op["stage"] == "PLUGIN_OPERATION_STAGE_ACCEPTED"
    assert op["candidate"]["state"] == "PLUGIN_RUNTIME_STATE_PLANNED"
    assert not op.get("active")
    repeated = client.post(url, json={"plugin_ids": [CANARY]}).json()
    assert repeated["operations"][0]["operation_id"] == op["operation_id"]
    assert db.execute(
        "SELECT action FROM console_deployment_intent WHERE node_id=%s", (node_id,)
    ).fetchall() == [("stage_release",)]
    release = next(r for r in releases.values() if r["release_id"] == op["release_id"])
    cutover(client, node_id, token, op["operation_id"], release)
    ready = client.post(url, json={"plugin_ids": [CANARY]}).json()
    assert ready["operations"] == []
    assert ready["already_ready"] == [CANARY]


def test_batch_reserves_pending_resources_and_returns_each_rejection(client):
    sync(client)
    node_id = "batch-small-" + uuid.uuid4().hex[:12]
    enroll(client, node_id, memory_bytes=1100 * MIB)
    result = client.post(
        f"/admin/v1/nodes/{node_id}/plugins:batch-deploy",
        json={"plugin_ids": [CANARY, VLM, "org.sensoryplex.embed-bge-onnx"]},
    ).json()
    assert len(result["operations"]) == 1
    assert result["rejected"] == [
        {"plugin_id": VLM, "reason": "upgrade_headroom_insufficient"},
        {"plugin_id": "org.sensoryplex.embed-bge-onnx", "reason": "insufficient_memory"},
    ]


def test_batch_requires_authenticated_agent_and_bounds_request(client, db):
    node_id = "batch-no-agent-" + uuid.uuid4().hex[:12]
    enroll(client, node_id, memory_bytes=16 * 1024 * MIB)
    db.execute("UPDATE console_node SET session_token_hash=NULL WHERE node_id=%s", (node_id,))
    url = f"/admin/v1/nodes/{node_id}/plugins:batch-deploy"
    result = client.post(url, json={"plugin_ids": [CANARY]}).json()
    assert result["operations"] == []
    assert result["rejected"] == [{"plugin_id": CANARY, "reason": "node_agent_not_enrolled"}]
    assert client.post(url, json={"plugin_ids": [CANARY] * 17}).status_code == 422


def test_batch_configs_are_reusable_and_repair_ready_installations(client, db, releases):
    """配置必须实际入库、匹配部署摘要，并能创建多模态 Revision；重试不生成新版本。"""
    sync(client)
    node_id = "batch-configs-" + uuid.uuid4().hex[:12]
    token = enroll(client, node_id, memory_bytes=32 * 1024 * MIB)
    url = f"/admin/v1/nodes/{node_id}/plugins:batch-deploy"
    components = {
        "ocr_fast": "org.sensoryplex.ocr-rapidocr",
        "asr_fast": "org.sensoryplex.asr-whisper-mlx",
        "vlm_enrich": VLM,
    }
    body = {"plugin_ids": list(components.values())}
    created = client.post(url, json=body)
    assert created.status_code == 200, created.text
    assert created.json()["rejected"] == []
    assert len(created.json()["operations"]) == 3

    def configs():
        return db.execute(
            "SELECT c.id,c.plugin_id,c.config_hash,c.revision FROM console_plugin_config c "
            "JOIN console_plugin_instance i ON i.plugin_id=c.plugin_id "
            "AND i.config_hash=c.config_hash WHERE i.node_id=%s ORDER BY c.plugin_id",
            (node_id,),
        ).fetchall()

    initial = configs()
    assert len(initial) == 3
    pending = client.post(url, json=body).json()
    assert [item["operation_id"] for item in pending["operations"]] == [
        item["operation_id"] for item in created.json()["operations"]
    ]
    assert configs() == initial
    intents = {
        intent["operation_id"]: intent
        for intent in heartbeat(client, node_id, token)["pending_intents"]
    }
    for operation in created.json()["operations"]:
        release = next(
            release
            for release in releases.values()
            if release["release_id"] == operation["release_id"]
        )
        cutover(
            client,
            node_id,
            token,
            operation["operation_id"],
            release,
            intent=intents[operation["operation_id"]],
        )

    # 模拟旧一键部署遗留：已经 ready，但没有任何可选的配置记录。
    db.execute("DELETE FROM console_plugin_config WHERE id = ANY(%s)", ([c[0] for c in initial],))
    repaired = client.post(url, json=body).json()
    assert repaired["operations"] == []
    assert repaired["rejected"] == []
    assert set(repaired["already_ready"]) == set(components.values())
    restored = configs()
    assert len(restored) == 3
    assert [(c[1], c[2]) for c in restored] == [(c[1], c[2]) for c in initial]
    client.post(url, json=body).raise_for_status()
    assert configs() == restored

    available = client.get("/admin/v1/plugin-configurations?limit=100").json()["items"]
    ids = {c[1]: c[0] for c in restored}
    assert set(ids.values()) <= {c["id"] for c in available}
    plan = {
        "name": node_id,
        "components": [
            {"node_id": key, "config_id": ids[value]} for key, value in components.items()
        ],
    }
    validation = client.post("/admin/v1/multimodal-pipelines:validate", json=plan)
    assert validation.status_code == 200, validation.text
    assert validation.json()["valid"] is True
    saved = client.post("/admin/v1/multimodal-pipelines", json=plan)
    assert saved.status_code == 201, saved.text
    assert saved.json()["graph_digest"] == validation.json()["graph_digest"]


def test_batch_reuses_explicit_saved_config_without_overwriting_it(client, db):
    sync(client)
    config = client.post(
        "/admin/v1/plugin-configurations",
        json={"plugin_id": VLM, "name": "custom-batch", "config": {"prompt": "保留自定义描述"}},
    ).json()
    node_id = "batch-custom-" + uuid.uuid4().hex[:12]
    enroll(client, node_id, memory_bytes=16 * 1024 * MIB)
    count = db.execute("SELECT count(*) FROM console_plugin_config").fetchone()[0]
    result = client.post(
        f"/admin/v1/nodes/{node_id}/plugins:batch-deploy", json={"plugin_ids": [VLM]}
    ).json()
    assert result["rejected"] == []
    assert len(result["operations"]) == 1
    assert db.execute("SELECT count(*) FROM console_plugin_config").fetchone()[0] == count
    stored = db.execute(
        "SELECT config_hash,config FROM console_plugin_instance WHERE node_id=%s", (node_id,)
    ).fetchone()
    assert stored[0] == config["config_hash"]
    assert stored[1]["prompt"] == "保留自定义描述"
