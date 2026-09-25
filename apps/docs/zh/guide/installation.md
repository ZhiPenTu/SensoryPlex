# 安装与前置要求

安装是按**角色**划分的，不是按包划分：控制面一律容器化，而任何要碰宿主加速器的东西按设计留在宿主。

## 先选形态

| 形态 | 适用场景 | 什么在哪跑 |
| --- | --- | --- |
| **单机开发** | 想看控制台、跑验收 | `deploy/compose/docker-compose.poc.yml` 全在一台机器；模型插件可选地跑在宿主 |
| **Mac mini 端侧节点** | 加速推理在 Apple Silicon 上 | 控制面容器化；runtime 与模型插件按 ADR-015 以 `launchd` 原生常驻 |
| **主节点 + 局域网 worker** | 加速器分散在多台机器 | 主节点持有控制面；worker 子节点按 ADR-026 注册端点、心跳并做预检 |
| **直播接入** | 除文件外还要 SRT | `make stream-up` 起 MediaMTX（仅回环端口） |

## 按角色的宿主前置要求

**控制面（一律容器）**

- Docker Compose v2，磁盘要放得下 Postgres 数据卷、NATS JetStream 数据与 `.data/`。
- 控制面验证不需要宿主 `python`、`node` 或 `uv`：`make lint-ruff`、`make test-py`、`make proto`、
  `make console-build` 与编排类验收都在容器内执行。

**明确的主机例外**

| 例外 | 为什么进不了容器 |
| --- | --- |
| `make configure` | 仓库以 bind mount 进容器，容器内无法把凭据写回宿主 `.env`。 |
| `cargo` / `make check` / Rust 测试 | 现有镜像都不带 `rustc`/`cargo`。 |
| 模型插件（`vlm`、`asr`、`ocr`、`embed`） | MLX/Metal、CoreML 与 ollama 端点只存在于宿主；`mlx-metal` 在 Linux 容器里根本编不出来。 |
| `launchd` 常驻形态 | 容器里没有 `launchctl` / `sysctl`。 |
| 媒体接入与回放验收 | HF 权重缓存、GStreamer 插件与 CoreML EP 都是宿主本地的。 |

## 已授权媒体样本

所有媒体路径都跑真实授权样本——项目不会用合成 fixture 冒充。样本以只读方式暴露：

```sh
MEDIA_DIR=~/Movies ./deploy/up.sh          # 把 ~/Movies 以只读方式挂到 api 容器的 /host-media
make media-replay MEDIA=/absolute/path/authorized-sample.mp4
```

Makefile 会把 `MEDIA=...` 重写为 `/host-media/$(basename)`，所以文件必须在 `MEDIA_DIR` 之内。

## 模型资产

| 插件 | 资产 | 布局 |
| --- | --- | --- |
| `ocr-rapidocr` | 随包携带的 PP-OCR ONNX 权重 | 包内路径，或显式 `model_dir` |
| `embed-bge-onnx` | `bge-small-zh-v1.5` 量化 ONNX | `.data/models/bge-small-zh-v1.5/onnx/model_quantized.onnx`（`up-events.sh` 的前置检查） |
| `vlm-moondream` | 本机 ollama 提供的视觉模型 | manifest 里放行 `127.0.0.1:11434` |
| `asr-whisper-mlx` | MLX Whisper，仅 Apple Silicon | 宿主虚拟环境（`uv run`） |

权重由运营预置。插件自己从不下载权重，唯一有文档的例外是 `ocr-rapidocr` 在权重缺失或摘要不匹配时会去
它的模型目录站拉取——这正是它 manifest 里声明 `network: allowlist` 的原因。

## GStreamer

真实解码（文件与 SRT）需要宿主有 GStreamer 开发文件。没有时 `make media-replay MEDIA_FEATURES=` 仍会产出
纯锚点报告——解码数据平面保持全零，并在 `blockers` 中声明该缺口，而不是静默跳过。

::: tip SRT 与 `ffprobe`
如果 `ffprobe` 对 SRT 地址报 `Protocol not found`，请用 GStreamer（`srtsink`/`srtsrc`）驱动 SRT。
`make live-check` 直接用 GStreamer，不依赖 OBS，也不经 RTMP 转封装。
:::

## 验证安装

```sh
./deploy/status.sh          # console / api / gateway 的HTTP探测
make capability-check       # 能力上报：不可用原因、加速器三态
make lint-ruff test-contracts
```

## 升级与回滚

- compose 里的镜像按 digest 固定；整体重建是 `./deploy/up.sh`，局部重建是 `./deploy/up.sh api console`。
- 数据库迁移只追加，并通过 `tools/migrate.py` 执行；`migrate` 服务在 `gateway`/`api` 启动前跑完。回滚**不能**
  改历史 revision，只能新增一条迁移。
- `./deploy/down.sh` 保留命名卷；`./deploy/down.sh --volumes` 会删掉它们。把 `--volumes` 当破坏性操作，
  先确认有备份。
- `./deploy/down-events.sh --volumes` 会额外删除 `.data/index`（向量库目录）。

## 接下来

- [部署](/zh/operations/deployment)：端口、数据卷与常驻形态。
- [配置参考](/zh/operations/configuration)：哪些环境变量不一致时服务会拒绝启动。
