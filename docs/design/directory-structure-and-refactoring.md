# SensoryPlex 代码库目录结构全景与整理方案

## 1. 现状剖析 (Current Architecture Overview)

SensoryPlex 采用 Polyglot Monorepo（多语言多组件单体仓库）架构，整合了 Rust 媒体运行时、Python 控制面与插件、Protobuf 契约中心以及 Web 前端控制台。

### 1.1 顶层目录结构

```text
SensoryPlex/
├── apps/               # 前端应用与静态文档站
│   ├── console/        # React 18 + Vite + Ant Design Web 控制台
│   └── docs/           # VitePress 开源多语言文档站
├── crates/             # Rust 核心库 (Workspace)
│   ├── execution/      # 原生执行循环与 DAG 调度支持
│   ├── media/          # 媒体格式探测与编解码能力矩阵
│   ├── runtime/        # 核心运行时 (GStreamer 管道、共享内存租约)
│   ├── sdk/            # Rust 原生插件 SDK
│   ├── storage/        # 通用对象存储抽象 (S3/本地文件)
│   └── timeline/       # 1 秒连续切片网格与多模态时间轴事实融合
├── proto/              # 通用 Protobuf/gRPC 契约（单一事实权威）
├── services/           # Python 后端控制面与常驻服务
│   ├── api/            # 核心控制面 (FastAPI：编排、节点拓扑、插件部署、素材管理)
│   ├── gateway/        # 兼容入口层
│   ├── index-worker/   # 向量检索服务与 JetStream 消费 Worker (Milvus/PgVector)
│   ├── mcp-server/     # SensoryPlex 官方 MCP 协议服务 (独立 Python 虚拟环境)
│   └── outbox-relay/   # PostgreSQL 事务性 Outbox → NATS JetStream 中继服务
├── plugins/            # 多模态模型处理器插件 (Python)
│   └── python/
│       ├── common/     # 插件通用 SDK (edge-material-sdk)
│       └── processors/ # 模型插件实现 (OCR, ASR, VLM, Embeddings)
├── deploy/             # Docker Compose 编排与环境起停脚本
├── db/                 # 数据库迁移脚本与版本控制
├── config/             # 预置流水线 DAG 配置
├── skills/             # AI 智能体技能定义 (Claude/Codex Skill)
├── tests/              # 自动化测试套件 (契约测试 tests/contracts + 集成测试 tests/integration)
└── tools/              # 宿主原生 Worker、构建生成、运维工具与验收测试脚本 (68个文件)
```

---

## 2. 存在的问题与代码坏味道 (Issues & Smells)

1. **临时与会话文件误提交**：
   - 根目录下曾存在 `:memory:.ses` 临时会话文件，需清理并完善 `.gitignore` 规则。
2. **`tools/` 目录扁平膨胀**：
   - 包含 68 个工具文件，涵盖了从一次性测试、代码生成、运维工具到生产级常驻守护进程（如 `task_executor.py`、`vlm_task_worker.py`）。
   - 新贡献者难以区分脚本的使用场景与执行边界。
3. **根目录文档归纳**：
   - 根目录下的历史规范文档（如 `开放式插件开发文档.md`、`技术选型ADR与V1实施蓝图.md`）承载了大量历史 ADR 与规范引用，未来应平滑纳入 `docs/` 或保持明确索引，避免根目录混乱。
4. **服务与插件的依赖隔离**：
   - 绝大多数服务位于同一个 `uv` 工作区，但 `mcp-server` 是独立工作区，需保持边界清晰，防止产生跨模块非法依赖。

---

## 3. 整理与重构方案 (Refactoring Roadmap)

### 阶段一：即时治理与清晰索引 (Phase 1: Immediate Hygiene)
- [x] 删除根目录历史提交的 `:memory:.ses` 文件；
- [x] 在 `.gitignore` 中加入 `:memory:.ses` 与 `*.ses` 规则；
- [x] 新增 `tools/README.md`，对 `tools/` 下 68 个工具建立职责分类与执行上下文矩阵（构建生成、宿主工作器、节点Agent、运维脚本、37项验证套件）；
- [x] 确立代码评审体系与功能模块规范。

### 阶段二：`tools/` 子目录化与 Shim 兼容 (Phase 2: Gradual Subdirectory Restructuring)
- 将脚本渐进拆分到子目录中：
  - `tools/codegen/`: `generate_proto.py`, `generate_console_types.py`, `migrate.py`, `configure.py`
  - `tools/workers/`: `task_executor.py`, `task_worker.py`, `vlm_task_worker.py`, `node_agent*.py`
  - `tools/verify/`: `verify_*.py`, `smoke_*.py`
  - `tools/ops/`: `setup_mcp.py`, `macos_resident.py`, `prune_installations.py`
- 为避免破坏现有 `Makefile` 和自动化脚本，在原路径保留轻量级转发入口（Shim），实现无缝平滑迁移。

### 阶段三：长远模块化收敛 (Phase 3: Module Packaging)
- 将 `node_agent.py` 与 `task_executor.py` 等核心生产级 Worker 进一步打包为结构化的 Python Package（如 `services/node-agent`）；
- 将 `verify_*.py` 逐步对接到统一的测试运行器（如 pytest 插件或定制 test runner）。
