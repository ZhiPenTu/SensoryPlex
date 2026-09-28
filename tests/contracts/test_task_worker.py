"""遗留宿主工作器与 v2 执行器的职责隔离。"""

import inspect

from tools import task_runner
from tools.task_worker import ensure_node_ready, process_pending_task


def test_legacy_worker_does_not_mask_agent_offline_state():
    class AgentNode:
        def fetchone(self):
            return {"node_id": "local-host", "status": "offline", "session_token_hash": "hash"}

    class Connection:
        def __init__(self):
            self.queries = []

        def execute(self, query, params):
            self.queries.append(query)
            return AgentNode()

    conn = Connection()
    ensure_node_ready(conn)
    assert len(conn.queries) == 1
    assert conn.queries[0].startswith("SELECT")


class _NoRow:
    def fetchone(self):
        return None


class _RecordingConnection:
    def __init__(self):
        self.queries: list[str] = []

    def execute(self, query, _params=()):
        self.queries.append(query)
        return _NoRow()


def test_legacy_task_worker_never_claims_orchestrated_v2_tasks():
    """旧 OCR 兼容器只能领取 legacy 意图和 legacy Job。"""
    conn = _RecordingConnection()

    assert process_pending_task(conn) is False

    intent_filter = "COALESCE(config->>'execution_mode', 'legacy_ocr_v1') = 'legacy_ocr_v1'"
    assert intent_filter in conn.queries[0]
    assert "pipeline.execution_mode='legacy_ocr_v1'" in conn.queries[1]
    assert "ORDER BY draft.dispatched_at ASC NULLS LAST, draft.created_at ASC" in conn.queries[1]


def test_legacy_runner_cannot_reintroduce_a_synchronous_vlm_pass():
    """`legacy_ocr_v1` 不能通过旧布尔开关绕过 VLM WorkQueue。"""
    source = inspect.getsource(task_runner.run_video_task)
    assert "VLM_MODULE" not in source
    assert "with_vlm" not in source
    assert "plugin_module=OCR_MODULE" in source
