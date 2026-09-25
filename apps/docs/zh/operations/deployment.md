# 部署

所有操作都通过 `deploy/` 下的脚本进行。脚本一律从仓库根执行，并固定使用同一份
`--env-file .env -f deploy/compose/docker-compose.poc.yml`，因此不可能出现漏了 `--env-file` 或传错 `-f`
路径的情况。**不要直接调用 `docker compose up`。**

## 脚本

| 脚本 | 作用 |
| --- | --- |
| `./deploy/up.sh [服务名...]` | `docker compose up -d --build --wait`；任一 healthcheck 未通过即失败 |
| `./deploy/down.sh [--volumes]` | 停止并移除容器；默认保留命名卷，加 `--volumes` 才删 |
| `./deploy/status.sh` | 容器状态，外加对 console/api/gateway/docs 的独立HTTP探测 |
| `./deploy/logs.sh [服务名]` | 跟踪日志（`--tail 200`），不带参数是全部服务 |
| `./deploy/up-events.sh` | 检查 BGE 权重后启动 `events` profile（`relay`、`index`） |
| `./deploy/down-events.sh [--volumes]` | 只停 `relay` 与 `index`；`--volumes` 额外删 `.data/index` |

等效 make 目标：`make up`、`make down`、`make infra`、`make events-up`、`make events-down`、`make events-logs`。

## 服务与端口

| 服务 | 宿主绑定 | 用途 |
| --- | --- | --- |
| `console` | `127.0.0.1:5173` | Web 控制台，nginx 静态托管 + 反向代理 |
| `api` | `127.0.0.1:8091` | 业务/管理/认证 API |
| `gateway` | `127.0.0.1:8090` | 兼容查询入口 |
| `docs` | `127.0.0.1:5174` | 本静态文档站 |
| `postgres` | `127.0.0.1:25432` | 元数据存储 |
| `nats` | `127.0.0.1:24222`（监控 `28222`） | 事件总线 |
| `relay`、`index` | 仅 compose 网络 | 常驻事件链路；检索面对宿主从不暴露 |

所有端口默认绑定回环。端口取值来自 `.env`，见 [配置参考](/zh/operations/configuration)。

## 数据与数据卷

| 存储 | 内容 | 何时被删除 |
| --- | --- | --- |
| `postgres-data`（命名卷） | 元数据、事实、outbox、迁移历史 | `./deploy/down.sh --volumes` |
| `nats-data`（命名卷） | JetStream stream 状态 | `./deploy/down.sh --volumes` |
| `.data/`（仓库目录） | 模型权重、Milvus Lite 库、生成的演示密码 | 手工删除，或 `./deploy/down-events.sh --volumes` 删 `.data/index` |

仓库根以 bind mount 进 `api`、`gateway`、`console`，让容器内验证能看到宿主工作区。授权媒体通过 `MEDIA_DIR`
单独挂载，并且是**只读**的。

## 部署组合

**仅控制面** —— `./deploy/up.sh`。这是默认组合，足够控制台、API 与元数据相关的工作。

**加上事件链路** —— 再跑 `./deploy/up-events.sh`。它要求
`.data/models/bge-small-zh-v1.5/onnx/model_quantized.onnx` 存在；缺失会快速失败，而不是背着你下载模型。

**加上宿主工作器** —— `make task-worker-daemon`，保持同机 `local-host` 节点在线，并用 GStreamer + OCR +
Timeline 融合执行被派发的工作。

**媒体流（可选、独立）** —— `make stream-up` / `make stream-down` 单独运行 MediaMTX，不影响控制台栈。

**macOS 常驻形态** —— `deploy/macos/launchd/` 渲染 `launchd` plist，`deploy/macos/bin/` 包装常驻 runtime。
`make resident-probe`（只读）、`resident-install`、`resident-status`、`resident-uninstall`。
`launchctl`/`sysctl` 只存在于 macOS，因此这一组是明确记录的主机例外。

## 部署验收

部署的验收标准是**动作**能跑通，而不是状态端点有应答。具体来说：

1. `./deploy/status.sh` 每个探测都报 `OK`；
2. 真实登录成功，并且一个写动作完成（建任务、发布方案 revision，或导入视频）；
3. 能力报告与你宣称的一致：`make capability-check`；
4. 如果你要宣称媒体闭环，`make golden-path-check` 在真实授权样本上通过。

容器 `healthy`、`/v1/health` 返回 `200`、节点已注册，都只是基础设施就绪——不是产品验收。浏览器卡在登录
也不算可视化验收。

## 回滚

- 镜像按 digest 固定。往前的重建用 `./deploy/up.sh`；不存在原地修改运行中镜像的方式。
- 迁移只追加。回滚的含义是**写一条新迁移**，绝不改写历史 revision。
- `./deploy/down.sh --volumes` 是破坏性操作：会删除数据库与 JetStream 状态。执行前先确认有备份。
- 替换某个服务之后要重启 `console`：它的 nginx 在启动时解析 `api` upstream，可能继续持有旧的容器 IP。
  见 [故障排查](/zh/operations/troubleshooting)。

## 托管文档站

文档站是纯静态的 VitePress 产物，任何静态服务器都能托管：

```sh
make docs-install   # 一次性在 docs 容器内跑 npm ci（唯一需要 registry 的步骤）
make docs-build     # 把产物写到 apps/docs/.vitepress/dist
make docs-serve     # 预览构建产物
```

在 compose 里由 `docs` 服务托管在 `127.0.0.1:5174`，内容来自镜像构建期产出的站点。`make docs-build`
还会把**根路径**产物覆盖进运行中的容器，因此只有在改动站点常量或 nginx 配置后才需要重建镜像
（`./deploy/up.sh docs`）。

`DOCS_BASE` 与 `DOCS_SITE_URL` 都是**构建期**变量，从命令行传入：

```sh
make docs-build DOCS_BASE=/sensoryplex/                   # 供子路径托管的产物
make docs-build DOCS_SITE_URL=https://docs.example.com     # 带 sitemap/绝对地址的产物
```

它们只改变产物内容。本机服务始终从 `127.0.0.1:5174` 的根路径托管，所以子路径产物只写到
`apps/docs/.vitepress/dist`，**不会**覆盖进容器。

提交文档改动前要跑的门禁是 `make docs-check`：它构建站点，并校验语言树对等与每一条内部链接、资源。

### GitHub Pages

本站以项目页形式发布在 <https://jaytu211.github.io/SensoryPlex/>，由**手动派发**的工作流
`.github/workflows/docs-pages.yml` 完成：

```sh
gh workflow run docs-pages.yml --ref master      # 或在 GitHub：Actions → docs-pages → Run workflow
```

手动是有意为之：本仓库不在 push 或 PR 上消耗托管 runner 额度（见[参与贡献](/zh/project/contributing)）。
仓库的 Pages 已启用，来源是 `GitHub Actions`，因此"发布"就是那次工作流跑成功——没有任何内容被推到分支上。

两个构建期常量由该工作流持有，改动时必须与本文档同步：

```yaml
DOCS_BASE: /SensoryPlex/                   # 项目页托管在子路径下
DOCS_SITE_URL: https://jaytu211.github.io   # 只写域名——子路径是另一件事
```

`DOCS_SITE_URL` **不能**带子路径。VitePress 交给 sitemap 包的是站点内相对路径，而 sitemap 包按
`new URL(路径, hostname)` 解析：带前导 `/` 的路径会把 `hostname` 里的子路径整体丢掉，于是
`https://jaytu211.github.io/SensoryPlex` 会悄悄产出指向 `https://jaytu211.github.io/...` 的 sitemap 条目。
`.vitepress/config.mts` 里的 `sitemap.transformItems` 负责把 `base` 补回去，`scripts/check-docs.mjs`
则在任何一条 sitemap URL 落在基础路径之外时直接让构建失败。

工作流先构建产物、跑同一套 `check-docs.mjs` 门禁，通过之后才部署——校验失败时线上仍是上一版，
而不是把一套链接不自洽的产物推上去。要在本机复现"线上那一份产物"：

```sh
make docs-check DOCS_BASE=/SensoryPlex/ DOCS_SITE_URL=https://jaytu211.github.io
```
