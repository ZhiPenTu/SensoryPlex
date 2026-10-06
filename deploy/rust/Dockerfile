# SensoryPlex 核心底座专用 Rust 构建与验证镜像
# 包含完整 GStreamer 开发依赖、Protobuf 编译器与工具链，
# 用于在隔离容器内执行 `orchestration-check`、`media-check`、`check` 等底座验证。
FROM rust:1.88-bookworm@sha256:af306cfa71d987911a781c37b59d7d67d934f49684058f96cf72079c3626bfe0

ENV DEBIAN_FRONTEND=noninteractive \
    CARGO_HOME=/usr/local/cargo \
    CARGO_TARGET_DIR=/target

# 安装编译系统、GStreamer 1.0 开发文件及音视频插件库
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        libglib2.0-dev \
        libgstreamer1.0-dev \
        libgstreamer-plugins-base1.0-dev \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        protobuf-compiler \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 创建 target 缓存目录并赋予权限
RUN mkdir -p /target && chmod 777 /target

WORKDIR /workspace

# 作为 Compose 常驻服务启动，供 exec 快速复用
CMD ["sleep", "infinity"]
