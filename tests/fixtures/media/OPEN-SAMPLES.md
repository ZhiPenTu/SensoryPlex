# 公开许可真实样本清单（0.1.0 媒体切片）

本目录约定的"有权限的真实视频"：下面这些样本来自 **Wikimedia Commons**、**Blender 基金会**与
**Zenodo** 等公开授权源，许可是 CC BY 3.0 / CC BY 4.0 / CC BY-SA 3.0 / CC BY-SA 4.0 或公有领域，
**不是**用户私有素材，也没有任何合成/构造内容（合成素材只允许用于拒绝路径，且不在此登记）。
媒体文件本体不入库（存放于 `.gitignore` 覆盖的 `video/samples/`），仅记录出处、许可与摘要以便
复现与追溯。

清单分两批：第一批（6 个）是 0.1.0 基座的 VP9/Opus 回放与抽帧样本；第二批（4 个）是
[ADR-009](../../../docs/adr/ADR-009-媒体格式支持矩阵与拒绝语义.md) §2 承诺矩阵里 H.264、VP8、
Vorbis、容器内 PCM 与**文件形态 MPEG-TS** 的准入补样。

```sh
# 复现任一测量（帧差 / 场景切换 / 响度），不依赖联网
ffmpeg -v error -i video/samples/<file> -an \
  -vf "tblend=all_mode=difference,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=/tmp/yavg" -f null -
ffmpeg -v error -i video/samples/<file> -an \
  -vf "select='gt(scene,0.3)',metadata=print:file=/tmp/scene" -f null -
ffmpeg -hide_banner -nostats -i video/samples/<file> -af ebur128=framelog=quiet -f null -

# 回放验收（真实解码 → descriptor → lease → 音频切段）
make media-replay MEDIA=$PWD/video/samples/<file>
```

## 样本与覆盖

| 文件（`video/samples/`） | 覆盖类别 | 时长 | 分辨率/帧率 | 许可 | 作者 |
| --- | --- | --- | --- | --- | --- |
| `slides-vrt-nodiscussion.480p.vp9.webm` | 静态投屏、屏幕文字、长静止段 | 685s | 854x480 @60 | CC BY-SA 4.0 | Discord 服务器用户（同意以 CC BY-SA 4.0 授权） |
| `slides-vrt-discussion.480p.vp9.webm` | 静态视频 + 多人语音讨论 | 1125s | 854x480 @60 | CC BY-SA 4.0 | 同上 |
| `screencast-video2commons.480p.vp9.webm` | 翻页/界面突变（长静止后突跳） | 552s | 854x480 @25 | CC BY-SA 4.0 | Juandev |
| `screencast-watchlist.480p.vp9.webm` | 翻页/界面突变（短静止） | 156s | 854x480 @30 | CC BY-SA 4.0 | Elisabeth Mandl (WMDE) |
| `sasebo-basketball.480p.vp9.webm` | 高速运动 + 现场语音 | 60s | 854x480 @29.97 | Public domain | U.S. Navy（美国政府作品） |
| `officehours-panel.480p.vp9.webm` | 多人对话 + 屏幕共享切换 | 2232s | 854x436 @25 | CC BY-SA 4.0 | LWyatt (WMF) |

出处（页面链接含完整许可与作者署名）：

- <https://commons.wikimedia.org/wiki/File:2026_S.Marchenko_presentation_on_VRT_system_without_discussion.webm>
- <https://commons.wikimedia.org/wiki/File:2026_S.Marchenko_presentation_on_VRT_system_with_discussion.webm>
- <https://commons.wikimedia.org/wiki/File:Video_na_Wikimedia_Commons,_video2commons_tutorial.webm>
- <https://commons.wikimedia.org/wiki/File:Screencast_Tutorial_Beobachtungslisten.webm>
- <https://commons.wikimedia.org/wiki/File:Sasebo_Bo_Boys_Youth_Basketball_Game_(999422).webm>
- <https://commons.wikimedia.org/wiki/File:September_2023_Future_Audiences_office_hours_part_2.webm>

下载的是 Commons 官方 480p VP9/Opus 派生版本（同一授权作品的转码，非二次创作），URL 形如
`https://upload.wikimedia.org/wikipedia/commons/transcoded/<a>/<ab>/<File>/<File>.480p.vp9.webm`。
下载日期 2026-09-22，使用 `curl --http1.1 -C -` 断点续传。

## 实测特性（2026-09-22，`macos-aarch64`，ffmpeg 9.0.1）

帧间差分为相邻帧 luma 平均绝对差（`tblend=difference` + `signalstats.YAVG`）；"大跳变"统计帧差
超过阈值的帧数，用于区分"压缩噪声/游标移动"与"整屏内容替换"。

| 文件 | 平均帧差 | 静止帧(<1.0) | 最长静止段 | 帧差>5 | 帧差>20 | 场景突变(>0.3) | 集成响度 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `slides-vrt-nodiscussion` | 0.08 | 98.4% | 45.6s | 216 | 0 | 0 | -19.5 LUFS |
| `slides-vrt-discussion` | 0.09 | 98.0% | 45.6s | 362 | 0 | 0 | -18.5 LUFS |
| `screencast-video2commons` | 0.32 | 95.5% | 21.1s | 233 | 27 | 17 | -24.2 LUFS |
| `screencast-watchlist` | 0.37 | 93.5% | 7.3s | 107 | 16 | 1 | -26.0 LUFS |
| `sasebo-basketball` | 3.24 | 5.2% | 0.5s | 163 | 11 | 10 | -20.6 LUFS |
| `officehours-panel` | 0.10 | 98.9% | 57.8s | 317 | 28 | 20 | -21.5 LUFS |

结论（与内容相符，不是按文件名推测）：

- 两份 VRT 录屏帧差>20 为 0，是**纯静态投屏**，只有讲解/讨论语音——适合"长时间无变化 + 有语音"路径。
- 两份 screencast 帧差>20 分别为 27 / 16，是**长静止后被整屏替换**——适合抽帧覆盖率与"翻页"路径。
- 篮球样本静止帧仅 5.2%、平均帧差 3.24，是**持续运动**样本。

## 摘要（SHA-256）

| 文件 | SHA-256 |
| --- | --- |
| `slides-vrt-nodiscussion.480p.vp9.webm` | `038c63e01486573eee53855443a51227634e2db6a6cbf30cca9e8e1c770c3db1` |
| `slides-vrt-discussion.480p.vp9.webm` | `b0457696e946be25b05e17efae097f202a99b0fbe7d9246a2c143a271fe794ea` |
| `screencast-video2commons.480p.vp9.webm` | `0a107a537ef7b24eceef2658e2fd4bc065d2861d7ce9c07e44056175f5d3d00c` |
| `screencast-watchlist.480p.vp9.webm` | `430cd0bb481e82284b55cafbd9a823dc856af534972afe4b03c3ee302716b7c5` |
| `sasebo-basketball.480p.vp9.webm` | `89eed55b7ed4e991f2c7d536de2aa777cf52542147a4eb3b76a583f1c89073e9` |
| `officehours-panel.480p.vp9.webm` | `8e470a81dd48088592e6504d2256e9de6009312a7ed0c3609faaf99b76e964f7` |

## 回放验收结果（2026-09-22/23，`make media-replay`，release + gstreamer）

6 个样本全部经真实解码 → descriptor → lease 交接 → 音频切段，校验脚本断言全部通过。

| 样本 | 时长 | 锚点 | 丢弃 | descriptors | leases | 音频段 | arena 峰值 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `slides-vrt-nodiscussion` | 685.0s | 75351 | 0 | 75488 | 75488/75488 | 137 | 3564864 |
| `slides-vrt-discussion` | 1125.0s | 123751 | 0 | 123976 | 123976/123976 | 225 | 3564864 |
| `screencast-video2commons` | 552.0s | 41402 | 0 | 41513 | 41513/41513 | 111 | 3564864 |
| `screencast-watchlist` | 156.4s | 12510 | 0 | 12542 | 12542/12542 | 32 | 3564864 |
| `sasebo-basketball` | 60.0s | 4800 | 0 | 4812 | 4812/4812 | 12 | 3564864 |
| `officehours-panel` | 2232.0s | 165999 | 1380 | 167825 | 167825/167825 | 446 | 1489376 |

计数自洽性对每个样本都成立：`descriptors == Σsamples + segments`、`leases_released == leases_issued`、
`descriptor_failures == 0`。摄像头/桌面录屏类（VP9/Opus, Matroska）与既有 HEVC/AAC MP4 走的是同一路径。

**新发现的真实差异（不是缺陷，已记录）**：这些样本是 Matroska/Opus，`initial_padding=312`（6.5 ms pre-skip）。
ffprobe 把首个音频点放在 12 ms，GStreamer 放在 6 ms，两者相差一个 pre-skip；视频轨完全一致。
校验脚本因此把起点容差按轨道区分（视频 ≤ 1 ms、音频 ≤ 25 ms），并把实测偏移打印为 `start_offsets`，
而不是把断言放宽到看不见问题——166 ms 级别的真实错位仍会失败。

**`officehours-panel` 的 1380 个丢弃**：全部来自锚点路径的毫秒粒度收缩（Opus 帧 20 ms，Matroska 毫秒
时间戳在个别位置重复），按 `collapsed_interval` 显式丢弃并计数；解码路径对同一现象记 `overlapping_samples`。

**arena 峰值已核对**：`officehours-panel` 峰值 1489376 = 单帧（该源音频是 **mono**，5 s 段仅 960000 B，
可复用单帧释放的区域）；`sasebo-basketball` 是 stereo，5 s 段 1920000 B 放不进单帧区域，于是新增提交，
峰值 = 1639680 + 1925184 = 3564864。`arena_peak_bytes` 因此定义为"已提交容量高水位"，语义已写入
`proto/media/v1/media.proto` 与 `docs/contracts/README.md`。

## M9 准入补样（2026-09-23）：H.264 / VP8+Vorbis / 文件形态 MPEG-TS / 容器内 PCM

ADR-009 §2 承诺矩阵里，H.264、VP8、Vorbis、容器内 PCM 与**文件形态 MPEG-TS** 此前都标 ⏳
（无真实回放样本）。这一批把它们补齐；公开授权源确实有可用素材，因此没有落进"找不到 → 只登记缺口"
的分支。

| 文件（`video/samples/`） | 覆盖的矩阵行 | 覆盖类别 | 时长 | 分辨率/帧率 | 许可 | 作者 |
| --- | --- | --- | --- | --- | --- | --- |
| `sintel-trailer.480p.h264.mp4` | H.264 + AAC-LC（MP4/MOV） | 快速运动 + 音乐/对白（电影预告片） | 52.2s | 854x480 @24 | CC BY 3.0 | Blender Foundation（"Sintel", Durian 项目） |
| `editing-basics-sandboxes.vp8.webm` | VP8 + Vorbis（MKV/WebM） | 屏幕录制翻页（2012 老 WebM，容器未写帧率） | 76.4s | 1280x1024 @24 | CC BY-SA 3.0 | Sage Ross (WMF) |
| `mpegts-h264-aac.live-recording.ts` | 文件形态 MPEG-TS | 本机 SRT 直推现场录制（重编码为 H.264/AAC） | 19.7s | 854x480 @25 | 派生自 CC BY-SA 4.0 样本 | Juandev（源素材） |
| `conger-conger.h264-pcm.mov` | 容器内 PCM（`pcm_s16le`） | 水下动物监测片段（H.264 High + 未压缩 PCM 音轨） | 9.0s | 640x480 @29.97 | CC BY 4.0 | Arbuatti, Alessio; Di Serafino, Alessandra; Lucidi, Pia |

出处：

- `sintel-trailer.480p.h264.mp4`：<https://download.blender.org/durian/trailer/sintel_trailer-480p.mp4>
  （Blender 基金会官方分发，CC BY 3.0；2026-09-23 下载，服务端 `Content-Length: 4372373`
  与本机文件一致）
- `editing-basics-sandboxes.vp8.webm`：Commons
  <https://commons.wikimedia.org/wiki/File:Editing_basics_-_Sandboxes.webm>
  （**原始上传**，Commons API 实测 `License=cc-by-sa-3.0`、`Artist=Sage Ross (WMF)`、
  `DateTimeOriginal=2012-11-28`；2026-09-23 下载）
- `mpegts-h264-aac.live-recording.ts`：**本机现场录制**，不是下载产物——用 `tools/verify_live.py`
  的 `Publisher` 把 `screencast-video2commons.480p.vp9.webm`（CC BY-SA 4.0, Juandev）经
  `mpegtsmux ! srtsink` 直推本地 SRT，再用 `srtsrc ! tsparse ! filesink` 收流落盘（SIGINT 收尾）。
  因此容器是 MPEG-TS，视频被重编码为 H.264、音频被重编码为 AAC-LC。
- `conger-conger.h264-pcm.mov`：Zenodo 记录 <https://doi.org/10.5281/zenodo.14173294>
  （API 实测 `license={'id': 'cc-by-4.0'}`、`publication_date=2024-11-16`；文件 `Conger conger.MOV`
  报告 3297942 B 与本机一致），下载 URL 为
  `https://zenodo.org/api/records/14173294/files/Conger%20conger.MOV/content`。

摘要（SHA-256，本机 `shasum -a 256` 复算）：

| 文件 | SHA-256 |
| --- | --- |
| `sintel-trailer.480p.h264.mp4` | `b670602fa00934ca27c4351bb0efe7ea7a07fae57284e44226025eeed7c51254` |
| `editing-basics-sandboxes.vp8.webm` | `5f28578e00e0fc7ed0d42ed2b0a27a792636a88998ac26f161858c3805e936b6` |
| `mpegts-h264-aac.live-recording.ts` | `c2cd5436fa52b5495f74461ac599e401b106cbbb8d69d2a1c600b257d215acbc` |
| `conger-conger.h264-pcm.mov` | `c62e13ba5b205d4ec40fdb4aa18993b9a4c22b80fabe0b577513ce82221f1489` |

实测准入证据（2026-09-23，`macos-aarch64`，GStreamer 1.28.7 / FFmpeg 9.0.1）。容器用
`gst-discoverer-1.0 -v` 读，其余字段来自 Runtime 回放报告：

```sh
target/release/sensoryplex-runtime replay config/pipelines/file-material.yaml \
  video/samples/<file> --report /tmp/r.pb --max-points 40000
# 解析报告：短脚本见 docs/verification.md 的 M9 一节（读取 ReplayReport 的
# decoded.tracks / rejected_tracks / source_codec / decoder_element / frame_rate_mode）
```

| 样本 | 容器 | 源编码（video/audio） | 解码器元素 | 源位深 | 采样格式 | 帧率模式 | descriptors |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `sintel-trailer.480p.h264.mp4` | `video/quicktime` | `video/x-h264` / `audio/mpeg` | `vtdechw0` / `avdec_aac0` | 8 | `4:2:0` | CONSTANT（声明 24/1） | 2487（video 42 + audio 2434 + 11 段） |
| `editing-basics-sandboxes.vp8.webm` | `video/webm` | `video/x-vp8` / `audio/x-vorbis` | `vp8dec0` / `vorbisdec0` | 8 | `4:2:0` | **UNKNOWN**（声明 0/0） | 5773（video 21 + audio 5736 + 16 段） |
| `mpegts-h264-aac.live-recording.ts` | `video/mpegts` | `video/x-h264` / `audio/mpeg` | `vtdechw0` / `avdec_aac0` | 8 | `4:2:0` | CONSTANT（声明 25/1） | 934（video 4 + audio 926 + 4 段） |
| `conger-conger.h264-pcm.mov` | `video/quicktime` | `video/x-h264` / `audio/x-raw` | `vtdechw0` / `demuxer_passthrough` | 8 | `4:2:0` | CONSTANT（声明 30000/1001） | 20（video 9 + audio 9 + 2 段） |

四条全部 `rejected=0`、`blockers` 为空、`descriptors_built == descriptors_validated`。

**从这批样本读到的事实（不是按文件名推测）：**

- **VP8 的 caps 什么都不说**：GStreamer 1.28.7 的 `video/x-vp8` 既没有 `profile` 也没有
  `bit-depth-luma` / `chroma-format`。报告里的 `8` 与 `4:2:0` 来自矩阵对 VP8 Profile 0 的
  **推导**（`capability.rs` 的 `implied_bit_depth` / `implied_chroma_format`），不是源声明的值——
  所以 VP8 的位深/采样格式证据强度**低于** H.264/HEVC（后两者是从 caps 真读出来的）。
- **2012 年的老 WebM 没有帧率**：源容器没有 `DefaultDuration`，`declared_frame_rate = 0/0`，
  报告如实写 `UNKNOWN`，没有补一个恒定值（ffprobe 的 `r_frame_rate=24/1` 是它自己的换算，
  不等于容器声明）。这条样本因此同时是"帧率未知必须显式表达"的正样本。
- **容器内 PCM 没有解码器元素**：MOV 直接存 `pcm_s16le`，`decodebin` 只建 `qtdemux`，
  没有 parser/decoder。报告写 `demuxer_passthrough`——这是一个**确定的答案**，不是空串
  （空串的含义仍是"这次运行没能归因到解码器"）。
- **`colorimetry` 只在源给出时才存在**：`mpegts` 样本的 H.264 caps 带 `bt601`，其余三条未采集到，
  按未知表达。该字段不参与准入判定，但也不能被反推成"已确认 SDR"。
- 四条样本的 `display_rotation_deg` / `applied_rotation_deg` 均**缺省**（v1 既不采集也不应用旋转）。

**`conger-conger.h264-pcm.mov` 为什么之前测不出来**：这条正是修复前的失败样本——PCM 轨被拒
`unknown_source_codec: codec_caps_not_collected`。原因不是矩阵写错，而是采集探针永远等不到
`deep-element-added` 的 sink caps（PCM 没有 parser/decoder 可供 autoplug）。修法是给 demuxer 的
**src pad** 挂探针（demuxer 的 src pad 是解析时才创建的，`iterate_src_pads()` 当时拿不到），
细节与 A/B 数据见 `docs/verification.md` 的"M9"一节。

**边界：** 这批样本只证明**准入与回放**（拒绝码、源上下文、解码器元素、计数自洽），
**没有**跑过任何模型，也不改变 `golden_path_verified` 恒为 false 的事实。

## 未覆盖范围（不得当作已完成）

- **断流重连**：文件样本无法覆盖，必须走 SRT。已用 `screencast-video2commons`（552 s）经
  GStreamer `srtsink` **直推** SRT 覆盖最小场景（`make live-check` 的 `stall_recovery`）：
  发布端在窗口中被 SIGINT、随后重新拉起，Runtime 测到 `stalls=1`、`stalled_ms=4723`、`recovered=true`。
  用户自有采集端（OBS / Mac mini）的直推样本仍待补，见 `docs/TODO.md` §2。
- **容器级 VFR**：这些文件都是可用的 CFR（25/30/60 fps），容器层没有 VFR 长间隙；"长间隙"只由
  内容静止段近似（最长 45.6s），不等价于 VFR 时间戳缺帧。
- 这些样本**没有**跑过模型：ASR/OCR/VLM/BGE 未接入，抽帧覆盖率结论也尚未产出——本清单只提供
  素材与特性测量，不代表 Golden Path 验收。
- 样本是 480p 转码：屏幕文字在 480p 下可辨识度有限，OCR 相关结论需换 720p/原始版本再测。
- **VP8 的位深/采样格式是矩阵推导值**（`video/x-vp8` caps 不含 `profile`/`bit-depth-luma`），
  证据强度低于 H.264/HEVC，不得当作"源声明 8-bit 4:2:0"。
- **文件形态 MPEG-TS 样本是本机重编码产物**（H.264/AAC），只证明 MPEG-TS 这一行在文件路径上
  能被准入与回放；它不代表采集端直出行为，也不能替代 `make live-check` 的 SRT 直播证据。
