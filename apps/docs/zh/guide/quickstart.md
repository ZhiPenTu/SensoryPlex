# 快速上手

目标：把容器栈跑起来、登录 Web 控制台，并搞清楚这个构建里究竟哪一段链路是活的。

## 前置要求

| 需要 | 用途 | 必须在哪 |
| --- | --- | --- |
| Docker Compose v2 | 整个控制面都在容器里 | 宿主 |
| `uv` + Python 3.12 | `make configure` 要写 `.env`；宿主侧媒体验收 | 宿主 |
| Rust 1.96（`cargo`） | `cargo build --workspace --locked`；现有镜像都不带 Rust 工具链 | 宿主 |
| GStreamer 开发文件 | 真实解码路径（`make media-replay`、工作器解码） | 宿主，仅媒体相关工作时需要 |
| 已授权媒体样本 | 所有媒体验收都跑真实授权样本 | 宿主，通过 `MEDIA_DIR` 暴露 |

默认端口：控制台 `5173`、文档站 `5174`、api `8091`、gateway `8090`、postgres `25432`、nats `24222`，
可在仓库根 `.env` 覆盖，见 [配置参考](/zh/operations/configuration)。

## 1. 配置与构建

```sh
make configure   # 宿主：把随机凭据写入 .env（不要提交 .env）
make setup       # api 容器：生成 proto；宿主：cargo build --workspace --locked
```

`make configure` 刻意在宿主执行：仓库以只读视图 bind 进容器，容器内写不进新凭据。

## 2. 启动容器栈

```sh
./deploy/up.sh          # docker compose up -d --build --wait，逐个等待 healthcheck
./deploy/status.sh      # 对 console / docs / api / gateway 做 HTTP 探测
```

任一服务没到 `healthy`，`./deploy/up.sh` 不会报告成功。只想重建部分服务用 `./deploy/up.sh api console`，
跟踪日志用 `./deploy/logs.sh [服务名]`，停止用 `./deploy/down.sh`（加 `--volumes` 会删数据卷）。
容器栈同时拉起静态文档站（`docs` 服务），托管的是镜像构建期产出的站点。

然后打开：

- Web 控制台 — <http://127.0.0.1:5173>
- 框架使用文档 — <http://127.0.0.1:5174>（英文在 `/`，简体中文在 `/zh/`）
- API 文档 — <http://127.0.0.1:8090/docs>（兼容网关同样提供 `/docs`）

## 3. 登录

```sh
make demo-seed    # 创建演示账号，随机密码写入 .data/demo-password
```

启用 demo 模式后，登录页底部会出现「填入演示账号」按钮。演示账号是特权账号——**不要**在生产性质节点上
播种，那边请把 `SENSORYPLEX_DEMO_*` 留空。

## 4. 可选：启动常驻事件链路

```sh
./deploy/up-events.sh   # relay（outbox → JetStream）+ index（消费 → 向量 → 检索面）
```

这是独立的 `events` profile，`./deploy/up.sh` **不会**拉起它。它会先检查
`.data/models/bge-small-zh-v1.5/onnx/model_quantized.onnx`，缺失就非零退出——**不会**替你联网下载。

## 5. 走一遍真实闭环

```sh
make task-worker-daemon   # 宿主常驻工作器：保持 local-host 在线，跑 GStreamer + OCR + Timeline 融合
make task-worker-status
```

接着在控制台里：**视频库** → 导入 `.mp4`/`.webm` → **处理任务** → 关联该视频与已发布方案 → 点「开始处理」。
宿主工作器处理完后该行变为「查看素材」：**素材检索** 展示按时间轴对齐的切片、文字观测，以及原片的
Range 流式回放。

最后跑自动化回归：

```sh
make golden-path-check    # 9 个场景：鉴权、节点就绪、上传、方案发布、任务分发、
                          # 端侧计算、向量落库、语义检索、原片回看
```

## 全新安装时你应该看到什么

| 现象 | 为什么这是正确的 |
| --- | --- |
| 搜索返回空数组 | 初始数据库没有业务数据。底座不会编造模型结果。 |
| `make golden-path-check` 报前置条件未满足 | 素材库还没有真实视频、宿主工作器没起，或节点离线。 |
| 未跑 `up-events.sh` 时语义检索报不可用 | 检索面是独立常驻进程；没有它时 API 显式失败，而不是返回假命中。 |

::: warning 不要从健康检查绿灯下结论
`/v1/health` 返回 `200`、容器 `healthy`、节点在线，都**不是**端到端证据。媒体闭环唯一的证据是同一条
真实授权样本走完 上传 → 分发 → 计算 → 向量 → 检索 → 回看。其它一切都只是基础设施就绪。
:::

## 接下来

- [控制台全流程](/zh/guide/console-walkthrough) — 同一条闭环，按界面逐屏说明。
- [部署](/zh/operations/deployment) — 端口、数据卷、macOS 常驻形态与回滚。
- [能力实现状态](/zh/reference/status) — 哪些已验证、哪些未验收、哪些未实现。
