# 本地开发与回滚

需要 Python 3.12、uv、Rust 1.96、Docker Compose v2。FFmpeg 仅用于真实媒体探测。
GStreamer、CUDA、TensorRT 和模型权重并非工程初始化的强制依赖，媒体接入阶段再安装。

```sh
make setup
make infra
make migrate
make gateway
```

另一终端可运行 `make runtime` 启动仅绑定 loopback 的 gRPC 控制端点。
`make pipeline-check` 验证 Pipeline 配置结构，不代表能力已安装。

也可运行 `make up`，构建 Gateway 容器、显式执行一次性迁移，再启动 Gateway。
本地 Compose 使用独立项目名 sensoryplex 和命名卷，默认端口：

| 组件 | 地址 |
| --- | --- |
| Gateway/OpenAPI | http://127.0.0.1:8090/docs |
| PostgreSQL | 127.0.0.1:25432 |
| NATS client / monitor | 127.0.0.1:24222 / 28222 |
| Rust Runtime gRPC（独立命令） | 127.0.0.1:50051 |

`make configure` 创建权限为 0600 的 `.env`，使用随机数据库密码和 API token；
文件存在时不覆盖。开发 API 使用 Bearer token，可在 Swagger 的 Authorize 中填写。
原始媒体 URI 和密钥不应放入 Pipeline 配置，只引用 secret 名称。

迁移命令使用事务与 PostgreSQL advisory lock，重复运行只校验 checksum，不重复建表。
已应用迁移被修改时拒绝启动迁移。不要在运行服务中自动建表，也不要手动删改历史迁移。

```sh
make check       # Rust fmt/clippy/test + Python lint/format/契约测试
make integration # 真实 PostgreSQL，随机隔离 schema，结束后只清理该测试 schema
make runtime-smoke # 实际 Rust 进程与 Python 生成客户端 gRPC 互通
make gateway-smoke # 对已启动容器验证鉴权、查询及未接入能力的错误响应
make media-check   # 带 gstreamer feature 的 clippy 编译门（需要 GStreamer 开发文件）
```

CI 使用临时 PostgreSQL；集成测试必须显式通过，不能把跳过等同于验收。
本地 DB 连接默认来自 `.env`，CI 可设置 `SENSORYPLEX_TEST_DATABASE_URL`。

`make down` 停止本项目容器并保留卷。数据库回滚采用备份恢复或新增补偿迁移，
不提供默认 drop-volume 操作。升级前保存镜像 digest、lockfile、配置和数据库备份。
当前 Compose 是单机开发环境，不是对外生产部署。

## Apple Silicon（macOS）开发与部署

macOS 是一等目标平台（ADR-008），但容器无法访问 Metal/ANE/CoreML，因此加速相关进程必须原生运行。

```sh
brew install ffmpeg gstreamer   # GStreamer 自带 libsrt 依赖；ffmpeg 仅用于离线探测
make setup
make infra                      # Compose: postgres + nats
make migrate
make gateway                    # 原生 uvicorn
make runtime                    # 原生命令行进程，gRPC 只绑定 127.0.0.1:50051
```

能力自检（不需要模型权重即可运行，未接入的能力会显式上报原因）：

```sh
make runtime-smoke   # 真实 Rust 进程 + Python 客户端，校验 DescribeCapabilities
```

- 支持的 memory kind 由 Runtime 上报：Apple Silicon 额外允许零拷贝 `unified_memory`，其余平台只有 `cpu_shared_memory`。
- `SENSORYPLEX_TOTAL_MEMORY_BYTES` 只在宿主探测不可用（容器、CI）时用于声明容量，不得用于伪造容量。
- Mac mini 常驻部署：用 `launchd` 托管原生进程，并按统一内存容量设置队列上限与模型量化档位——
  已落地为 `make resident-probe` / `resident-install` / `resident-status` / `resident-uninstall`
  （见 [macOS 常驻手册](macos-resident.md) 与 [ADR-015](../adr/ADR-015-macOS常驻形态与统一内存分级.md)）。
  安装时会渲染 `caffeinate -ims` 防休眠 job，但工具**不修改** `pmset`（需要 root，会持久改变机器行为），
  只打印需要人工确认的命令；16GB 机型落在 `small` 档，模型并发为 1，不得默认并行加载 ASR + OCR + Fast VLM。
- 容器化只适用于 `postgres`、`nats` 与无加速依赖的 `gateway`；不要把 `runtime`/媒体/推理放进 Linux 容器后再声明 macOS 加速可用。

真实文件回放（需要显式授权的样本，合成片段只能验证管道，不能作为验收）：

```sh
make media-replay MEDIA=/absolute/path/to/authorized-sample.mp4
```

该命令先构建带 `gstreamer` feature 的 release runtime，再用 ffprobe 读锚点、用 GStreamer 真实解码，
输出锚点 + 解码数据平面报告，并断言：摘要与文件一致、区间严格递增、锚点间无越界、报告不含媒体路径、
`golden_path_verified` 为 false、未实现能力出现在 `blockers`，以及
`descriptors_validated == descriptors_built`、`descriptor_failures == 0`、`leases_released == leases_issued`、
`descriptors_built == Σsamples + segments`、段字节合计等于音频轨字节、证据 descriptor 引用 arena 而非宿主路径。

切段长度由 `--audio-segment-ms N` 指定，默认 5000 ms，允许 200–30000 ms（越界即报
`audio_segment_ms_out_of_range`，不夹取）；Pipeline 配置里的 `audio_segmenter` 阶段声明该处理步骤。
段长度只影响分段粒度，不影响 descriptor 时间轴。

没有 GStreamer 开发文件的主机可以执行 `make media-replay MEDIA=… MEDIA_FEATURES=`，此时报告只含锚点，
`blockers` 会多出 `gstreamer_decode_not_implemented`，解码数据平面保持全零——这是诚实的降级，不是验收。
macOS 上解码属于 ms 级性能路径，用 `make media-replay` 的 release 构建测量，不要把 debug 结果当结论。

跨进程数据面验收（M3，同一份素材即可）：

```sh
make handoff-check MEDIA=/absolute/path/to/authorized-sample.mp4
```

它起三个进程：`verify_handoff.py`（编排/对账）、`sensoryplex-runtime replay --handoff-listen`
（生产者，持有共享内存与保留表）、`handoff_worker.py`（消费者，只能通过 gRPC 拿段名与窗口）。
断言包括：两个场景（保留表 4096 / 钉在 6）都满足
`offered == retained_total + retain_rejections` 与 `retained_total == retained + released + expired`、
运行结束时 `arena_live_slabs == 0`、越界/非法窗口/重复领取/迟到释放/过期 TTL 各自拿到稳定拒绝码，
且 `ReplayReport.handoff_state == exposed_on=<listen>`、`golden_path_verified == false`。

手工起一个数据面（只在回环地址上，非回环会被拒绝启动）：

```sh
target/release/sensoryplex-runtime replay config/pipelines/file-material.yaml /abs/sample.mp4 \
  --report /tmp/report.pb --handoff-listen 127.0.0.1:50511 \
  --handoff-arena-bytes 268435456 --handoff-retained-limit 4096 --handoff-ttl-ms 30000
```

不带 `--handoff-listen` 的 replay 仍然合法，但报告会写 `handoff_state=not_exercised`——
它不代表数据面已验证。安全边界（段名随机派生、lease 不是内存隔离、同 UID 可见性）见
[ADR-010](../adr/ADR-010-跨进程数据面的安全边界.md)。

SRT 实时接入验收（M4，需要先 `make stream-up`）：

```sh
make stream-up && make live-check
```

发布端由 `tools/verify_live.py` 自己拉起：GStreamer `srtsink` 把登记在册的授权样本**直推** SRT
（不经 RTMP 转封装、不占用 OBS 会话；配置与命令见 [OBS 推流手册](obs-streaming.md)）。
五个场景都会被检查：稳定窗口（`stalls == 0`、`blockers` 为空）、VideoToolbox 视频
（视频帧时长按 PTS 差分补齐，`duration_unavailable` 不得出现）、断流恢复
（发布端被 SIGINT 后再拉起，`stalls >= 1`、`stalled_ms > 0`、`recovered == true`）、
无源（必须 exit 1 并给出显式原因）、实时数据面交接（独立进程按 lease 读取并释放，账目对得上）。
路径上已有别的发布者时脚本会拒绝运行，不会挤掉正在直播的 OBS。

手工跑一次实时接入（URI 只经环境变量传入，绝不进命令行与报告）：

```sh
SENSORYPLEX_SRT_LIVE_URI='srt://127.0.0.1:8890?streamid=read:live/obs' \
  target/release/sensoryplex-runtime ingest config/pipelines/srt-live.yaml \
  --report /tmp/live.pb --duration-ms 20000
```

直播没有已知时长（报告 `duration_ms=0`、`content_hash` 为空、没有 anchor 区间），
断流只被**测量**（重连归 `srtsrc auto-reconnect`），详见
[验证记录](../verification.md) 的"M4"一节。

## 可选向量基础设施

`deploy/compose/docker-compose.vector.yml` 包含 etcd、MinIO 和 Milvus，镜像锁定 digest，
与基础 Compose 合并使用。MinIO 采用 pgsty 维护的发行镜像。初始 Gateway 不使用该栈，
因此默认不拉取/启动；此配置尚未经过向量写入/检索验收。

```sh
docker compose --env-file .env -f deploy/compose/docker-compose.poc.yml \
  -f deploy/compose/docker-compose.vector.yml up -d etcd minio milvus
```

MinIO 密钥由最新 `make configure` 模板生成。已有 `.env` 不会自动覆盖，需自行补齐
`MINIO_ROOT_USER` 与随机 `MINIO_ROOT_PASSWORD`。不要用默认管理员密码。
