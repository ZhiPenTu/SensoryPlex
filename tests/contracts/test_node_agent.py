"""Node Agent 对任务意图的显式失败语义。"""

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
