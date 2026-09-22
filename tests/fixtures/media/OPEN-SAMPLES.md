# 公开许可真实样本清单（0.1.0 媒体切片）

本目录约定的"有权限的真实视频"：下面这些样本来自 **Wikimedia Commons**，许可为
CC BY-SA 4.0 或公有领域，**不是**用户私有素材，也没有任何合成/构造内容。媒体文件本体不入库
（存放于 `.gitignore` 覆盖的 `video/samples/`），仅记录出处、许可与摘要以便复现与追溯。

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

## 未覆盖范围（不得当作已完成）

- **断流重连**：需要 SRT 直播源，文件样本无法覆盖，仍为 `UnavailableSource`。
- **容器级 VFR**：这些文件都是可用的 CFR（25/30/60 fps），容器层没有 VFR 长间隙；"长间隙"只由
  内容静止段近似（最长 45.6s），不等价于 VFR 时间戳缺帧。
- 这些样本**没有**跑过模型：ASR/OCR/VLM/BGE 未接入，抽帧覆盖率结论也尚未产出——本清单只提供
  素材与特性测量，不代表 Golden Path 验收。
- 样本是 480p 转码：屏幕文字在 480p 下可辨识度有限，OCR 相关结论需换 720p/原始版本再测。
