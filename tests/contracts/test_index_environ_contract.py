"""检索面读配置的契约：只认 OS 给的那份环境，不认第三方 import 顺手写进 `os.environ` 的值。

背景（ADR-027 §10/§11）：`pymilvus.settings` 在 import 期调用 `load_dotenv()`，python-dotenv
会从 `pymilvus/settings.py` 所在目录**向上找 `.env`**（`__main__` 没有 `__file__` 时退回用 cwd）。
因此"venv 放在哪"会改变 `serve` 读到的配置：主机 venv 在仓库根下（命中仓库 `.env`），容器里在
`/app/.venv`（不命中）。这些用例把这条路径**固定成可判定的契约**，不依赖运行时的实际布局。
"""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sensoryplex_index_worker import BASE_ENVIRON
from sensoryplex_index_worker.cli import build_parser, consume_options_of, run_serve

TOKEN_VAR = "SENSORYPLEX_INDEX_AUTH_TOKEN"


def test_snapshot_is_taken_before_pymilvus_can_write_to_the_environment(tmp_path):
    """真造一次陷阱：cwd 放一份 `.env`，用 `python -c` 跑（dotenv 在无 `__file__` 时用 cwd）。

    子进程里 `os.environ` **会**被 dotenv 写上令牌（陷阱是真的），但 `BASE_ENVIRON` 不该有——
    它必须在 `pymilvus` 被 import 之前取好。顺序若被改坏，这条立刻变红。
    """

    (tmp_path / ".env").write_text(f"{TOKEN_VAR}=leaked-by-dotenv\n", encoding="utf-8")
    probe = (
        "import os;"
        "import sensoryplex_index_worker as worker;"
        f"print(repr(os.environ.get({TOKEN_VAR!r})));"
        f"print(repr(worker.BASE_ENVIRON.get({TOKEN_VAR!r})))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        # 子进程环境里先摘掉同名变量：dotenv 默认 `override=False`，留着会让"陷阱"看不出来。
        env={key: value for key, value in os.environ.items() if key != TOKEN_VAR},
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-400:]
    from_os, from_snapshot = completed.stdout.split()
    assert from_os == repr("leaked-by-dotenv"), "陷阱没被触发：dotenv 这次没写进 os.environ"
    assert from_snapshot == repr(None), "快照晚于 pymilvus 的 import，配置来源又成了部署布局的函数"


def test_serve_refuses_without_a_token_even_when_os_environ_gets_one(monkeypatch):
    """`--auth-token` 缺失且快照里没有令牌时必须原因码退出，与 `os.environ` 之后被写什么无关。"""

    monkeypatch.delitem(BASE_ENVIRON, TOKEN_VAR, raising=False)
    monkeypatch.setenv(TOKEN_VAR, "written-into-os-environ-after-import")
    with pytest.raises(SystemExit) as exited:
        run_serve(SimpleNamespace(database_url="postgresql://placeholder/none", auth_token=""))
    assert exited.value.code == "index_auth_token_required"


def test_parser_defaults_come_from_the_snapshot_not_from_os_environ(monkeypatch):
    """`--uri` 这类默认值同样只从快照来：环境里后写进去的同名变量不许影响命令行默认值。"""

    monkeypatch.setitem(BASE_ENVIRON, "SENSORYPLEX_MILVUS_URI", "/snapshot/vector.db")
    monkeypatch.setenv("SENSORYPLEX_MILVUS_URI", "/written-into-os-environ/vector.db")
    arguments = build_parser().parse_args(
        ["inspect", "--vector-index-key", "material_text_bge_small_zh_v1_5_d512_v1"]
    )
    assert arguments.uri == "/snapshot/vector.db"


def test_consume_admission_reads_the_snapshot_not_os_environ(monkeypatch):
    """分级背压准入也必须只认快照：否则"这台机器注入没注入档位"同样是部署布局的函数。

    `os.environ` 里放一组"越界"的档位（像 dotenv 顺手写进来的那样），快照里什么都不放：
    读快照 → `not_injected`（不拦）；读 `os.environ` → `event_inflight_exceeds_tier_cap`。
    """

    monkeypatch.delitem(BASE_ENVIRON, "SENSORYPLEX_EVENT_QUEUE_CAPACITY", raising=False)
    monkeypatch.delitem(BASE_ENVIRON, "SENSORYPLEX_RESIDENT_TIER", raising=False)
    monkeypatch.setenv("SENSORYPLEX_EVENT_QUEUE_CAPACITY", "16")
    monkeypatch.setenv("SENSORYPLEX_RESIDENT_TIER", "small")
    arguments = build_parser().parse_args(
        [
            "serve",
            "--vector-index-key",
            "material_text_bge_small_zh_v1_5_d512_v1",
            "--model-dir",
            "/workspace/.data/models/bge-small-zh-v1.5",
            "--consume",
            "--consume-batch",
            "200",
        ]
    )
    options = consume_options_of(arguments)
    assert options is not None and options.batch == 200
