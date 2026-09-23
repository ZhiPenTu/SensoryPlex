# SensoryPlex

端侧 AI 多模态素材预处理框架。按现有 ADR 建立 **Rust Core + Python AI SDK +
Protobuf/gRPC + FastAPI** 工程，为 SRT/文件接入、感知、时间轴融合和可溯源检索提供基础。

当前版本 **0.1.0：可运行工程底座**。已实现素材元数据事务写入、不可变 revision、
来源/模型血缘校验，以及带鉴权的关键词、标签、时间范围查询。媒体侧已接入 GStreamer 真实解码
（可选 `gstreamer` feature）：解码 → 有界 arena → `BufferDescriptor` → lease 签发/校验/释放 → 音频 5 秒切段，
并对视频做自适应抽帧（keep/skip 全部带原因），已在授权样本与 6 个公开许可样本上通过回放验收。
跨进程数据面（M3）也已落地：`replay --handoff-listen` 把样本留在共享内存里，独立进程按 lease 读取、
校验摘要并显式释放，容量与 lease 生命周期都有上限（见 ADR-010；消费方目前是验收脚本，不是模型 worker）。
SRT 实时接入（M4）同样可用：`ingest` 在有限窗口内拉流、解码并测量断流与恢复，重连归解码元素
（`srtsrc auto-reconnect`），本进程只测量；直播没有已知时长，因此不产出 anchor 区间。
背压指标、模型推理、NATS 任务分发与 Milvus 语义索引尚未接入；
相关 API 明确报告能力不可用。

## 快速开始

需要 Rust 1.96、Python 3.12、uv 和 Docker Compose v2。目标平台为 `macos-aarch64`
（Apple Silicon，含 Mac mini 端侧部署）与 `linux-x86_64`（NVIDIA 性能主线）；macOS 侧
加速进程需原生运行，容器无法访问 Metal/CoreML。

```sh
make setup
make up
```

打开 [API 文档](http://127.0.0.1:8090/docs)。认证 token 在自动生成且被 Git 忽略的
`.env` 中。初始数据库没有业务数据，搜索返回空数组；工程不会自动生成模型结果。

本地源码开发、端口、测试、迁移和回滚见 [开发手册](docs/runbooks/development.md)。
OBS 本机推流可执行 `make stream-up`，配置与接流地址见 [OBS 推流手册](docs/runbooks/obs-streaming.md)。

```sh
make check
make integration
make pipeline-check
make runtime
make media-replay MEDIA=/absolute/path/to/authorized-sample.mp4
make handoff-check MEDIA=/absolute/path/to/authorized-sample.mp4
make stream-up && make live-check
```

`make media-replay` 需要 GStreamer 开发文件；没有的主机可加 `MEDIA_FEATURES=` 退回纯锚点报告
（解码数据平面保持全零，并在 `blockers` 中声明未实现）。`make handoff-check` 在同一份素材上再跑一次
跨进程数据面验收：Runtime 保留字节，独立 Python 进程按 lease 读取、校验摘要并释放，
越界与过期路径必须给出显式拒绝码。`make live-check` 需要先 `make stream-up`：脚本自己用 GStreamer
`srtsink` 把授权样本直推 SRT（不经 RTMP 转封装、不占用 OBS 会话），再跑四个场景
（稳定窗口、断流恢复、无源失败、实时数据面交接）。

## 工程结构

```text
proto/                  common / material / runtime / gateway 的版本化契约
crates/                 runtime、media、timeline、storage、execution、sdk
plugins/python/common/  edge_material_sdk 与生成的 Python 消息
services/gateway/       FastAPI 与 PostgreSQL 元数据适配器
db/migrations/          只追加的显式 PostgreSQL 迁移
config/pipelines/       文件与 SRT 实时接入的 pipeline 配置
deploy/compose/         本地容器基础设施
tests/                  契约测试、真实 PostgreSQL 集成测试、媒体样本说明
tools/                  配置、代码生成、迁移、测试与真实媒体探测
```

## 设计依据与状态

- [技术选型 ADR 与 V1 实施蓝图](技术选型ADR与V1实施蓝图.md)
- [开放式插件开发文档](开放式插件开发文档.md)
- [ADR-009：媒体格式支持矩阵与拒绝语义](docs/adr/ADR-009-媒体格式支持矩阵与拒绝语义.md)
- [ADR-010：跨进程数据面的安全边界与可见性](docs/adr/ADR-010-跨进程数据面的安全边界.md)
- [实现状态与后续阶段](docs/implementation-status.md)
- [契约与查询语义](docs/contracts/README.md)
- [初始化验证记录](docs/verification.md)

跨服务消息必须先修改 Proto；模型结果必须携带时间锚点、来源、版本和置信度语义。
控制总线不传媒体 bytes。未知、失败、冲突与降级保持可见，测试 fixture 不能替代
真实视频/流与模型的端到端验收。
