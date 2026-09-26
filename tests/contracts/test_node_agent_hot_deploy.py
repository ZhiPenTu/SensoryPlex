"""热部署执行器的意图校验、回报形状与"绝不猜成功"的观测语义（纯本地：不起进程、不联网）。

真实平台服务托管、真实候选进程与真实蓝绿由 `tools/verify_plugin_hot_deploy.py --scope native`
在宿主上验收；本文件只固定"意图 → 事实"翻译层里可离线判定的那部分契约。
"""

from types import SimpleNamespace

import pytest

from tools.node_agent_hot_deploy import HotDeployExecutor


class RecordingClient:
    """只记录 Agent 回报，不触发任何网络请求。"""

    def __init__(self):
        self.reports = []

    def report_deployment(self, **payload):
        self.reports.append(payload)

    def download_release_bundle(self, release_id, target):  # pragma: no cover - 不应被调用
        raise AssertionError(f"unexpected download of {release_id} -> {target}")


class FakeSupervisor:
    """平台服务的替身：只回答状态，被动调用一律失败。"""

    name = "fake"

    def __init__(self, *, loaded=False, running=False, detail="", last_exit_code=0):
        self._state = SimpleNamespace(
            loaded=loaded, running=running, detail=detail, last_exit_code=last_exit_code
        )

    def state(self, unit_name):
        return self._state

    def load(self, unit_name, spec):  # pragma: no cover - 不应被调用
        raise AssertionError("supervisor.load must not be called")

    def stop(self, unit_name):  # pragma: no cover - 不应被调用
        raise AssertionError("supervisor.stop must not be called")


def intent(**overrides):
    payload = {
        "intent_id": "intent_1",
        "instance_id": "inst_1",
        "node_id": "local-host",
        "plugin_id": "org.sensoryplex.deploy-canary",
        "action": "DEPLOYMENT_ACTION_STAGE_RELEASE",
        "artifact_digest": "sha256:" + "a" * 64,
        "config": {},
        "operation_id": "op_1",
        "generation": 3,
        "release_id": "rel_1",
        "bundle_digest": "sha256:" + "b" * 64,
        "runtime_instance_id": "rti_1",
        "grace_period_ms": 3000,
        "config_hash": "sha256:" + "c" * 64,
        "deadline_unix_ms": 0,
    }
    payload.update(overrides)
    return payload


def executor(tmp_path, client=None, supervisor=None):
    return HotDeployExecutor(
        client or RecordingClient(),
        base_dir=tmp_path,
        platform="macos",
        arch="aarch64",
        supervisors=supervisor or FakeSupervisor(),
    )


def test_incomplete_intent_is_rejected_without_touching_anything(tmp_path):
    client = RecordingClient()
    hot = executor(tmp_path, client)

    assert hot.stage_release(intent(release_id="")) is False
    assert client.reports == [
        {
            "intent_id": "intent_1",
            "instance_id": "inst_1",
            "action": "DEPLOYMENT_ACTION_STAGE_RELEASE",
            "success": False,
            "actual_state": "PLUGIN_INSTANCE_STATE_FAILED",
            "error_code": "deployment_operation_missing:release_id",
            "error_detail": "intent rejected: deployment_operation_missing:release_id",
            "operation_id": "op_1",
            "generation": 3,
            "release_id": "",
            "runtime_instance_id": "rti_1",
        }
    ]
    # 拒绝发生在取制品之前：本机不应留下任何 staging/实例目录。
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"operation_id": ""}, "deployment_operation_missing:operation_id"),
        ({"bundle_digest": ""}, "deployment_operation_missing:bundle_digest"),
        ({"runtime_instance_id": ""}, "deployment_operation_missing:runtime_instance_id"),
        ({"artifact_digest": "md5:abc"}, "invalid_artifact_digest"),
        ({"config_hash": ""}, "deployment_operation_missing:config_hash"),
        ({"config_hash": "sha256:short"}, "invalid_config_hash"),
        ({"bundle_digest": "rel_1"}, "invalid_bundle_digest"),
    ],
)
def test_intent_identity_is_mandatory(tmp_path, overrides, code):
    client = RecordingClient()
    assert executor(tmp_path, client).stage_release(intent(**overrides)) is False
    assert client.reports[-1]["error_code"] == code
    assert client.reports[-1]["success"] is False


def test_expired_intent_fails_explicitly(tmp_path):
    client = RecordingClient()
    # 截止时间已过：不得下载、不得安装，直接显式失败（而不是挂到超时）。
    expired = intent(deadline_unix_ms=1)
    assert executor(tmp_path, client).stage_release(expired) is False
    assert client.reports[-1]["error_code"] == "operation_deadline_exceeded"


def test_report_omits_empty_optional_fields(tmp_path):
    """回报里空值必须被丢掉：`stage=0` 会被控制面判成 invalid_deployment_stage。"""
    client = RecordingClient()
    hot = executor(tmp_path, client)
    hot._report(
        intent(),
        success=True,
        stage="staging",
        staging_ms=1200,
        endpoint="",
        supervisor_id=None,
        verified_plugin_id="",
    )
    (payload,) = client.reports
    assert payload["stage"] == "PLUGIN_OPERATION_STAGE_STAGING"
    assert payload["staging_ms"] == 1200
    assert "endpoint" not in payload
    assert "supervisor_id" not in payload
    assert "verified_plugin_id" not in payload
    assert "draining_ms" not in payload
    # 意图身份必须逐字回带，控制面据此做 fencing。
    assert payload["generation"] == 3
    assert payload["operation_id"] == "op_1"
    assert payload["release_id"] == "rel_1"
    assert payload["runtime_instance_id"] == "rti_1"


def test_failure_detail_is_bounded(tmp_path):
    client = RecordingClient()
    executor(tmp_path, client)._fail(intent(), "candidate_start_failed", "x" * 5000)
    (payload,) = client.reports
    assert payload["success"] is False
    assert payload["error_code"] == "candidate_start_failed"
    assert len(payload["error_detail"]) == 500
    assert "stage" not in payload


def test_stop_without_local_record_does_not_invent_a_running_instance(tmp_path):
    client = RecordingClient()
    hot = executor(tmp_path, client)
    stop_intent = intent(action="DEPLOYMENT_ACTION_STOP")
    assert hot.stop_runtime(stop_intent) is True
    assert client.reports[-1]["actual_state"] == "PLUGIN_INSTANCE_STATE_STOPPED"
    assert client.reports[-1]["success"] is True


def test_drain_of_unknown_runtime_fails_explicitly(tmp_path):
    client = RecordingClient()
    drain_intent = intent(action="DEPLOYMENT_ACTION_DRAIN")
    assert executor(tmp_path, client).drain(drain_intent) is False
    assert client.reports[-1]["error_code"] == "drain_target_unknown"


def test_drain_refuses_to_shrink_grace_below_operation_budget(tmp_path):
    """排空窗口不得缩水：剩余操作时限比 grace 还短时显式失败 drain_timeout。"""
    client = RecordingClient()
    hot = executor(tmp_path, client)
    hot._remember(
        "rti_1",
        {
            "plugin_id": "org.sensoryplex.deploy-canary",
            "release_id": "rel_1",
            "artifact_digest": "sha256:" + "a" * 64,
            "unit_name": "org.sensoryplex.plugin.canary.rti_1",
            "desired_state": "running",
        },
    )
    drain_intent = intent(
        action="DEPLOYMENT_ACTION_DRAIN",
        grace_period_ms=120_000,
        deadline_unix_ms=1_000,
    )
    assert hot.drain(drain_intent) is False
    assert client.reports[-1]["error_code"] == "drain_timeout"


def test_observations_never_guess_success(tmp_path):
    client = RecordingClient()
    hot = executor(tmp_path, client, FakeSupervisor(loaded=False, running=False, detail="unloaded"))
    assert hot.observations() == []

    hot._remember(
        "rti_1",
        {
            "plugin_id": "org.sensoryplex.deploy-canary",
            "release_id": "rel_1",
            "artifact_digest": "sha256:" + "a" * 64,
            "unit_name": "org.sensoryplex.plugin.canary.rti_1",
            "desired_state": "running",
            "endpoint_file": str(tmp_path / "endpoint.json"),
        },
    )
    (observation,) = hot.observations()
    # 期望在跑、平台服务却说不存在：如实报 unknown，让控制面拿到 reconciliation_required。
    assert observation["reconciliation"] == "unknown"
    assert observation["observed_state"] == "stopped"
    assert observation["endpoint"] == ""
    assert observation["unit_loaded"] is False
    assert observation["detail"]

    hot.supervisor = FakeSupervisor(loaded=True, running=True)
    (observation,) = hot.observations()
    # 服务在跑但 endpoint 文件没了：同样不算 matched（不能凭"进程活着"推断可服务）。
    assert observation["reconciliation"] == "unknown"
    assert observation["observed_state"] == "running"


def test_registry_roundtrip_is_atomic_and_readable(tmp_path):
    hot = executor(tmp_path)
    hot._remember("rti_1", {"unit_name": "u1", "desired_state": "running"})
    hot._remember("rti_2", {"unit_name": "u2", "desired_state": "stopped"})
    assert hot.registry() == {
        "rti_1": {"unit_name": "u1", "desired_state": "running"},
        "rti_2": {"unit_name": "u2", "desired_state": "stopped"},
    }
    assert list(tmp_path.glob("*.tmp")) == []


def test_unreadable_registry_degrades_to_empty(tmp_path):
    (tmp_path / "hot-deploy.json").write_text("{not json")
    assert executor(tmp_path).registry() == {}
