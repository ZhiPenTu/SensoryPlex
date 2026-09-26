"""Node Agent 对任务意图的受控执行与显式失败语义。"""

from tools import task_executor
from tools.node_agent import execute_intent


class RecordingClient:
    """仅记录 Agent 上报，不触发网络请求。"""

    def __init__(self):
        self.reports = []

    def report_deployment(self, **kwargs):
        self.reports.append(kwargs)


def test_task_process_reports_runtime_executor_unavailable():
    client = RecordingClient()

    assert (
        execute_intent(
            {
                "intent_id": "task_1",
                "instance_id": "",
                "action": "DEPLOYMENT_ACTION_TASK_PROCESS",
                "artifact_digest": "sha256:" + "a" * 64,
            },
            client,
        )
        is False
    )

    assert client.reports == [
        {
            "intent_id": "task_1",
            "instance_id": "",
            "action": "DEPLOYMENT_ACTION_TASK_PROCESS",
            "success": False,
            "actual_state": "PLUGIN_INSTANCE_STATE_UNSPECIFIED",
            "error_code": "runtime_task_service_not_attached",
            "error_detail": "task_process requires a controlled runtime executor",
        }
    ]


def test_orchestrated_v2_task_uses_executor_before_completing_delivery(monkeypatch, tmp_path):
    """v2 分支必须先让执行器回执；不能沿用 legacy 的固定失败。"""
    client = RecordingClient()
    calls = []

    class FakeExecutor:
        def __init__(self, received_client, *, base_dir):
            calls.append((received_client, base_dir))

        def execute(self, intent):
            calls.append(intent)
            return True

    monkeypatch.setattr(task_executor, "TaskExecutor", FakeExecutor)
    intent = {
        "intent_id": "task_v2",
        "instance_id": "",
        "action": "DEPLOYMENT_ACTION_TASK_PROCESS",
        "artifact_digest": "sha256:" + "b" * 64,
        "config": {"execution_mode": "orchestrated_v2"},
    }

    assert execute_intent(intent, client, state_file=str(tmp_path / "agent.json")) is True
    assert calls[0] == (client, tmp_path / "plugins")
    assert calls[1] == intent
    assert client.reports == [
        {
            "intent_id": "task_v2",
            "instance_id": "",
            "action": "DEPLOYMENT_ACTION_TASK_PROCESS",
            "success": True,
            "actual_state": "PLUGIN_INSTANCE_STATE_READY",
        }
    ]
