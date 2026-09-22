# 工程初始化验证记录

本记录对应 0.1.0 工程底座，不代表完整 V1 媒体/模型链路验收。

| 验证 | 结果 |
| --- | --- |
| Rust workspace build/test | 6 个 crate 编译成功，4 项测试通过 |
| Rust fmt / clippy | 通过，clippy 使用 `-D warnings` |
| Python lint / format | 通过 |
| Python SDK、Manifest 契约测试 | 17 项通过 |
| PostgreSQL 集成测试 | 3 项通过，使用真实 PostgreSQL 和独立测试 schema |
| Rust / Python gRPC | 启动真实 Rust 进程，由 Python 生成客户端调用成功 |
| Proto 再生成 | 文件内容保持一致；Python runtime/stub 使用 SDK 包命名空间 |
| Pipeline 配置 | File Golden Path 配置结构验证通过 |
| NATS JetStream | 本地监控端点返回有效 JetStream 配置 |
| 基础及可选向量 Compose | 配置验证通过 |
| Gateway 容器构建与启动 | 通过；独立依赖层与 BuildKit uv 缓存已验证 |
| 已部署 Gateway 接口 | 元数据健康、Bearer 查询、未鉴权 401、语义未接入 501 均通过 |
| 容器与工作区代码一致性 | 运行容器的元数据适配器 SHA-256 与当前源文件一致 |

数据库测试覆盖事实写入、重复投递、版本冲突、历史版本保留、事务 outbox 原子性、
错误来源引用回滚、权限隔离、关键词/标签/置信度过滤和半开时间边界。测试 fixture
仅为契约输入，未被导入开发业务库，不代表 ASR/OCR/VLM 结果。

测试依赖当前存在两条第三方弃用提示：Starlette 的 httpx TestClient 兼容层，
以及 anyio BlockingPortal 别名。测试通过，提示未被屏蔽。

未验证范围：真实视频/流处理、AI 推理、GPU/NPU、Milvus 写入与语义检索、素材回跳、
2–5 秒延迟与连续运行指标。CI 文件已建立，但尚未在远端 GitHub Actions 执行。

复查命令：`make check`、`make integration`、`make runtime-smoke`、
`make pipeline-check`、运行容器后的 `make gateway-smoke`。
