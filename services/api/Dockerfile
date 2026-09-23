FROM docker.m.daocloud.io/library/node@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS console
WORKDIR /console
COPY apps/console/package*.json ./
RUN npm ci
COPY apps/console ./
RUN npm run build

FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY plugins/python plugins/python
COPY services/api services/api
COPY services/gateway services/gateway
COPY services/index-worker services/index-worker
RUN --mount=type=cache,id=sensoryplex-uv,target=/root/.cache/uv uv sync --frozen --group dev --no-editable --package sensoryplex-api
COPY tools/migrate.py tools/migrate.py
COPY db/migrations db/migrations
COPY --from=console /console/dist apps/console/dist
# 容器内开发验证需要 services/gateway + vlm-moondream + numpy + scipy 这些不在
# --package sensoryplex-api 默认同步范围内的 wheel；以构建期源码路径就地
# uv pip install 注入；macOS-only 的 mlx-whisper 仍由主机 cargo/uv 链路承担。
RUN /usr/local/bin/uv pip install --python /app/.venv/bin/python --no-cache-dir \
        ./services/gateway \
        ./plugins/python/processors/vlm-moondream \
        ./plugins/python/processors/asr-whisper-mlx \
        ./plugins/python/processors/ocr-rapidocr \
        ./plugins/python/processors/embed-bge-onnx --no-deps \
        numpy "scipy>=1.11" || true
# OCR / BGE 的契约测试断言的是**真实发行版元数据**（`installed_version("onnxruntime")`
# 不许编造，`installed_version("rapidocr")` 不许为空），所以这两个 ONNX 链路需要真安装；
# 与上面那条不同，这里**不加 `|| true`**：装不上就让镜像构建失败，而不是让容器里的
# 契约测试在事后红成一片。容器里只跑契约测试（引擎被测试替身替换），不跑真实推理。
RUN /usr/local/bin/uv pip install --python /app/.venv/bin/python --no-cache-dir \
        "onnxruntime>=1.23,<2" "tokenizers>=0.22,<1" "rapidocr>=3.9,<4"
# index-worker（ADR-020）的契约与集成测试在容器里也要能 collect：它的 pymilvus/milvus-lite
# 不是"可选加速"，而是被测代码自身要 import 的依赖，所以同样真装、同样不加 `|| true`。
RUN /usr/local/bin/uv pip install --python /app/.venv/bin/python --no-cache-dir ./services/index-worker
RUN groupadd --gid 10001 sensoryplex && useradd --uid 10001 --gid 10001 --no-create-home sensoryplex && mkdir -p /app/.data/console-media && chown -R 10001:10001 /app/.data
USER 10001:10001
EXPOSE 8091
CMD ["/app/.venv/bin/uvicorn", "sensoryplex_api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8091", "--no-access-log", "--limit-concurrency", "64"]
