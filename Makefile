UV ?= uv
CARGO ?= cargo
COMPOSE = docker compose --env-file .env -f deploy/compose/docker-compose.poc.yml
STREAM_COMPOSE = docker compose -f deploy/compose/docker-compose.stream.yml

.PHONY: setup configure proto check test integration format infra up down migrate gateway runtime pipeline-check runtime-smoke gateway-smoke media-replay media-check handoff-check backpressure-check
.PHONY: stream-up stream-down stream-status stream-logs live-check
setup: configure
	$(UV) sync --frozen
	$(MAKE) proto
	$(CARGO) build --workspace --locked

configure:
	$(UV) run --no-project python tools/configure.py

proto:
	$(UV) run python tools/generate_proto.py

format:
	$(CARGO) fmt --all
	$(UV) run ruff format .

check:
	$(CARGO) fmt --all -- --check
	$(CARGO) clippy --workspace --all-targets --locked -- -D warnings
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(MAKE) test

test:
	$(CARGO) test --workspace --locked
	$(UV) run python -m pytest tests/contracts -q

integration:
	$(UV) run python tools/test_integration.py

infra:
	$(COMPOSE) up -d --wait postgres nats

up:
	$(COMPOSE) up -d --build --wait

down:
	$(COMPOSE) down

stream-up:
	$(STREAM_COMPOSE) up -d
	@curl --fail --silent --show-error --connect-timeout 2 --max-time 3 --retry 5 --retry-connrefused --retry-delay 1 --retry-max-time 20 http://127.0.0.1:9998/metrics >/dev/null
	@echo "MediaMTX 已启动；OBS 服务器 rtmp://127.0.0.1:1935/live，串流密钥 obs。是否有媒体输入请执行 make stream-status。"

stream-down:
	$(STREAM_COMPOSE) down

stream-status:
	$(STREAM_COMPOSE) ps
	@curl --fail --silent --show-error --connect-timeout 2 --max-time 3 http://127.0.0.1:9998/metrics

stream-logs:
	$(STREAM_COMPOSE) logs --tail 100 mediamtx

migrate:
	$(UV) run python tools/migrate.py

gateway:
	$(UV) run uvicorn sensoryplex_gateway.app:create_app --factory --host 127.0.0.1 --port 8090 --no-access-log

runtime:
	$(CARGO) run --locked -p sensoryplex-runtime -- serve

pipeline-check:
	$(CARGO) run --locked -p sensoryplex-runtime -- check config/pipelines/file-material.yaml

runtime-smoke:
	$(CARGO) build --locked -p sensoryplex-runtime
	$(UV) run python tools/smoke_runtime.py

gateway-smoke:
	$(UV) run python tools/smoke_gateway.py

# Real decoding is opt-in: hosts without the GStreamer development files can pass MEDIA_FEATURES=
# and still get the anchors-only report. Acceptance runs build release because a debug build
# spends nearly all its time in unoptimised digests over decoded frames.
MEDIA_FEATURES ?= gstreamer
# Compile gate for the decode path: it needs the GStreamer development files, so it stays out of
# `make check` and runs where that toolchain exists (macOS CI job, Apple Silicon dev hosts).
media-check:
	$(CARGO) clippy --locked -p sensoryplex-media -p sensoryplex-runtime --all-targets --features "$(MEDIA_FEATURES)" -- -D warnings

media-replay:
	@test -n "$(MEDIA)" || { echo "usage: make media-replay MEDIA=/absolute/path/to/authorized-sample.mp4"; exit 1; }
	$(CARGO) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	$(UV) run python tools/verify_replay.py --media "$(MEDIA)"

# SRT 实时接入验收：脚本自己用 GStreamer `srtsink` 直推授权样本（不经 RTMP、不依赖 OBS 空闲），
# 覆盖稳定窗口、断流恢复、无源失败与实时数据面交接。需要 MediaMTX 已启动（make stream-up）。
live-check:
	$(CARGO) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	$(UV) run python tools/verify_live.py $(if $(SAMPLE),--sample "$(SAMPLE)",)

# 背压与队列可观察验收：描述符之后那条有界队列的水位/丢弃/超时/等待时间。
# 四个场景都不依赖 OBS：无消费者时队列必须显式拒绝，有消费者时等待时间才有样本。
backpressure-check:
	$(CARGO) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	$(UV) run python tools/verify_backpressure.py

# 跨进程数据面验收：Runtime 保留字节，独立 Python 进程按 lease 读取、校验并释放。
# 需要真实授权样本，与 media-replay 同一份素材即可。
handoff-check:
	@test -n "$(MEDIA)" || { echo "usage: make handoff-check MEDIA=/absolute/path/to/authorized-sample.mp4"; exit 1; }
	$(CARGO) build --locked --release -p sensoryplex-runtime --features "$(MEDIA_FEATURES)"
	$(UV) run python tools/verify_handoff.py --media "$(MEDIA)"
