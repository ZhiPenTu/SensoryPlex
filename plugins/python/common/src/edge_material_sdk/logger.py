"""SensoryPlex 统一规范化日志封装。

按照高性能底层与金融级应用规范设计：
1. 结构化日志：支持容器 JSON 行（Cloud-native/ELK/Loki）与本地调试 Console 彩色格式；
2. 全链路可观测：通过 contextvars 自动传播 trace_id 与调用上下文；
3. 严格数据脱敏与保护（契约与安全约定）：
   - 自动掩码 password、token、secret、authorization、私钥与连接串凭据；
   - 绝不把原始媒体帧、音频 PCM、大型张量或原始二进制缓冲区 dump 进日志，仅记录受控引用；
   - 超长字符串安全截断，防止大报文或 Base64 拖垮日志采集；
4. 审计与合规（Audit）：内置标准安全与操作审计日志生成语义；
5. 高性能零开销判断：未达到日志级别时不产生字典拷贝或脱敏开销；
6. 零额外依赖：基于 Python 标准库构建，与所有微服务、工具、CLI 与插件环境完全兼容。
"""

from __future__ import annotations

import contextvars
import datetime
import json
import logging
import os
import re
import sys
import traceback
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any

# ── 上下文变量（全链路追踪与环境绑定）──────────────────────────────────────────────

_CURRENT_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sensoryplex_current_trace_id", default=None
)
_CURRENT_LOG_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "sensoryplex_current_log_context", default=None
)


def set_trace_id(trace_id: str | None) -> None:
    """设置当前异步协程或线程的 trace_id。"""
    _CURRENT_TRACE_ID.set(trace_id)


def get_trace_id() -> str | None:
    """获取当前协程或线程绑定的 trace_id。"""
    return _CURRENT_TRACE_ID.get()


def set_log_context(**kwargs: Any) -> None:
    """向当前协程或线程绑定的日志上下文中合并键值对。"""
    ctx = dict(_CURRENT_LOG_CONTEXT.get() or {})
    ctx.update(kwargs)
    _CURRENT_LOG_CONTEXT.set(ctx)


def get_log_context() -> dict[str, Any]:
    """获取当前绑定的日志上下文字典拷贝。"""
    return dict(_CURRENT_LOG_CONTEXT.get() or {})


def clear_log_context() -> None:
    """清空当前绑定的 trace_id 与上下文字典。"""
    _CURRENT_TRACE_ID.set(None)
    _CURRENT_LOG_CONTEXT.set(None)


@contextmanager
def log_context(**kwargs: Any):
    """上下文管理器：在作用域内临时注入上下文属性，退出时自动恢复。"""
    prev_trace = _CURRENT_TRACE_ID.get()
    prev_ctx = _CURRENT_LOG_CONTEXT.get()

    new_trace = kwargs.pop("trace_id", None) or prev_trace
    new_ctx = dict(prev_ctx or {})
    new_ctx.update(kwargs)

    token_trace = _CURRENT_TRACE_ID.set(new_trace)
    token_ctx = _CURRENT_LOG_CONTEXT.set(new_ctx)
    try:
        yield
    finally:
        _CURRENT_TRACE_ID.reset(token_trace)
        _CURRENT_LOG_CONTEXT.reset(token_ctx)


# ── 安全脱敏与受控引用转换（AGENTS.md 与金融级安全约束）─────────────────────────────

REDACT_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "auth",
        "authorization",
        "auth_token",
        "api_token",
        "api_key",
        "access_token",
        "refresh_token",
        "session_token",
        "client_secret",
        "private_key",
        "cookie",
        "credentials",
        "credential",
    }
)

_URL_PASSWORD_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^:]+:)([^@]+)(@)")
_BEARER_TOKEN_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]+", re.IGNORECASE)

# LogRecord 标准内部属性，格式化时忽略这些属性，仅提取业务 extra
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
        "structured_extra",
        "trace_id",
    }
)


def sanitize_value(val: Any, visited: set[int] | None = None) -> Any:
    """递归脱敏与受控引用转换。

    确保：
    1. 敏感字段（password、token 等）不入日志；
    2. 连接串中的密码自动掩码（postgresql://user:***@host...）；
    3. 绝不将原始 buffer 字节、音频数据、大张量 dump 入日志；
    4. 循环引用安全熔断；
    5. 超长字符串自动摘要。
    """
    if val is None or isinstance(val, (int, float, bool)):
        return val

    # 保护原始二进制、音频 PCM 与数据面共享内存 buffer
    if isinstance(val, (bytes, bytearray, memoryview)):
        length = len(val)
        if length > 64:
            return f"<bytes len={length}>"
        return f"<bytes {bytes(val).hex()}>"

    # 保护高维张量 / 数组（如 numpy.ndarray / torch.Tensor / mlx.core.array）
    if hasattr(val, "shape") and hasattr(val, "dtype"):
        shape = tuple(val.shape) if hasattr(val.shape, "__iter__") else val.shape
        return f"<tensor shape={shape} dtype={val.dtype}>"

    # 字符串脱敏与超长截断
    if isinstance(val, str):
        if len(val) > 2048:
            val = val[:256] + f"... <truncated total_len={len(val)}>"
        val = _URL_PASSWORD_RE.sub(r"\1***\3", val)
        val = _BEARER_TOKEN_RE.sub(r"\1***", val)
        return val

    if visited is None:
        visited = set()
    val_id = id(val)
    if val_id in visited:
        return "<circular_ref>"
    visited.add(val_id)

    try:
        # Protobuf 消息安全转换
        if hasattr(val, "DESCRIPTOR") and hasattr(val, "SerializeToString"):
            try:
                from google.protobuf.json_format import MessageToDict

                return sanitize_dict(MessageToDict(val, preserving_proto_field_name=True), visited)
            except Exception:
                size = val.ByteSize() if hasattr(val, "ByteSize") else "?"
                return f"<{val.__class__.__name__} len={size}>"

        if isinstance(val, Mapping):
            return sanitize_dict(val, visited)
        if isinstance(val, (list, tuple, set)):
            return [sanitize_value(item, visited) for item in val]

        # dataclass / pydantic 模型
        if hasattr(val, "model_dump") and callable(val.model_dump):
            try:
                return sanitize_dict(val.model_dump(), visited)
            except Exception:
                pass
        elif hasattr(val, "__dict__") and not isinstance(val, type):
            try:
                return sanitize_dict(val.__dict__, visited)
            except Exception:
                pass

        return str(val)
    finally:
        visited.discard(val_id)


def sanitize_dict(d: Mapping[str, Any], visited: set[int] | None = None) -> dict[str, Any]:
    """字典键值脱敏。"""
    if visited is None:
        visited = set()
    d_id = id(d)
    visited.add(d_id)
    try:
        sanitized: dict[str, Any] = {}
        for k, v in d.items():
            key_str = str(k)
            lower_k = key_str.lower()
            if lower_k in REDACT_KEYS or any(
                sub in lower_k
                for sub in (
                    "password",
                    "secret",
                    "auth_token",
                    "api_token",
                    "api_key",
                    "private_key",
                )
            ):
                sanitized[key_str] = "***"
            else:
                sanitized[key_str] = sanitize_value(v, visited)
        return sanitized
    finally:
        visited.discard(d_id)


# ── 格式化器（JSON 与 Console）───────────────────────────────────────────────


class JsonLogFormatter(logging.Formatter):
    """结构化 JSON 日志格式化器（满足云原生容器与日志采集规范）。"""

    def format(self, record: logging.LogRecord) -> str:
        dt = datetime.datetime.fromtimestamp(record.created, datetime.UTC)
        iso_time = dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        message = record.getMessage()
        trace_id = getattr(record, "trace_id", None) or get_trace_id()

        payload: dict[str, Any] = {
            "timestamp": iso_time,
            "level": record.levelname,
            "logger": record.name,
            "message": sanitize_value(message),
        }
        if trace_id:
            payload["trace_id"] = trace_id

        payload["caller"] = f"{record.filename}:{record.lineno}"
        payload["func"] = record.funcName

        extra_data = getattr(record, "structured_extra", None)
        if not extra_data:
            extra_data = {
                k: v
                for k, v in record.__dict__.items()
                if k not in _STANDARD_RECORD_ATTRS and not k.startswith("_")
            }
        if extra_data:
            payload["extra"] = sanitize_dict(extra_data)

        if record.exc_info:
            exc_type, exc_val, exc_tb = record.exc_info
            error_info: dict[str, Any] = {
                "type": exc_type.__name__ if exc_type else "Exception",
                "message": str(exc_val),
            }
            if hasattr(exc_val, "code"):
                error_info["code"] = exc_val.code
            if hasattr(exc_val, "reason_code"):
                error_info["reason_code"] = exc_val.reason_code
            if exc_tb:
                error_info["traceback"] = "".join(
                    traceback.format_exception(*record.exc_info)
                ).strip()
            payload["error"] = error_info

        return json.dumps(payload, ensure_ascii=False)


class ConsoleLogFormatter(logging.Formatter):
    """控制台人性化彩色格式化器（本地开发与调试直观输出）。"""

    _COLORS = {
        "DEBUG": "\033[36m",  # 青色
        "INFO": "\033[32m",  # 绿色
        "WARNING": "\033[33m",  # 黄色
        "ERROR": "\033[31m",  # 红色
        "CRITICAL": "\033[1;31m",  # 粗体红
    }
    _RESET = "\033[0m"
    _DIM = "\033[2m"
    _CYAN = "\033[36m"
    _BLUE = "\033[34m"

    def __init__(self, colorize: bool | None = None):
        super().__init__()
        if colorize is None:
            colorize = sys.stderr.isatty() and os.getenv("NO_COLOR") is None
        self.colorize = colorize

    def format(self, record: logging.LogRecord) -> str:
        dt = datetime.datetime.fromtimestamp(record.created, datetime.UTC)
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        level = record.levelname
        trace_id = getattr(record, "trace_id", None) or get_trace_id()
        message = record.getMessage()

        extra_data = getattr(record, "structured_extra", None) or {}
        if not extra_data:
            extra_data = {
                k: v
                for k, v in record.__dict__.items()
                if k not in _STANDARD_RECORD_ATTRS and not k.startswith("_")
            }

        sanitized_extra = sanitize_dict(extra_data) if extra_data else {}
        extra_str = ""
        if sanitized_extra:
            items = [f"{k}={v}" for k, v in sorted(sanitized_extra.items())]
            extra_str = " | " + " ".join(items)

        trace_part = f" [trace_id={trace_id}]" if trace_id else ""

        if self.colorize:
            color = self._COLORS.get(level, "")
            line = (
                f"{self._DIM}{time_str}{self._RESET} "
                f"{color}[{level:5s}]{self._RESET} "
                f"{self._CYAN}[{record.name}]{self._RESET}"
                f"{self._BLUE}{trace_part}{self._RESET} "
                f"{message}{self._DIM}{extra_str}{self._RESET}"
            )
        else:
            line = f"{time_str} [{level:5s}] [{record.name}]{trace_part} {message}{extra_str}"

        if record.exc_info:
            tb_str = "".join(traceback.format_exception(*record.exc_info)).strip()
            line = f"{line}\n{tb_str}"

        return line


# ── SensoryPlex 统一 Logger 封装 ─────────────────────────────────────────────


class SensoryPlexLogger:
    """SensoryPlex 规范化日志记录器包装类。

    具备：
    - 结构化键值参数自动捕获并脱敏；
    - 异步与调用栈上下文（trace_id）自动注入；
    - 链式派生与属性绑定（bind / with_context）；
    - 企业级审计事件生成（audit）；
    - 准确的调用栈深度映射（保证记录代码文件名和行号非包装器自身）。
    """

    def __init__(self, logger: logging.Logger, context: dict[str, Any] | None = None):
        self._logger = logger
        self._context: dict[str, Any] = dict(context or {})

    @property
    def name(self) -> str:
        return self._logger.name

    def is_enabled_for(self, level: int) -> bool:
        return self._logger.isEnabledFor(level)

    def isEnabledFor(self, level: int) -> bool:
        return self._logger.isEnabledFor(level)

    def bind(self, **kwargs: Any) -> SensoryPlexLogger:
        """派生携带新绑定上下文的 Logger 实例。"""
        new_ctx = dict(self._context)
        new_ctx.update(kwargs)
        return SensoryPlexLogger(self._logger, new_ctx)

    def with_context(self, **kwargs: Any) -> SensoryPlexLogger:
        """bind 的别名。"""
        return self.bind(**kwargs)

    def _log(
        self,
        level: int,
        msg: Any,
        args: tuple,
        exc_info: Any = None,
        stack_info: bool = False,
        stacklevel: int = 2,
        **kwargs: Any,
    ) -> None:
        if not self._logger.isEnabledFor(level):
            return

        if "exc_info" in kwargs and exc_info is None:
            exc_info = kwargs.pop("exc_info")
        if "stack_info" in kwargs:
            stack_info = kwargs.pop("stack_info")
        if "stacklevel" in kwargs:
            stacklevel = kwargs.pop("stacklevel")

        # 合并全局上下文、Logger 实例上下文与单次调用参数
        combined_extra = dict(get_log_context())
        combined_extra.update(self._context)
        combined_extra.update(kwargs)

        trace_id = combined_extra.pop("trace_id", None) or get_trace_id()

        extra = {
            "structured_extra": combined_extra,
            "trace_id": trace_id,
        }

        try:
            self._logger._log(
                level,
                msg,
                args,
                exc_info=exc_info,
                extra=extra,
                stack_info=stack_info,
                stacklevel=stacklevel + 1,
            )
        except Exception:
            # 保证日志打印本身失败决不阻断主业务流程
            pass

    def debug(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, args, **kwargs)

    def info(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, args, **kwargs)

    def warning(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, args, **kwargs)

    def warn(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, args, **kwargs)

    def error(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, args, **kwargs)

    def critical(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.CRITICAL, msg, args, **kwargs)

    def exception(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, args, exc_info=True, **kwargs)

    def audit(
        self,
        action: str,
        target: str = "",
        outcome: str = "success",
        **kwargs: Any,
    ) -> None:
        """记录企业级安全与操作审计事件。"""
        self._log(
            logging.INFO,
            f"AUDIT {action} target={target} outcome={outcome}",
            (),
            event="audit",
            audit_action=action,
            audit_target=target,
            audit_outcome=outcome,
            **kwargs,
        )


# ── 配置与全局初始化 ─────────────────────────────────────────────────────────

_CONFIGURED: bool = False


def configure_logging(
    level: str | int | None = None,
    log_format: str | None = None,
    stream: Any = None,
    colorize: bool | None = None,
    force: bool = False,
) -> None:
    """初始化全局 SensoryPlex 日志系统配置。

    支持环境变量：
    - SENSORYPLEX_LOG_LEVEL（或回退 RUST_LOG）: DEBUG | INFO | WARNING | ERROR
    - SENSORYPLEX_LOG_FORMAT: json | console
    - SENSORYPLEX_LOG_STREAM: stderr | stdout
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    # 1. 确定日志级别
    if level is None:
        level_str = os.getenv("SENSORYPLEX_LOG_LEVEL") or os.getenv("RUST_LOG") or "INFO"
        level = getattr(logging, level_str.upper(), logging.INFO)
    elif isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    # 2. 确定输出流
    if stream is None:
        stream_env = os.getenv("SENSORYPLEX_LOG_STREAM", "stderr").lower()
        stream = sys.stdout if stream_env == "stdout" else sys.stderr

    # 3. 确定日志格式
    if log_format is None:
        log_format = os.getenv("SENSORYPLEX_LOG_FORMAT")
        if not log_format:
            is_atty = getattr(stream, "isatty", lambda: False)()
            log_format = "console" if is_atty else "json"

    # 4. 创建 Handler 与 Formatter
    handler = logging.StreamHandler(stream)
    if log_format.lower() == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(ConsoleLogFormatter(colorize=colorize))

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # 清理并挂接 Handler
    for existing_h in list(root_logger.handlers):
        if isinstance(existing_h, logging.StreamHandler):
            root_logger.removeHandler(existing_h)

    root_logger.addHandler(handler)
    _CONFIGURED = True


def get_logger(name: str | None = None, **kwargs: Any) -> SensoryPlexLogger:
    """获取 SensoryPlex 统一规范化日志记录器。

    参数：
    - name: 模块或服务标识（如 sensoryplex.api、sensoryplex.relay 等）
    - **kwargs: 当前 Logger 默认绑定的结构化上下文属性
    """
    if not _CONFIGURED:
        configure_logging()
    logger_name = name or "sensoryplex"
    raw_logger = logging.getLogger(logger_name)
    return SensoryPlexLogger(raw_logger, context=kwargs)
