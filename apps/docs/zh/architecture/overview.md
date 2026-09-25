# 系统总览

系统的分层目标是：**契约共享，运行时各自独立**。Rust 管运行时，Python 管模型适配，两者只通过生成的
Protobuf 代码相遇。

## 分层

```text
                    ┌───────────────────────────────────────────────┐
   Web 控制台        │  apps/console（React + TS + Vite，nginx 5173）│
                    └───────────────────────┬───────────────────────┘
                                            │ /v1 /auth /admin（同源）
                    ┌───────────────────────▼───────────────────────┐
   控制面            │  services/api (8091)      services/gateway    │
                    │  业务/管理/认证            兼容入口              │
                    └───────┬───────────────────────────────┬───────┘
                            │ SQL                           │ gRPC
                    ┌───────▼────────┐            ┌─────────▼─────────┐
   状态与事件        │  PostgreSQL    │            │  index 检索面      │
                    │  25432         │            │  (index:50077)     │
                    │  outbox ──────►│  NATS      └─────────▲─────────┘
                    └────────────────┘  JetStream           │
                            ▲           24222                │
                            │                              │
                    ┌───────┴──────────────────────────────┴────────┐
   运行时层          │  Rust crates: runtime / media / timeline /    │
                    │  storage / execution / sdk                    │
                    │  解码 → 有界 arena → BufferDescriptor          │
                    └───────────────────────┬───────────────────────┘
                                            │ lease（共享内存，host-local）
                    ┌───────────────────────▼───────────────────────┐
   端侧插件          │  vlm-moondream  asr-whisper-mlx  ocr-rapidocr │
                    │  embed-bge-onnx（gRPC 处理器插件）              │
                    └───────────────────────────────────────────────┘
```

## 进程清单

| 进程 | 端口（回环） | 语言 | 职责 |
| --- | --- | --- | --- |
| `console` | `5173` | nginx + React | 静态控制台托管，并把 `/v1`、`/auth`、`/admin` 反代到 API |
| `docs` | `5174` | nginx + VitePress | 静态框架使用文档站（不反代、不持凭据） |
| `api` | `8091` | Python / FastAPI | 业务、管理与认证端点；dev 镜像另带 ruff/pytest/proto 工具链 |
| `gateway` | `8090` | Python / FastAPI | 旧查询入口与启动兼容层 |
| `migrate` | — | Python | 一次性执行 `tools/migrate.py`，完成后退出 |
| `relay` | — | Python | 事务性 outbox → NATS JetStream（`events` profile） |
| `index` | `50077`（仅 compose 网络内） | Python | 向量写入、JetStream 常驻消费、gRPC 检索面 |
| `postgres` | `25432` | PostgreSQL 16 | 元数据、事实、outbox |
| `nats` | `24222` | NATS 2.11 | JetStream 事件总线（监控端口 `28222`） |

## 一段媒体如何变成一条素材

1. **接入** —— 文件或 SRT 流由 `MediaSourceRef`（secret 名称 + 摘要）引用。
2. **解码** —— GStreamer 解码进**有界 arena**；流水线在有控制的位置刻意“泄漏”出去，而不是全部缓冲。
3. **描述** —— arena 产出 `BufferDescriptor`：区间、内存类型与摘要。
4. **交接** —— descriptor 被签发成 **lease**，由独立进程读取、校验并释放。字节绝不以 protobuf 载荷搬运。
5. **感知** —— 插件在呈现时间轴上产出观测（文字块、转写、描述、向量）。
6. **融合** —— Timeline 层校验观测与血缘，然后把事实与 outbox 事件放在同一事务里写入。
7. **分发** —— relay 确认发布到 JetStream 之后才写 `published_at`。
8. **索引** —— 常驻索引进程消费事件，用 BGE 插件编码文本，写入向量库并提供检索。
9. **检索** —— gateway/API 把语义查询转发给检索面，命中后用 PostgreSQL 水合事实再返回。

## 边界规则

- **插件绝不 import services 内部模块。** 插件只依赖 SDK，不持有数据库凭据，读媒体一律经 lease reader。
- **Rust 管运行时，Python 管模型适配。** 加速留在持有它的那台宿主上。
- **任何原始数据都不跨控制边界。** 控制消息、事件与日志里没有帧、PCM、tensor 或密钥——只有受控引用。
- **所有队列与并发都有上限**，并有可观察的 timeout、取消、重试与失败语义。

## 仓库结构

| 路径 | 内容 |
| --- | --- |
| `proto/` | `common`、`material`、`media`、`runtime`、`gateway`、`index`、`node`、`orchestration` 的版本化契约 |
| `crates/` | Rust workspace：`runtime`、`media`、`timeline`、`storage`、`execution`、`sdk` |
| `plugins/python/common/` | `edge_material_sdk` 与生成的 Python 消息 |
| `plugins/python/processors/` | 四个端侧模型插件 |
| `apps/console/` | React + TypeScript + Vite 控制台 |
| `apps/docs/` | 本文档站 |
| `services/` | `api`、`gateway`、`outbox-relay`、`index-worker` |
| `db/migrations/` | 只追加、显式执行的 PostgreSQL 迁移 |
| `config/pipelines/` | 文件与 SRT 实时接入的 pipeline 配置 |
| `deploy/` | compose 文件、nginx 配置、起停脚本、macOS launchd 模板 |
| `tests/` | 契约测试、真实 PostgreSQL 集成测试、媒体样本说明 |
| `tools/` | 配置、代码生成、迁移、验收与真实媒体探测工具 |

## 继续阅读

- [契约与代码生成](/zh/architecture/contracts)
- [服务与节点](/zh/architecture/services)
- [可编排执行核心](/zh/architecture/orchestration)
- [事件链路与检索](/zh/architecture/event-pipeline)
