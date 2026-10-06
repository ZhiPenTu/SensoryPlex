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
└── tools/              # 辅助工具与执行器分类目录 (含透明兼容 Shim)
    ├── codegen/        # 契约编译与数据库迁移 (4 文件)
    ├── workers/        # 宿主守护与任务执行器 (12 文件)
    ├── ops/            # 插件部署、运维与常驻管理 (7 文件)
    ├── media/          # 媒体探测与时序辅助 (5 文件)
    ├── verify/         # 端到端 ADR 验收测试套件 (37 文件)
    └── <shims>.py      # 根目录保留 65 个透明兼容 Shim，保障 Makefile/CI/导入无感
```

---

## 2. 治理成果与实施设计 (Implementation & Hygiene)

### 2.1 根目录卫生治理
- [x] 物理清除根目录下误提交的 `:memory:.ses` 临时会话文件；
- [x] 在 `.gitignore` 中完善 `:memory:.ses` 与 `*.ses` 规则。

### 2.2 `tools/` 物理子目录化归整
- [x] 将原 65 个工具脚本全量归档至 5 个职责单一的子目录（`codegen/`、`workers/`、`ops/`、`media/`、`verify/`）；
- [x] 调整各子目录脚本中的相对根路径计算（`.parents[1]` -> `.parents[2]`），支持子目录下直接执行；
- [x] 在原 `tools/` 路径部署透明兼容 Shim：
  - 基于 Python `sys.modules[__name__] = _target_module` 别名技术，实现外部符号静态导入、私有方法访问与 monkeypatch 的 100% 状态透传；
  - 命令行入口支持 `main()` 转发与 `runpy.run_path` 兼容回退。

---

## 3. 验证情况 (Verification)

- **静态检查**：容器内 `make lint-ruff` 100% 通过（405 files clean，零违规）；
- **契约测试**：容器内 `make test-contracts` 100% 通过（492 项契约测试全部 passed）；
- **文档构建**：容器内 `make docs-check` 100% 通过。
