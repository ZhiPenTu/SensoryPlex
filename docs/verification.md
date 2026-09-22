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
  （本条记录的是当时状态；抽帧已于 2026-09-23 接入并从 `blockers` 移除，见下文"M1 自适应抽帧"一节，
  其余判断仍然有效。）
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
