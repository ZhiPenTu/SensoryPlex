"""ADR-026 集成测试：真实 PostgreSQL 验证子节点拓扑管理、预检、Agent 通道与审计。"""

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
PASSWORD = "test-node-topology-pass-2026"


@pytest.fixture
def node_database():
    url = os.environ["SENSORYPLEX_TEST_DATABASE_URL"]
    schema = "node_test_" + uuid.uuid4().hex
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
def client(node_database):
    settings = Settings(database_url=node_database)
    app = create_app(settings)
    with TestClient(app) as test_client:
        login = test_client.post(
            "/auth/v1/session", json={"username": "admin", "password": PASSWORD}
        )
        assert login.status_code == 200
        test_client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        yield test_client


def test_node_enrollment_and_heartbeat_flow(client):
    # 1. 管理员生成注册令牌
    token_resp = client.post(
        "/admin/v1/nodes/enrollment-tokens",
        json={"node_id": "test-mac-mini-01", "expires_in_minutes": 30},
    )
    assert token_resp.status_code == 201
    tok_data = token_resp.json()
    enroll_token = tok_data["token"]
    assert enroll_token.startswith("sp_enroll_")

    # 2. Agent 使用令牌入网注册
    enroll_payload = {
        "enrollment_token": enroll_token,
        "node_id": "test-mac-mini-01",
        "display_name": "Mac mini M4 Pro",
        "is_co_located": True,
        "capabilities": {
            "platform": "macos",
            "arch": "aarch64",
            "cpu_cores": 12,
            "memory_bytes": "34359738368",
            "unified_memory_bytes": "34359738368",
            "supported_artifacts": ["local_native", "container"],
            "accelerators": [
                {
                    "accelerator": "metal",
                    "platform": "macos",
                    "state": "ACCELERATOR_STATE_AVAILABLE",
                    "runtime_version": "metal4",
                }
            ],
            "labels": {"rack": "shelf-1"},
        },
    }
    enroll_resp = client.post("/v1/agent/enroll", json=enroll_payload)
    assert enroll_resp.status_code == 200
    enroll_data = enroll_resp.json()
    assert enroll_data["success"] is True
    session_token = enroll_data["session_token"]
    assert session_token.startswith("sp_node_")

    # 3. 重复使用该一次性令牌应失败
    dup_resp = client.post("/v1/agent/enroll", json=enroll_payload)
    assert dup_resp.status_code == 401
    assert dup_resp.json()["reason_code"] == "enrollment_token_already_used"

    # 4. 管理员查看节点清单
    nodes_resp = client.get("/admin/v1/nodes")
    assert nodes_resp.status_code == 200
    items = nodes_resp.json()["items"]
    assert len(items) == 1
    assert items[0]["node_id"] == "test-mac-mini-01"
    assert items[0]["is_co_located"] is True
    assert items[0]["status"] == "NODE_STATUS_READY"
    assert items[0]["capabilities"]["cpu_cores"] == 12

    # 5. Agent 发送心跳
    hb_resp = client.post(
        "/v1/agent/heartbeat",
        json={
            "node_id": "test-mac-mini-01",
            "session_token": session_token,
            "timestamp_unix_ms": 1727164800000,
            "available_memory_bytes": "25000000000",
            "current_concurrency": 0,
            "running_instance_ids": [],
        },
    )
    assert hb_resp.status_code == 200
    hb_data = hb_resp.json()
    assert hb_data["status"] == "NODE_STATUS_READY"
    assert hb_data["heartbeat_interval_ms"] == 5000


def test_preflight_and_deployment_with_data_locality(client):
    # 创建局域网远端节点 (is_co_located=False)
    tok1 = client.post(
        "/admin/v1/nodes/enrollment-tokens",
        json={"node_id": "remote-linux-gpu", "expires_in_minutes": 60},
    ).json()["token"]

    client.post(
        "/v1/agent/enroll",
        json={
            "enrollment_token": tok1,
            "node_id": "remote-linux-gpu",
            "display_name": "Ubuntu Workstation",
            "is_co_located": False,
            "capabilities": {
                "platform": "linux",
                "arch": "x86_64",
                "cpu_cores": 32,
                "memory_bytes": "68719476736",
                "supported_artifacts": ["local_native", "container"],
            },
        },
    )

    # 创建同机数据面节点 (is_co_located=True)
    tok2 = client.post(
        "/admin/v1/nodes/enrollment-tokens",
        json={"node_id": "local-main-node", "expires_in_minutes": 60},
    ).json()["token"]

    client.post(
        "/v1/agent/enroll",
        json={
            "enrollment_token": tok2,
            "node_id": "local-main-node",
            "display_name": "Local Mac Host",
            "is_co_located": True,
            "capabilities": {
                "platform": "macos",
                "arch": "aarch64",
                "cpu_cores": 10,
                "memory_bytes": "34359738368",
                "supported_artifacts": ["local_native"],
            },
        },
    )

    # 预检：在远端节点上预检 VLM 插件（读取共享内存） -> 必须报 data_locality_violation
    pre_remote = client.post(
        "/admin/v1/nodes/remote-linux-gpu/preflight",
        json={"node_id": "remote-linux-gpu", "plugin_id": "org.sensoryplex.vlm-moondream"},
    )
    assert pre_remote.status_code == 200
    assert pre_remote.json()["eligible"] is False
    assert pre_remote.json()["reason_code"] == "data_locality_violation"

    # 尝试强行在远端节点部署 VLM 插件 -> 必须 422 拒绝并阻断
    deploy_fail = client.post(
        "/admin/v1/nodes/remote-linux-gpu/plugins/org.sensoryplex.vlm-moondream:deploy",
        json={},
    )
    assert deploy_fail.status_code == 422
    assert deploy_fail.json()["reason_code"] == "data_locality_violation"

    # 预检：在远端节点上预检 BGE Embedding 插件（仅消费文本观测，不碰共享内存） -> 允许通过
    pre_bge = client.post(
        "/admin/v1/nodes/remote-linux-gpu/preflight",
        json={"node_id": "remote-linux-gpu", "plugin_id": "org.sensoryplex.embed-bge-onnx"},
    )
    assert pre_bge.status_code == 200
    assert pre_bge.json()["eligible"] is True

    # 预检：在同机节点上预检 VLM 插件 -> 允许通过
    pre_local = client.post(
        "/admin/v1/nodes/local-main-node/preflight",
        json={"node_id": "local-main-node", "plugin_id": "org.sensoryplex.vlm-moondream"},
    )
    assert pre_local.status_code == 200
    assert pre_local.json()["eligible"] is True


def test_deployment_lifecycle_and_agent_reporting(client):
    # 注册同机节点
    tok = client.post(
        "/admin/v1/nodes/enrollment-tokens",
        json={"node_id": "worker-node", "expires_in_minutes": 60},
    ).json()["token"]

    enroll_res = client.post(
        "/v1/agent/enroll",
        json={
            "enrollment_token": tok,
            "node_id": "worker-node",
            "display_name": "Worker Node",
            "is_co_located": True,
            "capabilities": {
                "platform": "macos",
                "arch": "aarch64",
                "cpu_cores": 8,
                "memory_bytes": "17179869184",
                "supported_artifacts": ["local_native"],
            },
        },
    ).json()
    session_token = enroll_res["session_token"]

    # 下发部署
    deploy_res = client.post(
        "/admin/v1/nodes/worker-node/plugins/org.sensoryplex.vlm-moondream:deploy",
        json={},
    )
    assert deploy_res.status_code == 201
    inst_data = deploy_res.json()
    assert inst_data["actual_state"] == "installing"
    assert inst_data["desired_state"] == "ready"

    # Agent 心跳认领部署意图
    hb_res = client.post(
        "/v1/agent/heartbeat",
        json={
            "node_id": "worker-node",
            "session_token": session_token,
            "timestamp_unix_ms": 1727164800000,
        },
    ).json()
    intents = hb_res["pending_intents"]
    assert len(intents) == 1
    intent = intents[0]
    assert intent["action"] == "DEPLOYMENT_ACTION_INSTALL"
    assert intent["plugin_id"] == "org.sensoryplex.vlm-moondream"

    # Agent 上报安装成功
    report_res = client.post(
        "/v1/agent/report",
        json={
            "intent_id": intent["intent_id"],
            "instance_id": intent["instance_id"],
            "node_id": "worker-node",
            "action": intent["action"],
            "success": True,
            "actual_state": "ready",
        },
    )
    assert report_res.status_code == 200

    # 验证插件实例状态已流转为 ready
    node_detail = client.get("/admin/v1/nodes/worker-node").json()
    assert len(node_detail["instances"]) == 1
    assert node_detail["instances"][0]["actual_state"] == "ready"


def test_node_drain_and_revoke_flow(client):
    tok = client.post(
        "/admin/v1/nodes/enrollment-tokens",
        json={"node_id": "drain-revoke-node", "expires_in_minutes": 60},
    ).json()["token"]

    enroll = client.post(
        "/v1/agent/enroll",
        json={
            "enrollment_token": tok,
            "node_id": "drain-revoke-node",
            "display_name": "Drain Revoke Node",
            "is_co_located": True,
            "capabilities": {
                "platform": "linux",
                "arch": "x86_64",
                "cpu_cores": 4,
                "memory_bytes": "8589934592",
                "supported_artifacts": ["local_native"],
            },
        },
    ).json()
    session_token = enroll["session_token"]

    # 1. 排空节点
    drain_res = client.post("/admin/v1/nodes/drain-revoke-node:drain")
    assert drain_res.status_code == 200
    assert drain_res.json()["status"] == "NODE_STATUS_DRAINING"

    # 排空状态下预检拒绝
    pre = client.post(
        "/admin/v1/nodes/drain-revoke-node/preflight",
        json={"node_id": "drain-revoke-node", "plugin_id": "org.sensoryplex.embed-bge-onnx"},
    ).json()
    assert pre["eligible"] is False
    assert pre["reason_code"] == "node_draining"

    # 2. 撤销节点
    revoke_res = client.post("/admin/v1/nodes/drain-revoke-node:revoke")
    assert revoke_res.status_code == 200
    assert revoke_res.json()["status"] == "NODE_STATUS_REVOKED"

    # 撤销后心跳返回 NODE_STATUS_REVOKED 告知 Agent 退出
    hb = client.post(
        "/v1/agent/heartbeat",
        json={
            "node_id": "drain-revoke-node",
            "session_token": session_token,
            "timestamp_unix_ms": 1727164800000,
        },
    )
    # session_token_hash 已被清空
    assert hb.status_code in {200, 401}

    # 3. 检查审计日志
    audit_resp = client.get("/admin/v1/audit-events?limit=50")
    assert audit_resp.status_code == 200
    actions = [evt["action"] for evt in audit_resp.json()["items"]]
    assert "node.token.create" in actions
    assert "node.enroll.success" in actions
    assert "node.drain" in actions
    assert "node.revoke" in actions


class _MockAgentClient:
    def __init__(self, test_client, node_id, session_token):
        self.client = test_client
        self.node_id = node_id
        self.session_token = session_token

    def report_deployment(
        self, intent_id, instance_id, action, success, actual_state, error_code="", error_detail=""
    ):
        payload = {
            "intent_id": intent_id,
            "instance_id": instance_id,
            "node_id": self.node_id,
            "action": action,
            "success": success,
            "actual_state": actual_state,
            "error_code": error_code,
            "error_detail": error_detail,
        }
        res = self.client.post(
            "/v1/agent/report",
            json=payload,
            headers={"Authorization": f"Bearer {self.session_token}"},
        )
        return res.json()


def test_plugin_clean_uninstall_and_directory_removal(client, tmp_path):
    import json

    from tools.node_agent import execute_intent

    # 1. 注册节点
    node_id = "clean-uninstall-node-" + uuid.uuid4().hex[:6]
    token_res = client.post(
        "/admin/v1/nodes/enrollment-tokens",
        json={"node_id": node_id, "expires_in_minutes": 30},
    ).json()
    enroll = client.post(
        "/v1/agent/enroll",
        json={
            "enrollment_token": token_res["token"],
            "node_id": node_id,
            "display_name": "Clean Test Node",
            "is_co_located": True,
            "capabilities": {
                "platform": "macos",
                "arch": "aarch64",
                "cpu_cores": 8,
                "memory_bytes": "17179869184",
                "unified_memory_bytes": "17179869184",
                "supported_artifacts": ["local_native"],
            },
        },
    ).json()
    assert enroll["success"] is True
    session_token = enroll["session_token"]
    agent_client = _MockAgentClient(client, node_id, session_token)
    state_file = tmp_path / f"{node_id}.json"
    state_file.write_text(
        json.dumps({"session_token": session_token, "main_url": "http://testserver"})
    )

    # 2. 部署插件
    plugin_id = "org.sensoryplex.ocr-rapidocr"
    deploy_res = client.post(
        f"/admin/v1/nodes/{node_id}/plugins/{plugin_id}:deploy", json={}
    ).json()
    assert deploy_res["actual_state"] == "installing"

    # Agent 心跳认领 install 意图
    hb1 = client.post(
        "/v1/agent/heartbeat",
        json={
            "node_id": node_id,
            "session_token": session_token,
            "timestamp_unix_ms": 1727164800000,
        },
    ).json()
    intents = hb1.get("pending_intents", [])
    assert len(intents) >= 1
    intent = intents[0]
    assert intent["action"] == "DEPLOYMENT_ACTION_INSTALL"

    # 旧 install 意图不含受控制品；必须拒绝，不能伪造安装成功。
    ok = execute_intent(intent, agent_client, state_file=str(state_file))
    assert ok is False

    # 验证插件版本化独立目录存在
    digest_clean = intent["artifact_digest"].replace("sha256:", "")
    instance_dir = tmp_path / "plugins" / plugin_id / digest_clean
    assert not instance_dir.exists()
    # 卸载场景使用已停止的目录夹具，仅验证清理语义，不冒充真实制品部署。
    instance_dir.mkdir(parents=True)
    (instance_dir / "instance.json").write_text("{}")

    # 3. 触发卸载
    uninst_res = client.post(f"/admin/v1/nodes/{node_id}/plugins/{plugin_id}:uninstall")
    assert uninst_res.status_code == 200
    assert uninst_res.json()["desired_state"] == "uninstalled"

    # Agent 心跳认领 uninstall 意图
    hb2 = client.post(
        "/v1/agent/heartbeat",
        json={
            "node_id": node_id,
            "session_token": session_token,
            "timestamp_unix_ms": 1727164800000,
        },
    ).json()
    uninst_intents = hb2.get("pending_intents", [])
    assert len(uninst_intents) >= 1
    u_intent = next(i for i in uninst_intents if i["action"] == "DEPLOYMENT_ACTION_UNINSTALL")

    # 物理执行卸载
    ok_uninst = execute_intent(u_intent, agent_client, state_file=str(state_file))
    assert ok_uninst is True

    # 断言：物理目录必须彻底删除，零残留！
    assert not instance_dir.exists()
    assert not (tmp_path / "plugins" / plugin_id).exists()

    # 验证 API 状态为 uninstalled
    node_info = client.get(f"/admin/v1/nodes/{node_id}").json()
    inst = next(x for x in node_info.get("instances", []) if x["plugin_id"] == plugin_id)
    assert inst["actual_state"] == "uninstalled"


def test_node_agent_deregister_and_cleanup(client, tmp_path):
    node_id = "dereg-node-" + uuid.uuid4().hex[:6]
    token_res = client.post(
        "/admin/v1/nodes/enrollment-tokens",
        json={"node_id": node_id, "expires_in_minutes": 30},
    ).json()
    enroll = client.post(
        "/v1/agent/enroll",
        json={
            "enrollment_token": token_res["token"],
            "node_id": node_id,
            "display_name": "Dereg Node",
            "capabilities": {
                "platform": "macos",
                "arch": "aarch64",
                "cpu_cores": 8,
                "memory_bytes": "17179869184",
                "unified_memory_bytes": "17179869184",
                "supported_artifacts": ["local_native"],
            },
        },
    ).json()
    session_token = enroll["session_token"]

    # 部署插件
    client.post(f"/admin/v1/nodes/{node_id}/plugins/org.sensoryplex.ocr-rapidocr:deploy", json={})

    # Agent 调用反注册
    dereg_res = client.post(
        "/v1/agent/deregister",
        json={"node_id": node_id, "session_token": session_token},
    ).json()
    assert dereg_res["status"] == "NODE_STATUS_REVOKED"

    # 校验节点状态已撤销，插件全被标记卸载
    node_info = client.get(f"/admin/v1/nodes/{node_id}").json()
    assert node_info["status"] == "NODE_STATUS_REVOKED"
    for inst in node_info["instances"]:
        assert inst["actual_state"] == "uninstalled"
