UV ?= uv
CARGO ?= cargo
COMPOSE = docker compose --env-file .env -f deploy/compose/docker-compose.poc.yml

.PHONY: setup configure proto check test integration format infra up down migrate gateway runtime pipeline-check runtime-smoke gateway-smoke media-replay
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

media-replay:
	@test -n "$(MEDIA)" || { echo "usage: make media-replay MEDIA=/absolute/path/to/authorized-sample.mp4"; exit 1; }
	$(CARGO) build --locked -p sensoryplex-runtime
	$(UV) run python tools/verify_replay.py --media "$(MEDIA)"
