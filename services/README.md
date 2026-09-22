# 服务边界

已实现 `gateway/`：REST、认证、owner 查询过滤，以及 PostgreSQL metadata adapter。
Rust `crates/runtime` 提供独立 gRPC 控制进程入口。

以下服务按 ADR 保留逻辑边界，在对应阶段添加可运行包和容器：

| 服务 | 责任 | 依赖 |
| --- | --- | --- |
| media-worker | GStreamer 接入、抽帧、音频切段、Runtime buffer | runtime/media SDK |
| ai-worker | 独立进程模型插件与 execution adapter | Python/Native SDK |
| timeline-worker | 对齐观测、融合、提交素材与 outbox | timeline + metadata adapter |
| index-worker | embedding、Milvus upsert 与确认、重试 | model release + material revision |
| storage-adapter | NAS/MinIO 校验写入和引用管理 | storage traits |

不为尚未实现的 worker 创建返回 ready 的空服务。
