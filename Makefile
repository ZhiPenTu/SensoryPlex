# SensoryPlex 开发验证 Makefile
# ─────────────────────────────────────────────────────────────────────────────
# 约束：所有开发验证（lint / test / integration / proto / plugin artifact 等）默认
# 在容器内执行；本机不需要 uv / python / node 工具链。Rust 验证 (cargo) 受限于
# 现有 api/gateway/console/postgres/nats 镜像均不携带 rustc/cargo，仍由本机 cargo
# 执行直到批准专门容器为止；该边界见 README 与 AGENTS.md。
# ─────────────────────────────────────────────────────────────────────────────

# 执行位置（EXEC_MODE）：
#   container（默认）—— 在 compose 容器内执行，即上面的约束；
#   host               —— 同一组命令退回主机（`uv run --frozen python` + 主机 cargo）。
# host 只服务于没有 Docker 的环境，目前只有远端 CI：ubuntu runner 上没有本项目的
# compose 栈，macOS runner 上干脆没有 Docker，而 ADR-008 要求 macOS 必须是一等
# 验证目标（见 .github/workflows/ci.yml，三个 job 都显式 EXEC_MODE=host）。
# 两种模式的**步骤集合必须一致**，差别只有"在哪执行"：host 不允许少跑任何一步。
EXEC_MODE     ?= container

ifeq ($(EXEC_MODE),container)
# 容器入口固定使用同一 compose 文件与 .env；执行时一律 -T 去除 TTY 染色。
COMPOSE       = docker compose --env-file .env -f deploy/compose/docker-compose.poc.yml
EXEC_API      = $(COMPOSE) exec -T api
EXEC_GATEWAY  = $(COMPOSE) exec -T gateway
EXEC_CONSOLE  = $(COMPOSE) exec -T console
EXEC_MIGRATE  = $(COMPOSE) run --rm -T migrate
# 集成测试的库/总线地址：容器模式用 compose exec -e 从调用者环境注入；`-e` 必须写在 SERVICE
# **之前**（`docker compose exec [OPTIONS] SERVICE COMMAND`），所以这里不复用 EXEC_API。
# `SENSORYPLEX_TEST_NATS_URL` 给了容器内可达的默认值（`TEST_NATS_URL`），让真 JetStream 的
# 集成用例默认**真跑**而不是被 skip（跳过不算证据）；主机模式由调用者自己提供这两个变量
# （见下方 test-integration 注释）。
EXEC_TEST     = $(COMPOSE) exec -T -e PYTHONPATH=/workspace/services/api/src:/workspace/plugins/python/common/src:/workspace -e SENSORYPLEX_TEST_DATABASE_URL -e SENSORYPLEX_TEST_NATS_URL=$(TEST_NATS_URL) api
# api / gateway / console 容器里的可执行入口：
PY_API        = /app/.venv/bin/python
PY_GATEWAY    = /app/.venv/bin/python
else ifeq ($(EXEC_MODE),host)
# 主机模式：EXEC_* 全部退化为空前缀，让 `$(EXEC_API) $(PY_API) <cmd>` 展开成
# `uv run --frozen python <cmd>`；COMPOSE 仍保留，供有 Docker 的主机执行 up/down/infra。
COMPOSE       = docker compose --env-file .env -f deploy/compose/docker-compose.poc.yml
EXEC_API      =
EXEC_GATEWAY  =
EXEC_CONSOLE  =
EXEC_MIGRATE  =
EXEC_TEST     =
PY_API        = uv run --frozen python
PY_GATEWAY    = uv run --frozen python
else
$(error EXEC_MODE 只支持 container 或 host，当前为 "$(EXEC_MODE)")
endif

CARGO         ?= cargo
# Cargo 必须在主机上调用（没有容器带 rust 工具链）。下游 target 仍依赖此变量。
CARGO_HOST    ?= $(CARGO)
# 需要"主机专属资源"的验收也固定在主机执行，理由与 cargo 相同：HF 权重缓存、CoreML EP、
# MLX/Metal 都只存在于 macOS 主机，compose 容器里没有（.env 只挂 MEDIA_DIR）。
PY_HOST       ?= uv run --frozen python
# outbox-check / outbox-run 要连的 NATS：容器内是服务名 nats:4222，宿主是回环端口。
OUTBOX_NATS   ?= $(if $(filter container,$(EXEC_MODE)),nats://nats:4222,nats://127.0.0.1:24222)
# 集成测试（EXEC_TEST）连的 NATS：与 OUTBOX_NATS 同一套推导。
TEST_NATS_URL ?= $(if $(filter container,$(EXEC_MODE)),nats://nats:4222,nats://127.0.0.1:24222)

.PHONY: setup configure proto check test integration format infra up down migrate gateway runtime pipeline-check runtime-smoke gateway-smoke media-replay media-check handoff-check backpressure-check
.PHONY: stream-up stream-down stream-status stream-logs live-check model-check asr-check ocr-check embed-check index-check semantic-check parallelism-check plugin-artifact capability-check accelerator-check
.PHONY: outbox-check outbox-run
.PHONY: node-check
.PHONY: consume-check
.PHONY: event-pipeline-check events-up events-down events-logs
.PHONY: media-test resident-probe resident-install resident-uninstall resident-status
.PHONY: lint-ruff test-py test-contracts test-integration proto-generate plugin-artifact-check
.PHONY: timeline-check

# ── 项目引导 ────────────────────────────────────────────────────────────────

# setup 步骤的特殊性：configure 需要把生成的 .env 写回主机以便 compose 读取；
# proto 需要在容器中看到最新生成代码；cargo build 仍跑在主机。
setup: configure
	$(EXEC_API) $(PY_API) tools/generate_proto.py
	$(EXEC_API) $(PY_API) tools/generate_console_types.py
	$(CARGO_HOST) build --workspace --locked

# configure 必须在本机执行：tools/configure.py 会在仓库根写入随机凭据到 .env，
# 容器 bind mount 把同一个仓库根映射为只读视图，无法写入新凭据。本步骤之后
# 任何后续 `make up` 都能读取最新 .env。这是项目自带约束，非 Rust 类例外。
configure:
	uv run --no-project python tools/configure.py

# ── 代码生成（容器内） ────────────────────────────────────────────────────

proto:
	$(EXEC_API) $(PY_API) tools/generate_proto.py
	$(EXEC_API) $(PY_API) tools/generate_console_types.py

# ── 代码检查 / 测试（容器内） ─────────────────────────────────────────────

# lint-ruff 仅 lint Python 源码；不含 Rust 工具链。
lint-ruff:
	$(EXEC_API) $(PY_API) -m ruff check .
	$(EXEC_API) $(PY_API) -m ruff format --check .

# test-contracts 跑 pytest tests/contracts：不依赖外部服务，容器与主机都能跑。
test-contracts:
	$(EXEC_TEST) $(PY_API) -m pytest tests/contracts -q

# test-integration 跑 tests/integration：需要可写的 PostgreSQL（SENSORYPLEX_TEST_DATABASE_URL）
# 与可连的 NATS（SENSORYPLEX_TEST_NATS_URL，真 JetStream 用例用），容器模式由 compose
# postgres / nats 提供，主机模式由调用者提供。没有数据库的主机会在 tests/integration/test_console.py
# 上**报错而不是跳过** —— 刻意的：漏配数据库必须显式失败。主机模式（含远端 CI）没有 NATS 时，
# 真 JetStream 用例仍是显式 skip，所以那些用例的证据只来自容器模式。
test-integration:
	$(EXEC_TEST) $(PY_API) -m pytest tests/integration -q

# test-py = 契约测试 + 集成测试（services/api + services/gateway + plugins/python/common + tools）。
test-py: test-contracts test-integration

# check = lint-ruff + test-py + Rust fmt/clippy/test。
# Rust 部分仍调用主机 cargo，见顶部约束说明。
check: lint-ruff test-py
	$(CARGO_HOST) fmt --all -- --check
	$(CARGO_HOST) clippy --workspace --all-targets --locked -- -D warnings
	$(CARGO_HOST) test --workspace --locked
# 改动 check 的步骤时必须同步 .github/workflows/ci.yml 的 check-apple-silicon：
# macOS runner 既没有 Docker 也没有 PostgreSQL，只能跑 `make lint-ruff test-contracts`
# 加下面三条主机 cargo，集成测试由 ubuntu 的 check job 覆盖。

test:
	$(MAKE) test-py

integration:
	$(MAKE) test-integration

format:
	$(CARGO_HOST) fmt --all
	$(EXEC_API) $(PY_API) -m ruff format .

# ── 容器栈控制 ────────────────────────────────────────────────────────────

infra:
	$(COMPOSE) up -d --wait postgres nats

up:
	$(COMPOSE) up -d --build --wait

down:
	$(COMPOSE) down

# 一次性数据库迁移（容器内）。
migrate:
	$(EXEC_MIGRATE) $(PY_GATEWAY) tools/migrate.py

# ── 服务进程（容器即运行时） ───────────────────────────────────────────────
# gateway 进程由 compose `gateway` 服务提供；该命令只是把服务跑起来。
gateway:
	$(COMPOSE) up -d --wait gateway

# runtime 没有专门的容器镜像；按顶部约束仍由主机 cargo 启动。
runtime:
	$(CARGO_HOST) run --locked -p sensoryplex-runtime -- serve

pipeline-check:
	$(CARGO_HOST) run --locked -p sensoryplex-runtime -- check config/pipelines/file-material.yaml

runtime-smoke:
	$(CARGO_HOST) build --locked -p sensoryplex-runtime
	# 被验收的进程是**主机构建的原生二进制**（本机是 Mach-O），Linux 容器 exec 不了它，
	# 因此这一步与 embed-check / parallelism-check 同类，固定在主机侧执行。
	$(PY_HOST) tools/smoke_runtime.py

gateway-smoke:
	$(EXEC_GATEWAY) $(PY_GATEWAY) tools/smoke_gateway.py

# ── 媒体 E2E（Rust + 容器的组合） ──────────────────────────────────────────
# Rust 部分 cargo / gstreamer 走主机；脚本调用放容器内。

MEDIA_FEATURES ?= gstreamer
# 编译门：解码路径需要 GStreamer 开发文件，且仍需主机 cargo。
media-check:
	$(CARGO_HOST) clippy --locked -p sensoryplex-media -p sensoryplex-runtime --all-targets --features "$(MEDIA_FEATURES)" -- -D warnings

# 解码路径的单元测试需要 GStreamer 开发文件；没有的主机可加 MEDIA_FEATURES= 显式少跑。
media-test:
	$(CARGO_HOST) test --locked -p sensoryplex-media --features "$(MEDIA_FEATURES)"

media-replay:
	@test -n "$(MEDIA)" || { echo "usage: make media-replay MEDIA=/absolute/path/to/authorized-sample.mp4"; exit 1; }
	$(CARGO_HOST) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	# 把授权样本以只读方式 bind 到容器，避免主机直传。
	$(EXEC_API) $(PY_API) tools/verify_replay.py --media "/host-media/$(notdir $(MEDIA))"

# SRT 实时接入验收。需要 MediaMTX 已启动 (make stream-up)。
live-check:
	$(CARGO_HOST) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	$(EXEC_API) $(PY_API) tools/verify_live.py $(if $(SAMPLE),--sample "/host-media/$(notdir $(SAMPLE))",)

backpressure-check:
	$(CARGO_HOST) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	$(EXEC_API) $(PY_API) tools/verify_backpressure.py

capability-check:
	$(CARGO_HOST) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	$(EXEC_API) $(PY_API) tools/verify_capability.py

# ── 宿主加速器上报验收（ADR-022） ──────────────────────────────────────────
# 要真读宿主（system_profiler / CoreML framework / nvidia-smi），这些只存在于主机，
# 与 embed-check / parallelism-check 同类固定走主机侧执行。
accelerator-check:
	$(CARGO_HOST) build --locked --release -p sensoryplex-runtime
	$(PY_HOST) tools/verify_accelerator_report.py

handoff-check:
	@test -n "$(MEDIA)" || { echo "usage: make handoff-check MEDIA=/absolute/path/to/authorized-sample.mp4"; exit 1; }
	$(CARGO_HOST) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	$(EXEC_API) $(PY_API) tools/verify_handoff.py --media "/host-media/$(notdir $(MEDIA))"

# ── 真实媒体端到端：Runtime → Timeline → 授权追加 → outbox → JetStream（ADR-028） ──
# 这一段是 TODO「真实媒体端到端」里 Runtime → Timeline → metadata writer/outbox 的闭环。
# 固定在**主机**执行，两条理由都不可绕：
#   1. runtime 二进制是主机 Mach-O（`target/release/sensoryplex-runtime`），容器里
#      `docker compose exec` 会直接 `Exec format error`；
#   2. 验收要用的真实 VLM 端点（ollama，127.0.0.1:11434）与 HF/MLX 一样只存在于主机。
# PostgreSQL 与 NATS JetStream 仍在 compose 里跑，从宿主回环端口（25432 / 24222）连；
# 验收脚本自己建隔离 schema 与独立 JetStream stream，跑完即删，不碰开发用的库与 stream。
timeline-check:
	@test -n "$(MEDIA)" || { echo "usage: make timeline-check MEDIA=/absolute/path/to/authorized-sample.mp4"; exit 1; }
	$(CARGO_HOST) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	$(PY_HOST) tools/verify_timeline_handoff.py --media "$(MEDIA)"

# ── 插件产物（容器内） ────────────────────────────────────────────────────

# plugin-artifact 对 sdk 部分可在容器内执行；macOS-only plugin 包保留主机路径。
plugin-artifact:
	$(EXEC_API) $(PY_API) tools/plugin_artifact.py --sbom plugins/python/processors/asr-whisper-mlx
	$(EXEC_API) $(PY_API) tools/validate_plugin.py plugins/python/processors/asr-whisper-mlx/plugin.yaml
	$(EXEC_API) $(PY_API) tools/plugin_artifact.py --sbom plugins/python/processors/vlm-moondream
	$(EXEC_API) $(PY_API) tools/validate_plugin.py plugins/python/processors/vlm-moondream/plugin.yaml
	$(EXEC_API) $(PY_API) tools/plugin_artifact.py --sbom plugins/python/processors/ocr-rapidocr
	$(EXEC_API) $(PY_API) tools/validate_plugin.py plugins/python/processors/ocr-rapidocr/plugin.yaml
	$(EXEC_API) $(PY_API) tools/plugin_artifact.py --sbom plugins/python/processors/embed-bge-onnx
	$(EXEC_API) $(PY_API) tools/validate_plugin.py plugins/python/processors/embed-bge-onnx/plugin.yaml

plugin-artifact-check:
	$(EXEC_API) $(PY_API) tools/plugin_artifact.py --check plugins/python/processors/asr-whisper-mlx || true
	$(EXEC_API) $(PY_API) tools/plugin_artifact.py --check plugins/python/processors/vlm-moondream || true
	$(EXEC_API) $(PY_API) tools/plugin_artifact.py --check plugins/python/processors/ocr-rapidocr || true
	$(EXEC_API) $(PY_API) tools/plugin_artifact.py --check plugins/python/processors/embed-bge-onnx || true

# 模型插件链路验收（M8）：需要本机 VLM 服务（默认 http://127.0.0.1:11434）；
# cargo build 走主机，verify_model 在容器内执行，MEDIA 通过 bind 进入容器。
model-check:
	@test -n "$(MEDIA)" || { echo "usage: make model-check MEDIA=/absolute/path/to/authorized-sample.mp4"; exit 1; }
	$(CARGO_HOST) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	$(EXEC_API) $(PY_API) tools/verify_model.py --media "/host-media/$(notdir $(MEDIA))" $(if $(MODEL),--model "$(MODEL)",)

# ASR 插件链路验收（M10）：需要本机已装 mlx-whisper；cargo 走主机，
# verify_asr 在容器内执行。
asr-check:
	@test -n "$(MEDIA)" || { echo "usage: make asr-check MEDIA=/absolute/path/to/authorized-speech-sample.mp4"; exit 1; }
	$(CARGO_HOST) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	$(EXEC_API) $(PY_API) tools/verify_asr.py --media "/host-media/$(notdir $(MEDIA))" $(if $(MODEL),--model "$(MODEL)",) $(if $(MODEL_DIR),--model-dir "$(MODEL_DIR)",)$(if $(LANGUAGE), --language "$(LANGUAGE)",)

# ── 媒体流接入（独立 compose） ─────────────────────────────────────────────

stream-up:
	docker compose -f deploy/compose/docker-compose.stream.yml up -d
	@curl --fail --silent --show-error --connect-timeout 2 --max-time 3 --retry 5 --retry-connrefused --retry-delay 1 --retry-max-time 20 http://127.0.0.1:9998/metrics >/dev/null
	@echo "MediaMTX 已启动；OBS 服务器 rtmp://127.0.0.1:1935/live，串流密钥 obs。是否有媒体输入请执行 make stream-status。"

stream-down:
	docker compose -f deploy/compose/docker-compose.stream.yml down

stream-status:
	docker compose -f deploy/compose/docker-compose.stream.yml ps
	@curl --fail --silent --show-error --connect-timeout 2 --max-time 3 http://127.0.0.1:9998/metrics

stream-logs:
	docker compose -f deploy/compose/docker-compose.stream.yml logs --tail 100 mediamtx

# ── 事件链路常驻（ADR-024 / ADR-025 / ADR-027） ──────────────────────────────
# relay（outbox → JetStream）与 index（消费 → 向量 → 检索面）在 compose 里属于 `events`
# profile：`make up` / `make events-up` 之外的命令不会拉起它们，避免默认栈拖着 BGE 与向量库。
# 主机侧只负责起停；链路验收在容器内执行（`make event-pipeline-check`）。
events-up:
	./deploy/up-events.sh

events-down:
	./deploy/down-events.sh

events-logs:
	$(COMPOSE) --profile events logs -f --tail 200 relay index

# ── 演示账号种子（一次性） ────────────────────────────────────────────────
# 在 api 容器内用 sensoryplex-user 创建 demo 账户，密码落到 /workspace/.data/demo-password，
# 随后主机读这个文件并写入 .env 的 SENSORYPLEX_DEMO_PASSWORD，再 ./deploy/up.sh api 让
# 容器加载新环境变量；登录页底部会切换为"本地演示可一键填写账号…"并出现
# 「填入演示账号」按钮。注意 .env 写回是主机例外（容器 bind 是只读视图）。
.PHONY: demo-seed demo-reset
demo-seed:
	@mkdir -p .data
	$(EXEC_API) sh -lc 'set -e; PWFILE=/workspace/.data/demo-password; rm -f "$$PWFILE"; \
		/app/.venv/bin/sensoryplex-user demo --display-name "演示账号" --roles admin,operator \
		--generate-password --password-file "$$PWFILE"'
	@test -s .data/demo-password || { echo "demo password file missing" >&2; exit 1; }
	@PW=$$(cat .data/demo-password); \
	{ grep -v '^SENSORYPLEX_DEMO_' .env > .env.tmp && mv .env.tmp .env; } || true
	@printf '\nSENSORYPLEX_DEMO_USERNAME=demo\nSENSORYPLEX_DEMO_PASSWORD=%s\n' "$$PW" >> .env
	@echo "[demo-seed] demo 账号已建，凭据已写入 .env，正在重启 api 容器..."
	$(COMPOSE) up -d --wait api
	@echo "[demo-seed] 验证 /auth/v1/demo-account："
	@curl -fsS -X GET "$${CONSOLE_PORT:+http://127.0.0.1:$$CONSOLE_PORT}/auth/v1/demo-account" \
		-H "Origin: http://127.0.0.1:$${CONSOLE_PORT:-5173}" \
		-H "Referer: http://127.0.0.1:$${CONSOLE_PORT:-5173}/" | head -c 200 && echo

demo-reset:
	$(EXEC_API) /app/.venv/bin/python -c "import os,psycopg; from sensoryplex_api.auth import password_hash; \
		pw=os.environ['SENSORYPLEX_DEMO_PASSWORD']; \
		from sensoryplex_api.settings import Settings; s=Settings(); \
		conn=psycopg.connect(s.database_url.get_secret_value()); \
		conn.execute('UPDATE console_user SET password_hash=%s,disabled=false WHERE username=%s', (password_hash(pw),'demo')); \
		conn.commit(); print('demo 密码已重置')"

# ── 前端控制台（容器内执行） ────────────────────────────────────────────

# console 镜像构建内已包含 npm ci + npm run build；运行 console-build 仍会重新跑一遍
# 以便在迭代 console 源码后立即刷新 /usr/share/nginx/html（容器内 /workspace/apps/console
# 通过 bind mount 反映主机源文件）。
console-build:
	@test "$(EXEC_MODE)" = container || { echo "console-build 只能在容器内执行：/workspace 与镜像自带的 node 只存在于 console 镜像里" >&2; exit 1; }
	$(EXEC_CONSOLE) sh -lc 'cd /workspace/apps/console && npm ci --no-audit --no-fund && npm run build'

console-check:
	@test -n "$(MEDIA)" || { echo "usage: make console-check MEDIA=/absolute/path/to/authorized.webm"; exit 1; }
	$(EXEC_API) $(PY_API) tools/verify_console.py --media "/host-media/$(notdir $(MEDIA))"

# console-dev（vite dev server）：在 console 容器内 bind 主机源文件后启动 vite。
# vite 默认监听 127.0.0.1，容器内执行；为让主机浏览器访问，先 exec console 把 vite
# 改成 --host 0.0.0.0 并把 5173 端口临时映射（compose 中 console 已暴露 5173）。
console-dev:
	@test "$(EXEC_MODE)" = container || { echo "console-dev 只能在容器内执行：/workspace 与镜像自带的 node 只存在于 console 镜像里" >&2; exit 1; }
	$(EXEC_CONSOLE) sh -lc 'cd /workspace/apps/console && npm ci --no-audit --no-fund && npm run dev -- --host 0.0.0.0 --port 5173'

# console-prepare / console-api：容器内启动 sensoryplex-api 的 prepare / serve 入口；
# 运行时直接由 compose `api` 服务承担。
console-prepare:
	$(EXEC_API) $(PY_API) -m tools.console_dev prepare

console-api:
	@echo "console-api 由 compose \`api\` 服务提供，使用 ./deploy/up.sh 启动。"

# ── macOS 常驻形态（必须在本机执行） ──────────────────────────────────────

# launchd / launchctl / sysctl 只存在于 macOS 宿主，容器里没有；因此本组目标是
# 明确的"主机例外"，与上面的 configure 同类，不放进 EXEC_* 容器。
# 分级依据与验收见 docs/adr/ADR-015 与 docs/runbooks/macos-resident.md。
resident-probe:
	uv run python tools/macos_resident.py probe

resident-install:
	uv run python tools/macos_resident.py install

resident-uninstall:
	uv run python tools/macos_resident.py uninstall

resident-status:
	uv run python tools/macos_resident.py status --verify-endpoint

# ── OCR 插件链路验收（M8，ADR-016） ───────────────────────────────────────
# 权重随 rapidocr 轮子携带（也可用 MODEL_DIR 指向本机目录）；cargo 走主机，
# verify_ocr 在容器内执行，MEDIA 通过 bind 进入容器。
# EXPECT=text 用于有文字的样本（要求有块）；EXPECT=empty 用于无文字样本
# （要求 blocks=[] 且带 empty_reason，证明"没找到文字"与"处理失败"可区分）。
# PROVIDER=cpu|coreml：coreml 必须真的被会话选中，否则显式失败（不静默退回 CPU）。
.PHONY: ocr-check
ocr-check:
	@test -n "$(MEDIA)" || { echo "usage: make ocr-check MEDIA=/absolute/path/to/authorized-video.webm [EXPECT=text|empty] [PROVIDER=cpu|coreml]"; exit 1; }
	$(CARGO_HOST) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	$(EXEC_API) $(PY_API) tools/verify_ocr.py --media "/host-media/$(notdir $(MEDIA))" --provider "$(if $(PROVIDER),$(PROVIDER),cpu)" --expect "$(if $(EXPECT),$(EXPECT),text)" $(if $(MODEL_DIR),--model-dir "$(MODEL_DIR)",)

# ── BGE 文本向量链路验收（M8，ADR-017） ───────────────────────────────────
# 这条链路**不接数据面**：输入是上游 OCR 观测里的文字。验收自己先跑一遍真实 OCR 链路
# （四进程）产出 ocr_blocks，再让 BGE 插件编码成"维度版本化"的归一化向量；
# 也可以用 OBSERVATIONS=<既有 ai-worker.json> 复用上游报告、跳过重跑 OCR。
# PROVIDER=cpu|coreml：coreml 必须真的被会话选中，否则显式失败（不静默退回 CPU）。
.PHONY: embed-check
embed-check:
	@test -n "$(MEDIA)" || { echo "usage: make embed-check MEDIA=/absolute/path/to/authorized-video.webm [PROVIDER=cpu|coreml] [MODEL_DIR=/path/to/bge-weights] [OBSERVATIONS=/path/to/ocr-ai-worker.json]"; exit 1; }
	$(CARGO_HOST) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	$(PY_HOST) tools/verify_embed.py --media "$(MEDIA)" --provider "$(if $(PROVIDER),$(PROVIDER),cpu)" $(if $(MODEL_DIR),--model-dir "$(MODEL_DIR)",) $(if $(OCR_MODEL_DIR),--ocr-model-dir "$(OCR_MODEL_DIR)",) $(if $(OBSERVATIONS),--input-observations "$(OBSERVATIONS)",)

# ── 向量落库与检索闭环验收（M8 剩余，ADR-020） ──────────────────────────────
# 输入必须是**真实** BGE 观测（`tools/verify_embed.py --keep-workspace` 的 ai-worker.json），
# 不是自己造的向量：本目标跑真实 PostgreSQL（隔离 schema + 真实迁移）与真实 Milvus Lite，
# 以独立进程调用 `python -m sensoryplex_index_worker.cli`，验写入确认、跨进程持久、
# 回查过滤（owner / ready / material 状态）、幂等与四类显式失败（维度、不可达、契约漂移、目录被锁）。
# Milvus Lite 是本地文件形态且**进程独占**（目录 flock），所以这一步固定在主机执行；
# 服务端 Milvus 形态未验收（本机 Docker Hub 不可达），见 ADR-020 §6/§7。
index-check:
	@test -n "$(EMBEDDINGS)" || { echo "usage: make index-check EMBEDDINGS=/absolute/path/to/ai-worker.json（先跑 uv run --frozen python tools/verify_embed.py --media <sample> --keep-workspace 得到它）"; exit 1; }
	$(PY_HOST) tools/verify_index.py --embeddings "$(EMBEDDINGS)"

# ── 网关语义检索接线验收（② / ADR-023） ─────────────────────────────────────
# 不需要先备好 ai-worker.json：本目标自己把三段真实文本送进真实 BGE 插件进程，得到真实观测，
# 再用真实 Milvus Lite 落库、起**常驻检索面**（`... cli serve`，持有向量库的唯一进程），
# 最后由真实 API 走 HTTP 做语义检索并水合事实。验的是：同源守卫（错 release / 混装）、
# 非 owner 丢弃计数、状态码分野（503 网关侧 / 502 上游拒绝）、令牌与不可达、目录锁、
# 契约漂移必须在启动时拒绝、以及线上不外泄。Milvus Lite 是进程独占的本地文件，
# 与 HF 权重一样只存在于主机，所以固定在主机执行（理由同 cargo 与 index-check）。
semantic-check:
	$(PY_HOST) tools/verify_semantic_search.py $(if $(DATABASE_URL),--database-url "$(DATABASE_URL)",) $(if $(MODEL_DIR),--model-dir "$(MODEL_DIR)",) $(if $(PROVIDER),--provider "$(PROVIDER)",)

# ── outbox → NATS JetStream 的 relay：验收与常驻运行（① / ADR-024） ──────────
# 这个目标与其它 *-check 不同，它**在 api 容器内**执行：需要的只有真实 PostgreSQL 与真实
# JetStream（都在 compose 里），Milvus Lite / HF 权重 / CoreML 一个都不用——没有退回主机的理由。
# 边界（不得含糊）：本目标只验"发布这一跳"——事件确认发到 JetStream、`Nats-Msg-Id` 去重、
# 漂移不静默、NATS 不可达显式失败。"事件被消费成向量、并且能被检索到"由 `consume-check`
# 单独验（ADR-025）；`outbox-check` 通过**不等于**"向量已经被事件驱动地写进去了"。
node-check:
	$(EXEC_API) $(PY_API) tools/verify_node_topology.py --base-url http://127.0.0.1:8091

outbox-check:
	$(EXEC_API) $(PY_API) tools/verify_outbox_relay.py --nats-url "$(OUTBOX_NATS)"

# 常驻形态：前台运行本机 relay，一有事件落 outbox 就发布到 JetStream（Ctrl-C / SIGTERM 优雅收尾）。
# 只做发布，不做消费；消费侧是 `sensoryplex-index-worker.cli serve --consume`（见 consume-check）。
outbox-run:
	$(EXEC_API) $(PY_API) -m sensoryplex_relay.cli --nats-url "$(OUTBOX_NATS)"

# ── 事件驱动的常驻消费验收（① / ADR-025） ────────────────────────────────────
# 闭环证据：真实写侧落 outbox（事实与事件同事务）→ 真实 relay 发到 JetStream →
# `cli serve --consume` 常驻消费 → 真实 BGE 编码写入真实 Milvus Lite → **同一个进程**的检索面
# 立刻检索到；另验重放不重复、坏事件 fail-stop（退出码 3）、启动期三类显式失败
# （stream 缺失 / durable 漂移 / NATS 不可达）以及状态行不外泄。
# Milvus Lite 的数据目录是进程独占的本地文件、BGE 权重也只存在于主机，所以固定在主机执行
# （理由同 semantic-check / index-check）。
consume-check:
	$(PY_HOST) tools/verify_index_consume.py $(if $(DATABASE_URL),--database-url "$(DATABASE_URL)",) $(if $(NATS_URL),--nats-url "$(NATS_URL)",) $(if $(MODEL_DIR),--model-dir "$(MODEL_DIR)",) $(if $(PROVIDER),--provider "$(PROVIDER)",)

# ── 事件链路常驻的容器内闭环验收（ADR-027，接 ADR-019 / ADR-024 / ADR-025） ──
# 证据链：真实写侧落 outbox → **compose 里常驻的** relay 发到真 JetStream → **compose 里常驻的**
# index 消费成 embedding_record(ready) → 真 Milvus Lite → api 的 `mode=semantic` 经真 gRPC 命中。
# 在 api 容器内执行：真 PostgreSQL、真 JetStream、真向量库都在 compose 里，容器里没有"退回主机"的理由。
# 与 consume-check 的分工：那个验"单进程内的消费正确性"（含 fail-stop / 重放 / 启动失败），
# 这个验"两个常驻服务真的在 compose 里把链路跑通了"，并用真实 HTTP 语义检索收口。
event-pipeline-check:
	$(EXEC_API) $(PY_API) tools/verify_event_pipeline.py

# ── 模型 worker 分级并发上限验收（M8 剩余，ADR-021） ────────────────────────
# 上游必须是**真实** `ocr_blocks` 观测：每个 MEDIA 跑一次未改动的 tools/verify_ocr.py 真实链路
# （真实 replay → 真实 OCR 插件 → worker）。文件回放按 1s 抽帧、worker 又在处理前一次性 List，
# 所以一个样本只交付个位数帧：要凑出 N 路在飞窗口就给多个授权样本（空格分隔）。
# 本目标跑真实插件进程与真实 `sensoryplex-runtime serve`，以独立进程调用 tools/ai_worker.py。
# 固定在主机执行的理由同其它 *-check：HF 权重缓存与 CoreML EP 只存在于 macOS 主机（.env 只挂 MEDIA_DIR）。
parallelism-check:
	@test -n "$(MEDIA)" || { echo "usage: make parallelism-check MEDIA=\"/abs/sample-a.webm [/abs/sample-b.webm ...]\" [INPUTS=4] [PROVIDER=cpu|coreml] [MODEL_DIR=/path/to/bge-weights] [OCR_MODEL_DIR=/path/to/rapidocr]"; exit 1; }
	$(CARGO_HOST) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	$(PY_HOST) tools/verify_model_parallelism.py $(foreach item,$(MEDIA),--media "$(item)") --inputs "$(if $(INPUTS),$(INPUTS),4)" --provider "$(if $(PROVIDER),$(PROVIDER),cpu)" $(if $(MODEL_DIR),--model-dir "$(MODEL_DIR)",) $(if $(OCR_MODEL_DIR),--ocr-model-dir "$(OCR_MODEL_DIR)",)
