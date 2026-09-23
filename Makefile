# SensoryPlex 开发验证 Makefile
# ─────────────────────────────────────────────────────────────────────────────
# 约束：所有开发验证（lint / test / integration / proto / plugin artifact 等）必须
# 在容器内执行；本机不再依赖 uv / python / node 工具链。Rust 验证 (cargo) 受限于
# 现有 api/gateway/console/postgres/nats 镜像均不携带 rustc/cargo，仍由本机 cargo
# 执行直到批准专门容器为止；该边界见 README 与 AGENTS.md。
# ─────────────────────────────────────────────────────────────────────────────

# 容器入口固定使用同一 compose 文件与 .env；执行时一律 -T 去除 TTY 染色。
COMPOSE       = docker compose --env-file .env -f deploy/compose/docker-compose.poc.yml
EXEC_API      = $(COMPOSE) exec -T api
EXEC_GATEWAY  = $(COMPOSE) exec -T gateway
EXEC_CONSOLE  = $(COMPOSE) exec -T console
EXEC_MIGRATE  = $(COMPOSE) run --rm -T migrate

# api / gateway / console 容器里的可执行入口：
PY_API        = /app/.venv/bin/python
PY_GATEWAY    = /app/.venv/bin/python

CARGO         ?= cargo
# Cargo 必须在主机上调用（没有容器带 rust 工具链）。下游 target 仍依赖此变量。
CARGO_HOST    ?= $(CARGO)

.PHONY: setup configure proto check test integration format infra up down migrate gateway runtime pipeline-check runtime-smoke gateway-smoke media-replay media-check handoff-check backpressure-check
.PHONY: stream-up stream-down stream-status stream-logs live-check model-check asr-check plugin-artifact capability-check
.PHONY: media-test resident-probe resident-install resident-uninstall resident-status
.PHONY: lint-ruff test-py test-contracts test-integration proto-generate plugin-artifact-check

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

# test-py 跑 pytest，对 services/api + services/gateway + plugins/python/common + tools。
# macOS-only plugin 包（asr-whisper-mlx / vlm-moondream）的 contracts 测试在容器内不可
# 导入（依赖 mlx-whisper），保留在 host-side `make test-contracts-host`。
test-py:
	docker compose --env-file .env -f deploy/compose/docker-compose.poc.yml exec -T -e SENSORYPLEX_TEST_DATABASE_URL api $(PY_API) -m pytest tests/contracts tests/integration -q

# integration 直接调用 tools/test_integration.py，访问 postgres 与 api 容器。
test-integration:
	docker compose --env-file .env -f deploy/compose/docker-compose.poc.yml exec -T -e SENSORYPLEX_TEST_DATABASE_URL api $(PY_API) -m pytest tests/integration -q

# check = lint-ruff + test-py + Rust fmt/clippy/test。
# Rust 部分仍调用主机 cargo，见顶部约束说明。
check: lint-ruff test-py
	$(CARGO_HOST) fmt --all -- --check
	$(CARGO_HOST) clippy --workspace --all-targets --locked -- -D warnings
	$(CARGO_HOST) test --workspace --locked

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
	$(EXEC_API) $(PY_API) tools/smoke_runtime.py

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

handoff-check:
	@test -n "$(MEDIA)" || { echo "usage: make handoff-check MEDIA=/absolute/path/to/authorized-sample.mp4"; exit 1; }
	$(CARGO_HOST) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	$(EXEC_API) $(PY_API) tools/verify_handoff.py --media "/host-media/$(notdir $(MEDIA))"

# ── 插件产物（容器内） ────────────────────────────────────────────────────

# plugin-artifact 对 sdk 部分可在容器内执行；macOS-only plugin 包保留主机路径。
plugin-artifact:
	$(EXEC_API) $(PY_API) tools/plugin_artifact.py --sbom plugins/python/processors/asr-whisper-mlx
	$(EXEC_API) $(PY_API) tools/validate_plugin.py plugins/python/processors/asr-whisper-mlx/plugin.yaml
	$(EXEC_API) $(PY_API) tools/plugin_artifact.py --sbom plugins/python/processors/vlm-moondream
	$(EXEC_API) $(PY_API) tools/validate_plugin.py plugins/python/processors/vlm-moondream/plugin.yaml
	$(EXEC_API) $(PY_API) tools/plugin_artifact.py --sbom plugins/python/processors/ocr-rapidocr
	$(EXEC_API) $(PY_API) tools/validate_plugin.py plugins/python/processors/ocr-rapidocr/plugin.yaml

plugin-artifact-check:
	$(EXEC_API) $(PY_API) tools/plugin_artifact.py --check plugins/python/processors/asr-whisper-mlx || true
	$(EXEC_API) $(PY_API) tools/plugin_artifact.py --check plugins/python/processors/vlm-moondream || true
	$(EXEC_API) $(PY_API) tools/plugin_artifact.py --check plugins/python/processors/ocr-rapidocr || true

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
	$(EXEC_CONSOLE) sh -lc 'cd /workspace/apps/console && npm ci --no-audit --no-fund && npm run build'

console-check:
	@test -n "$(MEDIA)" || { echo "usage: make console-check MEDIA=/absolute/path/to/authorized.webm"; exit 1; }
	$(EXEC_API) $(PY_API) tools/verify_console.py --media "/host-media/$(notdir $(MEDIA))"

# console-dev（vite dev server）：在 console 容器内 bind 主机源文件后启动 vite。
# vite 默认监听 127.0.0.1，容器内执行；为让主机浏览器访问，先 exec console 把 vite
# 改成 --host 0.0.0.0 并把 5173 端口临时映射（compose 中 console 已暴露 5173）。
console-dev:
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
