"""进程环境快照（ADR-027 §10 第 4 条，排查过程见 §11）：只认 OS 交给本进程的那份环境。

为什么需要它：`pymilvus.settings` 在 **import 期**调用 `load_dotenv()`，而 python-dotenv 从
`pymilvus/settings.py` 所在目录**向上找 `.env`**（`__main__` 没有 `__file__` 时还会退回用 cwd）。
于是同一份代码会因为**venv 恰好放在哪**而读到两套配置：

- 主机：venv 在仓库根下（`<repo>/.venv/`），向上走就到仓库根，**命中仓库 `.env`**；
- 容器：venv 在 `/app/.venv`、仓库挂在 `/workspace`，向上走找不到，**不命中**。

实测（同一段代码，两处环境）：

```
# 主机 —— python -m sensoryplex_index_worker.cli serve（真实路径）
$ uv run --frozen python -c "from dotenv import find_dotenv; print(find_dotenv())"
/Users/.../SensoryPlex/.env            # ← 命中
# 容器 —— /app/.venv/bin/python /tmp/find2.py
venv-relative search: ''                # ← 不命中
```

配置来源不能是"venv 放在哪"的函数：`serve` 的文档契约是"**缺失即拒绝启动**"，而主机上
`--auth-token` 缺失时进程却拿到了仓库 `.env` 里的令牌、照常起服务——契约与行为不一致，且
不一致的方向取决于部署布局。所以本模块在**任何会写 `os.environ` 的 import 之前**取一份快照，
`cli` 读配置一律读快照：**第三方 import 顺手写进来的值不是配置来源**。

快照必须早于 `from .milvus_store import VectorIndex`（它 import `pymilvus`），因此
`__init__.py` 把它排在第一位；这条顺序约束是**语义**要求，不是风格偏好。
"""

from __future__ import annotations

import os

BASE_ENVIRON: dict[str, str] = dict(os.environ)
"""OS 交给本进程的环境，在第三方 import 有机会改写 `os.environ` 之前取好。"""
