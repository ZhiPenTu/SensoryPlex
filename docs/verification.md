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
2–5 秒延迟与连续运行指标。CI 文件已建立，但尚未在远端 GitHub Actions 执行（本条为当日状态；远端 CI 已于 2026-09-23 实际通过，见文末 M7 一节）。

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

- macOS CI job（`check-apple-silicon`）当时尚未在 GitHub Actions 远端执行；本地只在 `macos-aarch64` 上跑过同一条命令序列。（已过期：2026-09-23 远端 `macos-15-arm64` runner 上真实通过，见文末 M7 一节。）
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
  （本条记录的是当时状态；SRT 接入已于 2026-09-23 落地，`replay` 的拒绝码改为
  `srt_source_requires_ingest_command`，见下文"M4"一节。）
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
  （本条记录的是当时状态；抽帧已于 2026-09-23 接入并从 `blockers` 移除，见下文"M1 自适应抽帧"一节，
  其余判断仍然有效。）
- SRT 仍为 `UnavailableSource`；没有 SRT 端点，也没有断流重连验证。
  （本条记录的是当时状态；该范围已于 2026-09-23 补齐，见下文"M4"一节，其余判断仍然有效。）
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

### 媒体格式准入：静默降级实测（2026-09-23）

为 ADR-009（媒体格式支持矩阵与拒绝语义）提供依据，在 `macos-aarch64`（GStreamer 1.28.7、FFmpeg 9.0.1）
上做了三条探针。三条都在**现有解码链路原样复现**（`decodebin` + 与 `decode.rs` 相同的 capsfilter），
目的不是跑通格式，而是证明"当前没有准入判据"。

**1. 10-bit HEVC 被静默降成 8-bit，链路成功退出。**

```console
$ ffmpeg -f lavfi -i testsrc2=size=320x240:rate=10:duration=2 -c:v libx265 \
    -pix_fmt yuv420p10le /tmp/p10.mp4
$ ffprobe -show_entries stream=codec_name,profile,pix_fmt -of csv=p=0 /tmp/p10.mp4
hevc,Main 10,yuv420p10le
$ GST_DEBUG=GST_ELEMENT_FACTORY:4 gst-launch-1.0 -v filesrc location=/tmp/p10.mp4 \
    ! decodebin ! videoconvert ! 'video/x-raw,format=RGBA' ! fakesink sync=false
creating element "h265parse"
creating element "vtdec_hw"
audio/x-raw(memory:GLMemory), format=(string)NV12
video/x-raw, format=(string)RGBA
```

源是 Main10，产物是 8-bit `NV12` → `RGBA`，没有任何拒绝码，`DecodedTrackStat.pixel_format` 只会记录
`RGBA`。**这是静默降级，属于 AGENTS.md 禁止的行为**（合成素材仅用于证明拒绝路径缺失，不作正样本）。

**2. 无法识别的 pad 只写日志。** `crates/media/src/decode.rs` 对非 `video/`、`audio/` 前缀的 pad 走
`tracing::warn!("decoded pad ignored: unsupported media type")`，`ReplayReport.blockers` 与
`DecodedTrackStat.drop_reasons` 都不会出现该事件，报告读起来像"源里本来就没有这条轨道"。

**3. 多声道原样透传。** 合成 5.1 AAC 源：

```console
$ gst-launch-1.0 -v filesrc location=/tmp/s51.mp4 ! decodebin ! audioconvert \
    ! 'audio/x-raw,format=F32LE' ! fakesink sync=false
audio/x-raw, format=(string)F32LE, rate=(int)48000, channels=(int)6
```

音频 capsfilter 不约束声道数，6 声道进入数据平面。v1 按 mono/stereo 承诺，多声道属于未声明地带。

**同时实测到解码器选择随平台变化**（`h265parse` → `vtdec_hw`，`avdec_aac`），且本机
`gst-inspect-1.0 avdec_hevc` 不存在：macOS 的 HEVC 覆盖来自 VideoToolbox。Linux 侧解码器可用性
未验证，不得外推。本机 FFmpeg 为 `--enable-gpl` 构建（含 libx264/libx265），发布产物需核对。

**这轮没有产生任何格式支持结论。** ADR-009 的矩阵尚未实现：`capability.rs`、几何/位深契约字段、
解码器元素上报都还是待办，`golden_path_verified` 继续为 false。

### M1 自适应抽帧：接线与真实样本覆盖率（2026-09-23）

抽帧从"只有内核"接到了真实解码路径：视频帧在**进入 arena 之前**做判定，判定结果只有两种——
keep（首帧 / 内容变化 / 静止心跳）或带原因的 skip（速率上限 / 尚未变化 / PTS 非单调 / 签名缺失）。
被跳过的帧不交接，但一定计数；轨道上的 `dropped_samples` 也包含这些跳过，两处是同一批帧的两个视角。
`adaptive_sampling_not_implemented` 已从 `blockers` 移除。

策略为默认值：`min_interval_ms=1000`、`static_hold_ms=5000`、`change_threshold=8`（8x8 亮度签名，
平均绝对差 ≥ 8 视为内容变化）。真实样本回放（`--report` 报告可复核，`--verify-only` 可重放校验）：

| 样本 | 时长 | 观测视频帧 | 保留 | 保留率 | 首帧 | 内容变化 | 静止心跳 | rate_limited | no_change_yet | max_gap_ms | 速率上界 | descriptors |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `video/1.mp4` | 30.6s | 918 | 26 | 2.83% | 1 | 25 | 0 | 727 | 165 | 2867 | 31 | 1354 |
| `screencast-watchlist` | 156.4s | 4691 | 41 | 0.87% | 1 | 15 | 25 | 1171 | 3479 | 5000 | 157 | 7892 |
| `sasebo-basketball` | 60.0s | 1799 | 32 | 1.78% | 1 | 30 | 1 | 909 | 858 | 5005 | 60 | 3045 |
| `screencast-video2commons` | 552.0s | 13800 | 132 | 0.96% | 1 | 39 | 92 | 3168 | 10500 | 5000 | 552 | 27845 |
| `slides-vrt-nodiscussion` | 685.0s | 41100 | 145 | 0.35% | 1 | 17 | 127 | 8555 | 32400 | 5000 | 685 | 34533 |
| `slides-vrt-discussion` | 1125.0s | 67500 | 233 | 0.35% | 1 | 17 | 215 | 13747 | 53520 | 5000 | 1125 | 56709 |
| `officehours-panel` | 2232.0s | 55794 | 470 | 0.84% | 1 | 36 | 433 | 11270 | 44054 | 5000 | 2232 | 112501 |

**结论都是可复核的，不是"看起来合理"：**

1. **覆盖率的分子分母都有出处。** 分母是 `SamplingReport.observed`（解码器实际交给采样器的帧数），
   而且与 ffprobe 路径的视频锚点数**逐样本相等**（7 个样本 diff=0，校验脚本已把它写成断言）。
   保留率随内容类型单调：静止投屏 0.35% < 翻页 0.87–0.96% < 高速运动 1.78% < 手机竖屏短视频 2.83%。
2. **静止心跳是按 hold 触发的，不是碰巧。** 4 个静止样本的 `max_gap_ms` 精确等于 `static_hold_ms=5000`；
   运动样本 `sasebo-basketball` 为 5005（hold + 一个观测到的帧间隔），`video/1.mp4` 为 2867（变化更频繁）。
   校验脚本按 `hold + max_frame_interval_ms` 判定上界，而不是放宽一个凭感觉的容差。
3. **保留数由内容决定，不由速率上限决定。** 每个样本的 `kept` 都远低于 `max_keeps_bound`
   （如 `officehours-panel` 470 vs 2232）；速率上限只是护栏，不是抽样目标。
4. **跳过原因全部显式。** 真实样本上只出现 `rate_limited` 与 `no_change_yet`；
   `missing_signature`、`non_monotonic_pts` 由单元测试覆盖，没有在真实样本上凭空消失。
5. **计数自洽。** `observed == kept + Σskipped`、`kept == 视频轨 samples`、
   `Σ track.samples + Σskipped == decoded_items`（与锚点路径对齐）、视频轨 `dropped_samples == skipped`
   （说明没有采样前的丢弃）。`descriptors == Σsamples + segments` 与 lease 收支保持成立。
6. **交接量确实下降。** `slides-vrt-discussion` 视频交接从 67500 降到 233（-99.7%），总 descriptor
   从 123976 降到 56709；同时 `arena_peak_bytes` 不变（3564864 / 1489376）——峰值由单帧加整段音频决定，
   抽帧改变的是流量，不是峰值。这一点也说明"抽帧省内存"目前没有证据，不能这样说。

**顺带修掉一个内核缺陷：** `last_pts_ms` 原先只在 keep 分支推进，导致（a）一次 skip 之后的乱序帧会被
判为"顺序正常"，（b）帧间隔统计退化成 keep 间隔，使 gap 上界无法复核。现在每个被观测的帧都会推进游标，
并新增 `max_frame_interval_ms` 与 `observed_span_ms` 供报告复核。

**契约新增：** `SamplingReport`（策略参数 + 观测/保留/跳过计数 + gap 与上界）随 `DecodedDataPlane.sampling`
上报；`DecodedTrackStat.samples` 明确为"已交接的样本数"（视频即保留数），`last_end_ms` 明确为
"该轨解码到哪里"（抽帧不会缩短它）。

**未验证范围（不得当作完成）：** 覆盖率是**帧数**口径，不是语义口径。被跳过的帧是否真的没有携带
OCR/ASR/VLM 需要的信息，只有接入模型（M8）之后才能验证；当前策略是启发式，不能据此声称"抽帧不丢语义"。
6 个样本仍是 CFR，VFR 与断流重连样本仍缺（见 `docs/TODO.md`）。

### M3 跨进程数据面：lease 消费方与真实交接（2026-09-23）

在这一轮之前，lease 只是"签发后立刻在同一个进程里释放"：账面对得上，但没有真实交接，
`ReplayReport.blockers` 一直挂着 `lease_consumer_not_implemented`。本轮把它做成**三个进程**的数据面，
blocker 也随之移除。

**结构（三者必须是不同进程，否则验收没有意义）**

1. `tools/verify_handoff.py`（编排 + 对账）：解析生产者输出、起消费者、核对两侧计数。
2. `sensoryplex-runtime replay … --handoff-listen 127.0.0.1:PORT`（生产者）：解码后把样本留在 POSIX
   共享内存里，按 lease 授权窗口，最后打印 `handoff_ready` / `handoff_stats` 并向 `ReplayReport`
   写 `handoff_state`。
3. `tools/handoff_worker.py`（消费者）：**独立 Python 进程**，不参与解码、不知道媒体文件，
   只能通过 gRPC 拿到段名与窗口；`shm_open` + `mmap` 段，校验摘要后显式 `Release`。

消费者进程与生产者进程 PID 不同这条是断言，不是描述；段名每次运行随机派生
（`/sp.<12 位十六进制>`），无法从报告里的 arena handle 推导。

**真实执行结果（`uv run python tools/verify_handoff.py --media <样本>`，两个场景各跑一次）**

| 样本 | 场景 | 保留表上限 | arena | offered | retained | rejected | released | expired | full_retention |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `video/1.mp4` | large_bounds | 4096 | 256 MB | 1347 | 1347 | 0 | 1346 | 1 | true |
| `video/1.mp4` | bounded_backlog | 6 | 128 MB | 1347 | 6 | 1341 | 5 | 1 | false |
| `sasebo-basketball` | large_bounds | 4096 | 256 MB | 3033 | 3033 | 0 | 3032 | 1 | true |
| `sasebo-basketball` | bounded_backlog | 6 | 128 MB | 3033 | 6 | 3027 | 5 | 1 | false |
| `officehours-panel` | large_bounds | 4096 | 256 MB | 112055 | 4096 | 107959 | 4095 | 1 | false |
| `officehours-panel` | bounded_backlog | 6 | 128 MB | 112055 | 6 | 112049 | 5 | 1 | false |

`offered` 与报告对得上：`video/1.mp4` 的 `Σtrack.samples = 1347`（26 保留视频帧 + 1321 音频样本），
`officehours-panel` 为 112055（470 + 111585）；两者的 `descriptors - Σtrack.samples` 分别是 7 与 446，
即各自的音频段数（前者 6 段 + 1 个尾部 partial 段）。
`large_bounds` 在 `officehours-panel` 上确实放不下整批（112055 > 4096），因此它不是"必然全保留"的断言：
脚本要求所有拒绝都必须是**容量原因**，而不是无条件的零拒绝。

**结论都是可复核的，不是"看起来跑通了"：**

1. **两条恒等式 + 无悬挂 slab。** 每个场景都断言 `retained_total == retained + released + expired`
   与 `offered == retained_total + retain_rejections`，并断言结束时 `arena_live_slabs == 0`。
2. **越界不夹取。** 消费者请求跨到相邻 buffer 的窗口得到 `mapping_out_of_range`，
   只给 `offset` 不给 `length`（或反之）得到 `ambiguous_window`；生产者侧的 `request_rejections > 0`
   与消费者侧拒绝计数必须一致（两处独立统计）。
3. **摘要绑定窗口。** 整条 buffer 的 `content_hash` 与 `Acquire` 返回的窗口摘要是两个值；
   脚本在至少两条视频 buffer 上验证过子窗口摘要与整条不同，且内容与 mmap 读到的字节一致。
   不足两条视频 buffer 时该检查记 `sub_window_tested=false`，不会假装做过。
4. **lease 生命周期有出口。** 故意让一条 lease 以 50 ms TTL 过期：过期后 buffer 被回收，
   迟到的 `Release` 得到 `unknown_or_released_lease`，同一个 buffer 再次 `Acquire` 得到 `unknown_buffer`。
   `expired == 1` 是断言，其余 buffer 必须显式释放（`released == leases_issued - expired`）。
5. **有界容量是显式拒绝，不是堆积。** `bounded_backlog` 把保留表钉在 6：
   拒绝数 1341 / 3027 / 112049，原因全部是 `handoff_backlog_full`，进程内存不随样本时长增长。
6. **诚实性。** `ReplayReport.handoff_state` 在带 `--handoff-listen` 时是 `exposed_on=127.0.0.1:<port>`，
   不带时是 `not_exercised`——一次没起消费方的 replay 不会被读成"数据面已验证"。
   `verify_replay.py` 已把 `EXPECTED_BLOCKERS` 收紧为空集，并断言 `handoff_state == not_exercised`；
   `golden_path_verified` 仍为 false。

**顺带修掉三个真实缺陷（都是被这轮验收逼出来的）：**

- `Arena::read(offset, len)` 原先要求 `offset` **恰好等于** slab 起点，子窗口一律读不到 —— 交接窗口天生是子窗口。
  现在按 slab 区间做包含判定（`containing_slab`）。
- `BufferHandoff::expire()` 先做 lease 反查再回收，顺序反了，导致"buffer 挂着已失效 lease 永不释放"。
  现在先回收再清 lease。
- 窗口合法性原先排在 `buffer_already_leased` 之后，导致非法窗口会掩盖"已被领走"这个更准确的原因；
  现在先判窗口。

**契约新增：** `media/v1/handoff.proto`（`BufferHandoffService`：`List`/`Stats`/`Acquire`/`Release`，
`RetainedBuffer`、`HandoffStats`、`AcquireBufferRequest{offset,length,ttl_ms}`），
`ReplayReport.handoff_state`，以及 Runtime CLI 的 `--handoff-listen` / `--handoff-arena-bytes` /
`--handoff-retained-limit` / `--handoff-ttl-ms` / `--handoff-wait-timeout-ms` / `--handoff-idle-timeout-ms`。
契约测试 `tests/contracts/test_handoff_contract.py` 6 项通过（`make check` 的契约测试总数 35 项）。

**复现命令：** `make handoff-check MEDIA=/absolute/path/to/authorized-sample.mp4`（先构建 release runtime）。

**未验证范围（不得当作完成）：**

- **同 UID 进程之间没有逐 buffer 内存隔离**：lease 约束的是"该不该读那个窗口"，不是"能不能读到字节"，
  消费者是受信组件。这一条由消费者以 `same_uid_segment_visibility` 显式上报，安全边界见
  [ADR-010](adr/ADR-010-跨进程数据面的安全边界.md)。
- 数据面只在本机有意义（共享内存不可跨主机映射），`--handoff-listen` 只接受回环地址；
  跨机要走别的传输方式，本轮没有做。
- 消费方是**验收脚本**，不是模型 worker：ASR/OCR/VLM/BGE 仍未接入，语义可见性仍无证据。
- 未验证跨平台：全部在 `macos-aarch64` 完成，Linux/x86_64 侧（`docs/TODO.md` M6）未验收。
- 未验证长时间运行下的段泄漏与反复 replay 的清理行为；未验证进程被强杀后段名残留的表现。

### M4 SRT 实时接入：真实直推、断流恢复与实时数据面（2026-09-23）

接入层是 MediaMTX 1.21.1（Compose 项目 `sensoryplex-stream`，端口只绑回环，见
[OBS 推流手册](runbooks/obs-streaming.md)）。这一轮的发布端**不由 OBS 提供**：验收脚本自己用
GStreamer `srtsink` 把登记在册的授权样本直推出去，所以既没有 RTMP 转封装，也不占用 OBS 会话。

发布端（`tools/verify_live.py` 内建，等价命令）：

```sh
gst-launch-1.0 -e filesrc location=video/samples/screencast-video2commons.480p.vp9.webm \
  ! decodebin name=d \
  d. ! queue ! videoconvert ! videoscale ! video/x-raw,format=I420 \
     ! x264enc tune=zerolatency speed-preset=ultrafast key-int-max=60 bitrate=2500 \
     ! h264parse ! queue ! mux. \
  d. ! queue ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2 \
     ! avenc_aac bitrate=128000 ! aacparse ! queue ! mux. \
  mpegtsmux name=mux ! srtsink uri=srt://127.0.0.1:8890 streamid=publish:live/obs
```

消费端是 Runtime 的 `ingest` 命令；URI 只从环境变量读，不进命令行与报告：

```sh
SENSORYPLEX_SRT_LIVE_URI='srt://127.0.0.1:8890?streamid=read:live/obs' \
  target/release/sensoryplex-runtime ingest config/pipelines/srt-live.yaml \
  --report /tmp/live.pb --duration-ms 12000
```

**接入层探测（真实命令，不是推导）：**

| 探测 | 结果 |
| --- | --- |
| SRT 发布 | `streamid=publish:live/obs` 被接受（配置为 `authInternalUsers: user: any`）；`read:` 不需要凭据 |
| URI 形式 | `srtsrc uri="srt://127.0.0.1:8890?streamid=read:live/obs"` 可用，query 参数被元素解析 |
| 断流 | 发布端 SIGINT 后 `paths{name="live/obs",state="notReady"}`，读者停止收到样本 |
| 恢复 | `srtsrc auto-reconnect=true` 的**同一进程**在发布端回来后自动续上；Runtime 只测量，不控制重连 |
| 无源 | 没有发布者时 `read:` 连不上；`ingest` 跑满窗口后以 `live_window_produced_no_samples` 失败（exit 1） |

**四个场景（`make live-check`，macOS arm64 + MediaMTX arm64 容器）：**

| 场景 | 窗口 | samples | descriptors | stalls | stalled_ms | max_stall | pts_gap | recovered | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| steady | 12 000 ms | 963 | 664（0 失败） | 0 | 0 | 0 | 0 | false | PASS |
| stall_recovery | 20 000 ms | 1162 | 839（0 失败） | 1 | 4723 | 4723 | 4722 | true | PASS |
| no_source | 1 500 ms | 0 | 0 | 1（`stream_gap_at_window_end`） | 1502 | 1502 | -1（未知） | false | 必须失败，exit 1 |
| live_handoff | 8 000 ms | 692 | 493（0 失败） | 0 | 0 | 0 | 0 | false | PASS |

断流场景的实测明细：发布端在窗口第 6 秒被 SIGINT、第 9 秒重新拉起，事件记为
`started=7428ms ended=12151ms gap=4723ms pts_jump=4722ms reason=stream_gap`；
`reconnect_owner=srtsrc auto-reconnect`。重连由解码元素负责，本进程没有自己的重连逻辑，
因此报告里只有"测到断流 + 测到恢复"，没有"我重连了几次"这种无法观察的断言。

`live_handoff` 与 M3 是同一条数据面：`--handoff-listen 127.0.0.1:<port>` 下由
`tools/handoff_worker.py`（独立进程，63 项检查通过）按 lease 读取并释放，`handoff_stats` 显示
`consumer_seen=true`、`retained=0`、`arena_live_slabs=0`，且
`released + expired + retained == retained_total`（32 == 32）。

**诚实性断言（脚本逐项检查）：**

- `golden_path_verified` 恒为 false；稳定窗口 `blockers` 为空，无源窗口为 `live_window_produced_no_samples`；
- 报告原始字节、Runtime stdout 与命令行里都不出现 URI、`streamid` 或 `127.0.0.1`（URI 只经环境变量传入）；
- 直播没有已知时长：`source.duration_ms == 0`；直播不可复现：`content_hash == ""`，不用占位摘要；
- 只列出真实观测到的轨道（video 854x480、audio 48 kHz/2ch，`timing_known=true`）；
  `codec` 留空表示未知——容器编码名尚未采集，属 M9 的采集范围；
- 没有样本的窗口不是成功：`samples == 0` 即 exit 1 并写明原因。

**顺带修掉一个真实缺陷：** `GstDecoder::open_live` 原先在 `gst::init()` 之前解析 `srtsrc`；
外部插件需要插件注册表，未初始化时直接 panic（`GStreamer has not been initialized`，
实测于 `crates/media/src/decode.rs`）。现在 `open_file` / `open_live` 在创建元素**之前**初始化。
直播路径另用 3 秒状态变更上限（直播不做 preroll，pad 是异步出现的），无源时不再白等文件路径的 15 秒。

**复现命令：** `make stream-up && make live-check`。脚本在检测到已有发布者（例如正在直播的 OBS）时
直接拒绝运行，不去挤掉别人的会话。

**未验证范围（不得当作完成）：**

- 用户自有采集端（OBS）的 SRT 直推已于同日实测，见下文"M4+ OBS 自有采集端直推与视频时长缺口"；
  Mac mini、以及"发布端与消费端不同机"仍未验证（本轮两者都在同一台 Mac 上）。
- SRT 加密（`passphrase` / `pbkeylen`）与"需要凭据的 publish"未验证：当前 publish 走 `user: any`。
- 只在本机回环与 `macos-aarch64` 上验收；`linux-x86_64` 侧未执行。
- 直播没有 anchor 区间（没有已知时长），M1 的覆盖率口径在直播下只有 `sampling.observed/kept` 数字。
- 未验证小时级长直播的段清理、arena 碎片化与断流次数上限，也未验证连续多次断流。
- 未验证 VFR、设备直出与 720p 屏幕文字的直播样本。
- 运行时解码仍会出现一条 macOS GL 警告（`GStreamer-GL-WARNING ... NSApplication`）：VideoToolbox
  解码出 GLMemory，`videoconvert` 负责下载。实测不影响结果（0 descriptor 失败），但无头常驻形态
  （M5）下是否稳定未验证。

### M4+ OBS 自有采集端直推与视频时长缺口（2026-09-23）

上一节的发布端是脚本；这一节记录**用户自己的 OBS** 直推 SRT，以及它暴露出的一个真实缺陷。

**OBS 侧填写（用户实测通过的形态）：** 设置 → 直播 → 服务=自定义，服务器
`srt://127.0.0.1:8890?streamid=publish:live/obs`，串流密钥**留空**（见
[OBS 推流手册](runbooks/obs-streaming.md)）。

**先记下两次失败形态（都是 MediaMTX 的真实回执，不是猜测）：**

| 发布端配置 | MediaMTX 日志 | 结论 |
| --- | --- | --- |
| `srt://127.0.0.1:8890?streamid=publish:live` + 密钥 `obs` | `closed: invalid stream ID` / `no stream is available on path 'live/obs'` | streamid 必须精确写成 `publish:live/obs` |
| 同上，去掉 `?streamid=` 只留服务器地址 | `closed: path 'live' is not configured` | 接入层只登记了 `live/obs` 一个路径，不会自动创建其他路径 |

**接入侧实测：** `paths{name="live/obs",state="ready"}`、日志
`[SRT] [conn …] is publishing to path 'live/obs'`，2 条轨道（H264、MPEG-4 Audio）；
`ingest` 20 秒窗口得到 `samples=1586`、`descriptors=990`（0 失败）、`leases_released=990`、
`stalls=0`、`pts_gap_total=0`、`ended_by_deadline=true`、`blockers` 为空、
`golden_path_verified=false`，报告原始字节里没有 URI/`streamid`/`127.0.0.1`。

**发现的缺陷（真实输入才暴露）：** 同一次接入里视频轨 **0 帧**，`dropped_samples=601`、
`drop_reasons=['duration_unavailable']`。根因不是接入层丢包，而是**编码器不带 timing**：
OBS 的 Apple VideoToolbox H.264 直推过来后，接收端 caps 只有
`video/x-h264, stream-format=(string)byte-stream`（没有 framerate），因此 `h264parse` 不给
buffer 设置 duration；旧实现在 `DecodeSession::accept()` 里"没有正时长就丢"，等于按编码器
实现差异把整条视频轨丢掉。MP4 回放与 x264 脚本发布端都带 timing，所以之前没有暴露。

**修复（`crates/media/src/decode.rs` + `proto/media/v1/media.proto`）：** 驱动入口改为
`push()`：时长缺失但时间戳可用的样本按轨道挂起一个，由**同一轨下一个样本的 PTS 差分**补出时长
（真实测量值）；差分 ≤ 0、超过 5000 ms、以及窗口结束时仍未补出的样本都**显式丢弃并记原因**。
新增 `DecodedTrackStat.duration_derived_samples` 区分"容器声明"与"差分推导"，两者不许混算。

**修复后对同一路真实 OBS 流复测（20 秒窗口）：** video `samples=4`、`1280x720 RGBA`
（3.6 MB/帧）、`duration_derived_samples=4`、`descriptor_failures=0`；source tracks 现在报出
`video 1280x720, timing_known=true`；596 次丢弃里 594 次是抽帧跳过（`rate_limited=117`、
`no_change_yet=477`——现场画面基本静止，心跳 5 秒），只有 2 次与时长有关
（`duration_delta_nonpositive`、`duration_unresolved_at_end` 各 1）。audio `samples=1004`、
48 kHz/2ch、5 个段（1 个 partial）。视频只留 4 帧是抽帧策略的结果，不是丢帧。

**回归覆盖：** 新增 6 个单测（`crates/media/src/decode.rs` 的 `decode::tests`，59 个 media
测试全通过）覆盖"差分补时长/自带时长不计入推导/超范围丢弃/非正丢弃/窗口末尾挂起/下一个样本
无时间戳"；`tools/verify_live.py` 新增 `videotoolbox_video` 场景（用 OBS 同类的 `vtenc_h264`
直推），把"整条视频轨消失"钉成断言。

**五场景复跑（`make live-check`，OBS 停流后，2026-09-23）：** 五个场景全部 PASS。新增的
`videotoolbox_video` 自己的发布端**同样不带 timing**——`video samples=3`、
`duration_derived_samples=3`（三帧全部由 PTS 差分定时，修复前这一整轨会被丢掉）、854x480、
`duration_unavailable` 不再出现。其余场景：steady `samples=960` / `descriptors=662`（0 失败）；
stall_recovery `samples=1158`、`stalls=1`、`stalled_ms=4797`（`started=7432ms ended=12229ms`）、
`recovered=true`；no_source exit 1（`live_window_produced_no_samples`，`samples=0`）；
live_handoff `released+expired+retained=32 == retained_total=32`、独立消费者 63 项检查通过。
同一脚本连跑两次的 `samples` 会差百分之几（发布端不是实时 paced），属吞吐差异，不是数据面差异。

**仍未验证（不得当作完成）：**

- 采集端只测了本机 OBS；Mac mini 与跨机部署、OBS 之外的采集端未验证。
- `duration_delta_nonpositive` 在真实流里出现过 1 次（PTS 重复或非单调）：当前按丢弃处理并计数，
  但没有针对 B 帧重排序的专门验证。
- SRT 加密（`passphrase` / `pbkeylen`）与带凭据的 publish、`linux-x86_64` 侧仍未验证。

### M2 背压与队列可观察：直播实测、两条恒等式与按种类分配（2026-09-23）

本节记录 M2 的验收：把"等待、峰值、丢弃、超时"以**计数 + 原因**暴露，并用**用户自己的 OBS 直播**
作为真实输入。契约见 `proto/media/v1/media.proto` 的 `BackpressureReport`（装配于
`DecodedDataPlane.backpressure`），语义写进 [契约文档](contracts/README.md)，决策见
[ADR-011](adr/ADR-011-保留窗口按种类分配.md)。**本轮新增了一个真实缺陷的修复**（见下），
不是"补一个指标字段"。

**报告给出的三条有界队列**（`name[unit]=current/peak/capacity`）：`handoff_retained_table[items]`
（保留表总深度）、`handoff_retained_kind[items]`（**最深的单一种类**）、`handoff_arena_bytes[bytes]`
（共享段已用）。外加 `state`（`ok|degraded|saturated`）、`degraded_entries`/`saturated_entries`、
按原因与按种类的丢弃、`timeouts_total`/`residency_*` 的 lease 等待时间、`sampling_throttled_samples`。
`observed=false` 表示**这次运行没有可测量的有界队列**（例如未暴露数据面的 replay），
此时其余字段无意义——**不得读成"压力为零"**。

**两条恒等式**（消费者与编排脚本各算一遍，只报"健康"不算证据）：

```
dropped_total == Σ drop_reasons[].count == Σ drop_kinds[].count   # 每个丢弃都有原因，且能按种类读出来
retained_total == retained + released_total + expired_total        # 每条保留的 buffer 都有归宿（ADR-010）
```

**实测暴露的真实缺陷（本轮修掉）：** 保留表是**所有 buffer 种类共用**的一张 FIFO。实时流里音频
按 ~47 Hz 产生 PCM 段，视频经抽帧后 keep 只有几 Hz，先到的种类几个周期就把整张表占满。
修复前 10 秒 OBS 直播窗口（`retained_limit=32`）实测：`retained=32/32` **全部是 `audio_pcm`**、
消费者 `video_buffers=0`、`handoff_worker.py` 退出码 1（`listing.has_video` 失败）、
`dropped=502` 且 `drop_kinds=audio_pcm:500,video_frame:2`。这不是单进程不变量能发现的：
"表满了"本身并不违反任何既有约束，只有真实直播输入才把它暴露出来。

**修复（ADR-011 的核心决策）：** 保留表分两层上限——总上限 `retained_limit` 之外，单一种类上限
`retained_kind_limit = max(1, retained_limit / 2)`，到配额以独立拒绝码 `handoff_kind_quota_full` 拒绝
（**在** `handoff_backlog_full` **之前**判，两种有界行为在报告里可区分）。`HandoffStats` 增加
`retained_kind_limit` / `retained_by_kind` / `retained_kind_peak` 说明窗口由谁组成。

**修复后同一路 OBS 直播复测（2026-09-23，10 秒窗口，默认 `retained_limit=32`）：**

- 消费者**真的拿到了视频**：`video_buffers=3 audio_buffers=16`，独立进程
  `handoff worker ok: 70 checks`（退出码 0）。
- 三条队列自证有界：`handoff_retained_table[items]=19/19/32`、`handoff_retained_kind[items]=16/16/16`
  （= 32 的一半，说明第二个种类开始就被配额挡住，而不是把整张表吃干）、
  `handoff_arena_bytes[bytes]=11190272/11190272/67108864`。
- 账目：`dropped=544 reasons=544 kinds=544`，原因只有容量类 `handoff_kind_quota_full`；
  `state=saturated`、`degraded=1 saturated=1`；`throttled=198`（**降级先于拒绝**：先按
  `throttle_factor=4` 放大采样间隔，再拒保留）；`retained=0`、`arena_live_slabs=0`、
  `released=18`、`residency_samples=19 max_ms=11635`；
  `retained_kind_limit=16`（报告与账目一致）、`retained_kind_peak=16`（未越界）、
  `released+expired+retained=19 == retained_total=19`。
- 同轮 `steady` 场景：`samples=979`、`descriptors_validated=625`（0 失败）、`stalls=0`、
  `handoff_state=not_exercised`，报告如实写 `backpressure observed=false state=ok` 而不是一组干净的零。

**不依赖 OBS 的回归（`uv run python tools/verify_backpressure.py`，4/4 PASS）：**

| 场景 | 关键结果 |
| --- | --- |
| `motion_queue_saturates` | `table=8/8/8`、`kind=4/4/4`、`dropped=2208`（两种原因）、`throttled=1318`、`degraded=1 saturated=1`，被拒的里面有视频帧（`video_frame:11`） |
| `static_still_fills` | 静止段也会填满有界队列：`dropped=2513`、`kinds=audio_pcm:2496,video_frame:17`，keep 全部有心跳原因 |
| `consumer_measures_wait` | 消费者跟得上时 `dropped=0`、`state=ok`、`released=455`、`residency_samples=456`、`retained_peak=456`，三条队列 `4096/2048/512MiB` |
| `no_retention_control` | 没有保留队列 → `observed=false`、无队列/无水位数，但同趟确实解码了（`descriptors=156`） |

**单元与契约测试：** `cargo test --offline -p sensoryplex-media --features gstreamer` 75 项通过
（含 `one_kind_cannot_take_the_whole_window`、`the_backlog_and_the_arena_are_both_bounded`）；
`tests/contracts/test_backpressure_contract.py`（7 项）与 `test_handoff_contract.py` 全绿；
`make check` 45 项通过。

**仍未验证（不得当作完成）：**

- GStreamer `queue` 元素与 `appsink max_buffers` **没有计数出口**：三条队列覆盖的是 arena 与保留表，
  不是 GStreamer 内部队列，不能据此宣称"全链路队列都可观察"。
- 只在本机回环与 `macos-aarch64` 验收；`linux-x86_64`、Mac mini / 跨机未验证。
- 未验证小时级长直播，也未验证唯一 kind 长时间贴住配额时的尾延迟（当前只观察了 10–20 秒窗口）。
- **单一种类流只能用一半窗口**（如纯音频直播 `retained_limit=32` 实际 16 条）是 ADR-011 显式接受的代价。
- 模型 worker 未在本节接入：M2 只证明"视频帧能交出去"。语义链路见下方"M8"一节（已接入 VLM）。
- 运行时解码仍有一条 macOS GL 警告（`GStreamer-GL-WARNING ... NSApplication`），不影响结果。

**五场景复跑（`make live-check`，OBS 停流后，2026-09-23，全部 PASS）：** 本轮改动的场景函数与断言
在**脚本自带发布端**（GStreamer `srtsink` 直推）上同样通过，说明直播侧结论不是靠 OBS 会话"恰好成立"：

| 场景 | 结果 |
| --- | --- |
| `steady` | `samples=958`、`descriptors_validated=660`（0 失败）、`stalls=0`、`blockers=[]`、`observed=false state=ok` |
| `videotoolbox_video` | `video samples=3`、`duration_derived_samples=3`、`854x480`，无 `duration_unavailable` |
| `stall_recovery` | `samples=1232`、`stalls=1`、`stalled_ms=4681`（started=7423 ended=12104，`pts_jump_ms=4674`）、`recovered=true` |
| `no_source` | exit 1、`live_window_produced_no_samples`、`samples=0`（不制造空成功） |
| `live_handoff` | `samples=693`、`table=18/18/32`、`kind=16/16/16`、`dropped=473 reasons=473 kinds=473`、`throttled=129`、`released=17`、`residency_samples=18 max_ms=9580`；消费者 `video_buffers=2 audio_buffers=16`，独立进程 70 项通过 |

**再次复跑（2026-09-23，OBS 停流后，5/5 PASS）：** 上述结论可复现。同一轮 `live_handoff` 实测
`samples=690`、`table=18/18/32`、`kind=16/16/16`、`dropped=470 reasons=470 kinds=470`
（拒绝原因只有 `handoff_kind_quota_full`）、`throttled=129`、`released=17`、
`residency_samples=18 max_ms=9568`，消费者 `video_buffers=2 audio_buffers=16`，
`released+expired+retained=18 == retained_total=18`、`arena_live_slabs=0`；
其余四场景（`steady`/`videotoolbox_video`/`stall_recovery`/`no_source`）同样 PASS。
运行后 `live/obs` 回到 `state=notReady`（用户 OBS 已停）。

### M8 模型插件：真实 VLM 端侧接入与观察语义（2026-09-23）

本节记录 M8 的第一步：把**真实模型**接到 M1/M3 之后的链路上。消费方不再只是验收脚本，而是
一个**独立插件进程** + 一个**只做发现与调用的 worker**。决策与边界见
[ADR-012](adr/ADR-012-模型插件与端侧推理边界.md)，契约见 [契约文档](contracts/README.md)
的"模型插件契约"，插件开发说明见 [plugins/python/README.md](../plugins/python/README.md)。

**接入的模型：** 本机 ollama（v0.4.1）上的 `moondream:v2`（VLM，1.7 GB，端侧推理，不出网）。
模型身份**来自服务本身**：`GET /api/tags` 报告
`digest=ad0714b7b564d9f658cb78befffffb74d688bfa8e624a557152518aa8bff159e`，插件据此拼出
`modelArtifactDigest=sha256:ad0714b7…`、`modelReleaseId=ollama:moondream:v2@ad0714b7b564`、
`executionBackend=ollama-0.4.1`。

**四进程（编排 / 生产者 runtime / 插件 / worker）**，`make model-check MEDIA=video/1.mp4`
在授权样本 `video/1.mp4` 上通过（`model acceptance: real frames -> local VLM -> anchored observations passed`）：

| 观察项 | 实测值（2 帧） |
| --- | --- |
| 帧锚点（`time_range` = 源帧半开区间） | `[0,33)`、`[4000,4033)`，`timing_source=media_pts` |
| `content_hash`（= 该帧 lease 窗口摘要） | `sha256:beabe84d…`、`sha256:97f95fd2…`，与 `source_digest` 逐位相等 |
| `observation_id` | `obs_ffe95468…`、`obs_f0437ef0…`（由稳定输入派生，唯一） |
| `modelArtifactDigest` | `sha256:ad0714b7…`（= ollama 实测摘要） |
| 置信度语义 | `confidence` 缺省 + `confidence_unavailable_reason=model_does_not_report_calibrated_confidence` |
| 插件产物摘要 | `sha256:d9dddf20…`（= 本机复算，`plugin_artifact.py --check` 通过） |
| 单帧端到端耗时 | 0.63 s / 1.13 s（读字节 + 推理 + 归还） |
| 账目（运行中） | `retained_by_kind={audio_pcm:16, video_frame:8}` |
| 账目（收尾） | `released_total=24 retained=0 expired=0 retained_total=24`、`arena_live_slabs=0` |
| 消费者归还 | `drain.discarded=22 failures=[]`（不消费的音频条目显式 `discard`） |
| 帧字节不外泄 | worker 报告与插件 stdout 中无媒体名/路径/URI/段名/base64 像素（脚本断言） |

**验收暴露并修掉的 4 个真实缺陷**（不是"一次就过"）：

| # | 现象 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | `model_endpoint_http_404` | `/api/tags`、`/api/version` 是 GET，却被写成 POST | `_request_json(body=None → GET)`，不设"两个都试"的兜底 |
| 2 | `data_plane_stats_failed:UNAVAILABLE` | 数据面空闲超时 4 s < 单帧推理时间，推理期无 RPC → 数据面中途关闭 | 验收 `IDLE_TIMEOUT_MS=60_000` |
| 3 | `handoff_lease_accounting_failed: retained_total=24 released=1 expired=0 still_retained=23` | 消费者只归还消费的视频帧，未处理音频条目；数据面要求每条保留有归宿 | worker 增加 drain 循环显式 `reader.discard()`；验收断言 `failures` 空且 `discarded>0` |
| 4 | 验收误报"无匹配帧 / id 不唯一" | worker 用 proto-JSON 小驼峰 `observationId`，验收脚本按 snake_case 取值 | 验收脚本改读 `observationId` |

**测试与静态检查：** `make check` 通过（含新增 `tests/contracts/test_model_plugin_contract.py` 12 项，
覆盖插件 manifest 形态、身份探测、buffer 输入拒绝路径、`buffer_reader_not_attached`、
`unsupported_memory_kind`、observation 锚点/摘要/置信度语义）；`ruff check` / `ruff format --check` 全绿；
`make check` 内含 `tools/plugin_artifact.py --check`，确保 manifest 的 digest 不是占位串。

**仍未验证（不得当作完成）：**

- 只有 VLM 一个模型：ASR / OCR / BGE **未接入**；CoreML / Metal 仍 `execution_backend_not_implemented`。
- **模型输出质量不稳定**：`moondream:v2` 是极小 VLM，同一帧两次推理可能给出不同文本，
  本轮实测到一次明显退化输出（非空、但明显是幻觉）。此项只证明**链路语义**正确，
  **不**证明描述可用；本节的 `payload.text` 不作为语义质量证据。
- `local_native` 插件**未签名**（只在 manifest 写 `signatureUnavailableReason`），
  签名 / SBOM 只有结构预检，没有真实验签。
- 未做 worker 的 durable 幂等、插件崩溃后的 lease 回收、沙箱与"无外网"策略的强制执行。
- `golden_path_verified` 恒为 false；2–5 秒语义可见性未测；小时级长直播未测。
- 只在本机回环 `macos-aarch64` 验收；`linux-x86_64`、Mac mini / 跨机未验证。

### M9 媒体格式准入：拒绝语义落地与实测（2026-09-23）

**平台：** `macos-aarch64`（GStreamer 1.28.7、FFmpeg 9.0.1）。命令：
`make capability-check`（**19/19**）、`make live-check`（5/5）、
`cargo test --offline -p sensoryplex-media --features gstreamer`（**103 passed**，其中
`crates/media/src/capability.rs` 的矩阵与拒绝码单测 27 条）。

实现位置：`crates/media/src/capability.rs`（承诺矩阵 + `classify_track` + `NO_DECODER_ELEMENT`）、
`crates/media/src/decode.rs`（源格式采集、按 stream ID 关联、demuxer src pad 探针、链头准入探针、
被拒 pad 终结、视频链 `gldownload`、拒绝上报）、`tools/verify_capability.py`（验收脚本）。
Proto 字段归属见 [准入契约](contracts/README.md)；样本出处、许可与 SHA-256 见
`tests/fixtures/media/OPEN-SAMPLES.md`。

#### 正样本：承诺矩阵内的 6 个登记样本全部未被误拒

容器由 `gst-discoverer-1.0 -v` 读；其余字段来自 Runtime 回放报告：

```sh
target/release/sensoryplex-runtime replay config/pipelines/file-material.yaml <media> \
  --report /tmp/r.pb --max-points 40000      # 把整段流跑完，不做截断
```

```python
# 解析报告（--offline 不需要额外依赖，直接用生成的 SDK 类型）
import sys

sys.path.insert(0, "plugins/python/common/src")
from edge_material_sdk.generated.media.v1 import media_pb2 as m

r = m.ReplayReport()
r.ParseFromString(open("/tmp/r.pb", "rb").read())
for t in r.decoded.tracks:  # 被准入的轨道
    print(
        t.track_kind,
        t.source_codec,
        t.decoder_element,
        t.source_bit_depth,
        t.source_chroma_format,
        repr(t.colorimetry),
        t.frame_rate_mode,
        f"{t.declared_frame_rate_num}/{t.declared_frame_rate_den}",
        t.samples,
    )
for x in r.decoded.rejected_tracks:  # 被拒轨道（唯一原因位置）
    print("REJECTED", x.track_kind, x.code, x.detail, x.container)
```

| 样本 | 容器 | 源编码（video/audio） | 解码器元素 | 源位深 | 采样格式 | colorimetry | 帧率模式 | descriptors |
|---|---|---|---|---|---|---|---|---|
| `video/1.mp4` | `video/quicktime` | `video/x-h265` / `audio/mpeg` | `vtdechw0` / `avdec_aac0` | 8 | `4:2:0` | `bt709` | CONSTANT（声明 30/1） | 1354（video 26 + audio 1321 + 7 段） |
| `screencast-watchlist.480p.vp9.webm` | `video/webm` | `video/x-vp9` / `audio/x-opus` | `vtdechw0` / `opusdec0` | 8 | `4:2:0` | 未采集（空） | CONSTANT（声明 30/1） | 7892（video 41 + audio 7819 + 32 段） |
| `sintel-trailer.480p.h264.mp4` | `video/quicktime` | `video/x-h264` / `audio/mpeg` | `vtdechw0` / `avdec_aac0` | 8 | `4:2:0` | 未采集（空） | CONSTANT（声明 24/1） | 2487（video 42 + audio 2434 + 11 段） |
| `editing-basics-sandboxes.vp8.webm` | `video/webm` | `video/x-vp8` / `audio/x-vorbis` | `vp8dec0` / `vorbisdec0` | 8（推导） | `4:2:0`（推导） | 未采集（空） | **UNKNOWN**（声明 0/0） | 5773（video 21 + audio 5736 + 16 段） |
| `mpegts-h264-aac.live-recording.ts` | `video/mpegts` | `video/x-h264` / `audio/mpeg` | `vtdechw0` / `avdec_aac0` | 8 | `4:2:0` | `bt601` | CONSTANT（声明 25/1） | 934（video 4 + audio 926 + 4 段） |
| `conger-conger.h264-pcm.mov` | `video/quicktime` | `video/x-h264` / `audio/x-raw` | `vtdechw0` / `demuxer_passthrough` | 8 | `4:2:0` | 未采集（空） | CONSTANT（声明 30000/1001） | 20（video 9 + audio 9 + 2 段） |

6 条全部 `rejected=0`、`blockers` 为空、`descriptors_built == descriptors_validated`，
`drop_reasons` 只出现自适应抽帧自己的原因（`no_change_yet` / `rate_limited` /
`duration_unresolved_at_end`），**没有**准入拒绝码，也没有 `decode_stalled`。

**读表时必须知道的四件事（都不是推测）：**

- **VP8 的 `8` 和 `4:2:0` 是矩阵推导值**：`video/x-vp8` 的 caps 在 GStreamer 1.28.7 里既没有
  `profile` 也没有 `bit-depth-luma` / `chroma-format`，报告里的位深/采样格式来自
  `capability.rs` 的 `implied_bit_depth` / `implied_chroma_format`（VP8 只有 Profile 0，
  8-bit / `4:2:0` 是该 profile 的定义）。证据强度**低于** H.264/HEVC 的真读值。
- **`editing-basics-sandboxes.vp8.webm` 的帧率是真 UNKNOWN**：2012 年的原始上传没有
  `DefaultDuration`，`declared_frame_rate=0/0`，报告写 `FRAME_RATE_MODE_UNKNOWN` 而不是补一个恒定值
  （ffprobe 报的 `r_frame_rate=24/1` 是它自己的换算，不等于容器声明）。它是"帧率未知必须显式表达"
  的正样本。
- **容器内 PCM 没有解码器元素**：MOV 直接存 `pcm_s16le`，`decodebin` 只建 `qtdemux`，
  没有可归因的 parser/decoder，报告写 `demuxer_passthrough`（`capability::NO_DECODER_ELEMENT`，
  已写进 `media.proto` 的 `decoder_element=17` 注释）。空串的含义仍然是"本次运行没能归因"。
- `display_rotation_deg` / `applied_rotation_deg` 在 6 条上均**缺省**（v1 不采集也不应用旋转，
  不填 0 冒充"未旋转"）；`colorimetry` 只在源 caps 给出时存在，不参与准入判定，
  也不得被反推成"已确认 SDR"。

#### 负样本：13 条拒绝路径，拒绝码逐字等于 ADR-009 §3 命名表

全部由 FFmpeg 现场合成/重封装；它们**只证明拒绝路径**，不是任何格式的可用性证据。

| 负样本（场景名） | 拒绝码 | track_kind | detail | container | 同流中仍解码的轨道 |
|---|---|---|---|---|---|
| `ten_bit_source_is_rejected_before_the_8bit_raw_caps` | `unsupported_bit_depth_10bit` | video | `bit-depth-luma=10` | `video/quicktime` | audio |
| `multichannel_audio_is_rejected_by_measured_channels` | `unsupported_channel_layout_multichannel` | audio | `6` | `audio/x-m4a` | 无 |
| `avi_container_is_rejected_for_both_tracks` | `unsupported_container_avi`（2 条） | video + audio | `video/x-msvideo` | `video/x-msvideo` | 无 |
| `raw_elementary_stream_is_not_read_as_an_unknown_container` | `unsupported_container_raw_es` | video | `video/x-h265` | `video/x-h265` | 无 |
| `non_media_pad_is_counted_instead_of_logged` | `unsupported_media_type_text_x_raw` | other | `text/x-raw` | `video/quicktime` | video + audio |
| `second_video_track_is_a_track_layout_rejection` | `unsupported_track_layout_multiple_video` | video | `more_than_one_video_track` | `video/quicktime` | video + audio |
| `chroma_other_than_420_is_rejected` | `unsupported_chroma_format_4_2_2` | video | `4:2:2` | `video/quicktime` | 无 |
| `mp3_is_not_read_as_aac` | `unsupported_codec_mp3` | audio | `mpegversion=1` | `video/quicktime` | 无 |
| `h264_10bit_is_rejected_by_profile` | `unsupported_bit_depth_10bit` | video | `bit-depth-luma=10` | `video/quicktime` | 无 |
| `vp8_in_an_unsupported_container_is_rejected` | `unsupported_container_avi` | video | `video/x-msvideo` | `video/x-msvideo` | 无 |
| `vorbis_in_ogg_is_rejected_by_container` | `unsupported_container_ogg` | audio | `audio/ogg` | `audio/ogg` | 无 |
| `mpegts_container_does_not_promise_ac3` | `unsupported_codec_ac3` | audio | `audio/x-ac3` | `video/mpegts` | video |
| `pcm_outside_a_promised_container_is_rejected` | `unsupported_container_wav` | audio | `audio/x-wav` | `audio/x-wav` | 无 |

每条都满足：命令 exit 0（**显式拒绝不是运行失败**）、命令行 `rejected=` 与报告
`DecodedDataPlane.rejected_tracks` 条数一致、每条被拒轨道都带 `detail` 与 `container`、
输出里**不出现** `decode_stalled`；同一条流里没被拒的轨道照常产出 descriptor，全轨被拒时
`descriptors_validated == 0`。

后三条是本轮新增的**相邻**负样本，各自钉住矩阵里一行的边界：

- **VP8 行**：VP8 落在不承诺的容器（AVI）里仍由**容器**判据挡下，不因为编码被承诺就放行。
- **Vorbis 行**：Vorbis 的母容器 Ogg 不在矩阵内，容器判据先于编码判据。这条**故意用 FFmpeg 现场
  编码**（`vorbis` 编码器需 `-strict -2`）而不是把 WebM 里的 Vorbis 重封装进 Ogg：重封装会保留源的
  pre-skip，首帧 PTS 变成 -0.0005，撞上参考探针"拒绝而不 clamp"的既有策略
  （`invalid_probe_output: timestamp=-0.000522`，见下文边界）。那是**报告之前**的失败，不是准入拒绝。
- **MPEG-TS 行**：容器承诺不等于编码承诺——TS 里的 AC-3 必须被**编码**判据挡下，
  而同一条流里的 H.264 视频轨照常解码。
- **PCM 行**：矩阵承诺的是"容器里的 PCM"，不是 WAV 这个容器本身；换了容器就不再承诺。

#### 本轮补样本时暴露并修掉的三个**真实缺陷**

**(a) 多视频轨竞态（`crates/media/src/decode.rs`）**

现象：双视频轨场景随机失败——HEVC 报 `pipeline_state_change_failed`，VP9/软件解码报 `decode_stalled`。
根因是两件事叠加，两处都必须修：

1. **被准入拒绝的 pad 悬空**：此前只 `return`，不链接任何元素，上游解码器拿到 `not-linked`。
   修法是新增 `terminate_rejected_pad()`，把被拒 pad 接到 `fakesink(sync=false, async=false)`。
   （该 sink **不指定 `name`**：曾用固定名，第二条被拒轨道报 "not unique in bin"。）
2. **`vtdec_hw` 偶发按 GLMemory 协商输出**：同一进程里出现第二个 VideoToolbox 解码实例时，
   `vtdec_hw` 的 src 模板把 `video/x-raw(memory:GLMemory)` 排在首位，而 `videoconvert` 不接受
   GLMemory。修法是在视频链的 `queue` 与 `videoconvert` 之间插入 `gldownload`（sink 同时接受
   GLMemory 与系统内存，src 只输出系统内存），元素缺失时退回原链路。

A/B 实测（用临时开关 `SP_NO_GLDOWNLOAD` 分离变量，开关已移除）：单轨从未复现
（`sintel` ×15、`1.mp4` ×10 全 0 失败）；双轨修复前/后失败次数：

| 样本 | 修复前 | 只加 `gldownload` | 两项都修（现状） |
|---|---|---|---|
| `two-video-h264.mp4` | 4/10 | 2/10 | **0/10** |
| `two-video.mp4`（HEVC） | 6~7/10 | 5/10 | **0/10** |
| `two-video-vp9.mkv` | 10/10 | 9/10 | **0/10** |

纯 `gst-launch` 的上游 `decodebin` 也能复现（7/12），说明问题在链路接线而不在 Runtime 的报告层。
`GStreamer-GL-WARNING ... NSApplication` 是无害噪声，不是本问题的原因。

**(b) 容器里直存 raw 采样时源编码采集不到**

现象：`conger-conger.h264-pcm.mov` 的 PCM 轨被拒
`unknown_source_codec: codec_caps_not_collected`。PCM 没有 parser/decoder，`deep-element-added`
的 sink caps 探针永远等不到它（`decodebin` 只为它建 `qtdemux`）。修法有两处：

- `install_demux_src_probe()`：给 demuxer 的 **src pad** 挂探针；这些 pad 是**解析时**才创建的，
  因此必须同时挂到 `element.connect("pad-added", ...)` 上（只 `iterate_src_pads()` 拿不到）。
- `SourceHive::record_caps()` 增加 `demux_src: bool` 参数与 `container_raw: BTreeSet<String>`，
  把"没有解码器"钉成显式取值 `demuxer_passthrough`（**不是空串**；空串仍是"本次运行没能归因"）。

**(c) `audio/x-wav` / `audio/x-flac` 被误读成裸 ES**

它们在容器表里本来就有短名（`wav` / `flac`），却先在裸 ES 表里命中，于是报
`unsupported_container_raw_es` 而不是 `unsupported_container_wav`。修法是把它们从裸 ES 表移出
（它们**有容器头**），并补 2 条命名断言（`unsupported_container_wav` / `unsupported_container_flac`）。

#### MPEG-TS：文件形态与直播形态都有证据

- **文件形态**（本轮新增）：`mpegts-h264-aac.live-recording.ts`，见上方正样本表，
  `rejected=0`、H.264 `vtdechw0` / AAC `avdec_aac0`、`colorimetry=bt601`。
  录制方式（OBS 必须先停，同一条 `live/obs` 路径会冲突）：

  ```sh
  # 发布端：tools/verify_live.py 的 Publisher 把 screencast-video2commons.480p.vp9.webm
  # 经 mpegtsmux ! srtsink 直推 srt://127.0.0.1:8890?streamid=publish:live/obs
  # 录制端（SIGINT 收尾才能得到干净 TS）：
  gst-launch-1.0 -e srtsrc uri=srt://127.0.0.1:8890?streamid=read:live/obs \
    ! tsparse ! filesink location=video/samples/mpegts-h264-aac.live-recording.ts
  ```

  该样本是**本机重编码产物**（VP9/Opus → H.264/AAC），只覆盖文件路径的准入与回放。
- **直播形态**：`make live-check` 五个场景通过；`steady` 场景下 MPEG-TS over SRT 的 video/audio
  均未被拒（`samples > 0`、`blockers` 为空）。

#### 边界与仍未验证范围

- **参考探针对负时间戳的策略没变**：WebM 里的 Vorbis 重封装进 Ogg 会保留 pre-skip → 首帧 PTS 为负
  → `crates/media/src/probe.rs` **故意拒绝**（Runtime 在出报告前以
  `invalid_probe_output: timestamp=-0.000522` 退出）。这是"拒绝而不 clamp"的既有约定，不是准入拒绝；
  负样本因此改用现场编码（首帧 PTS=0）。
- VP8 的位深/采样格式是矩阵推导值；文件形态 MPEG-TS 样本是本机重编码产物——两条都写在
  ADR-009 §10 与 `OPEN-SAMPLES.md` 的"未覆盖范围"里。
- 其余未验证范围见 ADR-009 §10：仅 `macos-aarch64`；Linux / Mac mini 未验证；
  **旋转（几何）**既未采集也未应用；CAPS 变化重判与"相同 raw caps 来自不同源格式"只有单元测试级
  证据，没有端到端样本；E-AC-3 / DTS / TrueHD 与真实 HDR 素材仍无样本；容器级 VFR 与设备直出仍无样本；
  `golden_path_verified` 恒为 false。
- **边界（读报告时必须知道的）**：`DecodedDataPlane.tracks` 对 video/audio 两个分支各留一条统计，
  被拒轨道或源里没有的轨道写 `samples=0` 且源字段为空；它不是"已准入但信息未知"，
  **被拒的唯一原因位置是 `rejected_tracks`**。`ReplayReport.blockers` 不列媒体准入项：
  拒绝是流级事实，不是构建级缺失。

### M10 模型插件：真实 ASR 端侧接入与音频样本布局契约（2026-09-23）

本节记录 M8 的第一个剩余子项：**流式 ASR**（蓝图第 5 周"音频段 → 文本"）。第二个模型插件
`plugins/python/processors/asr-whisper-mlx` 消费 Runtime 数据面里的**真实音频段**，用**本机权重**的
MLX Whisper 产出带锚点/来源/显式置信度语义的转写 observation。决策与边界见
[ADR-014](adr/ADR-014-ASR插件与音频样本布局契约.md)，契约见 [契约文档](contracts/README.md)
的"模型插件契约"。

**接入的模型：** Apple Silicon 原生的 `mlx-whisper 0.4.3`（`mlx 0.32.2`）+
`mlx-community/whisper-large-v3-turbo`，端侧推理、不出网。模型身份**来自即将加载的权重文件本身**：
`weights.safetensors` 1,613,977,612 字节，`sha256:951ed3fc…`，由验收脚本用
`huggingface_hub.snapshot_download` 独立复算后与 `provenance.modelArtifactDigest` 比对（不自证）。
选它的理由：ADR-008 把 Apple Silicon 定为一等目标，`mlx` 有 macOS arm64 轮子；本机实测单段
5 秒音频推理 0.4–0.8 s。

**验收命令：** `make asr-check MEDIA=video/samples/screencast-video2commons.480p.vp9.webm`
（四进程：编排 / `runtime replay --handoff-listen` / 插件 / `tools/ai_worker.py`）。

#### 验收暴露并修掉的 5 个真实缺陷（不是"一次就过"）

| # | 现象 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | worker 报 `no_audio_segment_buffer_to_process`，保留表里只有 `video_frame`/`audio_pcm` | `crates/media/src/decode.rs` 的 `emit_segment()` 只调 `hand_off()`（进程内 arena + lease 自校验），**没有**像每一条 `audio_pcm` 那样调 `handoff.retain_or_reject()`。于是段描述符从来没进跨进程保留表 | `emit_segment()` 对段描述符补上 `retain_or_reject()`（与 `process_sample` 同构）。M8 没暴露它，因为 M8 的验收是用 `audio_pcm` 填满 `retained_by_kind` 的 |
| 2 | `AssertionError: runtime exited before 'handoff_stats'` | 验收脚本在 `finally` 里先 `producer.kill()`，之后才去读生产者退出时才打印的 `handoff_stats` | 照 M8 的顺序改：先 `plugin.kill()` 让生产者走完"消费者已离开"的空闲收尾，再 `producer.finish()` 并对 `handoff_stats` 对账；同时把 `IDLE_TIMEOUT_MS` 与 `WAIT_TIMEOUT_MS` 的语义分开（前者决定收尾速度，后者要覆盖插件 Start 的冷缓存下载） |
| 3 | 数据面的 offer 数与解码样本数对不上（`video/1.mp4`：`offered=1354` vs `samples=1347`） | `tools/verify_handoff.py` 的会计口径仍按"样本数"，段描述符进表后两者不再等价（差 7 就是段数） | 对照基准改为**报告亲手交接过的 descriptor 数** `decoded.descriptors_built`，并要求 `descriptors_built == Σtrack.samples + audio_segments.segments`——这个差必须被报告显式解释，不能当成误差抹掉 |
| 4 | `worker should only note that the segment name exists, never carry it`（恰好两次） | 泄漏检查把**单条 observation 的 payload** 当成了 worker 报告传入，而 `segment_name_hint` 是 worker 自己写的字段，只查 observation 永远查不到 | 改成对 worker 的整份报告查一次（与 M8 相同） |
| 5 | 第二轮验收被自己的断言拦下：`obs_fae5a997…: sub-segment [940, 29880] leaves its own window [0, 5015]` | 不是脚本 bug，是**模型的真实行为**：Whisper 退化时会给出越出窗口的时间戳（5 秒窗口上给出 29.88 s 的结束时间）。此前的验收断言"子段必须落在窗口内"，等于**假设模型守规矩** | 语义改为**不夹取、不丢弃**：`_segment_payload()` 写 `timing_outside_window` 布尔（`start_ms < 窗口起点 or end_ms > 窗口终点`），`_payload()` 写 `segments_outside_window` 计数；验收改为"越窗必须被标记且计数对得上"，契约测试 `test_out_of_window_sub_segments_are_flagged_not_clamped_or_dropped` 锁死（原值保留 + 文本保留 + observation 锚点仍是源段区间），并补一条"窗口内必须**不**被标记"的反向断言 |

缺陷 1 的修法有**可复现的 A/B**：`crates/media/src/decode.rs` 新增单测
`audio_segments_reach_the_cross_process_retained_table`，把 `retain_or_reject()` 注释掉即
`retained_by_kind.get("audio_segment") == None` 必红，恢复即绿。

#### 实测结果（2 个真实音频段，样本 `screencast-video2commons.480p.vp9.webm`）

| 观察项 | 实测值 |
| --- | --- |
| 源段锚点（`time_range` = 段半开区间） | `[0,5015)`、`[5015,10015)`，`timing_source=media_pts`（未重新计时） |
| `content_hash`（= 该段 lease 窗口摘要） | `sha256:9eeb2e0b…`、`sha256:564e5b29…`，与 `source_digest` **逐位相等** |
| `observation_id` | `obs_fae5a997…`、`obs_5fe411fb…`（由稳定输入派生，唯一） |
| `artifactDigest`（插件包） | `sha256:f854a08b…`（= 本机复算，`plugin_artifact.py --check` 通过） |
| `modelArtifactDigest`（权重文件） | `sha256:951ed3fc…`（= 验收脚本独立复算，非配置里的版本号） |
| `executionBackend` / `modelReleaseId` | `mlx-0.32.2` / `mlx-whisper:mlx-community/whisper-large-v3-turbo@951ed3fc1203` |
| 置信度语义 | `confidence` 缺省 + `confidence_unavailable_reason=model_does_not_report_calibrated_confidence` |
| 输入事实（payload） | 窗口 1：`sample_format=F32LE`、`input_sample_rate=48000`、`input_channels=2`、`input_samples=240648`、`whisper_samples=80216`；窗口 2：`input_samples=240000`、`whisper_samples=80000`（16 kHz 单声道） |
| 子段时间 | `segment_timing=media_pts_window_relative_plus_window_start`（窗口起点 + 模型相对时间）；本轮两个子段 `timing_outside_window=false`、`segments_outside_window=0`——**越窗与否是跑出来的结果，不是常量**（越窗路径见缺陷 5） |
| 单段端到端耗时 | 1321.1 ms（首个）/ 783.6 ms（插件内 `inference.duration_ms`：1306.9 / 769.0 ms） |
| 账目（运行中） | `retained_by_kind={audio_pcm:16, audio_segment:4, video_frame:8}`（**段也在表里**） |
| 账目（收尾） | `retained_total=28 released_total=28 retained=0 expired=0 arena_live_slabs=0` |
| 消费者归还 | `drain.discarded=26 failures=[]`（不消费的条目显式 `discard`） |
| 运行时报告 | `descriptors_built=1012`、`rejected_tracks=0`、`audio_segments.segments=4`，`blockers=max_points_truncated,decode_truncated` |
| 背压（同轮） | `state=saturated`、`retained_table 28/32`、`retained_kind 16/16`、`arena_peak=20915328 B`、`dropped_total=984`（全部 `handoff_kind_quota_full` / kind `audio_pcm`）、`sampling_throttled_samples=600`（`throttle_factor=4`）——段只占 4 条，单一种类 16 条上限由 `audio_pcm` 触顶 |
| 报告自证布局 | ReplayReport 断言 `audio_segments.listed[].sample_format == "F32LE"` 且音频轨 `audio_format == "F32LE"` |
| 字节不外泄 | worker 报告、插件 stdout/stderr 中无媒体名/路径/`srt://`/段名（脚本断言） |

**转写文本（原样，不作质量证据）：** 本表是**修复越窗后的那一轮**（即上表 evidence 对应的运行）。

- 窗口 1（`[0,5015)`）：`Dobri den.`，`compression_ratio=0.556`、`temperature=0.2`、
  `avg_logprob=-0.818`，语言检测 `no`——5 秒里只吐了一句问候，其余语音没被转出来。
- 窗口 2（`[5015,10015)`）：`A představuji vám videotutoriál na téma jak nahrát video do Wikipare.`
  （捷克语），`compression_ratio=0.949`、`temperature=0.0`、`avg_logprob=-0.210`，语言检测 `cs`。

**同一素材、同一命令换一轮跑，结果可以不同**：本缺陷 5 记录的那一轮，窗口 1 是 Whisper 的**重复退化**
（`Rik for at man prist for at man prist …`，`compression_ratio=22.2`、`temperature=1.0`、语言 `no`），
时间戳还越出了窗口。所以这四段文本**不能**当质量证据：它们同时说明**语言检测与文本质量必须由消费者
自己判断**。同一插件在 `language=en` 固定的另一段 5 秒窗口上曾给出
`I will show you a tutorial on how to record video in Wikipedia.`，自动模式下同一素材会逐窗口给出不同语言。
本插件因此**原样带出** `avg_logprob` / `no_speech_prob` / `compression_ratio` / `temperature`
四个解码诊断，并明确它们**不是**校准置信度——下游要用重复率筛掉退化段，只能靠这些量。

#### 测试与静态检查

- `cargo test --offline -p sensoryplex-media --features gstreamer`：**105 passed**（含新增
  `audio_segments_reach_the_cross_process_retained_table` 与 `an_unknown_sample_layout_is_dropped_instead_of_guessed`）。
- `make check` 通过：`cargo fmt --check`、`clippy -D warnings`、`ruff check`/`ruff format --check`、
  两个插件的 `plugin_artifact --check`、workspace `cargo test`、契约测试 **78 passed**。
  新增的 `tests/contracts/test_asr_plugin_contract.py` 20 项覆盖输入准入、未知布局拒绝、
  下混/重采样、锚点/摘要/置信度语义、稳定 ID、空转写的两种原因、超长文本失败、后端异常不外泄、
  越窗子段必须标记+计数（不夹取、不丢弃）且窗口内**不得**被标记、manifest 摘要与 schema、
  网络白名单与可写路径边界；`tests/contracts/test_media_contract.py`
  加一条 `sample_format` 显式未知契约。M10 当时的契约计数为 78；加入 M5 的
  `test_macos_resident_contract.py` 11 项后当前为 **89 passed**，见文末 M5 一节。
- `make integration`：11 passed。回归：`make model-check MEDIA=video/1.mp4`（M8）、
  `make handoff-check MEDIA=video/1.mp4`（修口径后 2 场景通过）、`make capability-check`（**19/19**）
  全部通过。

#### 仍未验证（不得当作完成）

- 只有两个模型（VLM + ASR）；**OCR 与 BGE 未接入**；CoreML / Metal 仍 `execution_backend_not_implemented`。
- 只在本机 `macos-aarch64` 验收；`linux-x86_64` 与 **Mac mini / 跨机未验证**——ASR 后端的
  `mlx` 是 Apple Silicon 专属，Linux 侧需要另选后端（ADR-014 §7）。
- 转写**质量**未验收：本节只证明链路语义（锚点、摘要、来源、账目、显式未知）正确，
  不证明转写可用；上面窗口 1 的重复退化就是反例。没有 WER/CER 度量，也没有多人对话与
  中英混说样本（见 `docs/TODO.md` §2）。
- 段是固定 5 秒切分（`segment_ms`），**没有按静音切分、没有与说话人对齐**，因此一个段可能横跨
  多个说话人；长段的窗口边界会切在词中间（窗口 2 的 `Wikipare` 就是被切出来的）。
- `timeout_s` 之外没有取消语义的端到端验证（插件声明 `cancellation: true`，但本轮没有中途取消的样本）。
- 插件仍 `local_native` **未签名**；`golden_path_verified` 恒为 false；小时级长直播未测。

### M5 macOS 常驻形态与统一内存分级（2026-09-23）

本节记录 M5：把原生 `sensoryplex-runtime serve` 交给**用户级 launchd** 常驻，并按宿主统一内存
自动选择队列/保留窗口/arena/模型并发上限。决策与边界见
[ADR-015](adr/ADR-015-macOS常驻形态与统一内存分级.md)，操作步骤见
[运行手册](runbooks/macos-resident.md)。运行平台 `macos-aarch64`（macOS 26.5.2，arm64，
M2 Max，32 GiB 统一内存）。

**分级表**（`tools/macos_resident.py` 的 `TIERS`，半开区间，档位之外不吸附）：

| 档位 | 统一内存 | 媒体队列 | 保留窗口 | 保留 arena | 模型并发 |
| --- | --- | --- | --- | --- | --- |
| `small` | 16–24 GiB | 16 | 16 | 32 MiB | 1 |
| `medium` | 24–32 GiB | 32 | 32 | 64 MiB | 2 |
| `large` | 32–64 GiB | 64 | 64 | 128 MiB | 3 |
| `xlarge` | ≥64 GiB | 128 | 128 | 256 MiB | 4 |

`medium` 档**锚定**今天的默认值（`crates/media/src/handoff.rs` 的 `DEFAULT_RETAINED_LIMIT=32`
与 `DEFAULT_RETAIN_ARENA_BYTES=64 MiB`），即"不改现有行为"；契约测试从**源码**解析这两个常量比对，
避免表与实现各写一份。

| 验证 | 结果 |
| --- | --- |
| `macos_resident.py probe` | 32.0 GiB / `source=sysctl` / 分级 `large`；`retained_limit=64`（单类 32）、arena 128 MiB、模型并发 3、预算 10.7 GiB；两个 pipeline 的 `queue_capacity=32` 报"匹配"并注明该字段只被校验 |
| 探测来源与非法声明 | `sysctl` 优先；宿主探测失败才接受 `SENSORYPLEX_TOTAL_MEMORY_BYTES`（`source=env`）；非正整数直接 `ValueError`；都不可用是 `unavailable`（**不是 0**） |
| `install` | 打印 `pmset` 现状 **`sleep 1`** 与人工命令（工具**不**改系统设置）；`org.sensoryplex.runtime` pid=6538 running |
| `status --verify-endpoint` | runtime / caffeinate 双 running；gRPC `127.0.0.1:50051` 返回 `state=degraded`、`platform=macos-aarch64`、`unified_memory_bytes=34359738368`（与宿主探测一致）、`admitted_memory_kinds=[cpu_shared_memory, unified_memory]`、`unavailable_capabilities=[media_ingestion, model_inference, event_dispatch, semantic_index]` |
| 环境注入核对 | `launchctl print gui/501/org.sensoryplex.runtime` 的 `environment` 含 `SENSORYPLEX_HANDOFF_ARENA_BYTES=134217728`、`SENSORYPLEX_UNIFIED_MEMORY_BYTES=34359738368`、`RUST_LOG=info` |
| `pmset -g assertions` | pid 6541 的 `caffeinate -ims` 持有 `PreventUserIdleSystemSleep` + `PreventSystemSleep`（asserting forever） |
| **崩溃重启** | `kill -9 6538` 后 3 秒 `status` 显示**新 pid 6644** running → `KeepAlive.SuccessfulExit=false` 生效 |
| **登录/引导自启** | `launchctl bootout` → `launchctl print` 确认 not loaded → `launchctl bootstrap gui/501 …`（**不** kickstart）→ 2 秒后 running **pid=6997** → `RunAtLoad` 生效 |
| **分级上限注入媒体作业** | `sensoryplex-media-run replay config/pipelines/file-material.yaml video/1.mp4`：打印分级值，报告 `anchors=2237 decoded_items=2239 descriptors=1354 rejected=0 leases 1354/1354 segments=7`；再以 `--handoff-listen 127.0.0.1:64555`（无消费者）跑，`handoff_stats` 实测 `retained_limit=64 retained_kind_limit=32 retained_peak=47`、backpressure `state=saturated`，以 `handoff_consumer_never_connected` 退出（预期） |
| `uninstall --purge-logs` | 两个 label 已卸、plist 与 `resident.env` 已删、日志已清；残留检查：无 `serve` 进程、无 `caffeinate -ims`、50051 无监听 |
| 契约测试 | `tests/contracts/test_macos_resident_contract.py` 11 项通过（分级边界不吸附、`medium` 锚定源码常量、探测来源、模板严格渲染、两个 plist 语义、`resident.env` 内容、包装脚本三种语义）；`tests/contracts` 合计 **89 passed** |
| 静态检查 | `ruff check .` 与 `ruff format --check .` 通过（91 个文件已格式化） |

**仍未验证（不得当作完成）**

- 队列上限按分级生效**未验证**：`queue_capacity` 当前只被校验、未被运行时消费（ADR-015 §5）；
  模型并发只有配置事实，没有并发执行的端到端样本。
- 只在本机一台 Apple Silicon 机型验收；Mac mini 各档位与 16 GiB 的 `small` 档未实跑；
  断电重启、休眠唤醒、小时级长稳均未验证。
- `resident.env` 未收紧权限；分级值未回写进 `DescribeCapabilities`；`golden_path_verified` 恒为 false。

### M7 Apple Silicon CI 远端执行与加固（2026-09-23）

**先纠正一条过期陈述**：`docs/TODO.md` §M7 原文写 `check-apple-silicon` "从未在远端跑过"，
这与事实不符。远端早已真实执行并通过：

| Run | 结果 |
| --- | --- |
| 35850290513（M9 的 push） | job 107146096696 `conclusion=success`，11 个 step 全绿 |
| 35860979204（M10 的 push） | 三个 job 全绿：`check` 2m50s、`check-console` 58s、`check-apple-silicon` 2m5s（job 107180840473） |

远端 runner 实测为 **`macos-15-arm64`**（Image Version 20260907.0337.1、macOS 15.7.9、
24G830），是真 Apple Silicon，不是 x86 交叉编译。

**本次加固**（`.github/workflows/ci.yml`）：

- `on:` 增加 `workflow_dispatch`，允许在不制造空提交的前提下重跑。
- `check-apple-silicon` 增加硬断言 step：`test "$(uname -m)" = "arm64"` 并打印 `hw.memsize`。
  若镜像哪天变成 x86，"macOS CI 通过"必须先红，而不是悄悄退化成"在 Intel 上通过"。
- `make media-check` 之后增加 `make media-test`（解码路径单测，含保留表 A/B 回归；带 `gstreamer`
  feature 才存在），并把它对应的目标加入 `Makefile`。

**加固后的远端验证（2026-09-23，run 35863597690）**：本次 push 触发一次真实 run，三个 job 全绿：

| Job | 耗时 | 结果 |
| --- | --- | --- |
| `check` | 2m52s | success |
| `check-console` | 1m0s | success |
| `check-apple-silicon` | 3m46s | success（13 个 step 全绿） |

`check-apple-silicon` 的实测要点：runner image 为 **`macos-15-arm64`**（Image Release
`macos-15-arm64/20260907.0337`），新增的断言 step 输出 `arm64`（`test "$(uname -m)" = "arm64"` 通过）
并打印 `sysctl -n hw.memsize` = **7,516,192,768（7.0 GiB）**；新增的解码路径单测 step
`cargo test --locked -p sensoryplex-media --features gstreamer` 输出 **105 passed**；
`brew install gstreamer`、`make media-check`、`make runtime-smoke` 均通过。

**仍未验证**：Windows 与 Linux NVIDIA 侧没有 CI job；runner 只有 7 GiB 内存、低于 `small` 档下限，
因此 `launchd` 常驻形态（M5）**不能**在 CI 里验收，只能真机跑（见本节上面 M5 一节）。

### Console 应用准备流程（2026-09-23）

本轮新增 `apps/console` 与 `services/api`，Gateway 保留兼容导入入口。验收范围是脱离 Runtime
执行服务也能工作的应用功能；不将上传或草稿持久化当作媒体准入、AI 处理或 Golden Path 成功。

- `make proto`：新增 Console 消息生成通过；再次从 descriptor 生成 TypeScript，与已有生成文件一致。
- `npm --prefix apps/console run build` 与 `format:check`：通过；主入口 gzip 约 104.5 KiB。
- Ruff 对本次 API、兼容 Gateway、工具及测试的检查与格式检查：通过。
- Python 契约测试：57 passed；真实 PostgreSQL 集成测试：10 passed（含 7 个 Console 场景）。
  覆盖会话/CSRF/Origin、角色与 owner、文件摘要/Range/重启/取消、配置版本/方案引用、凭据撤销、
  登录限流、账户停用/权限调整/密码重设后的会话与 token 失效。Starlette TestClient 有 2 条弃用警告。
- `tools/verify_console.py`：使用 `agent-browser@0.38.1` 跑通 6 组真实浏览器检查：
  登录；按 Schema 保存插件配置和方案；上传/摘要比对/播放；任务草稿刷新与执行禁用；
  凭据创建/隐藏/撤销；只读菜单、直达路由拒绝、跨 owner 不可见、390×844 导航与无横向溢出。
  浏览器未报告运行时异常。自动化结束关闭测试浏览器。
- 浏览器样本为登记的 public-domain `sasebo-basketball.480p.vp9.webm`，
  SHA-256 `89eed55b7ed4e991f2c7d536de2aa777cf52542147a4eb3b76a583f1c89073e9`。
  真实播放和 seek 已观察到，仍标记 `awaiting_admission`；没有触发 Runtime。
- `make console-prepare` 独立安装 28 个 API 依赖到 `.data/console-venv`，未安装模型包；
  `make console-api` 使用该环境启动，通过浏览器登录页、持久配置/文件/草稿读取及授权 Range 检查。
- 自动化产物保存在 Git 忽略的 `.data/console-preview/ui-d3995b3e/`，包括 `result.json`、
  `playback.png`、`jobs.png`、`mobile.png`，不含密码和令牌快照。此前手工验收截图也位于预览目录。
- 独立预览 schema 数据对账：2 份配置、2 份方案、2 份上传、2 份任务草稿、21 条审计；
  `material_unit=0`、`processing_job=0`。这些是本次真实页面操作，不是业务成功 fixture。

本轮全仓 `make check` **未通过**：最后一次在现有媒体工作区的 `crates/media/src/decode.rs:2463`
遇到多余闭合括号，停在 `cargo fmt`。早些时候 Rust clippy / 109 项测试通过的结果不能代表
后续并行修改的最终状态；本轮没有改动这些媒体实现。当前应用验证不等于全仓验收。
Docker 构建尝试在拉取 Node 基础镜像时遇到配置镜像站 `docker.1panel.live` EOF，未完成构建，
不能宣称容器部署通过。当前已验证的是宿主机同源 API + SPA 运行路径。

尚未验收或尚未实现：Runtime 安装/启停/卸载、方案发布、任务执行/恢复、模型结果持久化接线、
素材原片定位、语义索引、MCP/对外 gRPC、容器运行、远程部署、负载压测及崩溃孤儿 Blob 自动回收。
运行方式与资源限制见 [Console 手册](runbooks/console.md)。

#### 登录页演示账号填入（2026-09-23）

新增“填入演示账号”按钮，仅显式配置了独立演示凭据的预览环境启用；普通 API 默认返回
`enabled=false`。预览 prepare 创建 `demo`，不暴露 `admin` 的随机密码。停用演示账号或
修改密码后，服务端不再返回演示凭据。Proto 已重新生成。

前端构建、Ruff、Prettier 与 11 项 PostgreSQL 集成测试通过。真实浏览器点击后验证用户名为
`demo`、密码已填入且仍在登录页；再点击登录成功进入视频库并显示“演示用户”，无运行时异常。
截图：`.data/console-preview/demo-login.png`。
