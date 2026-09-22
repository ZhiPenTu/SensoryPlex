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

### GStreamer 解码、descriptor 交接与音频切段（2026-09-22）

同一平台 `macos-aarch64`，同一授权样本 `video/1.mp4`（SHA-256 `3d94f00fe81b…`）。本切片把锚点路径
换成真实解码：GStreamer 解出帧与 PCM，写入有界 arena，签发真实 lease，校验后再释放，并把音频切成
5 秒段。**仍未接入抽帧、背压指标、SRT、模型与 2–5 秒语义可见性。**

```sh
make media-replay MEDIA=/Users/tuzhipeng/Documents/SensoryPlex/video/1.mp4
```

| 观察 | 结果 |
| --- | --- |
| 结论 | PASS：`descriptors=2246`、`descriptors_validated=2246`、`descriptor_failures=0`、`leases=2246/2246`（无泄漏）、`segments=7` |
| 解码量与锚点一致 | 视频 918 样本、音频 1321 样本，合计 2239，等于 `decoded_items`；锚点路径同源得到 918 + 1319 |
| 计数自洽（校验脚本断言） | `descriptors_built == Σsamples + segments`（2239 + 7 = 2246）；`decoded_bytes == Σtrack.bytes`；`leases_released == leases_issued` |
| 内存边界 | arena 容量 64 MiB，`arena_peak_bytes=2073600`，等于一帧 RGBA 540×960×4；`decoded_bytes=1908975616` 为累计吞吐，不驻留 |
| 交接证据 | 每轨首个 descriptor 记录 `memory_kind=cpu_shared_memory`、`locator.handle=arena-3d94f00fe81b`、`offset=0`、`read_only=true`、`content_hash=sha256:…`；locator 不含任何宿主路径 |
| 音频切段 | 6 个完整 5 秒段（8 84736 B）+ 1 个 102400 B 尾部 partial 段；段字节合计 5410816，等于音频轨字节；相邻段首尾相接（毫秒取整处允许 1 ms） |
| 双路时间轴对齐 | ffprobe 锚点与 GStreamer 解码在呈现时间轴上一致：两轨首个样本均为 0 ms，视频末帧 30600 ms 落在末锚点区间 [30567, 30627) 内 |
| 全量检查 | `make check` 通过（rustfmt + clippy `-D warnings` + workspace 测试 + ruff + 27 项契约测试）；`make integration` 3 项通过；`make runtime-smoke` PASS |
| Rust 单元测试 | `sensoryplex-media` 28 项通过：不含 GStreamer 的 arena/lease/descriptor/segment/probe/source 逻辑，含毫秒取整不切碎连续流、时间洞关闭段而不放宽、格式中途变化拒绝、停滞生产者被上限拦截 |

**根因记录：容器 edit list 与呈现原点。** 该 MP4 视频轨带 edit list
（`edit list 0 - media time: 15000, duration: 2754000`）。ffmpeg/ffprobe 会应用 edit list，因此视频 PTS
从 0 开始；GStreamer 默认保留媒体时间戳，首帧报告 166 ms（15000/90000 s），导致 descriptor 时间轴整体
比锚点晚 166 ms。修复方式是在 pipeline 的 segment event 上取呈现原点并整体减去，而不是给某条轨道打补丁。
修正后视频 `first_pts_ms=0`、`timeline_offset_ms=167`：167 是 166.67 ms 的毫秒取整结果，与 ffprobe 的
`pts_time=0.000000` 对齐；残差小于契约的毫秒粒度，因此记录为偏移而不是"误差"。

**取舍记录：`DISCONTINUITY_THRESHOLD_MS=250`。** AAC 每帧 1024 采样 @44.1 kHz = 23.22 ms，转成毫秒后步长
在 23/24 ms 之间摆动。若按"PTS 必须等于游标"判断断流，连续流会被切成 292 段而不是 7 段；因此只有偏差
超过 250 ms（约 10 帧）才判定为真实时间洞并关闭当前段。段长度硬上限仍按 `2 × segment_ms` 计算并留出
取整余量，停滞的生产者不会让缓冲区无界增长。

**`overlapping_samples=2` 的解释。** 音频末尾 3 帧落在同一毫秒（30604 ms）。解码路径保留其载荷——那是真实
解出的 PCM——但计入 `overlapping_samples`；锚点路径要求区间严格递增，因此把同毫秒的点显式丢弃，
报告为 `drop_reasons=collapsed_interval`、`dropped_items=2`。两处计数描述的是同一现象，不是两次丢失。

**性能记录：** 交接校验最初对同一份载荷做两次 SHA-256，debug 构建下约 83 ms/descriptor；改为一次
`sha256` 生成 + 内存中按字节比对后消除。验收固定使用 `make media-replay` 的 release 构建，debug 构建
几乎全部时间花在未优化摘要上，不作为性能结论。

**未验证范围（不得当作媒体验收）：**

- `blockers` 仍为 `adaptive_sampling_not_implemented` 与 `lease_consumer_not_implemented`：抽帧与背压、
  真实 lease 消费方（模型 worker）未实现，`golden_path_verified` 恒为 false。
- SRT 仍为 `UnavailableSource`；没有 SRT 端点，也没有断流重连验证。
- 模型链路全部未接入：ASR/OCR/VLM/BGE 无实现，CoreML/Metal 后端仍报 `execution_backend_not_implemented`。
- 该样本由 FFmpeg 生成/转码（`encoder=Lavf58.20.100`），不是设备直出；静态投屏、翻页切换、运动/多人对话
  三类样本尚未回放，抽帧覆盖率结论不成立。
- 检查在 `macos-aarch64` 本地完成；macOS CI job 与 `linux-x86_64` 侧解码验收未执行。
- `make media-replay` 不在 CI 中：它需要真实授权媒体，合成片段不能作为验收证据。

### 公开许可样本矩阵：VP9/Opus 与容器差异（2026-09-23）

用户没有现成的三类样本，因此改用 6 个**明确开放许可**的真实素材（Wikimedia Commons，CC BY-SA 4.0 或
公有领域）覆盖静止/翻页/运动/多人对话四类路径。素材本体不入库，出处、许可、摘要与实测特性见
`tests/fixtures/media/OPEN-SAMPLES.md`。这一轮的目标是**用真实素材暴露容器与编码差异**，不是性能结论。

| 样本 | 时长 | 锚点 | 丢弃 | descriptors | leases | 音频段 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `slides-vrt-nodiscussion` | 685.0s | 75351 | 0 | 75488 | 75488/75488 | 137 | PASS |
| `slides-vrt-discussion` | 1125.0s | 123751 | 0 | 123976 | 123976/123976 | 225 | PASS |
| `screencast-video2commons` | 552.0s | 41402 | 0 | 41513 | 41513/41513 | 111 | PASS |
| `screencast-watchlist` | 156.4s | 12510 | 0 | 12542 | 12542/12542 | 32 | PASS |
| `sasebo-basketball` | 60.0s | 4800 | 0 | 4812 | 4812/4812 | 12 | PASS |
| `officehours-panel` | 2232.0s | 165999 | 1380 | 167825 | 167825/167825 | 446 | PASS（修校验口径后） |

计数自洽性在每个样本上成立：`descriptors == Σsamples + segments`、`leases_released == leases_issued`、
`descriptor_failures == 0`、`Σlisted.bytes == 音频轨字节`。Matroska/VP9/Opus 与 HEVC/AAC MP4 走的是同一条
解码 → descriptor → lease → 切段路径。

**这轮暴露的三个真实问题（都不是靠合成片段能发现的）：**

1. **Opus pre-skip 造成起点差异。** `initial_padding=312`（6.5 ms）：ffprobe 把首个音频点放在 12 ms，
   GStreamer 放在 6 ms，相差恰好一个 pre-skip；视频轨完全一致（偏移 0）。原先校验脚本用 1 ms 容差，
   因此 `officehours-panel` 直接失败。处理方式是**按轨道区分容差并把实测偏移打印出来**
   （`start_offsets=[video:+0ms,audio:-6ms]`），而不是把容差整体放宽到看不见问题：166 ms 级别的真实
   错位（edit list 缺陷）仍然会被判失败。
2. **毫秒时间戳下的段边界重叠。** Opus 帧 20 ms，Matroska 用毫秒存储时间戳，段起点会出现最多 14 ms 的
   回退（如 `30060→35060` 之后接 `35046→40046`）。段本身按采样游标切分，因此不是切错；校验脚本改为
   允许一个编解码帧（25 ms）的边界重叠，并保留段不连续阈值（250 ms）拦截真实空洞。
3. **`arena_peak_bytes` 语义需要说清。** `officehours-panel`（**mono** 音频）峰值 1489376 恰等于单帧，
   `sasebo-basketball`（stereo）峰值为 3564864 = 单帧 1639680 + 5 s 段 1925184。原因是 arena 为 bump
   分配 + 空闲链：mono 段（960000 B）能复用单帧释放的区域，stereo 段（1920000 B）不能，于是新增提交。
   该字段是"已提交容量高水位"，不是并发存活字节；语义已写入 proto 与契约文档。

**未验证范围（不得当作完成）：** 样本矩阵只覆盖回放与交接，不含抽帧、背压指标、SRT 与任何模型；
6 个样本都是 CFR，容器级 VFR 与断流重连仍无样本；`officehours-panel` 的 1380 个丢弃全部是毫秒粒度
`collapsed_interval`，与解码路径的 `overlapping_samples` 描述同一现象。剩余样本与模块见 `docs/TODO.md`。
