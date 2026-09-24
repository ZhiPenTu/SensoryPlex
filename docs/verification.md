# 工程初始化验证记录

本记录对应 0.1.0 工程底座，不代表完整 V1 媒体/模型链路验收。

最新素材查询与回看验收见 [2026-09-24 专项记录](verification-material-review.md)。

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
  （2026-09-24 收口：宿主侧"有没有"已由 ADR-022 的 `host_accelerators` 单独上报，见文末；该句其余判断仍然有效。）

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
- 模型链路全部未接入：ASR/OCR/VLM/BGE 无实现，CoreML/Metal 后端仍报 `execution_backend_not_implemented`
  （2026-09-24 收口：执行后端仍是"本进程不执行推理"，宿主"有没有"改由 ADR-022 的 `host_accelerators` 回答，见文末）。
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

- 只有 VLM 一个模型：ASR / OCR / BGE **未接入**；CoreML / Metal 仍 `execution_backend_not_implemented`
  （2026-09-24 收口：宿主"有没有"改由 ADR-022 的 `host_accelerators` 回答，见文末）。
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

- 只有两个模型（VLM + ASR）；**OCR 与 BGE 未接入**；CoreML / Metal 仍 `execution_backend_not_implemented`
  （2026-09-24 收口：宿主"有没有"改由 ADR-022 的 `host_accelerators` 回答，见文末）。
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

#### 补记：容器化之后远端 CI 曾全线失败（2026-09-23，run 35886176518 → 35887640419）

OCR 提交（`6b22cd9`）触发的 run **35886176518 三个 job 全部在第一个 `make proto` 就失败**：
macOS runner 报 `make: docker: No such file or directory`，ubuntu runner 报
`couldn't find env file: .../.env`。根因不是 OCR 代码，而是更早的一次改动：`76743e6` 把
`Makefile` 全面**容器化**（`docker compose exec` + 必需 `.env`），但 `.github/workflows/ci.yml`
没有同步——也就是说**"容器化"这件事从未在远端被验证过**，直到下一次 push 才暴露。

修复（`90ef107`）给 `Makefile` 增加 `EXEC_MODE ?= container | host`：host 模式只做前缀退化
（`EXEC_API=` 空、`PY_API=uv run --frozen python`），并把 `test-py` 拆成不依赖外部服务的
`test-contracts` 与需要 PostgreSQL 的 `test-integration`；`ci.yml` 三个 job 统一
`EXEC_MODE=host`。关键约束是**两种模式展开后的步骤集合必须逐字相同**（本机用
`make check EXEC_MODE=container` 的展开结果与修复前对比过），否则"主机退化"会变成"悄悄少跑"。

本机实测（macos-aarch64，2026-09-24）：`make proto EXEC_MODE=host` 生成目录无差异；
`make check EXEC_MODE=host`（临时 PostgreSQL 容器）契约 112 + 集成 11 通过；
`make lint-ruff test-contracts EXEC_MODE=host`（无数据库）112 passed；
`make runtime-smoke EXEC_MODE=host` PASS。远端 **run 35887640419 三个 job 全绿**（2m56s）。

（BGE 切片之后契约测试总数已从 112 增至 154，见文末 M8 BGE 一节。）

#### 补记二：容器模式的 `make check` 其实一直跑不通，且镜像是过期的（2026-09-24）

上面那次修复只验证了 host 模式；**默认的容器模式仍然没跑通过**。`90ef107` 把集成测试
前缀写成 `EXEC_TEST = $(EXEC_API) -e SENSORYPLEX_TEST_DATABASE_URL`，展开后是
`docker compose exec -T api -e VAR ...`——`-e` 必须写在 SERVICE **之前**
（`docker compose exec [OPTIONS] SERVICE COMMAND`），于是 `make check` / `make integration`
在容器模式下直接 `exec: "-e": executable file not found in $PATH`（错误 127），
集成测试一次都没执行过。改成 `$(COMPOSE) exec -T -e SENSORYPLEX_TEST_DATABASE_URL api`
后才真正跑起来。修好后立刻暴露两条**镜像侧**缺口：

1. `services/api/Dockerfile` 只往容器 venv 里注入 `vlm-moondream` / `asr-whisper-mlx`
   两个插件；OCR 提交（`6b22cd9`）与 BGE 提交新增的 `ocr-rapidocr` / `embed-bge-onnx`
   从未进过镜像，容器里 `pytest tests/contracts` 直接 collect error
   （`ModuleNotFoundError: No module named 'edge_material_plugin_ocr_rapidocr'`）。
   而且这两个插件的契约测试断言的是**真实发行版元数据**
   （`installed_version("onnxruntime")` 不许编、`installed_version("rapidocr")` 不许空），
   所以镜像必须真装 `onnxruntime` / `tokenizers` / `rapidocr`；这一条显式**不加 `|| true`**，
   装不上就让镜像构建失败，而不是让容器里的契约测试事后红成一片。
2. 重跑 `docker compose build api` 时，Docker Desktop 的文件共享缓存给了一次**过期构建
   上下文**：镜像里 `site-packages/sensoryplex_api/interfaces/assets.py` 的 md5 与仓库、
   与任何 git revision 都不相同（缺 `/v1/materials/{key}/sources/{asset_id}` 路由），
   于是 `tests/integration/test_material_review.py` 12 个用例全部 404 失败——
   看起来像"代码回归"，实际是镜像旧了一个文件。再 build 一次后 baked 文件 md5 与仓库一致
   （`d75d2cf8e1f886401351a16dac43f7c6`）。教训：容器模式跑集成测试前先核对
   **镜像内 baked 源码 vs 仓库源码**（`diff -r` 或 `md5`），不要把过期镜像读成代码回归。

修完两条后的实测（macos-aarch64，2026-09-24）：`make check EXEC_MODE=container` 全绿——
ruff `All checks passed` + `117 files already formatted`、契约 **154 passed**、
集成 **24 passed**（此前是 12 failed / 12 passed）、`cargo fmt --check` / `clippy -D warnings` /
`cargo test --workspace` 全过。

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

### M8 OCR：真实帧 → 本机 PP-OCR，以及 CoreML 执行后端的实测约束（2026-09-23）

本节记录 M8 的第二个剩余子项：**画面文字识别**（蓝图第 5 周"帧 → 文字块"）。第三个模型插件
`plugins/python/processors/ocr-rapidocr` 消费 Runtime 数据面里的**真实视频帧**，用**随包携带**的
PP-OCR ONNX 权重产出带帧像素坐标、来源与显式"无置信度"语义的文字块。决策与边界见
[ADR-016](adr/ADR-016-OCR与ONNX执行后端.md)。

**接入的模型：** `rapidocr 3.9.2` + `onnxruntime 1.30.0`，权重不再是"下载一个文件"而是
**三个模型的组合**（det/cls/rec）。身份来自**实际被会话加载的那三个文件**的字节摘要，
`artifact_digest` 是三者按角色排序折叠的组合摘要（不是配置里的版本号）：

| 角色 | 文件 | 字节 | SHA-256（独立复算） |
| --- | --- | --- | --- |
| det | `PP-OCRv6_det_small.onnx` | 9 929 594 | `090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f` |
| cls | `ch_ppocr_mobile_v2.0_cls_mobile.onnx` | 585 532 | `e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c` |
| rec | `PP-OCRv6_rec_small.onnx` | 21 234 383 | `6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884` |
| **组合** | — | — | `31df9f5afcc7dacbf15acc380117833d2fece9f894f8e7f533f8b89cd3dc2cf6` |

`tools/verify_ocr.py` **自己**重新计算这四行（独立实现，不调用插件代码），并要求 observation 的
`provenance.modelArtifactDigest` 与之逐字相等。

**验收命令：** `make ocr-check MEDIA=... [EXPECT=text|empty] [PROVIDER=cpu|coreml]`
（四进程：编排 / `runtime replay --handoff-listen` / 插件 / `tools/ai_worker.py`）。

#### 实测结果（三个真实样本，全部走完整四进程链路）

| 场景 | 样本 | 帧 | 结果 | 单帧推理 |
| --- | --- | --- | --- | --- |
| 有文字（默认 `EXPECT=text`） | `screencast-video2commons.480p.vp9.webm` 854×480 | 2 | 各 **6 块 / 87 字**，首块 `Jak nahrát video do Commons` | 228 ms / 216 ms |
| 无文字（`EXPECT=empty`） | `video/1.mp4` 540×960（风电塔风景） | 2 | **0 块**，`empty_reason=model_found_no_text` | 170 ms / 142 ms |
| CoreML（`PROVIDER=coreml`） | 同上 screencast 854×480 | 1 | 6 块（与 CPU 结果一致） | **1163 ms** |

三次运行都通过：`Describe` 只声明 `media.video_frame` / `observation.ocr_blocks` / `cpu_shared_memory`；
锚点等于源帧半开区间（`timing_source=media_pts`）、`content_hash` 等于该帧 lease 窗口摘要、
`confidence` 缺省且写明原因、每个块的四点框落在帧内且归一化坐标在 `[0,1]`；
账目 `released + expired + retained == retained_total`、`retained=0`、`arena_live_slabs=0`；
插件 stdout/stderr 与 worker 报告都没有媒体名/路径。

#### CoreML 执行后端：选中了，但**不是加速**（本轮最重要的"负面证据"）

| 观察项 | CPU（`provider=cpu`） | CoreML（`provider=coreml`） |
| --- | --- | --- |
| 三个会话的 `get_providers()` | `['CPUExecutionProvider']` | `['CoreMLExecutionProvider', 'CPUExecutionProvider']`（首选 CoreML） |
| 断言 `execution_provider_not_selected` | 通过 | 通过（首选确实是 CoreML） |
| 同一帧推理耗时 | 216–228 ms | **1163 ms（约 5 倍慢）** |
| 单引擎 + 一帧常驻内存（`/usr/bin/time -l`） | ≈ 610 MiB | ≈ 2.4 GiB |
| ORT stderr | 无 | 大量 `E5RT ... unbounded dimension which is not supported ...` 与 `p2o_pd_op_*` |

`unbounded dimension` 说明 PP-OCR 的**动态 shape 与 NMS 子图无法编译成 CoreML 网络**，ORT 把这些
子图**分区回退到 CPU**。所以"会话首选是 CoreML"**不等于**"全部算子跑在 ANE/GPU"，本版本也因此
**不宣称 CoreML 加速**：只交付"请求显式、实际 provider 可观测、不一致就显式失败"的可选择后端，
默认仍是 `cpu`。manifest 的 `resources.memory` 取两条路径的上界加余量（`3Gi`）。

ONNX Runtime 在 macOS 上**没有独立的 Metal EP**（Apple 侧的执行后端就是 CoreML EP），因此本切片
不引入 `metal` 后端；Apple GPU 的使用在本项目里是间接的：ASR 走 MLX（原生 Metal）、VLM 走 ollama。

#### 本轮暴露并修掉的 3 个真实缺陷（不是"一次就过"）

| # | 现象 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | `provider=coreml` 时**第一次**实测得到 `execution_provider_not_selected:...CoreMLExecutionProvider`——原因串里的"实际值"和"请求值"是同一个东西 | `_backend_string()` 从 `self.config.provider` 反推"请求了哪个 provider"，而 `self.config` 要到 `configure()` 末尾才赋值；Start 期间它还是**默认的 `cpu`**。于是"请求 CoreML"被拿默认 CPU 去比对——正是本 ADR 要禁止的那类**静默降级**，只不过表现为误报失败而不是误报成功 | 请求值改为**显式入参**（`_session_summary(requested)` / `_backend_string(providers, requested)`），不再从可变状态反推；契约测试 `test_requested_provider_must_actually_be_selected` 锁死两侧（一致→通过、不一致→失败、会话缺失→失败） |
| 2 | `getattr(rapidocr, "__version__", "")` 恒为空 | `rapidocr` 模块没有 `__version__`（它的模块级 `__getattr__` 会直接抛 `AttributeError`），于是 payload 里的 `engine.runtime_version` 会写成 `unknown`——而它正是"这份结果由哪个运行时算出来的"证据 | 改用 `importlib.metadata.version("rapidocr")`（实测 `3.9.2`）与 `"onnxruntime"`；读不到发行版元数据时显式写 `unknown`，验收脚本把 `unknown` 判为失败 |
| 3 | 验收脚本自己误判两次：`plugin does not declare it consumes video frames`；`runtime reported unimplemented capabilities: ['max_points_truncated','decode_truncated']` | 前者：拿 **buffer 的 kind**（`video_frame`）去比**能力串**（`media.video_frame`）。后者：`--max-points` 是本次刻意设定的解码上限，运行时把它**诚实**记进 `blockers`，脚本却把所有 `blockers` 当成"能力未实现" | 验收脚本区分 `CONSUMES = "media.video_frame"`；`blockers` 只拒绝含 `not_implemented` 的条目（截断是设定，不是缺失） |

缺陷 1 值得单独记一笔：它的**表现**是"明明选了 CoreML 却报失败"，修掉之后 CoreML 才真正被选中。
也就是说这套断言的价值不在于"平时是否通过"，而在于它**不依赖默认值**。

#### 测试与静态检查

- 新增 `tests/contracts/test_ocr_plugin_contract.py`：**23 项**。覆盖输入准入（`memory_kind`、
  `buffer_id`/`kind` 缺失、stream 不匹配）、像素布局四条显式拒绝、**RGBA→BGR 通道顺序**、
  锚点/摘要/`timing_source`、`confidence` 缺省 + 原因、稳定 observation ID（同字节同 ID、
  不同摘要不同 ID）、空结果 `empty_reason`、越界（`MAX_BLOCKS` 计数 + 超长块失败）、
  provider 断言两侧、manifest 摘要/`local_native`/SBOM/schema/网络白名单与可写路径、
  摘要范围（改说明文字不变、改代码必变）、ONNX 容器探测（空/非 protobuf/正常）、
  组合摘要顺序无关且覆盖三角色、`installed_version` 不编造版本号。
- `uv run ruff format --check .` 与 `uv run ruff check .`：全仓通过（101 文件）；
  `uv run pytest tests/contracts -q`：**112 passed**。

#### 仍未验证（不得当作完成）

- 识别**质量**：没有准确率/召回基准，也没有按语言、字号、字体分层的评测——本切片只保证
  **链路与几何语义**正确，不保证"认得准"。样本偏拉丁与俄文字符，中文界面样本尚未覆盖。
- CoreML 的收益：见上，实测更慢；动态 shape 与分区回退未解决。
- `metal`（ONNX 路径）、`linux-x86_64`、Mac mini、跨机：均未验证。
- 插件仍未签名（只写明白原因），SBOM 只有结构预检；权重缓存目录与"绝不联网"只有 manifest
  声明（`allowedHosts: [www.modelscope.cn]`、`writablePaths: []`），没有 DNS/egress 强制执行。

### M8 BGE：真实上游 OCR 事实 → 本机 BGE 向量与维度版本化（2026-09-24）

第四个模型插件 `plugins/python/processors/embed-bge-onnx` 接在 OCR 后面：**消费上游观测**
（`observation.ocr_blocks` 里的真实文字）而不是字节，产出 `observation.text_embedding`。
因此这条链路的验收除了向量本身，还必须验"它真的没碰数据面"。决策见
[ADR-017](adr/ADR-017-BGE文本向量与维度版本化.md)。

#### 命令与角色

```bash
# 完整链路：验收自己先跑一遍真实 OCR 链路（四进程）产出 ocr_blocks，再让 BGE 消费它
make embed-check MEDIA=video/samples/screencast-video2commons.480p.vp9.webm
# 只换编码后端（coreml 必须真的被会话选中，否则显式失败）
make embed-check MEDIA=... PROVIDER=coreml
# 复用既有上游报告，跳过重跑 OCR（本机实测 1.2 s）
uv run --frozen python tools/verify_embed.py --media <sample> \
  --input-observations <sensoryplex-ocr-*/ai-worker.json> [--provider coreml]
```

角色与 M8 其他插件同构，但第 2 个角色是**上游链路的真实执行**、不是生产者进程：

1. `tools/verify_embed.py`（编排 + 对账）；
2. `tools/verify_ocr.py` 的完整四进程链路（Runtime replay → OCR 插件 → worker），产出真实文字块；
3. `python -m edge_material_plugin_embed_bge_onnx`（插件进程，Start 时真建会话并跑前向探针）；
4. `tools/ai_worker.py --input-observations`（worker：原样转发上游事实，不接数据面）。

本机实测（M2 Max / 32 GiB，`macos-aarch64`，2026-09-24）：

```text
upstream: OCR chain observations=2 released=19 engine=org.sensoryplex.ocr-rapidocr provider=cpu
weights:  .../snapshots/75c43b069aac4d136ba6bc1122f995fedcfd2781
  encoder: sha256:15b717c3...cd19bcc (24010842 bytes)
  model_config: sha256:d4193ead...6538ff42f (716 bytes)
  tokenizer: sha256:48cea5d4...339e8fae26 (439125 bytes)
  combined: sha256:1d01788f...aa117271 dimension: 512
embedded_inputs=2 observations=2 dimension=512 provider=cpu backend=CPUExecutionProvider

embed acceptance: real OCR facts -> local BGE (cpu) -> versioned-dimension vectors passed in 65.2s
```

四份摘要由 `tools/verify_embed.py` **独立复算**（不调用插件代码），并要求 observation 的
`provenance.modelArtifactDigest` 与 payload 里三个角色的 `sha256`/`bytes` 逐字相等——
"身份来自实际加载的文件字节"因此不是声明，而是被第三次复算过的事实。

#### 这条链路验了什么（都是真实执行结果）

- **不接数据面**：`Describe.memory_kinds == []`；worker 报告 `input_mode == "observation"`、
  `data_plane is None`、**没有** `runtime_stats` 键、`drain == {"discarded": 0, "failures": [],
  "leases": 0}`、`runtime_stats_after == {"leased": 0, "leases": 0}`。零 lease 是显式写出来的账目，
  不是留空。
- **维度版本化**：`dimension == 512` 同时等于本脚本独立读出的 `config.json.hidden_size` 与向量长度；
  `dimension_source == "config.json:hidden_size+probe_forward"`；
  `vector_index_key == "material_text_bge_small_zh_v1_5_d512_v1"`。
- **文本到向量的绑定**：`content_hash == text_sha256 == sha256(实际被编码的文本)`，而该文本由验收
  脚本**按契约独立重拼**（`join_separator="\n"`、跳过空块并计数）；`block_count`/`blank_blocks`/
  `char_count`/`source_modality` 逐项与上游块对账。
- **上游身份不丢**：`payload.input.*` 的 `observation_id`/`content_hash`/`stream_id`/`source_item_id`/
  `quality_state`/`time_range`/`model_release_id` 与上游观测逐项相等；`timeRange` 与
  `timing_source` 原样继承（本插件不重新计时）。
- **池化/归一化/置信度**：`pooling=cls`、`normalize=l2`、`norm == 1.0`（独立复算）、
  `vector_sha256` 由向量 float32 小端字节独立复算；`confidence` 缺省且带原因；
  `storage=inline_payload`、`vector_ref is None`（本切片**没有**向量库）。
- **负路径在活进程上验**：worker 收尾 Stop 之后重新 Start（状态可重入），再对同一个进程发三种
  不该接受的输入，全部拿到稳定原因码而不是"成功但结果为零"：
  `buffer_reader_not_attached`（喂 buffer）、`unsupported_input_modality:video_frame`
  （把上游观测的 modality 改错）、`input_text_empty`（`blocks=[]`）。
- **不外泄**：worker 报告与插件 stdout/stderr 里没有媒体名/绝对路径/`srt://`/`rtmp://`，
  payload 里也没有 `"vocab"`（整份词表塞不进来），且 payload 体积有上界（512 维向量的 JSON 约
  12 KB，上限 128 KB）。

#### cpu 与 coreml 的实测（同一份权重、同一批文字）

| 口径 | cpu | coreml |
| --- | --- | --- |
| session providers | `['CPUExecutionProvider']` | `['CoreMLExecutionProvider', 'CPUExecutionProvider']` |
| 单条短文本编码 | 0.78 ms | 3.16 ms |
| 链路内单条 observation（含分词/池化） | 1.03–1.26 ms | 5.01–9.19 ms |
| 同文本两次编码 | 向量摘要相同 | 向量摘要相同 |
| 跨 provider | — | `cos ≈ 1.0`，最长文本 0.996986（`max|Δ| = 1.08e-2`） |
| `norm` | 1.0 | 1.0 |

结论与 OCR（ADR-016 §5）一致：**`coreml` 是可选择、可观测、必须真的被选中的后端，但在本模型上
更慢（约 4–8 倍）**，因此默认仍是 `cpu`，本切片不宣称 CoreML 加速。跨 provider 的微小差异来自
量化算子在不同 EP 上的分区与求值顺序；同一 provider 内是确定的（同文本两次得到同一个向量摘要）。
Apple Silicon 上 ONNX 路径**没有**独立 Metal EP：GPU 是间接使用的（CoreML EP / ASR 走 MLX /
VLM 走 ollama），`metal` 不作为后端引入。

语义**合理性**（不是质量基准）：`cos("今天天气不错", "明天天气很好") = 0.8094`，而
`cos("今天天气不错", "端侧推理在本地运行") = 0.2745`、`cos("今天天气不错", "股票市场今日下跌")
= 0.4988`。向量空间顺序合理，但这**不是**召回/排序基准。

#### 本轮暴露并修掉的 3 个真实缺陷

| # | 现象 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | 用真实 HF 权重目录启动时 `model_file_not_found` | HF 快照把 ONNX 放在 `onnx/` 子目录，`tokenizer.json`/`config.json` 在快照根；插件原先把 `model_file` 当纯文件名 | `model_file` 允许是 `model_dir` 内的相对路径；绝对路径与 `..` 越界被拒（契约测试覆盖子目录 + 三种越界） |
| 2 | `--input-observations` 时插件拒绝配置 | worker 在 observation 路径仍注入 `handoff_endpoint` 与 `endpoint`/`model` 默认键，被插件 schema 的 `additionalProperties: false` 拒绝 | worker 只在 buffer 模式注入 `handoff_endpoint`，按输入路径决定模型服务默认键 |
| 3 | 验收脚本误判 `token_count` 与 `input.time_range` | payload 是 protobuf `Struct`：数字在 JSON 里都是 double（脚本却要求 `isinstance(int)`）；`Struct` 键保留 snake_case 而真实 proto 字段是 camelCase，两个 dict 直接比较必然不等 | 按"整数性"而不是类型比较；`time_range` 显式按键取值比较 |

缺陷 1 只在**真实权重目录**上才会暴露：用测试自造的平目录永远跑不出来。

#### 测试与静态检查

- `tests/contracts/test_embed_bge_onnx_contract.py`：**42 项**。覆盖输入准入（buffer / 非
  `ocr_blocks` / 上下文不匹配 / 身份校验）、文本边界六类、CLS 池化与 L2 归一化语义、稳定 ID、
  `content_hash` 与 `input.*` 的分工、provenance 与稳定 release、provider 断言两侧、
  `model_file` 子目录与越界拒绝、三份权重的组合摘要顺序无关、ONNX 容器探测（空/非 protobuf/正常）、
  `max_length` 超过位置编码上限、manifest/SBOM/schema/网络与可写路径、摘要范围（改说明文字不变、
  改代码必变）。
- `uv run pytest tests/contracts -q`：**154 passed**（含 BGE 42）。
- `uv run ruff format --check .` / `uv run ruff check .`：全仓通过。

#### 远端 CI（2026-09-24，两轮都全绿）

本切片两个提交（`a9a1a66` 容器验收修复、`a970569` BGE 插件）在远端各跑了一轮完整
`engineering-checks`，**三个 job 全绿**：

- run **35896034916**（`codex/embed-bge-onnx`，push，17:30:09Z）；
- run **35896037193**（`master`，push，17:30:10Z）：`check` 3m19s、`check-console` 59s、
  `check-apple-silicon` 3m36s（`macos-15-arm64`，含 `uname -m` 硬断言与解码路径单测）。

**流程异常（需要维护者确认）**：`master` 上这次更新**没有经过 Pull Request**。
`gh pr create` 当时返回 `No commits between master and codex/embed-bge-onnx`——即分支 push
之后（1 秒内）`master` 已被快进到同一个提交，push 事件的操作者是同一个账号。本机
`git` 侧没有任何 hooks / `remote.origin.push` refspec 能产生这种行为（已核对
`.git/config`、`core.hooksPath`、全局配置），所以这是一次**外部**动作，不是本切片主动
直推 `master`；无论来源如何都不符合新近起草的 `AGENTS.md`"禁止直推 master"约定，
建议维护者查一下是不是有脚本/其它会话在自动快进 `codex/*` → `master`。

#### 仍未验证（不得当作完成）

- 向量**质量**：没有检索/排序基准（召回、MRR），没有中文长文本、跨语言或领域文本评测；
  仓库登记样本里的屏幕文字是拉丁/俄文，中文界面样本尚未覆盖。
- 向量库：本切片不落库（`storage = inline_payload`、`vector_ref = null`）；`vector_index_key`
  只命名了 collection，Milvus 建索引、写入与检索都未验证。
- 维度版本化的**迁移**：换模型或换维度后旧向量重建还是并存，尚未决策。
- CoreML 收益：见上，实测更慢；动态 shape 与量化算子的分区回退未解决。
- 运行时（Rust）执行后端仍记为不可用：本版本没有任何 in-process `ExecutionBackend` 实现
  （`model_inference` 仍在 `unavailable_capabilities`）。这张表**不是**"CoreML 不可用"的证据，
  只说明"运行时自己不做推理"；宿主侧"有没有这块加速器"已由 ADR-022 的 `host_accelerators`
  单独探测并上报（见文末"M8 剩余：宿主加速器能力探测与上报（ADR-022）"）。
- `linux-x86_64`、Mac mini / 跨机未验证；插件未签名（只在 manifest 写明白原因），SBOM 只有结构预检；
  "绝不联网"只有 manifest 声明，没有 DNS/egress 强制执行。
- worker 的 durable 幂等与 lease 崩溃回收仍未做；observation 路径没有 lease，但这不改变 buffer
  路径的结论。

### M8 剩余：运行时消费分级队列上限（ADR-019）（2026-09-24）

**本轮收口的是一条自己写下来的缺口**（ADR-015 §5）：`queue_capacity` 只被校验、**没有被运行时消费**。
先核实事实再动代码：改动前 `SENSORYPLEX_MEDIA_QUEUE_CAPACITY` 与 `SENSORYPLEX_MODEL_PARALLELISM`
在全仓**没有任何 crate 读取**；描述符之后的真实有界队列是保留表（`RetainPolicy::default()` 的
`retained_limit=32`，带 `--handoff-listen` 时取 `config.retained_limit`），分级值只由
`macos_resident.py probe` 报告。决策与两个稳定失败原因见 [ADR-019](adr/ADR-019-运行时消费分级队列上限.md)。

环境：真机 Apple M2 Max / 32 GiB / macOS 26.x / arm64（`macos-aarch64`）；容器是 Linux aarch64
（Docker Desktop），因此**运行时的 Rust 侧只能在主机执行**——`target/release/sensoryplex-runtime`
是 Mach-O，容器内无法 exec；Python 侧校验按要求留在容器里（`docker compose exec -T api`）。
样本 `video/1.mp4`（hevc 540x960 + aac，真实授权样本）；报告写在 `<worktree>/target/*.pb`，
容器以 `/workspace/target/*.pb` 读到同一份文件。

| 验证 | 命令 | 结果 |
| --- | --- | --- |
| 未注入分级 | `sensoryplex-runtime replay config/pipelines/file-material.yaml …/video/1.mp4 --report target/tier-a.pb` | 运行照常完成；`queue_capacity state=not_injected declared=32 tier_capacity=not_injected tier=not_injected retained_limit=32` |
| `small` 档拒绝**声明值**越界 | `SENSORYPLEX_RESIDENT_TIER=small SENSORYPLEX_MEDIA_QUEUE_CAPACITY=16 … --report target/tier-b.pb` | `Error: … "queue_capacity_exceeds_tier_cap: declared=32 tier_capacity=16 tier=small"`，exit=1，且 `target/tier-b.pb` **不存在**（准入先于写报告） |
| `small` 档拒绝**保留窗口**越界 | `sed 's/queue_capacity: 32/queue_capacity: 16/' config/pipelines/file-material.yaml > target/file-material-declared-16.yaml`，再以 `small`/16 跑该 pipeline | 声明值 16 合规，但 `Error: … "retained_limit_exceeds_tier_cap: retained_limit=32 tier_capacity=16 tier=small"` —— **声明合规不等于队列合规** |
| `medium` 档拒绝数据面注入的窗口 | `SENSORYPLEX_RESIDENT_TIER=medium SENSORYPLEX_MEDIA_QUEUE_CAPACITY=32 … --handoff-listen 127.0.0.1:64555 --handoff-retained-limit 64 --handoff-arena-bytes 67108864 --handoff-wait-timeout-ms 1000` | 立即 `Error: … "retained_limit_exceeds_tier_cap: retained_limit=64 tier_capacity=32 tier=medium"`，exit=1，未打开数据面、未写报告 |
| `large` 档准入 | `SENSORYPLEX_RESIDENT_TIER=large SENSORYPLEX_MEDIA_QUEUE_CAPACITY=64 … --report target/tier-c.pb` | `queue_capacity state=admitted declared=32 tier_capacity=64 tier=large retained_limit=32`；报告 `media_queue.state=admitted` |
| 包装脚本注入（真实 `resident.env`） | 容器内 `macos_resident.py render --output /workspace/target/resident-render`（声明 32 GiB，`source=env`）→ 主机 `SENSORYPLEX_RESIDENT_ENV=…/resident.env …/sensoryplex-media-run replay config/pipelines/file-material.yaml …/video/1.mp4 --report target/tier-h.pb` | banner `分级 large: retained_limit=64 arena_bytes=134217728 queue_capacity_cap=64 model_parallelism=3`；运行时报 `tier=large tier_capacity=64` |
| 调用方预置同名环境变量 | `SENSORYPLEX_MEDIA_QUEUE_CAPACITY=4096 … sensoryplex-media-run replay …` | exit=2，`[media-run] SENSORYPLEX_MEDIA_QUEUE_CAPACITY 由常驻分级决定，不要在调用环境里预置` |
| plist 带上分级上限 | `render` 产出的 `runtime.plist` | `SENSORYPLEX_MEDIA_QUEUE_CAPACITY=64`、`SENSORYPLEX_MODEL_PARALLELISM=3` 已在 `EnvironmentVariables` 里（此前只有 handoff/内存/RUST_LOG） |
| 容器内校验报告自洽 | `docker compose exec -T api /app/.venv/bin/python tools/verify_replay.py --verify-only --media /host-media/1.mp4 --pipeline config/pipelines/file-material.yaml --report target/tier-h.pb` | 通过：`Replay verified: … anchors=2237 dropped=2 gaps=0 descriptors=1354 leases=1354/1354 segments=7 … queue_capacity=admitted/declared=32/tier_capacity=64/retained_limit=32` |
| `make check EXEC_MODE=container` | 见 Makefile | ruff `All checks passed`（117 文件已格式化）、契约 **155 passed**、集成 **24 passed**、`cargo fmt`/`clippy -D warnings`/`test` 全过 |
| `make media-check` / `make media-test` / `make pipeline-check` | 见 Makefile | `--features gstreamer` 的 clippy 通过；`sensoryplex-media` **105 passed**；`pipeline schema valid` |

**本轮踩到的两点环境事实（写下来避免重复踩）**

- **新增 proto 字段后必须重建 api 镜像**：`edge_material_sdk` 的生成代码是 Dockerfile 在构建时
  `COPY plugins/python` 装进 venv 的，`PYTHONPATH=/workspace` 不会覆盖它；不重建镜像时容器里的
  `report.media_queue` 会直接 `AttributeError`（等同于"验证跑的是旧契约"）。重建后与工作区一致。
- **媒体 E2E 的进程边界**：`verify_replay.py` 会 exec `target/release/sensoryplex-runtime`，
  而容器是 Linux、二进制是 macOS Mach-O；因此 Rust 侧在主机跑、Python 侧校验在容器跑，
  两边通过 `<worktree>/target/*.pb` 与 `/host-media/*`（bind mount）共享产物。

**提交前复跑（2026-09-24，`30794f8`）**：格式化与最终校验后，用同一份 release 二进制
在主机重跑了上表两条关键路径，结果与表格一致（未注入 → `anchors=2237 dropped=2 gaps=0
descriptors=1354 leases 1354/1354 segments=7`；`small`/16 → `queue_capacity_exceeds_tier_cap:
declared=32 tier_capacity=16 tier=small`，exit=1，报告文件不存在），并在容器内用
`--verify-only` 校验该报告的 `media_queue` 字段（`queue_capacity=not_injected/declared=32/
tier_capacity=0/retained_limit=32`）。

#### 远端 CI（2026-09-24）

本切片提交 `30794f8` 在远端跑了一轮完整 `engineering-checks`，**三个 job 全绿**
（run **35900927870**，`pull_request`，PR #4）：`check` 3m51s、`check-console` 55s、
`check-apple-silicon` 3m45s（`macos-15-arm64`）。

**流程异常（与上一轮 BGE 切片同类，需要维护者确认）**：PR #4 在 CI 变绿约 45 秒后
（`mergedAt=2026-09-23T18:17:45Z`）被**非 bot** 账号 `ZhiPenTu`（与本机同一个账号）合并，
merge commit `460ecf2`。本次**没有**由我执行 `gh pr merge`，也**没有**发生"绕过 PR 的快进"：
分支 push 前后各核对一次 `git ls-remote origin master`，两次都是 `0f40139`。仓库内找不到任何
自动合并机制（`.github/workflows/` 只有 `ci.yml`；全仓 grep `pr merge` / `auto-merge` /
`peter-evans` 无命中），私仓也没有 branch protection / rulesets（API 返回需要 GitHub Pro）。
所以这仍是一次**外部**动作（另一个会话或手工点击），建议维护者确认来源。

**该异常导致本节记录本身"滞后于合并一次"**：本节随 `b2e5914` 追加在 `30794f8` 之后，而
`master` 已停在 `460ecf2`（PR #4 的 merge commit，只含 `30794f8`）；本节经后续 PR 补入。

**仍未验证（不得当成完成）**

- `SENSORYPLEX_MODEL_PARALLELISM` 仍只有"已声明"这一层：`serve` 把它转述给
  `DescribeCapabilities.residency`，但没有任何 worker 按它限流，也没有并发执行的端到端样本。
- 档位覆盖：真机只跑了"未注入 / `small` 拒绝 / `medium` 拒绝 / `large` 准入"四种情形；
  `xlarge` 档与 24 GiB、64 GiB 之类的档位边界机型未实跑；`linux-x86_64` 未验证。
- 本轮做的是**准入**，不是"用 pipeline 文件驱动队列深度"：真实队列深度仍来自
  `--handoff-retained-limit`（包装脚本按分级注入）。`golden_path_verified` 恒为 false。

**一个既有现象（与本轮改动无关，未修复）**：同一套命令用 `sintel-trailer.480p.h264.mp4` 跑时，
`tools/verify_replay.py` 的 `check_track` 会失败——ffprobe 报 `duration_ms=52208`，解码侧视频
`last_end_ms=52209`，差 1 ms 使 `last_end_ms <= duration_ms` 不成立（本轮未触及解码与轨道统计路径；
`video/1.mp4` 无此现象）。登记在此，供后续单独处理。

### M8 剩余：向量索引落库与检索闭环（ADR-020）（2026-09-24）

第四个模型插件（BGE）刻意**不落库**：ADR-017 把它写成 `storage=inline_payload`、`vector_ref=null`。
本轮补上它身后的 sink：`services/index-worker` 把插件产出的向量写进向量库并**读回来确认**，
再提供一条把同一批向量检索回来的闭环。决策见
[ADR-020](adr/ADR-020-向量索引落库与检索闭环.md)。

#### 命令与角色

```bash
# 1) 上游：真实 OCR → 真实 BGE 权重，得到真实向量（不是自己造的向量）
uv run --frozen python tools/verify_embed.py \
  --media video/samples/screencast-video2commons.480p.vp9.webm --keep-workspace
# 2) 落库与检索闭环（真实 PostgreSQL 隔离 schema + 真实迁移 + 真实 Milvus Lite）
make index-check EMBEDDINGS=/var/folders/.../sensoryplex-embed-XXXX/ai-worker.json
```

角色四方：验收脚本（编排与对账，主机）→ `python -m sensoryplex_index_worker.cli`
（独立进程，`index`/`search`/`inspect`）→ 真实 PostgreSQL（本次新建隔离 schema，跑真实迁移
`0001`–`0003`）→ Milvus（本机 Milvus Lite **文件形态**；服务端形态同一客户端与同一 collection 契约，
但本机拉不到镜像，见下）。

#### 实测结果

```
workspace: /var/folders/.../sensoryplex-index-i158cr6e
database: 127.0.0.1:25432/sensoryplex_test (from env-file)
embeddings: 2 from ai-worker.json          # 真实 BGE 观测：dimension=512
vector uri: .../vector-edge.db  collection: material_text_bge_small_zh_v1_5_d512_v1  dimension: 512
Applied 0001_initial / 0002_console / 0003_embedding_index

index acceptance: real BGE vectors -> Milvus (2 rows) -> PostgreSQL provenance passed in 10.6s
```

11 个场景（同一轮全部执行，任何一条不满足即整体失败）：

| # | 场景 | 实测证据 |
| --- | --- | --- |
| 1 | 真实落库并确认 | 两条都 `state=ready`、`confirmed=true`、`vector_ref=milvus://material_text_bge_small_zh_v1_5_d512_v1/emb_…`；PostgreSQL 侧 `vector_ref`/`indexed_at` 都有值且 `error_code IS NULL` |
| 2 | 换进程重新打开同一个 Milvus | `inspect` 在新进程里数到 2 行 |
| 3 | 检索并回查事实 | 自检索 `distance≈1.0`；`material_unit_id`/`stream_id`/`start_ms`/`end_ms` 与素材事实一致，`dimension=512` |
| 4 | 非 owner 命中必须丢弃 | `results=[]`、`unindexed_hits=2`（Milvus 不是鉴权依据） |
| 5 | 被标 `failed` 的记录即使还在库里也不返回 | `unindexed_hits=1` |
| 6 | 幂等重跑 | 同一批 `embedding_id`、collection 不变、行数仍为 2 |
| 7 | 维度篡改（payload 声明 513，key 声明 512，向量长 512） | `vector_dimension_mismatch`，`detail=key=512 declared=513 actual=512`；PostgreSQL 留下一行 `failed`（带原因码），向量库行数不变 |
| 8 | 向量库不可达（`/dev/null/milvus-edge.db`） | 整轮失败：顶层 `error_code=vector_store_unavailable`（`detail=ConnectionConfigException`），`indexed=[]`，无 ready 行 |
| 9 | collection 契约漂移（同名但缺字段） | 两条观测都 `vector_collection_contract_mismatch`，`indexed=[]` |
| 10 | 不外泄 | 向量库里恰好 2 行；grep 被编码的原文片段、主机路径、DSN 全部找不到 |
| 11 | 数据目录被别的进程 flock 持有 | `vector_store_locked`（`detail` 只有文件名）、`indexed=[]`、ready 行数不变；持有者退出后同一目录立刻可用（`inspect rows=0`） |

场景 7/8/9/11 的失败面都是**结构化**的，不是"日志里有一行 warning"：

```json
{"failed":[{"detail":"key=512 declared=513 actual=512","observation_id":"obs_ab6a19ea…","reason_code":"vector_dimension_mismatch"}],"indexed":[<未被篡改的那一条>]}
{"error_code":"vector_store_unavailable","failed":[{"observation_id":null,"reason_code":"vector_store_unavailable"}],"indexed":[]}
{"failed":[{"detail":"material_text_bge_small_zh_v1_5_d512_v1","observation_id":"obs_ab6a19ea…","reason_code":"vector_collection_contract_mismatch"},{"…第二条同样是 contract_mismatch…"}],"indexed":[]}
{"error_code":"vector_store_locked","detail":"vector-locked.db","indexed":[]}
```

场景 9 之后直接查事实表，能看到"拒绝也要留痕"（`--keep-workspace` 保留隔离 schema）：

```
emb_cec0b975…|failed|milvus://material_text_bge_small_zh_v1_5_d512_v1/emb_cec0b975…|vector_collection_contract_mismatch|512
emb_1953e314…|failed|milvus://material_text_bge_small_zh_v1_5_d512_v1/emb_1953e314…|vector_collection_contract_mismatch|512
```

#### 本轮暴露并修掉的真实缺陷（7 条，全部由"真库 / 真库形态 / 真进程"抓出）

1. **回查 SQL 引用了不存在的列**：`material_unit` 的列是 `revision`，
   查询写成 `m.material_revision` → 真实 PostgreSQL 直接
   `psycopg.errors.UndefinedColumn: column m.material_revision does not exist`。纯函数测试全绿也照样漏。
2. **契约不符被兜底 `except` 吞掉**：`ensure_collection` 的通用包装把 `IndexContractError`
   改写成 `vector_collection_create_failed`，稳定原因码在最后一跳丢失。
3. **被拒绝的观测不留痕**：维度守卫在 `begin_pending` 之前抛出，于是"拒绝了"在库里查不到——
   等同于静默丢弃。改成先落 `pending` 再标 `failed`。
4. **锁冲突被读成"库不可用"**：Milvus Lite 的目录锁在 pymilvus 里变成一个笼统的
   `ConnectionConfigException`（异常链断在起本地服务的线程），于是"稍后重试就行"与
   "配置写错了"分不开。改成按同一个锁文件预检（`local_store_locked` → `vector_store_locked`）。
5. **验收脚本自身的进程边界**：Milvus Lite 的目录锁是进程级的，父进程开过的目录子进程打不开，
   而场景 9 要验的正是"子进程打开漂移 collection"。漂移 collection 改由子进程建。
6. **新增迁移必须同步 `/v1/health` 的版本集合**：`0003_embedding_index` 加进去之后，
   `SCHEMA` 仍是 `0002_console`，而健康检查断言的是"库里的迁移集合恰好等于镜像认识的集合"，
   `/v1/health` 直接 503 `schema_version_mismatch`——真实集成测试
   （`tests/integration/test_metadata.py`）当场变红。修法是 `SCHEMA` + `SCHEMA_VERSIONS`
   两处一起更新（见 `services/api/src/sensoryplex_api/app.py`）。
7. **api 镜像里没有 index-worker**：Dockerfile 只 `COPY services/api`/`services/gateway`，
   容器里的契约测试会 collect error。补 `COPY services/index-worker` 与
   `uv pip install ./services/index-worker`（**不加** `|| true`：装不上就让镜像构建失败，
   而不是让容器里的测试事后红成一片）。

#### 两条必须记住的环境事实

- **本机 Docker Hub 不可达，`milvusdb/milvus` 拉不下来**：
  `docker pull milvusdb/milvus:v2.5.10` → `Get "https://registry-1.docker.io/v2/": EOF`。
  `deploy/compose/docker-compose.vector.yml` 里的 etcd（quay.io）与 MinIO（pgsty）镜像在本地存在，
  但缺 Milvus 本体，**standalone 拓扑起不来**。因此本轮全部验收跑 Milvus **Lite 文件形态**，
  服务端形态**未经写入与检索验收**（ADR-020 §6）。
- **Milvus Lite 是进程独占的**：数据目录带 flock，同一路径不能被两个进程同时打开。
  edge 形态因此是"单写进程"：写入者与检索者不能并存；`vector_store_locked` 就是这条约束的稳定码。

#### `make check`（本切片收口，2026-09-24）

| 模式 | 结果 |
| --- | --- |
| `make check EXEC_MODE=host`（macos-aarch64，`SENSORYPLEX_TEST_DATABASE_URL` 指向 `127.0.0.1:25432`） | ruff `All checks passed` + `128 files already formatted`；契约 **198 passed**；集成 **31 passed**；`cargo fmt`/`clippy -D warnings`/`test --workspace` 全过 |
| `make check EXEC_MODE=container`（重建 api 镜像 + `make migrate` 应用 `0003` 后） | ruff `All checks passed` + `128 files already formatted`；契约 **198 passed**；集成 **31 passed**；`cargo fmt`/`clippy`/`test` 全过 |

契约与集成的增量都来自本切片：`tests/contracts/test_index_worker_contract.py`（**43** 项，纯判定与形状）
与 `tests/integration/test_index_records.py`（**7** 项，真实 PostgreSQL + 真实迁移）。
后者专治"只有真库能暴露"的缺陷：它直接断言 `0003` 的三条不变式在数据库里生效
（`embedding_record_ready_is_confirmed` / `embedding_record_ready_has_no_error` /
`embedding_record_failed_has_reason` 的约束名都能被 `CheckViolation` 逐个对上）。

#### 远端 CI 与合并（2026-09-24，PR #6）

本切片推成 [PR #6](https://github.com/ZhiPenTu/SensoryPlex/pull/6)（head `76d6b47`），
远端 workflow `engineering-checks` 在 push 与 PR 各跑一轮，**三个 job 全绿**：

| run | check | check-apple-silicon | check-console |
| --- | --- | --- | --- |
| 35908250264 | 3m13s | 2m13s（`macos-15-arm64`） | 1m4s |
| 35908283472 | 3m16s | 3m36s（`macos-15-arm64`） | 1m35s |

合并由仓库所有者账号 `ZhiPenTu` 执行（**本次没有**出现 ADR-019 切片里 PR #4/#5 那种
"非本人执行的自动合并"），合并提交 `88557d2`，`master` 从 `644bb8d` 前进到 `88557d2`。

提交前在同一棵树上复跑：`make index-check`（10.4s，11 个场景）、容器内
`tests/contracts/test_index_worker_contract.py`（43 passed）与
`tests/integration/test_index_records.py`（7 passed，容器 DSN）。

另有一条**运行迁移时必须知道的环境事实**（本轮真的踩到）：`migrate` 服务复用 gateway 镜像，
`db/migrations` 是 `COPY` 进镜像的而不是 bind mount，所以**新增迁移文件后不重建镜像，
`make migrate` 会读到旧镜像里的迁移集合、什么都不应用并以 0 退出**。本轮 `0003` 就是这么
"跑过了但没生效"，直到 `docker compose ... build gateway` 之后同一条命令才输出
`Applied 0003_embedding_index`。已写进 `docs/runbooks/development.md`。

#### 远端 CI：从"账号计费拦截"到第一次真跑（2026-09-24）

本切片开 PR 后远端三个 job 全红，但**红的原因不是代码**：三个 job 的 `steps` 都是空数组、
`runner_id = 0`，check-run 的注解原文是

```
The job was not started because recent account payments have failed or your spending limit
needs to be increased. Please check the 'Billing & plans' section in your settings
```

分界线很清楚：最后一次全绿是 `#55`（2026-09-23 20:31Z，真 runner，三个 job 分别 14 / 20 / 14 个
step），`#56`（20:36Z）起**连续 20 次**全红——其中包括 master 自己的 push（`#73` = `36fc487`，
即已合并的 PR #13）。所以这段全红**既不能读成"PR #14 的代码验证失败"，也不能读成"流水线坏了"**：
它是账号侧计费被拦，与提交内容无关，重跑没有意义。

把仓库改成 public（标准 runner 对公开仓库免费）后重跑 `#75` 的 attempt 2，job 才第一次真正落到
runner 上（`runner_id != 0`、`steps` 有内容）：`check-console` success，`check` 与
`check-apple-silicon` 都停在 `test-contracts`——这才是本切片真正的第 5 条缺陷。

| run | 触发 | 结论 | 说明 |
| --- | --- | --- | --- |
| `#55` | push `master` | success | 最后一次真跑成功的流水线 |
| `#56`–`#74`（19 次） | push / pull_request | failure（无效） | `steps = []`、`runner = 0`：账号计费拦截 |
| `#75` attempt 1 | pull_request `codex/index-pipeline` | failure（无效） | 同上 |
| `#75` attempt 2 | pull_request `codex/index-pipeline` | failure（真实） | 公开仓库后真跑，暴露第 5 条缺陷（ADR-027 §10.5） |
| `#76` / `#77` | push / pull_request `codex/index-pipeline` @ `e7c6480` | success | 第 5 条修好后三 job 全绿（`check` 15 step、`check-console` 14 step、`check-apple-silicon` 20 step），流水线恢复可用 |

该缺陷的复现与修复（容器内摘掉 `SENSORYPLEX_DATABASE_URL` 以对齐 CI 条件——CI 既没有仓库
`.env` 也没有这个变量，而本机容器两者都有）：

```
$ docker compose --env-file .env -f deploy/compose/docker-compose.poc.yml exec -T -w /workspace \
    api sh -lc 'env -u SENSORYPLEX_DATABASE_URL /app/.venv/bin/python -m pytest tests/contracts -q'

# 修前
E   pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
E   database_url
E     Field required [type=missing, ...]
FAILED tests/contracts/test_semantic_search_contract.py::test_blank_search_configuration_counts_as_unconfigured
1 failed, 14 passed in 1.38s

# 修后
363 passed in 5.04s
```

修复后的本机回归（容器内，与 CI 同一组命令）：

| 验证 | 结果 |
| --- | --- |
| `make lint-ruff` | `ruff check` 全过、`format --check` **161** 文件已格式化 |
| `make test-py` | 契约 **363 passed**（4.68s）+ 集成 **65 passed**（21.72s） |
| `make check` | 全过（ruff + 契约 + 集成 + cargo fmt/clippy/test） |

#### 仍未验证（不得当成完成）

- **常驻消费未接线**：没有 NATS/outbox 轮询把上游观测喂给 index-worker，本轮只有显式 CLI 调用，
  因此**没有**常驻 index-worker 服务/容器；`deploy/` 下也没有对应服务。
- **网关语义检索仍是 501**：`mode=semantic` 未接这批向量，RRF/混合检索、相关性排序均未做。
  （本切片的这道缺口已由 [ADR-023](adr/ADR-023-网关语义检索接线与索引检索面.md) 收口，
  见下文"网关语义检索接线（ADR-023）"；排序面仍未做。）
- **向量质量未验收**：没有带参考文本的检索样本，所以没有 recall/MRR/排序基准；
  本轮只证明"同一向量能取回自己的事实"。
- **共享向量去重未做**：同一段文字在多个 material 下会各存一份向量（`embedding_id` 含 material 作用域）。
- **服务端 Milvus / `linux-x86_64` / Mac mini 未验证**；`xlarge` 之外的机型档位未跑。
- 未做 worker 侧的 durable 幂等与崩溃回收（重跑靠确定性 `embedding_id` + upsert，不是靠事务队列）。
- `golden_path_verified` 仍恒为 false。

### M8 剩余：模型 worker 按分级并发上限限流（ADR-021）（2026-09-24）

ADR-015 §5/§7 与 ADR-019 §4/§7 反复登记的同一条缺口——`SENSORYPLEX_MODEL_PARALLELISM` 只有
"已声明"，没有任何执行点读它——本轮收口。决策见
[ADR-021](adr/ADR-021-模型worker按分级并发上限限流.md)。

#### 命令与角色

```bash
# 主机侧一键验收：未改动的 tools/verify_ocr.py × 3 个真实授权样本 → 真实 replay → 真实 OCR 插件
#                                    → 本切片改动的 tools/ai_worker.py（13 个场景）
make parallelism-check MEDIA="/abs/…/screencast-video2commons.480p.vp9.webm \
  /abs/…/slides-vrt-nodiscussion.480p.vp9.webm \
  /abs/…/slides-vrt-discussion.480p.vp9.webm" INPUTS=4
```

四个角色：验收脚本（编排与对账，主机）→ `sensoryplex-runtime replay`（真实解码与抽帧）→
真实 OCR 插件（`ocr-rapidocr`）→ **模型 worker**（本切片的限流对象）。
其中 `--runtime` 场景会**真起** `sensoryplex-runtime serve`，先独立 `DescribeCapabilities` 对账，
再把它交给 worker 的 `--runtime` 当作上限权威。

#### 实测结果

工作区 `/var/folders/.../sensoryplex-parallelism-1wbv1tv7`，耗时 208.7 s。上游批次是 **4 条真实观测**
（`obs_13bf4277…` / `obs_71d8cfcc…` / `obs_1dbea528…` / `obs_5a9cde84…`，来自
`screencast-video2commons` 2 条、`slides-vrt-nodiscussion` 1 条、`slides-vrt-discussion` 1 条）。

| # | 场景 | 实测 `model_concurrency`（逐字取自各 `*.json`） |
| --- | --- | --- |
| 1 | 什么都不注入 | `state=not_injected limit=1 requested=null source=none tier=not_checked tier_capacity=null peak_in_flight=1 retries=0 throttle_events={}` |
| 2 | `--model-parallelism 1` | `state=admitted limit=1 source=flag peak_in_flight=1 retries=0`（与改动前的串行语义一致） |
| 3 | flag 全开（`=4`） | `limit=4 source=flag peak_in_flight=4 attempts=10 completed=4 failed=0 exhausted=0 retries=6 throttle_events={"concurrency_limit": 6}` |
| 4 | env 全开（`=4`） | 与 #3 同形，仅 `source=env` 不同 |
| 5 | `--model-parallelism 2` | `limit=2 source=flag peak_in_flight=2 retries=1 throttle_events={"concurrency_limit": 1}` |
| 6 | env = `0` / 空串 / `abc` / flag = `abc` | `state=rejected`：`invalid_resident_limit: SENSORYPLEX_MODEL_PARALLELISM=0`、`… is set but empty`、`…=abc`、`invalid_resident_limit: --model-parallelism=abc`（各 exit 2，`frames=[]`——一条输入都没跑） |
| 7 | env=3 且 flag=2 | `state=rejected`：`model_parallelism_conflict: env=3 flag=2` |
| 8 | `--runtime`（真起 `large` 档 `serve`）、无请求值 | `state=admitted limit=3 source=runtime tier=large tier_capacity=3 peak_in_flight=3 retries=3 throttle_events={"concurrency_limit": 3}` |
| 9 | 同一次运行里 flag=2 ≤ 档位上限 3 | `limit=2 source=flag tier=large tier_capacity=3 peak_in_flight=2`（请求值不被上限顶掉，但 `tier`/`tier_capacity` 仍被带出） |
| 10 | 同一次运行里 flag=8 > 档位上限 3 | `state=rejected`：`model_parallelism_exceeds_tier_cap: requested=8 tier_capacity=3 tier=large`（**不夹取**到 3） |
| 11 | 运行时转述 `medium`（上限 2）、无请求值 | `limit=2 source=runtime tier=medium tier_capacity=2 peak_in_flight=2` |
| 12 | `--runtime` 指向不可达端点 | `state=rejected`：`runtime_capabilities_unavailable:UNAVAILABLE`（**不**退化成"没有上限"） |
| 13 | 同一个坏值在 Rust 与 Python 两侧 | `bad_resident_limit_parity: rust==python: invalid_resident_limit: SENSORYPLEX_MODEL_PARALLELISM=abc` |

场景 #3 报告里的关键细节（`flag_all.json`）：

```
frames[].attempts = [1, 2, 3, 4]                     # 4 条输入各自的重试次数，第 4 条重试了 3 次
drain = {'discarded': 0, 'failures': [], 'leases': 0}   # observation 路径不接数据面
runtime_stats_after = {'leased': 0, 'leases': 0}
attempts=10 completed=4 failed=0 exhausted=0 retries=6 peak_in_flight=4 max_attempts=4
```

**本轮拿到的关键新证据**：插件侧那道 `concurrency_limit` 闸门**真实发生了**
（BGE 插件 `max_concurrency=1`，而 worker 侧并发 4），并且被有界重试吸收到 4/4 全产出——
没有一条输入因为"可重试拒绝"而静默消失。ADR-019 §7 写下"未吃透"的那一点，至此有了实测。

#### 双语对照（同一个坏值在两侧必须是同一个原因串）

`tools/model_limits.py` 刻意**不用裸 `int()`**（`int("３")` 会通过，而 Rust 的 `str::parse::<usize>()` 不认），
改用 `^\+?[0-9]+$`。场景 #13 是端到端对照：Rust `serve` 与 Python worker 对
`SENSORYPLEX_MODEL_PARALLELISM=abc` 给出**逐字相同**的 `invalid_resident_limit: SENSORYPLEX_MODEL_PARALLELISM=abc`。

#### 静态与回归检查

| 检查 | 结果 |
| --- | --- |
| `make check EXEC_MODE=container` | ruff（`All checks passed!`）、契约 **230 passed**、集成 **31 passed**、cargo fmt / clippy（`-D warnings`）/ test 全过，exit 0 |
| `make check EXEC_MODE=host`（宿主 `DSN` 注入 `SENSORYPLEX_TEST_DATABASE_URL`） | 同上，exit 0 |
| `tests/contracts/test_model_limits.py` | **32 passed**：纯判定 + 契约形状替身（`ScriptedPlugin`、用 barrier 验跨线程峰值），不启真实模型 |

#### 仍未验证（不得当成完成）

- **档位覆盖**：真机只跑了"未注入 / flag 1 / flag 2 / flag 4 / env 4 / 坏值 / 冲突 / 运行时权威 /
  运行时限内请求值 / 运行时越界 / 运行时 medium / 端点不可达"这些情形，全部来自
  `macos-aarch64` / M2 Max / 32 GiB 一台机器；`small` 档（16 GiB）、Mac mini 各档位与
  `linux-x86_64` 未实跑。
- **并发压力来源**：`file-material` 回放按 `min_interval_ms=1000` 抽帧，单样本只交付 1–2 帧，
  所以 4 路并发的"压力"来自**多给样本**，不是单样本高吞吐；高帧率下的背压（上游满、worker 追不上）
  仍未验证，`retry_exhausted` 的路径只在契约测试的替身插件里覆盖。
- **常驻形态未接线**：worker 仍是**验收脚本形态**（CLI），没有常驻服务、没有跨进程队列节流；
  `MODEL_PARALLELISM` 只约束**单次 worker 进程内**的在飞调用数，不约束"同时起几个 worker"。
- `golden_path_verified` 恒为 false，本切片不改变这一结论。

#### 远端 CI 与合并（2026-09-24，PR #8）

本切片推成 [PR #8](https://github.com/ZhiPenTu/SensoryPlex/pull/8)（head `f5ef700`），
远端 workflow `engineering-checks` 在 push 与 PR 各跑一轮，**三个 job 全绿**：

| run | check | check-apple-silicon | check-console |
| --- | --- | --- | --- |
| 35915782118 | 3m44s | 3m26s（`macos-15-arm64`） | 1m1s |
| 35915826468 | 3m35s | 3m13s（`macos-15-arm64`） | 1m5s |

合并由仓库所有者账号 `ZhiPenTu` 执行（本次同样**没有**出现 ADR-019 切片里 PR #4/#5 那种
"非本人执行的自动合并"），合并提交 `5a7fbd4`，`master` 从 `ef26bfc` 前进到 `5a7fbd4`；
`git ls-remote origin master` 与本地 HEAD 已核对一致。

本轮**没有**动 `deploy/up.sh`——工作区里它有一处与 ADR-021 无关的既有改动（console 反代 reload），
保持未提交状态。

### M8 剩余：宿主加速器能力探测与上报（ADR-022）（2026-09-24）

**本轮问题。** ADR-012 §"未验证范围"、ADR-016 §9、ADR-017 §8 与 `docs/TODO.md` 记的是同一个缺口：
`capability::backends()` 把 `cpu` / `coreml` / `metal` **一律**记为 `execution_backend_not_implemented`。
这对**本进程**是事实（`crates/execution` 至今只有 trait，没有任何实现），但报告里再也读不到
"这台宿主有没有这块加速器"，于是"运行时自己不做推理"极易被读成"这台机器没有 CoreML"。
两件事被塌进了同一个字段。

**改动。** `proto/runtime/v1/runtime.proto` 新增 `AcceleratorState`（`AVAILABLE` / `UNAVAILABLE` /
`UNKNOWN` + `UNSPECIFIED`）与 `HostAccelerator`，`DescribeCapabilitiesResponse` 新增
`repeated HostAccelerator host_accelerators = 7`；`crates/runtime/src/accelerator.rs` 做真实探测
（`PROBE_TIMEOUT = 3s`，超时即 kill 并落 `UNKNOWN`），`capability::describe()` 接线，
`serve` 启动日志多一行 `accelerators=...`。`backends` 的语义**没有**改动，仍然全部 `Unavailable`
（本版本没有任何 in-process `ExecutionBackend`）。

#### 四路对账（`make accelerator-check`，本机 Apple M2 Max / `macos-aarch64`）

| 路径 | 断言 | 实测 |
| --- | --- | --- |
| ① 基线与**独立解析**的宿主直读逐条对账 | 状态与版本必须逐字等于 `system_profiler` / `plutil` 的直读值；`evidence` 无路径、有界 | `coreml=available(3520.5.1)`、`metal=available(metal4)` |
| ② `LANG=zh_CN.UTF-8` | `(accelerator, state, runtime_version)` 完全不变 | 与基线相同（判定只认 `spdisplays_*` / `sppci_*` 键名） |
| ③ `PATH=/nonexistent` | 工具缺失者必须落 `UNKNOWN` 且原因是 `probe_tool_missing:<source>`，**不许**落 `UNAVAILABLE` | `coreml=unknown`、`metal=unknown` |
| ④ 与执行后端分离 | 同一份报告里 `backends` 仍全部 `unavailable` 且带原因 | 3 条后端全 `unavailable`，加速器表不覆盖它 |

```
$ make accelerator-check
平台 macos-aarch64；被测二进制 /Users/tuzhipeng/Documents/SensoryPlex/target/release/sensoryplex-runtime
① 基线与宿主直读对账
  [('coreml', 1, '3520.5.1'), ('metal', 1, 'metal4')]
  coreml: framework_info ['framework=CoreML.framework', 'cf_bundle_version=3520.5.1']
  metal: system_profiler ['sppci_model=Apple M2 Max', 'sppci_cores=30', 'spdisplays_mtlgpufamilysupport=spdisplays_metal4']
② LANG=zh_CN.UTF-8 下判定不变
  [('coreml', 1, '3520.5.1'), ('metal', 1, 'metal4')]
③ PATH=/nonexistent：探测工具缺失必须落 unknown，不能落 unavailable
  [('coreml', 3, ''), ('metal', 3, '')]
④ 宿主加速器表不得替执行后端说话
  backends 全部 unavailable（3 条），accelerators 见 ①：两者结论互不覆盖
宿主加速器上报验收: PASS; platform=macos-aarch64; host_accelerators=[('coreml', 1, '3520.5.1'), ('metal', 1, 'metal4')]
```

（`1` = `ACCELERATOR_STATE_AVAILABLE`，`3` = `ACCELERATOR_STATE_UNKNOWN`。）

#### `serve` 启动日志（`RUST_LOG=info`，ADR-015 的同一行）

```json
{"timestamp":"2026-09-24T00:15:07.174461Z","level":"INFO","fields":{"message":"runtime control endpoint started","address":"127.0.0.1:50998","platform":"macos-aarch64","state":"degraded","accelerators":"coreml=available(3520.5.1),metal=available(metal4)","tier":"not_injected","media_queue_capacity":0,"model_parallelism":0},"target":"sensoryplex_runtime"}
```

#### 顺带修掉的既有缺陷：`make runtime-smoke` 在本机（容器模式）**必然失败**

改动前该目标在容器内执行 `tools/smoke_runtime.py`，而它启动的是**主机构建的 Mach-O 二进制**
（`target/debug/sensoryplex-runtime`）：Linux 容器 `exec` 不了它，结果是超时失败——
"验证跑的是旧契约"的另一种形态。现在这一步与 `embed-check` / `parallelism-check` 同类，
固定在主机侧执行（`PY_HOST`），并且脚本会断言加速器行、`backends` 与"探测不到 ≠ 不存在"。
被验收的二进制不存在时脚本直接显式失败，不再靠超时暴露问题。

```
$ make runtime-smoke
Rust server / Python gRPC client: PASS; platform=macos-aarch64; host accelerators=[coreml=available(3520.5.1), metal=available(metal4)]; unavailable backends and capabilities reported explicitly
```

#### 静态与回归检查

| 检查 | 结果 |
| --- | --- |
| `make check EXEC_MODE=container`（重建 api 镜像后） | ruff（`All checks passed!`）、契约 **233 passed**、集成 **31 passed**、cargo fmt / clippy（`-D warnings`）/ test 全过，exit 0 |
| `cargo test --locked -p sensoryplex-runtime --lib` | **22 passed**（新增 13 项：解析器分支、三个探测函数的三态、`probe_*` 原因串、超时被 kill） |
| `make accelerator-check` | 四路对账 PASS（见上） |

#### 仍未验证（不得当成完成）

- **`linux-x86_64` 的 `cuda` 分支从未在真机上跑过**：只有夹具单测，没有 NVIDIA 主机的
  `make accelerator-check` 记录，也没有带 GPU 的 CI job。远端 `check` job 是无 GPU 的 ubuntu
  runner，那里 `cuda` 只能落 `unknown(probe_*:nvidia_smi)`——`make runtime-smoke` 在那种环境下会
  接受这个结论，这正是"探测不到 ≠ 不存在"该有的行为，但它**不构成**cuda 分支可用性证据。
- **`linux-aarch64` 没有任何探测路径**（报告里 0 条），不是"已验证没有加速器"。
- **Mac mini / 跨机未验证**：只在开发机 `macos-aarch64` 上实测。Mac mini 是明确的端侧目标
  （ADR-008），需要单独跑一次 `make accelerator-check` 才能当作已验收。
- **`tensorrt` 只存在于 proto 的命名说明里**，没有探测路径，也不会被上报。
- **加速器可用 ≠ 推理可用**：Rust 侧仍无任何 in-process `ExecutionBackend`，`model_inference` 继续在
  `unavailable_capabilities` 中；插件侧的后端选择（ADR-016 / ADR-017）与本表不共享数据。
- `metal` 的 `runtime_version` 是 Metal **家族令牌**（`metal4`），`coreml` 的是 framework 的
  `CFBundleVersion`，两者不同名，不得横向比较。
- 探测是**首次 `DescribeCapabilities` 时做一次并缓存**（`OnceLock`），不是持续监控。

### M8 剩余：网关语义检索接线（ADR-023）（2026-09-24）

#### 命令与角色

```bash
# 主机侧一键验收（真实 BGE 插件进程 → 真实 Milvus Lite → 常驻检索面 → 真实 API 走 HTTP）
make semantic-check

# 容器内回归（api 镜像重建后；契约 + 真实 PostgreSQL 集成）
make test-py EXEC_MODE=container
```

固定在**主机**执行的理由与 `cargo` / `index-check` 相同：被验收的两样东西（Milvus Lite 数据目录、
HF 权重）只存在于主机。脚本 `--keep-workspace` 可留现场（工作区落在 `$TMPDIR/sensoryplex-semantic-*`）。

#### 实测结果（`make semantic-check`）

```
semantic search acceptance: real BGE -> Milvus Lite -> resident surface -> API hydration passed in 9.5s
```

本次实测的契约事实（脚本自报）：release `bge:bge-small-zh-v1.5@1d01788f1813`、
collection `material_text_bge_small_zh_v1_5_d512_v1`、`dimension=512`、
`index_version=milvus-flat-cosine-v1`。

| # | 场景 | 结果 |
| --- | --- | --- |
| 1 | 真实观测落库（Milvus + PostgreSQL 两侧对账） | 3 条 `ready` + `confirmed`，release 唯一；另一进程 `inspect` 看到同一批行与维度 |
| 2 | 检索面必须显式带令牌 | 不带令牌的 `serve` 拒绝启动（`index_auth_token_required`） |
| 3 | 起常驻检索面（持有向量库的唯一进程） | 就绪即对外服务，报告 encoder / collection / `index_version` |
| 4 | 端到端语义检索（真实编码 + 真实近邻 + 真实水合） | `mode=semantic`，`hits` 与 `materials` 同序同长，距离有界 |
| 5 | 非 owner 命中被丢弃 | 不进结果、计入 `unindexed_hits`，**不表现成"没有命中"** |
| 6 | keyword 不排名 | `hits` 为空，语义字段为空/0 |
| 7 | 线上真正传的东西（裸 gRPC） | 不外泄路径、令牌、权重目录等受控引用 |
| 8 | 令牌不符 | 503 + `semantic_index_unauthenticated` + `retryable=false` |
| 9 | 检索面不可达 | 503 + `semantic_index_unreachable` + `retryable=true`，响应里**没有** `materials` |
| 10 | 未配置 ≠ 曾经 501 | 能力表 false + `semantic_search_unavailable`；配了报 available 且 `reason` 为空 |
| 11 | 同源守卫两种形态 | 整库另一个 release / 同库混装 → 各自稳定码且整请求拒绝；清掉外来行后恢复 |
| 12 | collection 契约漂移 | **启动时**即拒（`vector_collection_contract_mismatch`），不留到第一次查询 |
| 13 | 目录锁 + 停止后可用 | 第二个进程被拒（`vector_store_locked`）；前一个优雅停止后立刻可再起 |

#### 顺带修掉的既有缺陷（5 条，全部由真链路抓出）

1. **`stable_code` 白名单太窄**：`execution_provider_not_selected:CPUExecutionProvider` 这类
   "码 + 冒号参数"被拒；收紧为 `^[a-z][a-z0-9_]{0,59}(:[A-Za-z0-9_.!<>=-]{1,40}){0,2}$` 并加
   `MAX_STABLE_CODE_LENGTH=120`（仍然有界，但不把合法码误判成脏数据）。
2. **HTTP 状态码被当成"是否可重试"的唯一表达**：见 ADR-023 §5；现在状态码只表示失败落在哪一环，
   `retryable` 是独立标记（`semantic_index_unauthenticated` 是 503 却不可重试）。
   该标记经 `HTTPException.headers` 在进程内传递，**不出现在响应头里**。
3. **连接池覆盖 DSN 的 `options`**：`_session_options()` 原来用
   `options=-c statement_timeout=...` **覆盖**调用方给的会话设置，静默丢掉隔离 schema 用的
   `-c search_path=...`，表现为"连上了却查不到表"（`relation "embedding_record" does not exist`）；
   改为**追加**。这条只在隔离 schema 的集成/验收环境里暴露。
4. **漂移的 collection 拖到第一次查询才失败**：`cli serve` 原来不 `ensure_collection()`，
   于是契约不符被包装成可重试的 `vector_search_failed`，把配置事实伪装成瞬时故障；
   现在启动即拒。
5. **`make gateway-smoke` 的语义断言写在旧契约上**：`services/gateway` 内嵌 `sensoryplex_api`，
   但断言仍写着 `== 501`，于是"旧实现 + 旧断言"自洽地 PASS——一个只证明"两个旧东西还一致"的
   冒烟检查。已同步改成 ADR-023 口径（503 + `semantic_search_unavailable` + `retryable=false`；
   空查询 422），并重建 gateway 镜像后复跑（见上表）。

#### 静态与回归检查

| 检查 | 结果 |
| --- | --- |
| `ruff check` / `ruff format --check`（容器内，`services tools tests`） | `All checks passed!` / `80 files already formatted` |
| `make test-py EXEC_MODE=container` | 契约 **247 passed** + 集成 **43 passed** |
| `cargo check --locked -p sensoryplex-sdk`（主机） | `Finished dev profile`（新增 `index` 模块可编译） |
| `make semantic-check`（主机，重建 api 镜像后复跑） | 13 场景 PASS，9.5s |
| `make gateway-smoke`（重建 gateway 镜像后） | `semantic 503 (semantic_search_unavailable, retryable=false) and 422 on empty query: PASS` |
| 部署态 API 直连（`curl`，未配置检索面） | 503 + `reason_code=semantic_search_unavailable` + `retryable=false`，响应头里**没有** `X-Retryable` |
| `make console-build` + 重建 console 镜像 | 服务中的 bundle 含 `semantic_search_unavailable` 与新文案 |

#### 一条必须记住的环境事实：gateway 镜像内嵌 api

`services/gateway` 是兼容入口，`sensoryplex_gateway.app` 直接
`from sensoryplex_api.app import create_app`，而它的 Dockerfile 把 `services/api` COPY 进镜像。
因此**API 的契约/行为改动必须同时重建 gateway 镜像**，否则两个入口对同一个请求给出不同答案。
本轮真的踩到：`make gateway-smoke` 在旧 gateway 镜像上仍然打印 `PASS`——旧断言写着 `== 501`，
跑的又是旧实现；重建 gateway（并同步改断言）后才是 ADR-023 的 503/422 口径。

#### 远端 CI（2026-09-24，PR #11）

`engineering-checks` 的三个 job（`check` / `check-console` / `check-apple-silicon`）在**任何 step 之前**
就秒失败：`steps: []`，从启动到结束约 3s。这与本分支的改动无关——它是仓库级环境问题，
PR #9 / #10 已记录同一现象。按既有口径处理：**不反复重跑**，也**不**据此声称远端验收通过；
本切片的证据全部来自上面的容器内检查与主机验收。

#### 仍未验证（不得当成完成）

- **常驻 index-worker 消费（NATS/outbox → sink）仍未接线**：检索面是常驻的，但"向量怎么进来"仍只有
  显式 CLI 调用（ADR-020 §9 的同一缺口）。
- **RRF / 混合检索 / 相关性校准未做**：`distance` 是 COSINE 距离，不是置信度；筛选条件只在 keyword
  模式生效（semantic 模式下显式 422）。
- **向量质量仍未验收**：没有带参考文本的检索样本 → 没有 recall/MRR/排序基准。
- **检索面跨机/跨容器部署未验证**：本切片只在本机回环 `127.0.0.1` 上验收；共享令牌 + 回环默认值
  是跨容器接入的**前置条件**，不是"跨机已验收"。
- **服务端 Milvus 拓扑未验收**（Docker Hub 不可达）、`linux-x86_64` 与 Mac mini 未验证。
- 检索面不做 durable 幂等、崩溃回收与排队补偿，`max_concurrency` 是固定上限。

### outbox 分发与消费去重边界（ADR-024）（2026-09-24）

切片：**① 常驻 index-worker 消费的上游那一跳**——把事务性 outbox 的事件确认发到 NATS JetStream，
并把 sink 侧的消费去重原语准备好。边界写在 [ADR-024](adr/ADR-024-outbox分发接线与消费去重边界.md) §9：
`NATS → sink` 的**消费循环**在本切片里**仍未接线**（它不只是接线问题，见下），所以本节的证据
**不代表**"向量已被事件驱动地写进去了"。这条边界已由下一个切片
[ADR-025](adr/ADR-025-常驻消费循环与sink接线.md) 收掉（见本文档"常驻消费循环与 sink 接线（ADR-025）"一节）；
本节保留当时的实测记录。

**接线与依赖**：新增工作区成员 `services/outbox-relay`（`sensoryplex-relay`，依赖 `nats-py` 与
`psycopg`）。`uv lock` 在 api 容器内执行，只新增 `nats-py v2.16.0` 与 `sensoryplex-relay v0.1.0`
两条（diff 纯增量：29 insertions / 0 deletions——`uv lock` 顺带重写过 17 行 nvidia optional-dependency
的 platform marker，去掉 `sys_platform` 守卫会让 macOS x86_64 去装 CUDA wheel，已逐行还原并复核
`uv lock --check` 通过）。api 镜像 `uv pip install ./services/outbox-relay`（不加 `|| true`），
gateway 镜像补 `COPY services/outbox-relay`（`uv sync --frozen --package sensoryplex-gateway`
需要工作区成员目录存在，`migrate` 复用 gateway 镜像）。容器内实测 `nats-py 2.16.0` /
`sensoryplex-relay 0.1.0` 可导入。

**执行位置**：`make outbox-check` **在 api 容器内**跑（与 `index-check` / `semantic-check` 的理由不同）：
它要的只有真实 PostgreSQL 与真实 JetStream，两者都在 compose 里，Milvus Lite / HF 权重 / CoreML
一个都不用。验收脚本自建**独立** stream `sensoryplex-events-acceptance` 与 subject 前缀
`sensoryplex.acceptance.events`，跑完删掉，不碰开发用的 `sensoryplex-events`。

**真实链路证据**（`make outbox-check`，9 个场景全过）：

| # | 场景 | 实测结果 |
| --- | --- | --- |
| 1 | 目标描述 | `--describe` 回 `postgres:5432/sensoryplex`，不含 DSN |
| 2 | 真实建 stream | 读回配置：subjects `["sensoryplex.acceptance.events.>"]`、`storage=file`、`max_age=604800`、`duplicate_window=7200`、上下界与契约一致 |
| 3 | 真实发布并对账 | 拉回 2 条消息，subject = `sensoryplex.acceptance.events.material.upserted`、`Nats-Msg-Id` = outbox 的 `event_id`、载荷与 outbox 行**逐字节**相同 |
| 4 | 确认之后才记账 | 两行 `published_at` 非空且 `attempt=1`，`pending` 归零 |
| 5 | 发不出去就不写 | 契约缺陷（envelope 与行 id 不一致）→ `failed=1` + `event_envelope_mismatch`，行仍未发布、`attempt=1`、`pending=1`；NATS 指向死地址 → exit 1 + `nats_unreachable`、**没有任何 status 行**、账目不变 |
| 6 | 重放去重 | 同一 `Nats-Msg-Id` 重发 → `PubAck.duplicate is True`，流内消息数仍为 2 |
| 7 | 漂移不静默 | 手工造 `max_age` 漂移的 stream → `event_stream_contract_mismatch`（detail 里点名 `max_age`），**读回后仍是漂移值**（没被修好）；删掉后 relay 按契约重建 |
| 8 | sink 去重 | 真 PostgreSQL 上 `record_consumed` 第一次 `True`、第二次 `False`，`consumed_at` 可观察，`consumed_event` 只有 1 行 |
| 9 | 不外泄 | 状态行字段集合与契约一致，无 DSN / schema 名 / 主机路径 / 载荷 |

**测试面**：`tests/contracts/test_outbox_relay_contract.py`（40 条：subject/event_type 准入、
`RelayOptions` 逐项边界、envelope 与行一致性、`stream_contract_diff` 的**秒**口径、
启动连接**有界**（三个假 nats 模块：永不返回 / 连接被拒 / 捕获参数）、状态行形状、
`describe_target` 抹凭据）与 `tests/integration/test_outbox_relay.py`（13 条，真实 PostgreSQL +
真实迁移：确认后才写、失败不写 `published_at`、`attempt` 计数、一批里一条坏事件不拖累其余、
`mark_published` 只翻一次、`(event_id, consumer_name)` 作用域）。容器内全量回归：
`make lint-ruff`（147 files，clean）、`make test-contracts`（**287 passed**）、
`make test-integration`（**56 passed**）、`make check`（含 `cargo fmt` / `clippy -D warnings` /
`cargo test --workspace`，通过）。

**真机跑出来的三个缺陷**（记在 [ADR-024](adr/ADR-024-outbox分发接线与消费去重边界.md) §8）：

1. **`max_age` / `duplicate_window` 的单位是秒**：nats-py 的 `StreamConfig` 声明
   `max_age: Optional[float] = None  # in seconds`，纳秒由库自己换算。按"proto 是纳秒"写成
   `7d * 1e9` 会被**再乘一次 1e9**，服务端回
   `invalid JSON: cannot unmarshal number ... into Go struct field StreamConfigRequest.StreamConfig.max_age`。
   契约测试里专门钉了一条"纳秒写法必须被判为漂移"。
2. **`nats.connect(max_reconnect_attempts=-1)` 连首次连接都无限重试**
   （`_select_next_server` 只在 `max_reconnect_attempts > 0` 时才放弃服务器）。这个坑是**测试先撞上**的：
   一条"缺 DSN 必须显式退出"的用例在容器里被 `SENSORYPLEX_DATABASE_URL` 顶掉，于是真的去连了一次
   NATS，测试挂了 6 分钟没动静（`nats.py` 的 `_select_next_server` 里，`max_reconnect_attempts <= 0`
   时服务器永不被剔除）。现在启动连接有上限（`--connect-timeout-s`，默认 10s）并显式报
   `nats_unreachable`，连上之后仍交给库做无限重连。
3. **`duplicate_window > max_age` 的 stream 建不出来**：验收场景 7 最初就踩在这上面
   （`duplicates window can not be larger then max age`），漂移改成只动 `max_age` 且仍大于
   `duplicate_window`。

另外确认一条**既有 schema 事实**：`consumed_event.consumed_at` 是 `NOT NULL DEFAULT now()`，
没有"在飞"态，所以"先认领、再干活"的两相接口在这个 schema 上表达不出来；做了它会在
"插了行、还没干完"的窗口崩溃时把重投变成**事件永久丢失**。因此 sink 原语是
**先干活后记账**（`record_consumed` 返回 `False` = 重复完成，幂等而非错误），且**不加迁移**。

#### 仍未验证（不得当成完成）

- **NATS → sink 的常驻消费循环没有写（本切片内）**：本轮只做"发布这一跳"。当时判断它不只是接线——
  上游 observation 连 `event_id` 都没有，唯一存在的 `material.upserted` 携带不了 BGE 需要的
  `ocr_blocks` 文本，所以当时认为"消费到 sink"要先补 observation 事件契约。
  **该结论已被下一个切片改写**：[ADR-025](adr/ADR-025-常驻消费循环与sink接线.md) 证明不需要新增
  observation 事件契约——事件只是通知，消费侧按 `payload_ref` 回查 `observation.payload_jsonb`
  即可拿到可编码文本；
- **没有 dead-letter 与重试上限**：`attempt` 只被计数（本轮最多到 1），超过阈值怎么办未定义；
- **relay 没进 compose**：常驻形态靠 `make outbox-run` 显式起（JetStream 里还没有消费者，
  先常驻一个只发不收的进程没有意义）；
- **NATS 无鉴权、无 TLS**：沿用 compose 里回环暴露的本地 NATS（ADR-010 的受控引用边界不变）；
- **没有端到端背压**：ADR-019 的队列上限没有接到 relay，`--batch` 是固定上限；
- 本目标在容器内执行，**没有**单独在 Apple Silicon / Mac mini 上验收（NATS 与 PostgreSQL
  与主机架构无关，但"跑在 MAC mini 家庭工作站上"这句话本轮没有证据）。

### 常驻消费循环与 sink 接线（ADR-025）（2026-09-24）

切片：**① 的下半跳**——把 JetStream 里的事件**真正消费成向量库里 ready 的事实**，并证明
"事件驱动写入的向量能在**同一个进程**的检索面里立刻被检索到"。设计决策与未验证边界见
[ADR-025](adr/ADR-025-常驻消费循环与sink接线.md)。

命令：`make consume-check`（`tools/verify_index_consume.py`，**主机执行**：Milvus Lite 的数据目录
是进程独占的本地文件、BGE 权重也只存在于主机，理由同 `semantic-check` / `index-check`）。

**真实角色（没有替身）**：真实写侧 `append_material`（素材 + 观测 + outbox **同事务**）→
真实 `python -m sensoryplex_relay.cli --once` 进程 → 真实 NATS JetStream（每场景独立 stream 与
前缀，用完删掉）→ `python -m sensoryplex_index_worker.cli serve --consume`（**常驻消费 + 检索面
同进程**，真实 BGE 权重、与检索面同一个编码器实例）→ 真实 Milvus Lite → 真实 gRPC 检索面查询。
事件、素材、观测、向量都不是脚本造的。

| # | 场景 | 实测结果 |
| --- | --- | --- |
| 1 | 事件驱动写入 + 同进程检索 | 真实写侧追加 2 条素材（各 1 条 `ocr_blocks` 观测）→ relay 发布 2 条事件（`published=2`/`failed=0`）→ 常驻消费后 `embedding_record` 2 行 `ready`（`vector_ref = milvus://<key>/<embedding_id>`、`indexed_at` 非空、`dimension=512`）、`consumed_event` 2 行、`model_release` 登记了编码器实测身份；查询文本经真实 BGE 编码后命中**排第一**是对的素材（`hits=2`、`unindexedHits=0`）；非 owner 查询 `hits` 为空且 `unindexedHits=2`（丢弃被计数，不是"没有命中"） |
| 2 | 重放不重复 | 换一个 durable（新消费视角）重投同一批事件：`consumed_total=2` 重新记账（`consumed_event` 变成 4 行），但 `embedding_record` 仍是 2 行、`inspect.rows` 仍是 2（`embedding_id` 确定性 → 向量不重复） |
| 3 | 单写进程与优雅停止 | 消费进程持锁期间第二个进程读同一目录 → `vector_store_locked`；SIGTERM 后退出码 0，且另一个进程能重新打开该目录（`inspect.rows=2`） |
| 4 | 坏事件 fail-stop | 指向不存在素材的事件（唯一一处刻意手写的坏事件）重投到 `max_deliver=2` → 进程**退出码 3** + 状态文件 `consume.fatal`（`event_retry_exhausted` / `event_missing_facts`）；`num_ack_pending >= 1`（没 ack）、该 consumer 的 `consumed_event` 0 行、vector 行 0 行 |
| 5 | 启动期显式失败 | stream 缺失 → 退出码 1 + `event_stream_missing`，且事后确认**流没有被建出来**；手工造一个 `max_deliver=1` 漂移的 durable → `event_consumer_contract_mismatch`；NATS 指向 `127.0.0.1:1` → `nats_unreachable`（三者都在"接上之前"失败，且状态行没有泄露 DSN 与主机路径） |
| 6 | 状态行契约 | `consume.status` 字段集合逐字等于契约的 16 项；整份状态行与就绪行里没有 DSN、主机路径、素材文本、令牌或向量库引用 |

本机一次实跑：`make consume-check` **13.6s** 全过（`outbox: 2 events appended by the real write path`
→ 场景 1/2/3 通过 → fail-stop → 三类启动失败）。

同切片的其余证据（容器内）：

- `tests/contracts/test_index_consumer_contract.py` **53 passed**：subject 精确绑定（不用 `>` 通配）、
  consumer 参数 16 条越界、`parse_payload_ref` 正反例、durable 契约六种漂移、枚举/字符串归一、
  `ensure_consumer` 三条路径（建 / 接受 / 漂移）、`payload_jsonb` → 插件文本契约往返、7 种不可编码
  payload 必须被拒（不截断）、状态行字段集合与不外泄；
- `tests/integration/test_index_consumer.py` **9 passed**（真 PostgreSQL + 真迁移，其中 3 例用真
  JetStream）：记账与重投幂等、缺事实/空文本零写入、只有 VLM 观测也消费、真 JetStream 投递→ack
  （`num_ack_pending == 0`）、坏事件重投到上限 fail-stop、stream 缺失必须报错。
  注意这一层用 `MemoryIndex` / `FixedEncoder` 替身，**不等于**向量真的写进了 Milvus——那由
  `make consume-check` 承担；

  这 3 例真 JetStream 用例在**容器模式**下默认真跑：`EXEC_TEST` 现在按 `TEST_NATS_URL`
  （默认 `nats://nats:4222`）注入 `SENSORYPLEX_TEST_NATS_URL`（与 `SENSORYPLEX_TEST_DATABASE_URL`
  同一套注入方式）。主机模式（含远端 CI）没有 NATS 时它们仍是**显式 skip**，所以这部分的证据
  只来自容器模式。

  **前置**：`consumer.py` / `contract.py` 是新增模块，容器里 import 的是镜像内 `site-packages`
  的副本（bind mount 的源码不参与 import），所以跑这些容器内用例前必须先 `./deploy/up.sh api`
  重建 api 镜像；否则会以 `ImportError: cannot import name 'consumer'` 直接红。

**顺带修掉的缺陷**：`consume_loop` 只把本轮 `failed` 写进状态行、`failed_total` 从不累加 →
常驻进程会永远显示"从没失败过"。契约测试钉的是字段集合、集成测试钉的是本轮计数，累计口径没人钉；
现在循环里累加，集成测试补了断言。另外两处是**验收脚本自己**的缺陷（protobuf JSON 省略 0 与空
repeated → 把"没这个键"读成"丢了 `None` 条"；状态行先写、进程后退出 → 判退出码读到的还是 `None`），
记在 [ADR-025](adr/ADR-025-常驻消费循环与sink接线.md) §10。

`golden_path_verified` 仍恒为 false：真实媒体端到端（真实视频 → Runtime/Timeline → 事件）仍未联调。

### 事件链路分级背压与容器化常驻（ADR-027）（2026-09-24）

切片：收掉 [ADR-025](adr/ADR-025-常驻消费循环与sink接线.md) §11 剩下的两句——**ADR-019 的分级上限
接到事件链路**、**relay 与消费进 compose**。设计决策与未验证边界见
[ADR-027](adr/ADR-027-事件链路分级背压与容器化常驻.md)。

命令：`make events-up`（起常驻）、`make event-pipeline-check`（容器内闭环验收，接 ADR-019 / ADR-024 / ADR-025）、
`make consume-check`（主机，回归）、容器内 `make test-py` 与 `make lint-ruff`。

**真实角色（没有替身）**：真实写侧 `sensoryplex_api.infrastructure.materials.append_material`
（素材 + 观测 + outbox 同一事务）→ compose 里**常驻**的 `relay` 服务 → 真实 NATS JetStream →
compose 里**常驻**的 `index` 服务（真 BGE 编码 → 真 Milvus Lite → 检索面同进程）→ 运行中的 api 进程
`POST /v1/materials:search`（`mode=semantic` 走真 gRPC）。

#### 常驻形态（`make events-up`）

```
 Container sensoryplex-nats-1 Healthy
 Container sensoryplex-index-1 Healthy
 Container sensoryplex-migrate-1 Exited
 Container sensoryplex-postgres-1 Healthy
 Container sensoryplex-relay-1 Healthy
NAME                  IMAGE                   COMMAND                  SERVICE   STATUS
sensoryplex-index-1   sensoryplex-api:0.1.0   "/app/.venv/bin/pyth…"   index     Up 12 minutes (healthy)
sensoryplex-relay-1   sensoryplex-api:0.1.0   "/app/.venv/bin/pyth…"   relay     Up 12 minutes (healthy)
```

`index-ready.json` 的消费块（真机产物）：

```json
{"command": "serve", "uri": "/workspace/.data/index/milvus.db",
 "encoder": {"backend": "onnxruntime-1.30.0/CPUExecutionProvider", "dimension": 512,
             "vector_index_key": "material_text_bge_small_zh_v1_5_d512_v1"},
 "consume": {"batch": 32, "durable": "sensoryplex-index-sink", "inflight_capacity": 32,
             "inflight_declared": 32, "inflight_state": "admitted", "resident_tier": "medium",
             "stream": "sensoryplex-events", "subject": "sensoryplex.events.material.upserted"}}
```

#### 四条准入路径（relay 容器内实测，逐条对照 ADR-019 §3）

| 输入 | 实测输出 | 退出码 |
| --- | --- | --- |
| 变量缺失（`env -u`，真没注入） | `{"batch": 200, "inflight_capacity": 0, "inflight_declared": 200, "inflight_state": "not_injected", "resident_tier": "not_injected", ...}` | 0 |
| 空串 | `invalid_resident_limit: SENSORYPLEX_EVENT_QUEUE_CAPACITY is set but empty` | 1 |
| 声明超档（默认 200 vs `medium` 32） | `event_inflight_exceeds_tier_cap: declared=200 tier_capacity=32 tier=medium` | 1 |
| 声明超档（`small` 16 vs `--batch 64`） | `event_inflight_exceeds_tier_cap: declared=64 tier_capacity=16 tier=small` | 1 |
| 消费侧默认 `--consume-batch 50` vs `medium` 32 | `event_inflight_exceeds_tier_cap: declared=50 tier_capacity=32 tier=medium` | 1 |

**一条真实后果（本轮发现，不是缺陷）**：两个进程的**内置默认深度**在 `small` / `medium` 档都超标
（relay 默认 200、消费默认 50，而两档上限是 16 / 32）。所以在注入了 `resident.env` 的机器上必须显式给
`--batch` / `--consume-batch`；compose 给的是 `${SENSORYPLEX_EVENT_RELAY_BATCH:-16}` 与
`${SENSORYPLEX_EVENT_CONSUME_BATCH:-32}`。这是**显式失败**而不是静默超限——原来的"默认 200 照跑"
现在会被一句话挡住。

#### `make event-pipeline-check`（容器内，连跑两次）

| # | 步骤 | 第一次 | 第二次 |
| --- | --- | --- | --- |
| 1 | 常驻服务准入对账 + 越界探针 | `relay 档位=medium 上限=32 每轮认领=16；index 消费在飞=32`；越界拒绝 `declared=64 tier_capacity=16 tier=small` | 同左 |
| 2 | 基线（写入前） | `unindexed_hits=4`（上一次运行留下的陈旧向量） | `unindexed_hits=5` |
| 3 | 真实写侧落事件 | 2 条事件（顺带清理上次残留 0 行） | 同左 |
| 4 | 向量落库 | 2 行 `ready`（`milvus://material_text_bge_small_zh_v1_5_d512_v1/emb_…`） | 2 行 `ready` |
| 5 | HTTP 语义检索 | `hits=['compose_acceptance_<run>_alpha'] index_version=milvus-flat-cosine-v1`，**第一名是相关素材**（排名对，这才是要钉的） | 同左 |
| 6 | 事实回查挡 stale | `materials=0 unindexed_hits=5`（删除事实后不再返回，陈旧向量如实计数） | 同左 |
| 7 | 清理与不外泄 | `residual_rows: {}`，状态行/响应无 DSN、主机路径、令牌 | 同左 |

结果行（第二次）：

```json
{"capacity": 32, "event": "event.pipeline.acceptance", "events": 2, "failures": [], "index_inflight": 32,
 "mode": "semantic", "query_hits": 1, "relay_inflight": 16, "residual_rows": {}, "tier": "medium",
 "unindexed_hits_after_cleanup": 5, "unindexed_hits_baseline": 5, "vectors": 2}
```

`query_hits` 一开始是 **2**（相关素材 + 无关素材都在窗口里），连跑几轮后变成 **1**：检索窗口是
`limit=5`，而向量库里累积的陈旧向量（每次运行留下 2 条）会把无关素材挤出窗口。**这也正是"相对基线"
判定与"第一名必须是相关素材"这两条断言存在的理由**——命中条数会随残留漂移，排名与
`unindexed_hits` 的相对变化不会。

**为什么判定是"相对基线"而不是"绝对为 0"**：向量本体不随事实行删除而消失（ADR-020 §2 的回查挡住了
"陈旧向量被当成事实返回"，但向量库里的行还在）。脚本因此用**本次运行唯一的批次标记**写文本与查询
——当前批次的向量必然排进窗口最前——并在通过时如实打印留下了几条。想从干净向量库重跑用
`./deploy/down-events.sh --volumes`。

**这条残留直接影响产品行为，不只影响验收**：陈旧向量会占满检索窗口（`limit=5`），所以随着累积，
同一个查询能返回的真实素材条数会变少（其余位置被计入 `unindexed_hits`）。这是**向量 GC 缺失**的
直接后果，不是验收脚本的假象——向量 GC（按 `material_unit_id` 删除 / 按 `indexed_at` 回收）是独立切片。

#### 回归（容器内）

| 验证 | 结果 |
| --- | --- |
| `make test-py` | 契约 **363 passed**（5.25s）+ 集成 **65 passed**（22.48s） |
| `make lint-ruff` | `ruff check` 全过、`format --check` 161 文件已格式化 |
| `make check`（收口：ruff + 契约 + 集成 + cargo fmt/clippy/test） | 全过——契约 363 + 集成 65 passed、Rust workspace 94 + 22 + 9 + 1 + 27 项通过（clippy `-D warnings`） |
| `make consume-check`（主机，带新的档位注入与 4 个新字段） | 6 场景全过 |
| `make semantic-check`（主机） | 全过，**9.2s**（§10 第 4 条修好后；修之前这一场在场景 2 超时 300s 失败） |

#### 顺带修掉的缺陷（4 条）

1. **契约与集成测试被宿主环境绑架**（真跑出来才看见）：api 容器现在真的配了检索面，于是
   `Settings(database_url=...)` 的默认值随环境变——`tests/contracts/test_semantic_search_contract.py`
   与 `tests/integration/test_metadata.py` / `test_semantic_search.py` 里三个"未配置检索面"的用例
   在容器里立刻变红（`assert 'index:50077' == ''`）。这不是"测试太严"，而是**用例没有自己控制环境**
   （`Settings` 既读进程环境也读仓库 `.env`）：新增 `tests/conftest.py::bare_settings`
   （`_env_file=None` + 摘掉 `SENSORYPLEX_INDEX_SEARCH_*`），断言"未配置 / 半配置会被拒绝"的用例改用它。
2. **空串配置会让 api 拒绝启动**：`${SENSORYPLEX_INDEX_SEARCH_TOKEN:-}` 只能给空串，而
   `Settings.validate_index_search` 把它读成"配置了一半"（`index_search_endpoint_required`）——
   任何早于本次改动的 `.env`（`make configure` 生成后不再覆盖）都会让 api 起不来。实测：
   `docker compose exec -e SENSORYPLEX_INDEX_SEARCH_TOKEN= …` 直接 ValidationError；
   第一次写 compose 接线时把 api 的终结点**硬编码**成 `index:50077`，把这个陷阱变成了"必然"
   （终点写死 + 令牌缺省 = 半配置）。现在**空白读成没配**、半配置仍被 `*_required` 挡住，
   契约测试补了三条断言；两条路径都在容器里复测过（空白 → `endpoint='' token=None` 正常启动；
   配齐 → `/v1/health` 报 `semantic_search: true`）。
3. **准入必须落在能被捕获的位置**：`consume_options_of` 原先在 `run_serve` 的 `try` 之外被调用，
   它新增的准入检查抛 `ResidencyError`（不是 `ConsumerError`）→ 会冒成 traceback 而不是原因码退出。
  现在参数越界与分级准入共用 `except ResidencyError → SystemExit(code)`。

4. **检索面的配置来源曾经是"venv 放在哪"的函数**（主机上才复现，容器里是绿的）：`pymilvus.settings`
   在 **import 期**调用 `load_dotenv()`，python-dotenv 从 `pymilvus/settings.py` 所在目录**向上找
   `.env`**（`__main__` 没有 `__file__` 时退回用 cwd）。于是"读不读得到仓库 `.env`"取决于 venv 路径：

   | 运行位置 | venv | 向上走到仓库根 | `find_dotenv()` | 后果 |
   | --- | --- | --- | --- | --- |
   | 主机（`make semantic-check`） | `<repo>/.venv/` | 能 | `/Users/.../SensoryPlex/.env` | 仓库 `.env` 被写进 `os.environ`，令牌"凭空"出现 |
   | api / index 容器 | `/app/.venv` | 不能 | 空 | 令牌保持缺失 |

   实测（主机，场景 2 的子进程真的红过）：

   ```
   $ make semantic-check
   subprocess.TimeoutExpired: Command '[... '-m', 'sensoryplex_index_worker.cli', ... 'serve' ...]'
   timed out after 300.0 seconds      # 没有 --auth-token 也起来了，说明令牌是"顺手"来的
   ```

   修法：不给第三方 import 决定配置来源。新增 `services/index-worker/src/sensoryplex_index_worker/environ.py`，
   在**任何会写 `os.environ` 的 import 之前**取 `BASE_ENVIRON` 快照，`cli` 的令牌、`--uri` /
   `--database-url` / `--nats-url` 默认值与分级背压准入一律只读快照（准入走
   `read_event_backpressure(..., environ=BASE_ENVIRON)`）。`relay` 侧不动（它不 import `pymilvus`）。
   修复后同一台主机、同一条命令：

   ```
   $ env -u SENSORYPLEX_INDEX_AUTH_TOKEN uv run --frozen python -m sensoryplex_index_worker.cli \
       --uri /tmp/sp_probe.db --database-url postgresql://placeholder/none serve \
       --vector-index-key material_text_bge_small_zh_v1_5_d512_v1 --port 60123 \
       --model-dir /tmp/none --model-file onnx/model_quantized.onnx
   index_auth_token_required
   exit=1
   ```

    契约测试 `tests/contracts/test_index_environ_contract.py`（4 项）把顺序约束钉住：真造一次陷阱
    （cwd 放 `.env` + `python -c`，dotenv 此时**一定**命中并写进 `os.environ`）并断言 `BASE_ENVIRON`
   里没有它；另加三项覆盖 `serve` 的 fail-closed、命令行默认值、以及消费准入只读快照。
   场景 2 里显式摘掉该变量的写法**保留**：那是为了让"操作者自己 export 过令牌"也不影响判定。

   修复后的整场验收（同一台主机）：

    ```
    $ make semantic-check
    ...
    semantic search acceptance: real BGE -> Milvus Lite -> resident surface -> API hydration passed in 9.2s
    exit=0
    ```

   排队中的两处一致性改动（同一次修复）：

   - `tests/contracts/test_event_backpressure_contract.py` 里给 `index_cli` 注入档位**必须写进
     `BASE_ENVIRON`**（注 `os.environ` 已经不起作用）——契约测试自己就是这条语义的第一份证据；
   - `serve` 的 ready 行（`--out`）从"开端口之后"挪到"消费侧确认之后"再写：文件滞后一点，
     但"文件说消费在跑"与"消费真的接上了"不再有窗口差。`index` 容器的健康检查仍是 TCP
     （进程活着且监听），因为它不会被上一轮遗留的 ready 文件骗过。

#### 仍未验证（不得当成完成）

- 检索面停机时的降级路径（`semantic_index_unreachable`）由 `tests/integration/test_semantic_search.py`
  覆盖，**不在**本切片的容器内闭环里；
- 服务端 Milvus 形态（消费与检索拆成两个进程）、多副本消费（第二个 index 进程）、跨主机 NATS 集群、
  `ack_wait` 到期后的自动重投：均未验收；
- 吞吐与延迟曲线未测（本切片接的是**准入**，不是吞吐达标）；
- 向量 GC 未做（见上）；
- `golden_path_verified` 仍恒为 false：真实媒体端到端（真实视频 → Runtime/Timeline → 事件）仍未联调。

### 网关检索面冒烟：从"断言固定 503"改成"断言声明与行为一致"（2026-09-24）

**背景**：`make gateway-smoke` 在 master 上是**红的**。`tools/smoke_gateway.py` 断言
"semantic 未配置 ⇒ 503 `semantic_search_unavailable`"，而 gateway 返回的是 200 真实检索：

```
AssertionError: (200, {'mode': 'semantic', 'index_version': 'milvus-flat-cosine-v1',
                       'unindexed_hits': 20, 'vector_index_key': 'material_text_bge_small_zh_v1_5_d512_v1', ...})
```

原因是 ADR-027 §10.3 / §10.4 那类陷阱的第三次现身：`Settings` 的 `env_file=".env"` 按 **cwd** 找，
而 gateway 容器的 `working_dir` 就是 `/workspace`（bind mount），于是它**顺手**读到了宿主仓库的
`.env`——`.env` 里一旦有了 `SENSORYPLEX_INDEX_SEARCH_*`（ADR-027 那次加的），capability 就变成
true。也就是说：**capability 是"这个部署有没有挂载仓库"的函数**，而不是任何一条声明的函数。
它没更早暴露，是因为 `make gateway-smoke` 既不在 `make check` 也不在 CI 里。

**三个动作**：

1. `deploy/compose/docker-compose.poc.yml` 给 `gateway` 补齐
   `SENSORYPLEX_INDEX_SEARCH_ENDPOINT` / `..._TOKEN` / `..._VECTOR_INDEX_KEY` 的**声明**，与 api 对称。
   不声明的话，没有 bind mount 的部署会静默退化成"api 有检索、gateway 没有"，而两边跑的是**同一份代码**。
   注意这**不等于**修好了来源问题：声明只是让它显式，容器仍然会去读 bind mount 的 `.env`（见下"仍未验证"）。
2. 冒烟改为断言**声明与行为一致**，而不是某一种形态下的固定状态码——因为两种形态都合法：

   | 形态 | 合法结果 |
   | --- | --- |
   | 默认栈（不含 `events` profile，检索面没起来） | 503 + `semantic_index_unreachable` + `retryable=true` |
   | `make events-up` 之后（检索面在跑） | 200 + `mode=semantic` + `vector_index_key` + `index_version` + `hits` |
   | 未声明检索面（`capabilities.semantic_search=false`） | 503 + `semantic_search_unavailable` + `retryable=false` |

   判定基准取 `/v1/health` 的 `capabilities.semantic_search`（服务自己的声明），**不取** `os.environ`
   也不取 `Settings`——按后者断言等于把"我在哪跑"当成契约。三种情形下都断言**绝不是 501**：
   "没实现"与"没部署"是两件事。
3. `request()` 的客户端超时 5s → 20s：检索面没起来时，API 自己要等满 gRPC 的
   `index_search_timeout_s`（默认 5s）才回 `semantic_index_unreachable`；客户端超时更短的话，
   冒烟看到的是"脚本太急"（`TimeoutError`）而不是"服务怎么答"。

**实测（容器内，两种形态都真跑）**：

| 形态 | `make gateway-smoke` |
| --- | --- |
| `events` profile 在跑 | `semantic 200 真实检索 (never 501) and 422 on empty query: PASS` |
| `make events-down` 之后 | `semantic 503 检索面不可达（retryable=true） (never 501) and 422 on empty query: PASS` |

同批回归：`make lint-ruff`（`ruff check` 全过 / `format --check` 161 文件）、`make check`（exit 0）、
`make event-pipeline-check`（7 步全过，`failures: []`、`query_hits: 1`）。

**仍未验证（不得当成完成）**：

- **容器内的配置来源仍然是"绑进来的仓库 `.env`"**，声明只是让取值显式；把
  `SENSORYPLEX_INDEX_SEARCH_ENDPOINT` 从 compose 里清掉再跑，gateway **照样**返回 200
  （实测：`env` 为空、行为已配置，断言当场红）——这说明"声明即来源"目前**不成立**。
  真正收口要给 `Settings` 一个显式的 env-file 开关（容器里指向空文件），属于下一批，需单独决策；
- `make gateway-smoke` **仍不在 `make check` / CI 里**（两处都没有 compose 栈），所以这类"能力与行为
  漂移"下次仍可能只在本机被发现；
- 向量 GC 未做：本次验收又把 `unindexed_hits` 从 5 推到 20，检索窗口会被陈旧向量占用。

---

## 局域网插件 worker 拓扑与能力预检闭环（ADR-026）

**背景**：
SensoryPlex 是以 Web Console 交付的边缘多模态素材底座。客户需要将模型插件部署在主节点同机或局域网内的异构计算节点（如 Mac mini、3090Ti 工作站或厂商 NPU 盒子）。控制面必须作为唯一调度权威，在 Web Console 提供安装位置选择、节点算力画像、状态管理与 5 项硬性预检，严格守护 host-local 共享内存安全边界。

**落地组件**：
1. **跨语言契约**：`proto/node/v1/node.proto` 定义 `NodeInfo`, `NodeCapabilityProfile`, `EnrollNodeRequest`, `NodeHeartbeatRequest`, `DeploymentIntent`, `PreflightRequest` 等契约，同步生成 Rust / Python / Console TypeScript 绑定。
2. **数据库迁移**：新增追加式迁移 `db/migrations/0004_node_topology.sql`，建立 `console_node`, `console_node_enrollment_token`, `console_plugin_instance`, `console_deployment_intent`, `console_task_assignment` 五张事实表。
3. **控制面与预检引擎**：`services/api/src/sensoryplex_api/infrastructure/preflight.py` 严格执行节点状态、数据本地性、制品形态、加速器匹配、资源预算 5 项硬性预检，绝不静默改派。
4. **子节点 Agent**：`tools/node_agent.py`，支持基于一次性令牌入网、周期心跳汇报指标、认领主节点下发的部署意图、不可变 digest 校验、安装/启停/卸载/回滚执行并上报结果。
5. **Web 控制台**：新增 `apps/console/src/features/Nodes.tsx` 拓扑管理界面，支持签发注册令牌、排空与撤销节点；插件中心 `Plugins.tsx` 接入按节点部署与实时预检校验。
6. **自动化验收工具**：`tools/verify_node_topology.py`（Makefile 目标 `make node-check`）。

**真实容器栈实测结果（make node-check）**：

| 验收场景 | 验证内容与断言 | 实测结果 |
| --- | --- | --- |
| 场景 1：多节点注册 | 签发短效令牌；注册同机数据面 Mac mini (Metal/CoreML)、局域网 3090Ti (CUDA) 与边缘 CPU 盒子；验证清单与画像 | PASS |
| 场景 2：心跳与状态机 | 周期心跳刷新；排空（draining）阻断新任务；撤销（revoked）吊销凭据，后续心跳阻断 | PASS |
| 场景 3：数据本地性 | 消费 `cpu_shared_memory` 的 VLM 部署到远程节点被 `data_locality_violation` 阻断（422）；同机节点通过；消费 observation 的 BGE 允许远端执行 | PASS |
| 场景 4：加速器对账 | Apple Silicon MLX 插件在 Linux 节点被 `unsupported_platform` 拒绝；向仅支持 Metal 的 Mac 请求 CUDA 被 `accelerator_not_available` 拒绝 | PASS |
| 场景 5：意图执行与回滚 | 下发部署意图（installing）→ Agent 认领并执行 → 状态转为 ready；无历史 digest 拒绝回滚（422）；升级后执行回滚恢复历史状态（rolled_back） | PASS |
| 场景 6：全生命周期审计 | 检验 `console_audit` 包含 `node.token.create`, `node.enroll.success`, `node.preflight.pass`, `node.preflight.reject`, `node.drain`, `node.revoke`, `plugin.instance.deploy`, `plugin.instance.ready` | PASS |

**回归验证**：
- `make node-check`：6 大场景全部通过（exit 0）；
- `make test-py`：440 个契约与集成测试全绿（371 passed in contracts, 69 passed in integration）；
- `make lint-ruff`：全量 Python 源码格式化与静态检查通过（167 文件）；
- `cargo test --workspace`：Rust 契约与全部单元/集成测试通过（154 passed）；
- `make console-build`：Web 前端构建通过（Vite 产物成功打包）；
- `./deploy/status.sh`：本地 Docker 容器栈（console, api, gateway, postgres, nats）全健康（200 OK）。

**当前边界与后续演进**：
- 节点入网当前使用短效凭据换取 Session Token；生产级跨机双向 mTLS 证书自动签发与 CA 轮换留待后续阶段实施；
- 主节点当前为单实例权威调度；高可用主节点选主与 Raft 复制留待高可用阶段规划。
