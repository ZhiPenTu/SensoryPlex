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

## 媒体接入切片：锚点、重排与 lease（2026-09-22）

平台 `macos-aarch64`，探测工具 `ffprobe 9.0.1`。本切片只做时间轴锚点，不做帧解码。

| 验证 | 结果 |
| --- | --- |
| Proto 契约 | 新增 `media/v1/media.proto`：`MediaSourceKind`、`MediaSourceRef`、`MediaTrack`、`MediaSourceDescription`、`TimelineAnchor`、`ReplayReport`；`make proto` 生成 5 个契约 |
| Rust 单元测试 | `sensoryplex-media` 13 项通过：lease 签发/过期/伪造检测/容量上限、ffprobe 帧率与时间戳解析、半开区间构建、间隙计数、B 帧解码顺序重排、未知时长丢弃末帧区间、锚点 id 确定性 |
| 全量检查 | `make check` 通过，契约测试 23 项（原 20 项）；`make integration` 3 项通过 |
| 管道冒烟（非验收） | `/tmp` 临时合成片段（testsrc2 + sine，H.264 B 帧 + AAC，4 秒，320x240）跑 `make media-replay`：293 个锚点（video 120 + audio 173）、`duration_ms=4000`、dropped 0、gaps 0、报告摘要与文件一致、报告字节中不含媒体路径 |
| 诚实性断言 | 校验 `golden_path_verified=false` 且 `blockers` 含 `gstreamer_decode_not_implemented`、`buffer_lease_handoff_not_implemented` |

**未验证范围（不得当作媒体验收）：**

- 上面的冒烟使用合成片段，只证明契约与管道成立；`tests/fixtures/media/` 仍为空，真实授权样本尚未回放。
- 未解码任何帧或音频：没有生成 `BufferDescriptor`，没有签发真实 lease（lease 仅有单元测试覆盖），也没有抽帧、音频切段与背压指标。
- SRT 路径为 `UnavailableSource`，调用即返回 `gstreamer_srt_ingest_not_implemented`，不产生任何锚点。
- 实测中 ffprobe 输出已是呈现顺序，重排计数为 0；重排逻辑仅由 B 帧解码顺序的单元测试覆盖。
- `make media-replay` 未加入 CI：它要求真实授权媒体，合成样本不能作为验收证据。

### 真实样本回放：`video/1.mp4`（2026-09-22）

用户提供的本地样本（`540x960` HEVC 30fps + AAC 单声道，容器时长 30627 ms，918 视频帧 +
1321 音频帧；`encoder` 标记为 `Lavf58.20.100`，即由 FFmpeg 生成或转码，不是设备直出）。
该文件不在 Git 中（`video/` 已加入 `.gitignore`）。

```sh
make media-replay MEDIA=/Users/tuzhipeng/Documents/SensoryPlex/video/1.mp4
```

| 观察 | 结果 |
| --- | --- |
| 结论 | PASS：2237 个锚点，无 `gaps`，无重排，报告摘要与文件 SHA-256 一致，报告字节不含媒体路径 |
| 计数自洽 | `decoded_items=2239` 恰等于 ffprobe 的 918 + 1321；`emitted=2237`、`dropped=2` |
| 丢弃原因 | 2 个 `collapsed_interval`：音频末尾 3 帧 PTS 落在同一毫秒（30604 ms），低于契约毫秒粒度，按规则显式丢弃而非伪造区间 |
| 时间结构 | 视频帧间隔 33/34 ms（611 + 306 次），音频帧间隔 23/24 ms（1024 采样 @44.1kHz），符合 CFR 预期 |
| B 帧重排 | 该样本解码顺序即呈现顺序，重排计数为 0；重排逻辑仍由单元测试覆盖 |

**仍不构成验收的部分：** 本样本只验证时间轴锚点与丢弃语义，不含解码、`BufferDescriptor`、
真实 lease 交接、抽帧、音频切段、ASR/OCR/VLM 与 2–5 秒语义可见性。最后一帧区间以容器时长收口，
因此可能长于该帧的实际显示时长（本例视频末帧 30567→30627 ms）。
