"""插件进程的 loopback endpoint 文件契约（ADR-030）。

热部署执行器以 `--port 0` 启动候选进程，由内核分配端口；插件完成绑定后必须把真实 loopback
endpoint **原子写出**到 `--endpoint-file`。执行器只读这个文件，**不从 stdout 推断端口**：
stdout 是非结构化输出，日志格式改一行就会把端口判错，而文件是可校验的结构化契约。
"""

from __future__ import annotations

import json
import os
import pathlib

FORMAT = "sensoryplex.plugin-endpoint/1"


def write_endpoint_file(
    path: str | os.PathLike[str],
    endpoint: str,
    *,
    plugin_id: str,
    plugin_version: str,
    artifact_digest: str,
) -> pathlib.Path:
    """原子写出 endpoint 文件：先写同目录临时文件，再 rename 替换。

    同目录 + rename 保证读到文件的执行器只会看到"完整"内容，不会读到大半截 JSON。
    """
    target = pathlib.Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp{os.getpid()}")
    temporary.write_text(
        json.dumps(
            {
                "format": FORMAT,
                "endpoint": endpoint,
                "pid": os.getpid(),
                "plugin_id": plugin_id,
                "plugin_version": plugin_version,
                "artifact_digest": artifact_digest,
            },
            sort_keys=True,
        )
        + "\n"
    )
    os.replace(temporary, target)
    return target


def remove_endpoint_file(path: str | os.PathLike[str] | None) -> None:
    """尽力删除 endpoint 文件；实例已停止时留着它会让调度器解析到死端点。"""
    if not path:
        return
    try:
        pathlib.Path(path).expanduser().unlink(missing_ok=True)
    except OSError:
        pass
