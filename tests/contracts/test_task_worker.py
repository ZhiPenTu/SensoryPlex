"""遗留宿主工作器与 v2 执行器的职责隔离。"""

from tools.task_worker import process_pending_task


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
