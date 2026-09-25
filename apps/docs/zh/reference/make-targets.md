# Make 目标

Makefile 是项目的入口，它把一条架构规则写进了命令里：**控制面验证在容器内执行**，而任何需要宿主加速器、
宿主媒体文件或 `cargo` 的部分在宿主执行。

## 目标在哪执行

| 位置 | 含义 |
| --- | --- |
| **api 容器** | `docker compose exec -T api ...`——Python 工具链、pytest、proto 生成、编排与事件类验收 |
| **gateway / console 容器** | 网关冒烟；控制台构建与 dev server |
| **宿主** | `cargo`、`launchd`、HF 权重、CoreML/MLX、MediaMTX、授权媒体样本 |
| **混合** | 部分步骤在容器、部分在宿主（例如先构建再验证） |

有一个刻意的例外：`make configure` 必须在宿主执行，因为容器内的仓库 bind mount 无法把新凭据写回宿主 `.env`。

## 容器

| 目标 | 执行位置 | 用途 |
| --- | --- | --- |
| `make up` / `make down` | 宿主 shell | 起停 compose 栈 |
| `make infra` | 宿主 shell | 只起 `postgres` 与 `nats` |
| `make gateway` | 宿主 shell | 只起兼容网关 |
| `make migrate` | api 容器 | 执行只追加迁移 |

## 代码生成与质量

| 目标 | 执行位置 | 用途 |
| --- | --- | --- |
| `make proto` | api 容器 | 从 `proto/` 重新生成 Python 绑定与控制台类型 |
| `make lint-ruff` | api 容器 | `ruff check` + `ruff format --check` |
| `make format` | 混合 | Rust `cargo fmt` 加 Python `ruff format` |
| `make test-contracts` | api 容器 | 契约测试，不需要外部服务 |
| `make test-py` / `make test-integration` | api 容器 | 契约 + 对真实 PostgreSQL 与 NATS 的集成测试 |
| `make check` | 混合 | lint + Python 测试 + Rust fmt/clippy/test |

## 媒体与运行时

| 目标 | 执行位置 | 用途 |
| --- | --- | --- |
| `make runtime` | 宿主 | 运行 Rust runtime 服务 |
| `make media-test` | 宿主 | 解码路径单元测试（需要 GStreamer 开发文件） |
| `make media-replay MEDIA=...` | 混合 | 单样本的真实解码、锚点与解码报告 |
| `make media-check MEDIA=...` | 宿主 | 锚点/媒体验收 |
| `make pipeline-check` | 宿主 | pipeline 配置检查 |
| `make live-check` | 混合 | SRT 接入场景；先跑 `make stream-up` |
| `make handoff-check MEDIA=...` | 混合 | 跨进程共享内存交接 |
| `make backpressure-check` | 混合 | 背压指标 |
| `make capability-check` | 混合 | 能力上报与不可用原因 |
| `make accelerator-check` | 宿主 | 加速器探测的四路对账 |

## 端侧模型插件

| 目标 | 执行位置 | 用途 |
| --- | --- | --- |
| `make model-check MEDIA=...` | 混合 | 经本机 ollama 的 VLM |
| `make asr-check MEDIA=...` | 混合 | 经 MLX Whisper 的 ASR |
| `make ocr-check MEDIA=...` | 混合 | 随包 PP-OCR ONNX 的 OCR（无文字样本用 `EXPECT=empty`，CoreML 用 `PROVIDER=coreml`） |
| `make embed-check MEDIA=...` | 混合 | BGE 文本向量 |
| `make parallelism-check MEDIA="a b" INPUTS=4` | 宿主 | 模型 worker 并发准入与重试账目 |

## 事件与检索

| 目标 | 执行位置 | 用途 |
| --- | --- | --- |
| `make outbox-check` / `make outbox-run` | api 容器 | outbox → JetStream 发布这一跳 |
| `make index-check EMBEDDINGS=...` | 宿主 | Milvus Lite 上的向量写后回读闭环 |
| `make consume-check` | 宿主 | 单进程 JetStream → 向量消费 |
| `make event-pipeline-check` | api 容器 | compose 内两个常驻服务的端到端 |
| `make semantic-check` | 宿主 | 网关语义检索场景 |
| `make events-up` / `events-down` / `events-logs` | 宿主 shell | compose 的 `events` profile |

## 拓扑与编排

| 目标 | 执行位置 | 用途 |
| --- | --- | --- |
| `make node-check` | api 容器 | 节点拓扑验收 |
| `make orchestration-check` | 宿主 | 编译内核 |
| `make orchestration-p1-check` | api 容器 | 持久编排场景 |
| `make orchestration-p2-check` | api 容器 | 分布式多节点场景 |

## 产品面与验收

| 目标 | 执行位置 | 用途 |
| --- | --- | --- |
| `make console-build` / `console-dev` | console 容器 | 重建或运行控制台 |
| `make console-check MEDIA=...` | api 容器 | 用真实样本验证控制台行为 |
| `make golden-path-check` | 混合 | 9 个场景的业务闭环 |
| `make docs-check` | docs 容器 | 构建本文档站并校验内部链接 |

## 文档站

| 目标 | 执行位置 | 用途 |
| --- | --- | --- |
| `make docs-install` | docs 容器 | 一次性 `npm ci`；docs 目标里唯一需要 npm registry 的一个 |
| `make docs-build` | docs 容器 | 从 `apps/docs` 构建静态文档站，产物写到 `apps/docs/.vitepress/dist` |
| `make docs-check` | docs 容器 | 构建 + 语言树对等 + 内部链接/资源校验 |
| `make docs-dev` | docs 容器 | 带热更新的 VitePress dev server，监听 `DOCS_DEV_PORT` |
| `make docs-serve` | docs 容器 | 预览已构建产物（与 `docs-dev` 共用同一端口） |

文档站是纯静态产物，因此 `make docs-build` 也是“产出可托管到任何地方的产物”的方式——见
[部署](/zh/operations/deployment)。安装与构建被拆成两个目标：registry 抖动不该让一次文档改动变成
构建失败；提交前要跑的门禁是 `make docs-check`。

## 仅宿主的目标组

| 目标 | 用途 |
| --- | --- |
| `make configure` | 把随机凭据写入 `.env` |
| `make resident-probe` / `resident-install` / `resident-status` / `resident-uninstall` | macOS `launchd` 常驻形态 |
| `make task-worker` / `task-worker-daemon` / `task-worker-status` / `task-worker-stop` | 宿主任务工作器（前台或后台常驻） |
| `make stream-up` / `stream-down` / `stream-status` / `stream-logs` | 独立的 MediaMTX compose |
| `make demo-seed` / `demo-reset` | 演示账号生命周期 |
| `make setup` | 配置、生成 proto、构建 Rust workspace |
