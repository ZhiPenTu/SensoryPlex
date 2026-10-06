# SensoryPlex 辅助工具与执行器分类指南 (`tools/`)

`tools/` 目录收录了 SensoryPlex 项目中用于**构建契约生成**、**宿主机原生任务执行器/守护进程**、**算力节点 Agent**、**运维管理**以及**全链路端到端验收脚本**的所有 Python 脚本与 Shell 工具。

为了解决历史积累导致的 `tools/` 根目录扁平膨胀问题，项目现已全面完成**物理子目录化归档**，并配合**双模透明兼容 Shim** 架构，实现了目录结构的清晰治理与历史调用链的 100% 向后兼容。

---

## 1. 物理子目录架构与工具全景分类

```text
tools/
├── codegen/     # 构建契约编译、前端类型生成、随机凭据配置与数据库迁移 (4 文件)
├── workers/     # 宿主原生守护进程、编排意图执行器、节点 Agent 与 VLM 延迟工作器 (12 文件)
├── ops/         # 插件 Release 打包校验、LaunchAgent 管理、MCP 配置与缓存清理 (7 文件)
├── media/       # 媒体探测、时间轴租约交接与并发显存档位限制 (5 文件)
├── verify/      # 全链路 ADR 验收测试套件与轻量冒烟脚本 (37 文件)
└── <shims>.py   # 根目录保留 65 个与子目录脚本同名的透明兼容 Shim，保障现有 Makefile/CI/导入无感
```

| 物理子目录 | 脚本列表 | 执行环境 | 核心职责说明 |
| :--- | :--- | :--- | :--- |
| **`tools/codegen/`**<br/>(Build & Codegen) | `generate_proto.py`<br/>`generate_console_types.py`<br/>`configure.py`<br/>`migrate.py` | 容器 / 宿主 | - `generate_proto.py`: 将 `proto/` 编译为 Python 与 TypeScript 契约<br/>- `generate_console_types.py`: 生成 Web 控制台前端类型定义<br/>- `configure.py`: 宿主机生成随机安全凭据写入 `.env`<br/>- `migrate.py`: 驱动不可变追加式数据库迁移 (Flyway 式) |
| **`tools/workers/`**<br/>(Host Daemons & Workers) | `task_executor.py`<br/>`task_worker.py`<br/>`vlm_task_worker.py`<br/>`node_agent.py`<br/>`node_agent_hot_deploy.py`<br/>`node_agent_platform.py`<br/>`ai_worker.py`<br/>`handoff_worker.py`<br/>`enrichment_worker.py`<br/>`run_native_workers.py`<br/>`task_runner.py`<br/>`install_agent.sh` | 宿主机原生<br/>(Host Native) | - `task_executor.py`: 消费 Node Agent 领取的 `orchestrated_v2` 编排任务，调度本地插件与共享内存<br/>- `task_worker.py`: 本地单机开发工作器（维持心跳与流水线处理，支持 `--daemon`）<br/>- `vlm_task_worker.py`: ADR-031 单并发 VLM 延迟满足队列工作器（带内存水位门禁与本地按需解码）<br/>- `node_agent.py`: 算力节点主守护进程（向控制面汇报硬件拓扑、维持心跳、承接任务）<br/>- `node_agent_hot_deploy.py`: ADR-030 插件热部署执行器（蓝绿切换、候选实例启动与健康就绪检查）<br/>- `ai_worker.py`: 独立进程模型推理执行辅助<br/>- `handoff_worker.py`: 跨进程共享内存租约交接工作器 |
| **`tools/ops/`**<br/>(Admin & Ops) | `macos_resident.py`<br/>`setup_mcp.py`<br/>`prune_installations.py`<br/>`restart_plugins.py`<br/>`plugin_artifact.py`<br/>`plugin_release.py`<br/>`validate_plugin.py` | 宿主机原生 | - `macos_resident.py`: 管理 macOS LaunchAgent 守护常驻进程<br/>- `setup_mcp.py`: 配置、检查或卸载 SensoryPlex MCP 与 AI 技能<br/>- `prune_installations.py`: 物理清理历史废弃插件版本与临时缓存<br/>- `restart_plugins.py`: 热重启本地受管插件进程<br/>- `plugin_release.py`: 打包受控平台定向 bundle 并校验整包摘要<br/>- `plugin_artifact.py`: 生成与检查插件 SBOM 与 artifact 签名<br/>- `validate_plugin.py`: 校验插件元数据清单 `plugin.yaml` 规范 |
| **`tools/media/`**<br/>(Media & Timeline) | `probe_media.py`<br/>`timeline_handoff.py`<br/>`enrichment_media.py`<br/>`model_limits.py`<br/>`console_dev.py` | 宿主 / 容器 | - `probe_media.py`: 快速探测媒体容器与音视频编码元数据<br/>- `timeline_handoff.py`: 时间轴数据帧跨进程租约交接辅助<br/>- `model_limits.py`: 模型显存与并发档位限制校验<br/>- `console_dev.py`: 辅助前端控制台本地代理调试 |
| **`tools/verify/`**<br/>(Verification Suite) | `verify_*.py` (34 个)<br/>`smoke_gateway.py`<br/>`smoke_runtime.py`<br/>`test_integration.py` | 容器 / 宿主 | - 覆盖编排调度、插件热部署、事件管道、BGE向量检索、硬件加速对账及 9 大黄金路径业务场景的自动化验收套件 |

---

## 2. 透明兼容 Shim 架构机制

为了避免物理文件迁移破坏外部既有依赖（如 `Makefile`、`deploy/` 运维脚本、`docker-compose` 启动命令以及 `tests/` 中的符号导入），根目录 `tools/` 为每个脚本部署了轻量透明兼容 Shim：

### Python Shim 设计 (`sys.modules` 别名注入)
```python
"""Compatibility shim forwarding to tools.<subfolder>.<mod>."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.<subfolder>.<mod> as _target_module  # noqa: E402

# 1. 模块别名注入：使得 import tools.xxx / from tools import xxx 与直接 import 子模块完全一致，
# 保证 monkeypatch、私有方法导入以及对象类型识别 100% 透传
sys.modules[__name__] = _target_module

# 2. 命令行入口分发：直接运行 python tools/xxx.py 时透传 main() 或 __main__ 逻辑
if __name__ == "__main__":
    if hasattr(_target_module, "main"):
        sys.exit(_target_module.main())
    else:
        target_path = str(Path(__file__).parent / "<subfolder>" / "<mod>.py")
        runpy.run_path(target_path, run_name="__main__")
```

### Shell Shim 设计 (`install_agent.sh`)
```bash
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/workers/install_agent.sh" "$@"
```

---

## 3. 开发者调用指引

1. **直接调用新路径（推荐）**：
   - 编写新代码、新测试或新脚本时，推荐直接引用规范化的子目录，例如：
     ```bash
     python tools/codegen/generate_proto.py
     python tools/workers/task_worker.py --daemon
     python tools/verify/verify_golden_path.py
     ```
2. **通过原有路径调用（兼容支持）**：
   - 历史 `Makefile` 目标、Docker Compose 配置与第三方脚本依然可以直接调用：
     ```bash
     python tools/generate_proto.py
     python tools/task_worker.py
     ```
