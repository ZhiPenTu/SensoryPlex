# 服务边界

已实现 `api/`：在一个 FastAPI 进程中装配 Business、Admin 与 Identity 路由，
共用身份与 PostgreSQL；媒体上传使用独立异步连接池。`gateway/` 是旧导入与启动入口的兼容层，
不再维护独立查询实现。前端位于 `apps/console`，Rust `crates/runtime` 保持独立控制进程。

工程边界见 [ADR-013](../docs/adr/ADR-013-应用API模块化合并与部署边界.md)，
[设计稿](../docs/design/console-mvp.md)保留完整 MVP 目标，
[运行手册](../docs/runbooks/console.md)区分已交付应用准备流程和待接入 Runtime 的动作。
REST 已接线；MCP / 对外查询 gRPC 尚未接线。没有独立 `admin-api` 进程。

以下服务按 ADR 保留逻辑边界，在对应阶段添加可运行包和容器：

| 服务 | 责任 | 依赖 |
| --- | --- | --- |
| media-worker | GStreamer 接入、抽帧、音频切段、Runtime buffer | runtime/media SDK |
| ai-worker | 独立进程模型插件与 execution adapter | Python/Native SDK |
| timeline-worker | 对齐观测、融合、提交素材与 outbox | timeline + metadata adapter |
| index-worker | **已落地**（`services/index-worker`，ADR-020）：embedding 落库、upsert 后读回确认、
  检索命中回查 PostgreSQL 事实（owner/ready/material）、稳定原因码；**无常驻消费**（NATS/outbox 未接线） | model release + material revision |
| storage-adapter | NAS/MinIO 校验写入和引用管理 | storage traits |

不为尚未实现的 worker 创建返回 ready 的空服务。
