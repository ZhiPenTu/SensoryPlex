# OBS 本机推流服务器

使用 MediaMTX 1.21.1，独立 Compose 项目 `sensoryplex-stream`，镜像锁定 digest。
它只转发/转封装媒体，不转码、不加载模型、不录制。Apple Silicon 的 GStreamer 与模型进程仍原生运行。

## 启动与 OBS 配置

```sh
make stream-up
```

OBS → 设置 → 直播（Stream）→ 服务选择「自定义」：

| 字段 | 填写值 |
| --- | --- |
| 服务器 | `rtmp://127.0.0.1:1935/live` |
| 串流密钥 | `obs` |
| 使用身份验证 | 关闭（仅本机接入） |

想验证**真正的 SRT 直出**（不经 RTMP 转封装），把服务改成 SRT 形式：

| 字段 | 填写值 |
| --- | --- |
| 服务器 | `srt://127.0.0.1:8890?streamid=publish:live/obs` |
| 串流密钥 | **留空**（OBS 会把密钥拼到 URL 后面，拼上就找不到路径） |
| 使用身份验证 | 关闭（仅本机接入） |

两类常见失败都能在 `make stream-logs` 里看到真实回执：

| OBS 现象 | MediaMTX 日志 | 原因 |
| --- | --- | --- |
| 无法连接 | `closed: path 'live' is not configured` | 只填了服务器地址（或 `streamid=publish:live`）：接入层只登记 `live/obs` 一个路径，不会自动创建 |
| 无法连接 | `closed: no stream is available on path 'live/obs'` 或 `invalid stream ID` | streamid 不完整/前缀不对，被当成读取请求 |

输出建议：Apple VT H.264 硬件编码、AAC 音频、关键帧间隔 2 秒、720p/30 FPS、视频码率 2500–4000 Kbps。
添加有授权的媒体源后点击「开始直播」。`obs` 是流路径标识，不是安全凭证。
仅支持同一台 Mac 上的 OBS；这些地址没有开放到局域网或公网。
注意：Apple VT H.264 **不写 VUI timing**，接收端 buffer 没有 duration；`ingest` 会用同一轨下一个
样本的 PTS 差分补时长并计入 `duration_derived_samples`（见 `docs/verification.md` 的 "M4+"）。

## 读取同一条流

| 用途 | 地址 |
| --- | --- |
| 后续 GStreamer/SRT 接入 | `srt://127.0.0.1:8890?streamid=read:live/obs` |
| VLC / RTSP 诊断（TCP） | `rtsp://127.0.0.1:8554/live/obs` |
| RTMP 读取 | `rtmp://127.0.0.1:1935/live/obs` |
| 本机指标 | `http://127.0.0.1:9998/metrics` |

服务器有流不等于 Runtime 已处理，也不代表 ASR/OCR/VLM 或 Golden Path 完成。
停止 OBS 后源应变为不可用；不提供占位片段或自动录制回放。

Runtime 侧的实时接入走 `ingest` 命令（见下文），它读的就是上面这条 `read:` URI。

```sh
make stream-status
make stream-logs
```

`paths` 指标中 `name="live/obs",state="ready"` 表示存在发布者；`paths_inbound_bytes` 增长
表示接收了媒体字节。指标端点可访问仅代表服务已启动，实际媒体仍需探测/解码验证。

## 用 GStreamer `srtsink` 直推 SRT（不经 RTMP）

OBS 默认推 RTMP；要验证**真正的 SRT 直出**（而不是 RTMP 到 MediaMTX 后再读 SRT），
可以用 GStreamer 自己当发布端。发布端做 H.264/AAC 编码并直接封进 MPEG-TS 交给 `srtsink`，
MediaMTX 只转发、不转码：

```sh
GST_PLUGIN_FEATURE_RANK="vtdec_hw:0,vtdec:0" gst-launch-1.0 -e \
  filesrc location=video/samples/screencast-video2commons.480p.vp9.webm ! decodebin name=d \
  d. ! queue max-size-buffers=60 ! videoconvert ! videoscale ! video/x-raw,format=I420 \
     ! x264enc tune=zerolatency speed-preset=ultrafast key-int-max=60 bitrate=2500 \
     ! h264parse ! queue ! mux. \
  d. ! queue max-size-buffers=200 ! audioconvert ! audioresample \
     ! audio/x-raw,rate=48000,channels=2 ! avenc_aac bitrate=128000 ! aacparse ! queue ! mux. \
  mpegtsmux name=mux ! srtsink uri="srt://127.0.0.1:8890" streamid=publish:live/obs max-bitrate=4000000
```

- `streamid=publish:live/obs` 在当前配置（`authInternalUsers: user: any`）下被接受；
  `read:` 不需要凭据。要改成需要凭据的 publish，得在 `authInternalUsers` 里加一条带 `pass` 的规则——
  **本轮没有验证带凭据的 publish**，也没有验证 SRT 加密（`passphrase` / `pbkeylen`）。
- 播放列表里 `d.` 的两条分支分别编码视频与音频；`! mux.` 把两路合进同一条 MPEG-TS。
- `GST_PLUGIN_FEATURE_RANK="vtdec_hw:0,vtdec:0"` 只影响**发布端**：macOS 上 VP9 硬解出 GLMemory，
  后面的 `videoconvert` 接不上，因此这里固定用软件解码。
- 已有一个发布者时（例如正在直播的 OBS），`overridePublisher: false` 会拒绝新发布者，**不会**挤掉它。
- 停止发布端：`Ctrl+C`（`-e` 会正常收尾）。发布端退出后路径回到 `notReady`，读者会断开。

## Runtime 实时接入（`ingest`）

`ingest` 在有限墙钟窗口内拉流、解码，并把样本交给与 `replay` **同一条** arena / descriptor /
lease /（可选）交接链路，同时测量断流与恢复：

```sh
SENSORYPLEX_SRT_LIVE_URI='srt://127.0.0.1:8890?streamid=read:live/obs' \
  target/release/sensoryplex-runtime ingest config/pipelines/srt-live.yaml \
  --report /tmp/live.pb --duration-ms 20000 [--stall-threshold-ms 1000] [--max-stalls 8]
```

- **URI 不进命令行**：变量名由 pipeline 的 `uri_secret_ref` 派生（`SRT_LIVE_URI` →
  `SENSORYPLEX_SRT_LIVE_URI`），报告与日志里只有引用名；缺变量时以 `live_uri_env_missing: <变量名>` 失败。
- 直播没有已知时长：报告里 `duration_ms=0`、`content_hash` 为空、没有 anchor 区间。
- 重连由解码元素负责（`srtsrc auto-reconnect=true`）；Runtime 只**测量**断流与恢复，
  `reconnect_owner` 如实写 `srtsrc auto-reconnect`。
- 窗口里一个样本都没有不是成功：会以 `live_window_produced_no_samples` 退出非 0。
- 完整验收（含断流恢复与实时数据面交接）见 `make live-check`；实测数据见
  [验证记录](../verification.md) 的"M4"一节。

## 有限时长的真实样本验证

可在 OBS 尚未推流时，使用仓库已登记的公有领域篮球样本模拟发布者；来源与 SHA-256 见
[公开样本清单](../../tests/fixtures/media/OPEN-SAMPLES.md)。不使用合成画面，不产生模型结果。
在一个终端发布 30 秒（编码在测试客户端完成，MediaMTX 不转码）：

```sh
ffmpeg -hide_banner -loglevel warning -nostdin -re \
  -i video/samples/sasebo-basketball.480p.vp9.webm -t 30 \
  -map 0:v:0 -map 0:a:0 -c:v libx264 -preset ultrafast -tune zerolatency \
  -pix_fmt yuv420p -r 30 -g 60 -threads 2 -c:a aac -b:a 128k \
  -rw_timeout 5000000 -f flv rtmp://127.0.0.1:1935/live/obs
```

另一终端通过 RTSP/TCP 读取 5 秒，确认视频为 H.264、音频为 AAC；无流时应失败。
本机 FFmpeg 未编译 SRT 协议，SRT 使用下方原生 GStreamer 验证：

```sh
ffprobe -v error -rtsp_transport tcp -timeout 5000000 -read_intervals '%+5' \
  -show_entries stream=codec_name,codec_type,width,height,sample_rate,channels \
  -of json rtsp://127.0.0.1:8554/live/obs
```

下面的独立 GStreamer 诊断管线读取同一路径并实际解码音视频，两条分支达到有限帧数后 EOS。
源断开不自动重连；可按 Ctrl+C 取消。该诊断不经过 Runtime 的 descriptor/lease/模型链路。

```sh
gst-launch-1.0 -e -v \
  srtsrc uri=srt://127.0.0.1:8890 streamid=read:live/obs auto-reconnect=false \
  ! tsdemux name=d \
  d. ! queue max-size-buffers=60 max-size-bytes=8388608 max-size-time=2000000000 \
     ! h264parse ! avdec_h264 max-threads=2 ! identity eos-after=30 ! fakesink sync=false \
  d. ! queue max-size-buffers=100 max-size-bytes=2097152 max-size-time=2000000000 \
     ! aacparse ! avdec_aac ! identity eos-after=50 ! fakesink sync=false
```

已有发布者时不运行测试推流：`overridePublisher: false` 会拒绝新发布者，保护正在使用的 OBS 会话。

### 本机验收记录（2026-09-23）

- 平台：macOS arm64 原生 FFmpeg/GStreamer；MediaMTX 在 Docker Linux arm64 中运行。
- 公有领域篮球样本 SHA-256 与清单一致；RTMP 发布后 RTSP 检出 H.264 854×480 与 AAC 48 kHz 双声道。
- 原生 GStreamer 经 SRT 读出并实际解码为视频 `I420`、音频 `F32LE`，两分支 EOS，退出码 0。
- 测试发布者退出后路径恢复 `notReady`；RTSP 与 SRT 无源读取均返回失败，测试进程已清理。
- 验收后容器 CPU 0.07%、内存约 45.72 MiB / 128 MiB（单次采样，非压测结论），无 OOM 或重启。
- `make integration`：3 项通过；`make check` 在 Rust 格式检查处被已有的 arena/handoff/shm/runtime
  未格式化改动阻断，其余检查没有执行；未改动这些工作区文件。Compose 校验与 `git diff --check` 通过。
- 用户 OBS 的 **SRT 直推**已于同日实测（见下一小节）；SRT 加密、带凭据 publish 与模型链路仍未验证；
  `golden_path_verified=false`。
- Runtime 的 SRT 接入与断流重连随后已在同一套接入层上验收（发布端为 GStreamer `srtsink` 直推，
  四个场景见 [验证记录](../verification.md) 的"M4"一节）；本轮记录里的 RTMP 发布路径仍然有效。

### OBS 自有采集端 SRT 直推（2026-09-23）

用上面那张 SRT 表格（服务器 `srt://127.0.0.1:8890?streamid=publish:live/obs`、密钥留空）推流后：

```sh
curl -s http://127.0.0.1:9998/metrics | grep -E '^paths\{'   # 期望 state="ready"
SENSORYPLEX_SRT_LIVE_URI='srt://127.0.0.1:8890?streamid=read:live/obs' \
  target/release/sensoryplex-runtime ingest config/pipelines/srt-live.yaml \
  --report /tmp/live-obs.pb --duration-ms 20000
```

- 实测（20 秒窗口）：`samples=1604 descriptors=1013`（0 失败）、`leases_released=1013`、`stalls=0`、
  `golden_path_verified=false`；video 1280×720 RGBA、audio 48 kHz/2ch。
- 期间发现并修复：OBS 的 Apple VT H.264 码流不带 timing，接收端 buffer 没有 duration，
  旧实现会把**整条视频轨**按 `duration_unavailable` 丢掉（601 次丢弃、0 帧）。现在由同一轨下一个
  样本的 PTS 差分补时长，并在 `DecodedTrackStat.duration_derived_samples` 里单独计数。
- 视频最终只留下少数帧属抽帧策略：画面静止时按 5 秒心跳保留，`sampling.kept` 与
  `skipped_no_change` 都在报告里，不是丢帧。
- 详细证据与未验证范围见 [验证记录](../verification.md) 的"M4+"一节。
- 同一路 OBS 直播也用于 M2 背压验收：10 秒窗口（`--handoff-listen`）下报告给出三条有界队列
  （`handoff_retained_table=19/19/32`、`handoff_retained_kind=16/16/16`、`handoff_arena_bytes`），
  `state=saturated`、丢弃 544 条全部是 `handoff_kind_quota_full`（音频侧到配额），
  独立消费者实际拿到 `video_buffers=3 audio_buffers=16`。这轮实测修掉了"音频块占满共用保留表、
  视频一帧也交不出去"的缺陷（[ADR-011](../adr/ADR-011-保留窗口按种类分配.md)）。

本地诊断输出位于 Git 忽略的 `.logs/stream-verification/`，包括 `report.json`、
`rtsp-probe.json`、`gstreamer.log` 与 `active-metrics.txt`，未保存媒体帧或录制文件。

## 资源与运维

- 上限：1 CPU、128 MiB 内存、64 PID、单路径最多 4 个读取者、512 包发送队列。
- 读写超时 10 秒，日志最多 2 × 5 MiB；关闭录制、HLS、WebRTC、MoQ、控制 API 与 pprof。
- 非 root、只读根文件系统、移除 capabilities；只有 loopback 端口映射，无媒体持久卷。
- Docker 启动后按 `unless-stopped` 自动恢复；Docker Desktop 未启动或 Mac 休眠时不可推流。
- 停止/撤销本次部署用 `make stream-down`，不影响主项目数据库、NATS、Gateway 或其卷。
- 升级需同时更新版本与 digest、复核配置并重跑真实媒体发布/读取。回滚使用上一版 Compose
  与配置执行 `make stream-up`；重建服务器会中断当前推流，需要 OBS 重新连接。
