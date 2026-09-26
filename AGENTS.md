# 务必按照高性能框架底层级项目规范进行开发

# 开发期验证环境规范：底座强制容器，子节点插件允许宿主原生

根据工程规范与架构边界，开发期验证严格区分**核心底座（Base Platform / Control Plane）**与**子节点插件（Sub-node Plugins / Workers）**两层执行环境：

## 1. 核心底座开发验证：一律强制走容器

所有与核心底座相关的构建、代码检查、数据库迁移与集成验证，都必须通过 `docker compose exec -T <service> ...` 在容器内执行；宿主机不再安装、也不再直接调用底座相关的 `uv` / `python` / `node` / `npm` 工具链：
- **覆盖范围**：
  - 代码规范与静态检查：`make lint-ruff`、`make format`（Python 与 Web 前端部分）；
  - 契约与 Proto 生成：`make proto`（`tools/generate_proto.py` + `tools/generate_console_types.py`）；
  - 数据库迁移：`make migrate`（`tools/migrate.py`，依赖严格的版本校验和事务锁）；
  - 控制面服务：`api`（8091，FastAPI 业务/管理/认证）、`gateway`（8090，兼容入口）、`console`（5173，Web 控制台静态构建与反代）、`docs`（5174，开源使用文档站，纯静态 VitePress 构建与托管）；
  - 消息与索引基础设施：`postgres`（25432）、`nats`（24222）、`relay`（outbox 投递）、`index`（向量检索服务）；
  - 单元与集成测试：`make test-py`、`make test-integration`、`make node-check`、`make orchestration-p1-check`、`make golden-path-check` 等底座编排与 API 验证。
- **目的**：确保底座运行环境纯洁、隔离、不依赖宿主机局部 Python/Node 环境，消除“在本地能跑但在生产容器无法启动”的依赖与配置漂移。
- **底座仅有的宿主例外**：
  - Rust/Cargo 工具链：现有 api / gateway / console / postgres / nats 镜像均不携带 `rustc` / `cargo`，宿主机 `cargo` 暂时承担 Rust 编译与测试（`cargo check`、`cargo test`、`make orchestration-check`）；待批准专用 Rust 工具链容器后再统一收回。
  - `make configure`：容器 bind mount 将仓库根以只读视图挂入容器，随机安全凭据写入宿主 `.env` 必须在宿主机执行。

## 2. 子节点插件：可以不需要走容器（允许宿主原生运行）

运行在各算力节点上的多模态模型处理器与 Worker（如 `ocr-rapidocr`、`vlm-moondream`、`asr-whisper-mlx`、`embed-bge-onnx` 及各类第三方算法插件），**可以不需要走容器**，直接在宿主机（Host）原生环境（Python / 虚拟环境）下运行与调试：
- **硬件加速器访问**：端侧模型强依赖宿主专属物理硬件与加速后端（例如 Apple Silicon 的 Metal / MLX / CoreML，以及特定 GPU / NPU 驱动与统一内存），开发期轻量 Linux 容器通常无法直接挂载或编译此类原生驱动（如 Apple Silicon 无法在 Linux 容器中编译 `mlx-metal`）；
- **进程与控制面隔离**：根据 ADR-001/010/012/026，节点插件设计为跨进程独立的受控 Worker，生命周期由 Node Agent / 外部进程直接拉起；插件通过标准 gRPC（`runtime.v1.ProcessorPluginService`）或跨进程共享内存租约（`LeaseBufferReader`）领料，内部不得依赖 services 内部模块，也不持有数据库凭据；
- **开发调试灵活**：插件开发者在本地开发机或局域网独立节点机（如 Mac mini、边缘设备）上进行算法调优、模型加载与推理验证（如 `make model-check`、`make asr-check`、`make ocr-check`、`make embed-check`）时，允许直接使用宿主虚拟环境（`uv run` / 本机 Python）运行，无需强行打包进容器；
- **协同方式**：宿主原生运行的插件通过宿主机暴露的网络端口（`127.0.0.1:8091`、`24222`、`25432` 等）与容器内的底座互通；底座调度器根据节点注册的端点通过 gRPC 派发任务。

## 3. 容器服务与绑定说明

- 容器服务与绑定：
  - `api`（8091）：执行 ruff/pytest/proto/integration/plugin-artifact/smoke_gateway 类 Python 验证；PYTHONPATH=/workspace + uv sync --group dev 装好的 ruff/pytest + vlm-moondream + numpy/scipy；mlx-whisper 以 `--no-deps` 注入，避免在 Linux 容器里编译 Apple Silicon only 的 mlx-metal。
  - `gateway`（8090）：执行依赖 `sensoryplex_gateway` 的 smoke / 测试；同 bind mount。
  - `console`（5173）：nginx 静态托管 + 反代 /v1|/auth|/admin → api:8091；保留 node + npm 让 `make console-build` / `make console-dev` 在容器内执行；apk add libstdc++ 提供 node 所需的 C++ ABI。
  - `postgres` / `nats`：基础设施，验证脚本通过 service name 连接。
- 授权样本通过 `MEDIA_DIR`（默认 `~/Movies`）以只读 bind 挂到 `/host-media`，Makefile 把用户传入的 `MEDIA=...` 重写为 `/host-media/$(notdir $(MEDIA))`，容器内脚本用绝对路径读取。

# SensoryPlex 工程约定

- 开发前阅读根目录两份需求文档，以及 docs/implementation-status.md。
- proto/ 是跨进程、跨语言唯一契约源；修改后运行 make proto，禁止手改生成代码。
- plugins 不得依赖 services 内部模块。Rust 管运行时，Python 管模型适配。
- 时间轴固定为同一 stream 的 [start_ms, end_ms) 毫秒偏移。
- 缺失模型置信度必须显式表示未知；禁止合成业务成功数据或静默 fallback。
- 数据库迁移仅追加，通过 tools/migrate.py 显式执行；历史 revision 不可覆盖。
- 不把原始帧、音频、tensor 或密钥放入控制消息/日志；只传受控引用。
- 所有队列与并发必须有上限，timeout、取消、重试与失败需要可观察语义。
- 修改后运行适用的 make check / make integration；媒体 E2E 必须用真实授权样本。
- 不用健康检查成功、未执行或跳过的测试宣称完整 Golden Path 已完成。
- 手写代码（Rust 的 `///`、`//!`、`//` 与 Python 的 docstring、`#`）的注释默认中文；
  proto 与生成代码保持英文，避免破坏跨语言契约与下游插件读取。
- 注释中英文边界：API、协议、容器、库与 ADR 编号（GStreamer、ffprobe、Matroska、Opus、
  segment event、unified_memory、gRPC、protobuf、ADR-003 等）保留英文原名；只翻译叙述性中文。

# 本地容器栈（deploy/）

容器编排文件：`deploy/compose/docker-compose.poc.yml`。
本机起停统一通过 `deploy/*.sh` 脚本；不直接 `docker compose up`，避免漏填 `--env-file .env`
或传错 `-f` 路径。所有脚本都从仓库根目录执行，并以仓库根作为 `docker compose` 的工作目录。

## 服务清单与本机端口

| 服务       | 端口（127.0.0.1）  | 镜像/构建                  | 用途 |
| ---------- | ------------------ | -------------------------- | ---- |
| console    | `CONSOLE_PORT=5173` | `apps/console/Dockerfile`  | 前端 web 容器（Nginx 静态托管 + 反代 `/v1 /auth /admin` 到 `api:8091`；容器内保留 node/npm 提供 `console-build` / `console-dev`） |
| docs       | `DOCS_PORT=5174`（dev/preview 用 `DOCS_DEV_PORT=5175`） | `apps/docs/Dockerfile` | 开源使用文档站（纯静态 VitePress：不反代、不持凭据、无 `depends_on`，可 `./deploy/up.sh docs` 单独起；容器内保留 node/npm 提供 `docs-install` / `docs-build` / `docs-check` / `docs-dev` / `docs-serve`） |
| api        | `API_PORT=8091`    | `services/api/Dockerfile`  | 业务/管理/认证 FastAPI；dev 镜像含 ruff/pytest/proto 工具链 + gateway/vlm-moondream wheels |
| gateway    | `GATEWAY_PORT=8090` | `services/gateway/Dockerfile` | 旧查询入口，迁移兼容层；smoke 验证在容器内执行 |
| postgres   | `POSTGRES_PORT=25432` | `postgres:16-alpine`      | 元数据存储；integration 测试 schema `sensoryplex_test` 在 compose 启动后手工创建一次 |
| nats       | `NATS_PORT=24222`  | `nats:2.11.3-alpine`       | 事件总线；监控端口 `127.0.0.1:28222` |
| migrate    | —                  | 复用 `services/gateway/Dockerfile` | 一次性执行 `tools/migrate.py`，完成后退出 |

端口值由根目录 `.env` 控制；不存在时使用括号内的默认值。

## bind mount

| source | target | 说明 |
| ------ | ------ | ---- |
| `${REPO_ROOT}` | `/workspace`（api/gateway/console/docs） | 把仓库根目录 bind 进容器，让 `make check` / `make test` / `make integration` 等开发验证在容器内看到本机源码；`docs` 服务借此把 `apps/docs/.vitepress/dist` 写到主机可见的位置。 |
| `${MEDIA_DIR:-/tmp}` | `/host-media`（api） | 把授权样本目录以只读方式挂入容器；Makefile 把 `MEDIA=...` 重写为 `/host-media/$(notdir $(MEDIA))`。 |

`REPO_ROOT` 默认 `../../`，与 compose 同目录的相对路径对应仓库根；`.env` 里已写
`REPO_ROOT=/Users/tuzhipeng/Documents/SensoryPlex`，可按需覆盖。`MEDIA_DIR` 默认
`/tmp`，可写为 `~/Movies` 或其它授权样本所在目录。

## 前端 web 容器（console）

- 多阶段构建：`node:20-alpine` 跑 `npm ci && npm run build`，再把 `apps/console/dist`
  拷进 `nginx:1.27-alpine`，同时保留 `node` 与 `npm` binary（`apk add libstdc++` 提供
  node 所需的 C++ ABI），让 `make console-build` / `make console-dev` 在容器内执行。
- nginx 单独承担 SPA 静态托管，并把 `/v1`、`/auth`、`/admin` 反代到 `api:8091`；
  业务路由在浏览器侧是同源（`http://127.0.0.1:5173/v1/...`），不出现跨域。
- `/static/*` 资产使用 30 天 `immutable` 缓存；`/v1|/auth|/admin` 反代不缓存。
- SPA history fallback：所有未匹配的非 `/v1|/auth|/admin` 请求回退到 `index.html`。
- 健康检查：`wget -q -O- http://127.0.0.1:5173/`。

## 文档站容器（docs）

- 多阶段构建：`node:20-alpine` 跑 `npm ci && npm run build`（VitePress），再把
  `apps/docs/.vitepress/dist` 拷进 `nginx:1.27-alpine`，同样保留 `node`/`npm`，让
  `make docs-install` / `docs-build` / `docs-check` / `docs-dev` / `docs-serve` 在容器内执行。
- 纯静态：**不**反代任何后端、不持有凭据、没有 `depends_on`；5174 托管构建产物，5175 只在开发期
  给 VitePress dev/preview 用（两者互斥）。
- cleanUrls：`/guide/quickstart` 与 `/guide/quickstart/` 都命中 `guide/quickstart.html`；未命中一律
  404（交给站点自带 `404.html`），不用 200 兜底。多语言靠目录前缀（`/zh/...`）。
- 站点静态资源放 `apps/docs/public/`（**不是** `.vitepress/public/`，VitePress 解析的是 `srcDir/public`）；
  `DOCS_BASE` / `DOCS_SITE_URL` 是构建期变量，只改变产物内容，本机服务始终按根路径托管。
- 健康检查：`wget -q -O- http://127.0.0.1:5174/`。

## 脚本使用说明

所有脚本在仓库根目录执行；脚本内部 `cd` 到仓库根再调用 `docker compose`，
并固定使用 `--env-file .env -f deploy/compose/docker-compose.poc.yml`。

### `./deploy/up.sh [额外参数]`

- 构建并启动整个 POC 容器栈（含文档站 `docs`）；`docker compose up -d --build --wait`。
- 等待每个服务的 `healthcheck` 通过；任一未通过即非零退出。
- 结束后打印本机访问入口（console / docs / api / gateway / PostgreSQL / NATS）与健康检查示例。
- 用法：
  - `./deploy/up.sh`                # 启动全部服务
  - `./deploy/up.sh api console`    # 只重建并启动指定服务
  - `./deploy/up.sh docs`           # 只重建并启动文档站（改了站点常量或 nginx 配置后走这条）

### `./deploy/down.sh [--volumes] [额外参数]`

- `docker compose down`，停止并移除容器、网络。
- 默认保留命名卷（`postgres-data`、`nats-data`）；显式传 `--volumes` 才删除。
- 用法：
  - `./deploy/down.sh`           # 停服务，保留数据
  - `./deploy/down.sh --volumes` # 停服务并删除数据卷

### `./deploy/status.sh`

- 打印 `docker compose ps` 状态，再独立探测 console / docs / api / gateway 的 HTTP 端点：
  - `http://127.0.0.1:${CONSOLE_PORT}/`
  - `http://127.0.0.1:${DOCS_PORT}/`（`docs` 服务未运行时打印 `SKIP` 而不是 `FAIL`）
  - `http://127.0.0.1:${API_PORT}/livez`
  - `http://127.0.0.1:${API_PORT}/v1/health`
  - `http://127.0.0.1:${GATEWAY_PORT}/v1/health`
- 期望 `2xx/3xx` 才算 OK；否则打印 `FAIL` 与状态码。

### `./deploy/logs.sh [服务名]`

- `docker compose logs -f --tail 200`；不带参数等于跟踪所有服务。

### `./deploy/up-events.sh` / `./deploy/down-events.sh`（事件链路常驻，ADR-027）

- 只操作 compose 的 `events` profile 里的两个服务：`relay`（outbox → JetStream）与 `index`
  （消费 → 向量 → 检索面）。`./deploy/up.sh` **不**拉起它们。
- `up-events.sh`：前置检查 `.env` 与 BGE 权重（`.data/models/bge-small-zh-v1.5/onnx/model_quantized.onnx`），
  缺失就打印从 HF 缓存固化的命令并以非零退出（**不联网下载**）；然后
  `docker compose --profile events up -d --wait relay index`。
- `down-events.sh [--volumes]`：用 `rm -sf relay index` 只收这两个服务（不会拆掉 api/postgres）；
  `--volumes` 额外删 `.data/index`（向量库目录）。
- 等效 make 目标：`make events-up` / `make events-down` / `make events-logs`；链路验收是
  `make event-pipeline-check`（**容器内**），单进程内的消费验收是 `make consume-check`（**主机**）。
- 检索面 `index:50077` **只**在 compose 网络内暴露（api 经它做 `mode=semantic`），主机不监听该端口。
- 深度上限来自 `.env` 的 `SENSORYPLEX_EVENT_QUEUE_CAPACITY`（档位）与
  `SENSORYPLEX_EVENT_RELAY_BATCH` / `SENSORYPLEX_EVENT_CONSUME_BATCH`：**必须 <= 上限**，
  否则两个服务在连任何东西之前就按 `event_inflight_exceeds_tier_cap` 拒绝启动（不夹取、不降级）。

## 演示账号

`apps/console` 登录页底部在 **demo 模式**下会出现「填入演示账号」按钮。开启方式：

```
make demo-seed
```

执行后 `api` 容器内用 `sensoryplex-user demo` 创建演示账号，密码随机生成到
`.data/demo-password`（容器 bind 视图，主机也能读），并写入 `.env` 的
`SENSORYPLEX_DEMO_USERNAME` / `SENSORYPLEX_DEMO_PASSWORD`；最后 `./deploy/up.sh api`
让 api 容器加载新环境变量，并 curl `/auth/v1/demo-account` 验证 `enabled:true`。

- `make demo-reset` 在 demo 账号被改密码或被禁用时，仅重置密码而不重新发。
- `.env.example` 已加入 `SENSORYPLEX_DEMO_USERNAME=demo` 与占位
  `SENSORYPLEX_DEMO_PASSWORD=REPLACE_WITH_GENERATED_PASSWORD`。
- 演示账号建表时只赋 `admin,operator` 两个角色；生产节点上请把
  `SENSORYPLEX_DEMO_*` 留空，登录页底部自动回退到「首次使用需由节点管理员创建账户」。

## Makefile（开发验证）

`make` 顶层目标与执行位置：

| 目标 | 执行位置 | 说明 |
| --- | --- | --- |
| `make up` / `down` / `infra` / `gateway` | host shell | 通过 `docker compose` 操作容器栈 |
| `make configure` | **host** | 写入随机凭据到 `.env`；容器 bind 是只读视图，必须主机执行 |
| `make proto` | api 容器 | `tools/generate_proto.py` + `tools/generate_console_types.py` |
| `make lint-ruff` / `format`（python 部分） | api 容器 | ruff 工具链来自 `uv sync --group dev` |
| `make test-py` / `test-integration` | api 容器 | pytest + compose 内 postgres；传 `SENSORYPLEX_TEST_DATABASE_URL` |
| `make plugin-artifact` / `plugin-artifact-check` | api 容器 | SDK 与 VLM 产物；ASR 仅做结构验证 |
| `make console-build` / `console-dev` / `console-prepare` | console 容器 | node/npm 已保留 |
| `make docs-install` / `docs-build` / `docs-check` | docs 容器 | 文档站依赖安装（唯一联网步骤）/ 构建产物 / 构建 + 语言树对等 + 内部链接校验 |
| `make docs-dev` / `docs-serve` | docs 容器 | 文档站开发预览（热更新 / 预览产物），监听 `DOCS_DEV_PORT=5175` |
| `make console-check` | api 容器 | `tools/verify_console.py` 读 `/host-media/<file>` |
| `make gateway-smoke` | gateway 容器 | `BASE=http://127.0.0.1:8090` 即容器自身 |
| `make runtime-smoke` / `integration` | api 容器 | 调 `tools/smoke_*.py` |
| `make outbox-check` / `outbox-run` | api 容器 | ADR-024：真 PostgreSQL + 真 NATS JetStream 的"发布这一跳"（`--nats-url` 容器内是 `nats://nats:4222`）；**NATS → sink 的消费循环未接线**，通过不等于向量已被事件驱动写入 |
| `make orchestration-p1-check` | api 容器 | ADR-029 P1：真实执行编排闭环验收（DAG、幂等、本地性、级联解锁、取消、重试、崩溃恢复） |
| `make golden-path-check` | api/host 协调 | GP-01：真实视频上传 -> 方案发布 -> 任务分发 -> 融合入库 -> 向量索引 -> 语义检索 -> 原片回看 |
| `make task-worker` | host | 宿主任务执行工作器：监听控制台任务，调度宿主 GStreamer + OCR + Timeline 融合 |
| `make check` / `test` / `format`（rust 部分） / `runtime` / `pipeline-check` / `media-check` / `media-replay` / `live-check` / `backpressure-check` / `capability-check` / `handoff-check` / `model-check` / `asr-check` | **host** | 调用主机 `cargo`；待批准工具链容器后再切回 |
| `make stream-up` / `stream-down` / `stream-status` / `stream-logs` | host | 媒体流独立 compose |
| `make plugin-release` / `plugin-release-verify` | **host** | ADR-030：构建受控 bundle（构建机需要联网 `pip download` 与宿主解释器）并独立复算整包/逐文件摘要；产物写进 `.data/releases/` |
| `make plugin-deploy-check-api` | api 容器 | ADR-030 控制面契约验收：release 导入与拒收、意图通路、状态机、fencing、蓝绿切换、排空、回滚、下载授权、余量不足、候选准入 |
| `make plugin-deploy-check-native` | **host** | ADR-030 原生执行器验收：真实平台服务（LaunchAgent）+ 真实进程 + endpoint 文件 + 真实首方插件蓝绿/排空/回滚/重启对账；**可重复运行**（槽位为空走 `provision`，槽位已有 active 自动走蓝绿 `upgrade` 并在报告里注明）；**Linux systemd 适配器尚未在 Linux 节点验收** |
| `make plugin-deploy-check` | api/host 协调 | 上面两项一起跑（控制面在容器、原生执行器在宿主） |

执行任何 `make <target>` 前确保 `./deploy/up.sh` 已经启动容器栈。


## 插件热部署（ADR-030，local_native 首期）

把「部署意图 + 状态上报」升级为真实执行闭环；首期只支持 SensoryPlex 自研、`local_native` 的
macOS/Linux 插件。热部署 == **独立进程的版本化蓝绿切换**，不提供也不承诺进程内 `hotReload`。

- 范围之外（不得宣称）：容器插件、第三方未签名插件、意图内的任意 URL/命令/宿主路径/密钥、
  跨机共享内存、业务 `Process` 验证、自动 CPU fallback。
- 意图只携带 `release_id`/`bundle_digest`/generation 等受控身份；Agent 从受认证端点
  `GET /v1/agent/releases/{release_id}/bundle` 取包（必须持有未完成意图），解包前拒绝路径穿越、
  符号链接、超限成员与任一摘要不符；安装用 bundle 内 `wheelhouse` **离线**完成，部署期不联网。
- 状态机 `accepted → staging → starting → validating → candidate_ready → cutting_over →
  draining_old → succeeded`；失败进 `failed`。切换前失败旧 active 指针不变；回滚 = 控制面创建
  **反向部署操作**，不改写历史。候选必须依次通过 `Describe` → 身份/摘要核对 → `ValidateConfig`
  → `Start` → 连续三次 `Health=ready` 才允许切换。余量不足返回 `upgrade_headroom_insufficient`，
  不停止旧版本、不降级为停机更新。
- 平台适配器：macOS 用户级 LaunchAgent、Linux systemd user unit，版本化 unit 名 + 受限环境 +
  私有日志 + 「异常退出才重启」。候选进程以 `--port 0` 启动，端口只从插件原子写出的 loopback
  endpoint 文件读取，不从 stdout 推断。
- 首方 release 构建流程：`make plugin-release PLUGIN_DIR=plugins/python/processors/<plugin>`
  → API 侧 `POST /admin/v1/plugin-releases:sync`（摘要由 API 复算，声明与实测不符即拒收）
  → Console 插件中心「热部署（ADR-030）」选已认证 release 发起部署/升级、看实时阶段与耗时、
  取消（仅切换前）或显式回滚。
- `plugins/python/processors/deploy-canary` 是**验收/自检探针**（无业务数据，行为由运行目录里的
  `mode.json` 决定），只用于验证执行器状态机与失败语义，不是业务插件，不得当生产负载。
- 验收证据边界：`make plugin-deploy-check-api`（容器内 92 项）与
  `make plugin-deploy-check-native`（宿主真实平台服务与真实首方插件）都通过时才算 macOS 实机
  闭环完成；这**不等于**整体 Golden Path 完成，`golden_path_verified=false` 保持不变。


## 全链路闭环与维护备忘

当需要利用本机资源跑通从「Web 控制台上传视频」到「最终拿到素材结果」的完整业务闭环时，执行以下维护流程：

1. **启动容器底座与常驻索引**：
   ```bash
   ./deploy/up.sh && ./deploy/up-events.sh
   ```
   启动基础容器栈：`console`（5173）、`api`（8091）、`postgres`（25432）、`nats`（24222），以及 ADR-027 常驻事件中继 `relay` 与向量索引检索面 `index`（Milvus Lite）。

2. **启动宿主机任务执行工作器（常驻守护）**：
   ```bash
   make task-worker-daemon    # 一键后台常驻运行（double-fork 脱离终端，SIGHUP 安全）
   make task-worker-status    # 查看工作器运行状态
   make task-worker-stop      # 停止后台工作器
   # 如需前台调试：make task-worker
   ```
   工作器负责保持同机节点 `local-host` 在线心跳（避免 503 节点离线），持续监听 Web 控制台派发的 `task_process` 任务意图，调度宿主 GStreamer 解码、RapidOCR 文本观测提取与 Timeline 融合，并将事实事务性追加入库；完成后标记 `completed`，触发底层自动向量化与检索面就绪。

3. **Web 控制台全流程操作**：
   - 访问 `http://127.0.0.1:5173`，使用演示账号登录（点击底部「填入演示账号」按钮）；
   - 在【视频库】（`/assets`）点击「导入视频」上传 `.mp4` 或 `.webm` 视频，完成后状态显示为待准入；
   - 在【处理任务】（`/jobs`）新建任务，关联上传的视频与已发布方案（如内置 OCR 方案），点击「开始处理」；
   - 宿主工作器处理完毕后，页面状态流转为「查看素材」，进入【素材检索】（`/materials`）浏览按时间轴对齐的多模态切片、查看文字观测并进行原片 Range 流式切片回放。

4. **全链路回归验收**：
   ```bash
   make golden-path-check
   ```
   自动化验证包含鉴权、节点就绪、视频上传、方案发布、任务分发、端侧计算、向量落库、语义检索与原片回看全部 9 个场景。

## 端口调整

`.env.example` 已加入 `CONSOLE_PORT=5173`、`API_PORT=8091`、`MEDIA_DIR`、`REPO_ROOT`、
`SENSORYPLEX_TEST_DATABASE_URL`。本地若被占用，直接修改仓库根 `.env`（不要改
`.env.example`）后再运行 `./deploy/up.sh`。

## 媒体流接入（可选，与前端无关）

`deploy/compose/docker-compose.stream.yml` 单独启动 MediaMTX（RTMP/SRT/RTSP）；
不影响前端容器栈，可用 `make stream-up` / `make stream-down`。
