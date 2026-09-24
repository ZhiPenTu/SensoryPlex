# SensoryPlex

端侧 AI 多模态素材预处理框架。按现有 ADR 建立 **Rust Core + Python AI SDK +
Protobuf/gRPC + FastAPI** 工程，为 SRT/文件接入、感知、时间轴融合和可溯源检索提供基础。

当前版本 **0.1.0：可运行工程底座**。已实现素材元数据事务写入、不可变 revision、
来源/模型血缘校验，以及带鉴权的关键词、标签、时间范围查询。媒体侧已接入 GStreamer 真实解码
（可选 `gstreamer` feature）：解码 → 有界 arena → `BufferDescriptor` → lease 签发/校验/释放 → 音频 5 秒切段，
并对视频做自适应抽帧（keep/skip 全部带原因），已在授权样本与 10 个公开许可样本上通过回放验收
（其中 6 个用于 ADR-009 格式准入矩阵，出处与许可见 `tests/fixtures/media/OPEN-SAMPLES.md`）。
跨进程数据面（M3）也已落地：`replay --handoff-listen` 把样本留在共享内存里，独立进程按 lease 读取、
校验摘要并显式释放，容量与 lease 生命周期都有上限（见 ADR-010；消费方目前是验收脚本，不是模型 worker）。
SRT 实时接入（M4）同样可用：`ingest` 在有限窗口内拉流、解码并测量断流与恢复，重连归解码元素
（`srtsrc auto-reconnect`），本进程只测量；直播没有已知时长，因此不产出 anchor 区间。
背压指标（M2）与四个端侧模型插件（VLM/ASR/OCR/BGE，ADR-012/016/017）已接入；BGE 向量也已能
落库并检索回来（`services/index-worker`，ADR-020，本机为 Milvus Lite 文件形态）。仍未接入的是
NATS 任务分发、常驻 index-worker 消费，以及网关侧语义检索——`mode=semantic` 仍返回 501，
服务端 Milvus 拓扑在本机 Docker Hub 不可达的情况下未经验收。相关 API 明确报告能力不可用。

## 快速开始

需要 Rust 1.96、Python 3.12、uv 和 Docker Compose v2。目标平台为 `macos-aarch64`
（Apple Silicon，含 Mac mini 端侧部署）与 `linux-x86_64`（NVIDIA 性能主线）；macOS 侧
加速进程需原生运行，容器无法访问 Metal/CoreML。

开发验证默认在容器内执行（`EXEC_MODE=container`，见 Makefile 顶部）。没有本机 compose
栈的环境可以用 `EXEC_MODE=host` 把同一组命令退回主机（`uv run --frozen python` + 主机
`cargo`），例如 `make check EXEC_MODE=host`；远端 CI 的三个 job 都是这样跑的。
没有 PostgreSQL 的主机（如 macOS runner）改跑 `make lint-ruff test-contracts`，
集成测试只由带数据库的 job 覆盖。

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
make media-test
make stream-up && make live-check
```

`make media-test` 只跑解码路径的单元测试（含保留表按种类分配的 A/B 回归），需要 GStreamer 开发文件。

端侧模型插件的验收都是**四个进程**（编排 / 生产者 runtime / 插件 / worker），各需要一份真实授权样本：

```sh
make model-check MEDIA=/absolute/path/to/authorized-sample.mp4    # VLM（本机 ollama）
make asr-check   MEDIA=/absolute/path/to/authorized-speech.webm   # ASR（本机 MLX Whisper）
make ocr-check   MEDIA=/absolute/path/to/authorized-video.webm    # OCR（随包 PP-OCR ONNX）
make embed-check MEDIA=/absolute/path/to/authorized-video.webm    # BGE 文本向量（随包 BGE ONNX，消费上游 OCR 文字）
```

`make ocr-check` 支持 `EXPECT=empty`：无文字样本上"0 块 + `empty_reason`"才是正确结果，
用它证明"模型没找到文字"与"处理失败"可区分；`PROVIDER=coreml` 走 CoreML 执行后端，
拿不到 CoreML 会话会显式失败（不静默退回 CPU，见
[ADR-016](docs/adr/ADR-016-OCR与ONNX执行后端.md)）。

`make index-check EMBEDDINGS=/absolute/path/to/ai-worker.json` 把这份真实向量接进
**落库与检索闭环**（ADR-020）：`services/index-worker` 写进 Milvus 并读回确认后才置 `ready`，
检索命中必须回查 PostgreSQL 的 `ready` + material + `source.owner` 才返回；真实 PostgreSQL
（隔离 schema + 真实迁移）与真实 Milvus Lite 上 11 个场景全过。输入取自
`uv run --frozen python tools/verify_embed.py --media <sample> --keep-workspace` 产出的
`ai-worker.json`。注意 Milvus Lite **是进程独占的**（同一数据目录不能被两个进程同时打开），
服务端形态未验收，见 [ADR-020](docs/adr/ADR-020-向量索引落库与检索闭环.md)。

`make embed-check` 是唯一**不接数据面**的链路：它先跑一遍真实 OCR 产出文字块，再让 BGE 消费这些
文字（`acceptsMemoryKinds: []`，喂字节以 `buffer_reader_not_attached` 明确拒绝），产出维度版本化的
L2 归一化向量。想跳过 OCR、直接复用已有的 `ai-worker.json` 时传
`OBSERVATIONS=/absolute/path/to/ai-worker.json`；`PROVIDER=coreml` 同样可用，但实测**更慢**
（同文本 0.78 ms vs 3.16 ms），见 [ADR-017](docs/adr/ADR-017-BGE文本向量与维度版本化.md)。

macOS 常驻形态（`launchd`）在主机的**用户级** LaunchAgents 中运行，按统一内存分级设置上限
（设计见 [ADR-015](docs/adr/ADR-015-macOS常驻形态与统一内存分级.md)，操作见
[macOS 常驻手册](docs/runbooks/macos-resident.md)）：

```sh
make resident-probe     # 只读：打印分级与上限
make resident-install   # 渲染 plist + launchctl bootstrap
make resident-status    # 两个 job 状态，--verify-endpoint 做一次真实 gRPC 调用
make resident-uninstall
```

`launchd`/`launchctl`/`sysctl` 只存在于 macOS 宿主，所以这一组是明确的"主机例外"，不放进容器目标。

分级表里的**模型并发**那一半由模型 worker 消费（[ADR-021](docs/adr/ADR-021-模型worker按分级并发上限限流.md)）：
`tools/ai_worker.py` 按 `--model-parallelism` / `SENSORYPLEX_MODEL_PARALLELISM` 与运行时转述的分级上限
做准入与在飞调用限流（坏值、来源冲突与越界在连插件之前 exit 2，**不夹取**），报告里的
`model_concurrency` 记实测 `peak_in_flight` 与重试账目——可重试拒绝（`concurrency_limit` /
`deadline_expired`）不再让输入静默消失：

```sh
make parallelism-check MEDIA="/abs/a.webm /abs/b.webm" INPUTS=4
```

宿主**加速器**是另一件事，也不再被混进执行后端那张表（[ADR-022](docs/adr/ADR-022-宿主加速器能力探测与上报.md)）：
`DescribeCapabilities` 的 `host_accelerators` 真去探测宿主（macOS 走 `system_profiler` 与 CoreML
framework，Linux 走 `nvidia-smi`），三态 `available / unavailable / unknown` 不得互相塌陷——
探测工具缺失、超时或读不懂一律落 `unknown` 而**不是**"不存在"。它只回答"这台宿主有没有这块加速器"：
Rust 侧至今没有任何 in-process `ExecutionBackend`，`model_inference` 仍在 `unavailable_capabilities` 里。

```sh
make accelerator-check   # 四路对账：宿主直读 / LANG=zh_CN / PATH=/nonexistent / 与执行后端分离
```

`make media-replay` 需要 GStreamer 开发文件；没有的主机可加 `MEDIA_FEATURES=` 退回纯锚点报告
（解码数据平面保持全零，并在 `blockers` 中声明未实现）。`make handoff-check` 在同一份素材上再跑一次
跨进程数据面验收：Runtime 保留字节，独立 Python 进程按 lease 读取、校验摘要并释放，
越界与过期路径必须给出显式拒绝码。`make live-check` 需要先 `make stream-up`：脚本自己用 GStreamer
`srtsink` 把授权样本直推 SRT（不经 RTMP 转封装、不占用 OBS 会话），再跑四个场景
（稳定窗口、断流恢复、无源失败、实时数据面交接）。

## 素材工作台

已提供独立前端 `apps/console` 和模块化后端 `services/api`，可登录、上传与预览视频、
保存插件配置和处理方案、创建任务草稿、查询真实素材、管理业务凭据和查看审计。
安装执行器、媒体准入与任务执行仍待接入，界面明确显示不可用；不会生成演示模型结果。

```sh
make console-build
make console-prepare
make console-api
```

访问 <http://127.0.0.1:8091>；账户 `admin`，随机密码位于 `.data/console-preview/admin-password`。
预览使用独立 schema，不修改旧 Gateway 数据。开发与部署细节见 [Console 运行手册](docs/runbooks/console.md)。

## 工程结构

```text
proto/                  common / material / runtime / gateway 的版本化契约
crates/                 runtime、media、timeline、storage、execution、sdk
plugins/python/common/  edge_material_sdk 与生成的 Python 消息
apps/console/           React + TypeScript + Vite 素材工作台
services/api/           模块化 Business / Admin / Identity API
services/gateway/       旧 Gateway 导入与启动兼容入口
db/migrations/          只追加的显式 PostgreSQL 迁移
config/pipelines/       文件与 SRT 实时接入的 pipeline 配置
deploy/compose/         本地容器基础设施
deploy/macos/           macOS launchd 常驻模板与分级包装脚本（ADR-015）
tests/                  契约测试、真实 PostgreSQL 集成测试、媒体样本说明
tools/                  配置、代码生成、迁移、测试与真实媒体探测
```

## 设计依据与状态

- [技术选型 ADR 与 V1 实施蓝图](技术选型ADR与V1实施蓝图.md)
- [开放式插件开发文档](开放式插件开发文档.md)
- [素材工作台 MVP 工程设计稿（应用准备流程已实现）](docs/design/console-mvp.md)
- [ADR-021：模型 worker 按分级并发上限限流（准入、重试与账目）](docs/adr/ADR-021-模型worker按分级并发上限限流.md)
- [ADR-022：宿主加速器能力探测与上报（三态、只写真值、与进程能力分离）](docs/adr/ADR-022-宿主加速器能力探测与上报.md)
- [ADR-019：运行时消费分级队列上限（准入，而不是改写）](docs/adr/ADR-019-运行时消费分级队列上限.md)
- [ADR-015：macOS 常驻形态（launchd）与统一内存分级](docs/adr/ADR-015-macOS常驻形态与统一内存分级.md)
- [ADR-014：ASR 插件与音频样本布局契约](docs/adr/ADR-014-ASR插件与音频样本布局契约.md)
- [ADR-013：应用 API 模块化合并与部署边界](docs/adr/ADR-013-应用API模块化合并与部署边界.md)
- [ADR-009：媒体格式支持矩阵与拒绝语义](docs/adr/ADR-009-媒体格式支持矩阵与拒绝语义.md)
- [ADR-010：跨进程数据面的安全边界与可见性](docs/adr/ADR-010-跨进程数据面的安全边界.md)
- [实现状态与后续阶段](docs/implementation-status.md)
- [契约与查询语义](docs/contracts/README.md)
- [初始化验证记录](docs/verification.md)

跨服务消息必须先修改 Proto；模型结果必须携带时间锚点、来源、版本和置信度语义。
控制总线不传媒体 bytes。未知、失败、冲突与降级保持可见，测试 fixture 不能替代
真实视频/流与模型的端到端验收。
