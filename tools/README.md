# SensoryPlex 辅助工具与执行器分类指南 (`tools/`)

`tools/` 目录收录了 SensoryPlex 项目中用于**构建契约生成**、**宿主机原生任务执行器/守护进程**、**算力节点 Agent**、**运维管理**以及**全链路端到端验收脚本**的所有 Python 脚本与 Shell 工具。

为了解决历史积累导致的 `tools/` 目录扁平膨胀（68 个工具文件）、职责混杂问题，本指南建立了清晰的功能分类体系，并提供了未来渐进式目录拆分的演进路线图。

---

## 1. 工具全景分类矩阵

| 分类模块 | 脚本列表 | 执行环境 | 核心职责说明 |
| :--- | :--- | :--- | :--- |
| **构建契约与数据库**<br/>(Build & Codegen) | `generate_proto.py`<br/>`generate_console_types.py`<br/>`configure.py`<br/>`migrate.py` | 容器 / 宿主 | - `generate_proto.py`: 将 `proto/` 编译为 Python 与 TypeScript 契约<br/>- `generate_console_types.py`: 生成 Web 控制台前端类型定义<br/>- `configure.py`: 宿主机生成随机安全凭据写入 `.env`<br/>- `migrate.py`: 驱动不可变追加式数据库迁移 (Flyway 式) |
| **宿主原生守护与工作器**<br/>(Host Daemons & Workers) | `task_executor.py`<br/>`task_worker.py`<br/>`vlm_task_worker.py`<br/>`ai_worker.py`<br/>`handoff_worker.py`<br/>`enrichment_worker.py`<br/>`run_native_workers.py`<br/>`task_runner.py` | 宿主机原生<br/>(Host Native) | - `task_executor.py`: 消费 Node Agent 领取的 `orchestrated_v2` 编排任务，调度本地插件与共享内存<br/>- `task_worker.py`: 本地单机开发工作器（维持心跳与流水线处理，支持 `--daemon`）<br/>- `vlm_task_worker.py`: ADR-031 单并发 VLM 延迟满足队列工作器（带内存水位门禁与本地按需解码）<br/>- `ai_worker.py`: 独立进程模型推理执行辅助<br/>- `handoff_worker.py`: 跨进程共享内存租约交接工作器 |
| **节点 Agent 与插件部署**<br/>(Node Agent & Deploy) | `node_agent.py`<br/>`node_agent_hot_deploy.py`<br/>`node_agent_platform.py`<br/>`install_agent.sh`<br/>`plugin_artifact.py`<br/>`plugin_release.py`<br/>`validate_plugin.py` | 宿主机原生 / 容器 | - `node_agent.py`: 算力节点主守护进程（向控制面汇报硬件拓扑、维持心跳、承接任务）<br/>- `node_agent_hot_deploy.py`: ADR-030 插件热部署执行器（蓝绿切换、候选实例启动与健康就绪检查）<br/>- `plugin_release.py`: 打包受控平台定向 bundle 并校验整包摘要<br/>- `plugin_artifact.py`: 生成与检查插件 SBOM 与 artifact 签名<br/>- `validate_plugin.py`: 校验插件元数据清单 `plugin.yaml` 规范 |
| **平台运维与宿主适配**<br/>(Admin & Ops) | `macos_resident.py`<br/>`setup_mcp.py`<br/>`prune_installations.py`<br/>`restart_plugins.py` | 宿主机原生 | - `macos_resident.py`: 管理 macOS LaunchAgent 守护常驻进程<br/>- `setup_mcp.py`: 配置、检查或卸载 SensoryPlex MCP 与 AI 技能<br/>- `prune_installations.py`: 物理清理历史废弃插件版本与临时缓存<br/>- `restart_plugins.py`: 热重启本地受管插件进程 |
| **底层媒体与时序工具**<br/>(Media & Timeline) | `probe_media.py`<br/>`timeline_handoff.py`<br/>`enrichment_media.py`<br/>`model_limits.py`<br/>`console_dev.py` | 宿主 / 容器 | - `probe_media.py`: 快速探测媒体容器与音视频编码元数据<br/>- `timeline_handoff.py`: 时间轴数据帧跨进程租约交接辅助<br/>- `model_limits.py`: 模型显存与并发档位限制校验<br/>- `console_dev.py`: 辅助前端控制台本地代理调试 |

---

## 2. 端到端验收测试套件 (`verify_*.py`)

目录下包含 37 个全链路端到端自动化验收脚本，覆盖 ADR 设计决策与核心工程约束：

### 编排与多节点 (Orchestration & Cluster)
- `verify_orchestration.py`: ADR-029 P1 单机真实编排闭环（DAG状态机、幂等、级联解锁、取消阻断、有界重试、崩溃恢复）
- `verify_orchestration_multinode.py`: ADR-029 P2 多节点集群编排与故障转移（Failover）验收
- `verify_orchestration_p3.py`: ADR-029 P3 场景化产品包与事件驱动任务调度唤醒
- `verify_node_topology.py`: ADR-026 局域网节点注册、心跳拓扑与同机共享内存感知
- `verify_golden_path.py`: 全链路业务黄金路径回归（GP-01：上传 -> 编排 -> 推理 -> 融合 -> 向量 -> 检索 -> 回放）

### 插件热部署与生命周期 (Plugin Hot Deploy)
- `verify_plugin_hot_deploy.py`: ADR-030 插件热部署双槽位蓝绿切换端到端验收（覆盖 API 控制面与原生执行器）
- `verify_plugin_platform.py`: 插件通用接入能力契约测试
- `verify_plugin_faults.py`: 插件异常崩溃、超时与隔离机制测试
- `verify_plugin_rejections.py`: 插件非法输入、签名不匹配与版本冲突拒绝语义测试
- `verify_plugin_inputs.py` / `verify_plugin_performance.py`: 插件输入模式与推理性能基准测试

### 事件总线与向量索引 (Event Pipeline & Vector Index)
- `verify_outbox_relay.py`: ADR-024 事务性 Outbox 到 NATS JetStream 发布链路验收
- `verify_vlm_workqueue.py`: ADR-031 JetStream WorkQueue 慢路径任务队列拉取与 Ack 确认机制
- `verify_index.py`: ADR-020 BGE 文本向量落库与 Milvus Lite 读写闭环
- `verify_index_consume.py`: ADR-025 事件消费循环与向量化落库写入闭环
- `verify_event_pipeline.py`: ADR-027 常驻 relay/index 与 API 语义检索端到端闭环
- `verify_semantic_search.py`: 常驻向量检索面与网关语义查询接口验收

### 媒体底层与端侧模型 (Media & Multimodal Models)
- `verify_accelerator_report.py`: ADR-022 宿主硬件加速器（Metal/CUDA/CoreML）探测对账
- `verify_capability.py`: ADR-009 媒体格式准入与显式拒绝错误码验证
- `verify_backpressure.py`: 解码与消费队列背压降级与可观测性验证
- `verify_live.py`: SRT 实时流接入、断流重连与交接验证
- `verify_replay.py`: 真实授权视频解码、自适应抽帧与 LeaseBuffer 写入验证
- `verify_handoff.py`: 跨进程共享内存交接与租约安全释放验证
- `verify_timeline_handoff.py` / `verify_timeline_semantic.py`: ADR-028 Timeline 时间轴连续切片事实入库与检索
- `verify_ocr.py`: ADR-016 RapidOCR 插件端侧推理与文本像素定位验证
- `verify_asr.py`: ADR-014 MLX-Whisper 插件音频转写与时间戳对齐验证
- `verify_embed.py`: ADR-017 BGE ONNX 文本嵌入与 L2 归一化向量生成验证
- `verify_model.py`: ADR-012 Moondream VLM 插件推理与 observation 输出验证
- `verify_model_parallelism.py`: ADR-021 模型 Worker 并发档位与显存防护验证
- `verify_multimodal_pipeline.py`: 多模态处理方案控制面契约校验
- `verify_multimodal_execution.py`: 调度真实多模态插件执行闭环并输出覆盖层报告
- `verify_console.py`: 控制台 Web API 端点与素材交互自动化验证
- `smoke_gateway.py` / `smoke_runtime.py`: 兼容网关与运行时轻量冒烟检查

---

## 3. 目录结构梳理与未来渐进式重构路线图

当前 `tools/` 目录文件较多（68 个），但由于以下关键调用链强依赖历史路径，**不宜直接粗暴移动文件**：
1. `Makefile` 中定义了 40+ 个核心 Target，直接调用 `tools/<script>.py`；
2. `deploy/` 下的运维脚本（如 `deploy/up-vlm-worker.sh`）依赖 `tools/vlm_task_worker.py`；
3. 本地开发与测试环境（包括宿主 LaunchAgent plist）硬编码了 `tools/` 路径。

### 推荐的渐进式重构方案（3 步走）：

1. **第一阶段（已实施）**：
   - 保留现有文件名与调用路径，确保 100% 向后兼容；
   - 补充 `tools/README.md` 与分类矩阵文档，规范各工具职责与执行上下文（容器 vs 宿主）。
2. **第二阶段（目录分组 + 兼容 Shim）**：
   - 规划目标子目录结构：
     - `tools/codegen/`: `generate_proto.py`, `generate_console_types.py`, `configure.py`, `migrate.py`
     - `tools/workers/`: `task_executor.py`, `task_worker.py`, `vlm_task_worker.py`, `node_agent*.py`
     - `tools/verify/`: `verify_*.py`, `smoke_*.py`
     - `tools/ops/`: `setup_mcp.py`, `macos_resident.py`, `prune_installations.py`
   - 在 `tools/` 原路径下保留轻量转发 Shim（通过 `runpy.run_path` 或软链接），平滑过渡；
   - 分批次更新 `Makefile` 与 `deploy/` 脚本中的调用目标。
3. **第三阶段（归档与收敛）**：
   - 移除原 `tools/` 根目录下的转发 Shim，完全收敛到子目录；
   - 将常用 Worker 封装为具备 CLI 入口点的 Python 包或 console_scripts。
