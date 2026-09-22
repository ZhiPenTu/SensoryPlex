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

## Apple Silicon 能力契约与本地媒体栈（2026-09-22）

本节记录 ADR-008 落地过程的真实验证结果，运行平台为 `macos-aarch64`（macOS 26.5.2，
arm64，M2 Max，32 GB 统一内存）。本次改动只涉及能力上报契约与文档，媒体与模型链路仍未实现。

| 验证 | 结果 |
| --- | --- |
| Proto 契约 | `runtime.proto` 新增 `DescribeCapabilities`、`BackendCapability`、`HostResources`、`CapabilityState`；`make proto` 生成 4 个契约 |
| 生成确定性 | 连续两次 `make proto` 后生成目录内容哈希一致，CI 的 `git diff --exit-code` 前提成立 |
| Rust 单元测试 | `sensoryplex-runtime` 6 项通过：平台标识取自构建目标、每个不可用后端必须给出原因、统一内存与 `unified_memory` 只在 Apple Silicon 上报 |
| Rust lint | `cargo clippy -p sensoryplex-runtime --all-targets --locked -- -D warnings` 通过 |
| 全量检查 | `make check` 通过：rustfmt + clippy + workspace test + ruff + 20 项契约测试（原 17 项） |
| PostgreSQL 集成测试 | `make integration` 3 项通过，真实 PostgreSQL，独立测试 schema |
| 真实 Rust gRPC 进程 | `make runtime-smoke` PASS，输出 `platform=macos-aarch64`；`unavailable_reason` 显式、`unified_memory_bytes` 大于 0 |
| 已部署 Gateway 容器 | `make gateway-smoke` PASS（元数据就绪、鉴权检索、401、未接入能力 501） |
| macOS 媒体栈 | Homebrew `gstreamer 1.28.7` 安装成功（带入 `srt 1.5.7`）：`srtsrc`/`srtsink`（rank primary）、`vtdec`（VideoToolbox）、`avfvideosrc` 可用；`pkg-config gstreamer-1.0` = 1.28.7；共 279 plugins / 1537 features |
| ffmpeg | 默认 `ffmpeg` 随 gstreamer 依赖从 8.1 升到 9.0.1；`tools/probe_media.py` 仍依赖 `ffprobe` |

**未验证范围（不得当作已完成）：**

- macOS CI job（`check-apple-silicon`）尚未在 GitHub Actions 远端执行；本地只在 `macos-aarch64` 上跑过同一条命令序列。
- `gst-inspect-1.0` 报告 2 个 blacklist 文件（`libgstpython.dylib`、`libgstvalidatessim.dylib`）与一条 GLib GIRepository typelib 警告（找不到 `libgobject-2.0.0.dylib`）。SRT 与 VideoToolbox 元素不受影响，但依赖 GObject introspection 的路径需再验证。
- 没有编写任何 GStreamer pipeline 代码：真实 File/SRT 回放、PTS 正确性、lease 生命周期与断流重连仍属第 2 周交付物，本次未产出任何媒体或模型结果。
- CoreML/Metal 后端仍为 `execution_backend_not_implemented`，`DescribeCapabilities` 只报告其不存在与原因，不代表能力可用。
