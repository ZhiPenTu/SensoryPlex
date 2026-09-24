"""SensoryPlex 统一日志封装契约与合规测试。"""

import io
import json
import logging
import sys

import pytest
from edge_material_sdk import (
    ConsoleLogFormatter,
    JsonLogFormatter,
    SensoryPlexLogger,
    clear_log_context,
    configure_logging,
    get_log_context,
    get_logger,
    get_trace_id,
    log_context,
    sanitize_dict,
    sanitize_value,
    set_log_context,
    set_trace_id,
)


@pytest.fixture(autouse=True)
def clean_context():
    clear_log_context()
    yield
    clear_log_context()


def test_get_logger_returns_sensoryplex_logger():
    logger = get_logger("test.module")
    assert isinstance(logger, SensoryPlexLogger)
    assert logger.name == "test.module"


def test_context_vars_propagation():
    assert get_trace_id() is None
    assert get_log_context() == {}

    set_trace_id("trace_12345")
    set_log_context(component="api", node_id="node_1")

    assert get_trace_id() == "trace_12345"
    assert get_log_context() == {"component": "api", "node_id": "node_1"}

    with log_context(trace_id="inner_trace", stream_id="str_99"):
        assert get_trace_id() == "inner_trace"
        ctx = get_log_context()
        assert ctx["stream_id"] == "str_99"
        assert ctx["component"] == "api"

    # 退出上下文管理器后恢复
    assert get_trace_id() == "trace_12345"
    assert "stream_id" not in get_log_context()

    clear_log_context()
    assert get_trace_id() is None
    assert get_log_context() == {}


def test_redact_sensitive_keys():
    data = {
        "username": "admin",
        "password": "supersecretpassword",
        "api_token": "tok_xyz123",
        "client_secret": "sec_456",
        "authorization": "Bearer token123",
        "nested": {
            "token": "nested_secret",
            "safe_val": 42,
        },
    }
    sanitized = sanitize_dict(data)
    assert sanitized["username"] == "admin"
    assert sanitized["password"] == "***"
    assert sanitized["api_token"] == "***"
    assert sanitized["client_secret"] == "***"
    assert sanitized["authorization"] == "***"
    assert sanitized["nested"]["token"] == "***"
    assert sanitized["nested"]["safe_val"] == 42


def test_redact_dsn_password_and_bearer_in_strings():
    dsn = "postgresql://sensoryplex:mypassword123@127.0.0.1:5432/sensoryplex"
    masked_dsn = sanitize_value(dsn)
    assert "mypassword123" not in masked_dsn
    assert masked_dsn == "postgresql://sensoryplex:***@127.0.0.1:5432/sensoryplex"

    auth_str = "Authorization header: Bearer abcdef123456789.xyz"
    masked_auth = sanitize_value(auth_str)
    assert "abcdef123456789" not in masked_auth
    assert masked_auth == "Authorization header: Bearer ***"


def test_protect_raw_binary_and_tensors():
    # 短字节保留十六进制
    short_b = b"hello"
    assert sanitize_value(short_b) == f"<bytes {short_b.hex()}>"

    # 长字节（原始媒体帧、PCM 音频、共享内存缓冲区）禁止 dump 到日志
    long_b = b"\x00" * 1024
    assert sanitize_value(long_b) == "<bytes len=1024>"

    # 模拟张量（numpy / PyTorch / mlx）
    class MockTensor:
        def __init__(self, shape, dtype):
            self.shape = shape
            self.dtype = dtype

    tensor = MockTensor((1, 512), "float32")
    assert sanitize_value(tensor) == "<tensor shape=(1, 512) dtype=float32>"


def test_circular_reference_protection():
    obj: dict = {"a": 1}
    obj["self"] = obj
    sanitized = sanitize_dict(obj)
    assert sanitized["a"] == 1
    assert sanitized["self"] == "<circular_ref>"


def test_json_formatter_outputs_valid_json_with_metadata():
    formatter = JsonLogFormatter()
    raw_logger = logging.getLogger("test.json")

    set_trace_id("tr_abc")
    record = raw_logger.makeRecord(
        "test.json",
        logging.INFO,
        "test_file.py",
        42,
        "Processed batch of items",
        (),
        None,
        func="process_batch",
        extra={"structured_extra": {"batch_size": 32, "password": "leak"}, "trace_id": "tr_abc"},
    )

    formatted = formatter.format(record)
    parsed = json.loads(formatted)

    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test.json"
    assert parsed["message"] == "Processed batch of items"
    assert parsed["trace_id"] == "tr_abc"
    assert parsed["caller"] == "test_file.py:42"
    assert parsed["func"] == "process_batch"
    assert parsed["extra"]["batch_size"] == 32
    assert parsed["extra"]["password"] == "***"
    assert "timestamp" in parsed


def test_json_formatter_with_exception_info():
    class BusinessError(Exception):
        code = "quota_exceeded"
        reason_code = "resource_exhausted"

    formatter = JsonLogFormatter()
    raw_logger = logging.getLogger("test.exc")

    try:
        raise BusinessError("Not enough quota")
    except BusinessError:
        exc_info = sys.exc_info()

    record = raw_logger.makeRecord(
        "test.exc",
        logging.ERROR,
        "test_file.py",
        100,
        "Operation failed",
        (),
        exc_info,
        func="do_op",
    )

    formatted = formatter.format(record)
    parsed = json.loads(formatted)

    assert parsed["level"] == "ERROR"
    assert "error" in parsed
    assert parsed["error"]["type"] == "BusinessError"
    assert parsed["error"]["message"] == "Not enough quota"
    assert parsed["error"]["code"] == "quota_exceeded"
    assert parsed["error"]["reason_code"] == "resource_exhausted"
    assert "traceback" in parsed["error"]


def test_console_formatter_produces_readable_text():
    formatter = ConsoleLogFormatter(colorize=False)
    raw_logger = logging.getLogger("test.console")

    record = raw_logger.makeRecord(
        "test.console",
        logging.WARNING,
        "file.py",
        10,
        "High latency warning",
        (),
        None,
        extra={"structured_extra": {"latency_ms": 150.5}, "trace_id": "tr_999"},
    )

    line = formatter.format(record)
    assert "[WARNING]" in line
    assert "[test.console]" in line
    assert "[trace_id=tr_999]" in line
    assert "High latency warning" in line
    assert "latency_ms=150.5" in line


def test_logger_bind_and_audit():
    stream = io.StringIO()
    configure_logging(level="DEBUG", log_format="json", stream=stream, force=True)

    base_logger = get_logger("sensoryplex.test")
    bound_logger = base_logger.bind(component="pipeline", worker_id="w-1")

    bound_logger.info("Worker started", max_tasks=10)
    lines = stream.getvalue().strip().split("\n")
    data = json.loads(lines[-1])

    assert data["message"] == "Worker started"
    assert data["extra"]["component"] == "pipeline"
    assert data["extra"]["worker_id"] == "w-1"
    assert data["extra"]["max_tasks"] == 10

    # 审计日志
    bound_logger.audit(
        action="plugin.install",
        target="ocr-rapidocr",
        outcome="success",
        user="admin",
    )
    lines = stream.getvalue().strip().split("\n")
    audit_data = json.loads(lines[-1])

    assert "AUDIT plugin.install" in audit_data["message"]
    assert audit_data["extra"]["event"] == "audit"
    assert audit_data["extra"]["audit_action"] == "plugin.install"
    assert audit_data["extra"]["audit_target"] == "ocr-rapidocr"
    assert audit_data["extra"]["audit_outcome"] == "success"
    assert audit_data["extra"]["user"] == "admin"
