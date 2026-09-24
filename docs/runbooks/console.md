# 素材工作台运行手册

Console 是独立的 React / TypeScript / Vite 工程；`services/api` 在一个 FastAPI 进程中装配
Business、Admin 和 Identity。`services/gateway` 仅保留旧导入入口，查询实现统一位于 API。
Runtime 仍是独立进程。本阶段交付应用准备流程，不宣称视频到 AI 素材的 Golden Path 已完成。

## 本地预览

需要 Node.js 24、Python 3.12、uv，以及已有 `.env` 和可连接的 PostgreSQL。
新检出可先执行 `make configure && make infra`，已有环境直接执行：

```sh
make console-build
make console-prepare
make console-api
```

访问 <http://127.0.0.1:8091>，账户 `admin`。随机密码保存在
`.data/console-preview/admin-password`（权限 0600，不写入日志或 Git）。重复 prepare 不重置账号。
登录页提供“填入演示账号”按钮，填入独立 `demo` 账户后仍需手动点击登录。该公开演示账户仅由
本地预览 prepare 创建，与 `admin` 的随机密码独立；常规 API 部署默认关闭演示入口。
停用演示账户或更改密码后，入口自动隐藏。

prepare 显式执行迁移，服务启动本身不迁移数据库。Python 依赖安装在独立的
`.data/console-venv`，只同步 `sensoryplex-api` 包，不为工作台安装模型执行依赖。

预览使用同一个 PostgreSQL 中独立的 `sensoryplex_console` schema，`search_path` 不回退到 public。
Blob 存入 `.data/console-preview/blobs`。因此不会读取或修改旧 Gateway 的素材记录；初始检索为空。
该 schema 只隔离本地预览数据，不构成独立数据库身份或安全边界。

前端开发时保持 API 运行，在另一个终端执行 `make console-dev`，访问 <http://127.0.0.1:5173>。
Vite 将 `/v1`、`/auth`、`/admin` 代理给 8091。生产构建由 API 同源提供，静态资源使用 `/static`，
避免与视频库的 `/assets` 页面冲突。刷新页面、直接打开深层路径均由 SPA 接管。

## 已实现的流程

1. 浏览器登录；菜单、路由及服务端同时执行权限检查。`admin` 角色不自动获得内容权限；
   预览账户同时有 `admin` 和 `operator`。媒体和素材查询始终检查 owner。
2. 插件中心读取仓库受控 Manifest，标明“源码可用 / 未验证来源”；保存符合 JSON Schema 的配置，
   同名配置创建不可变 revision，不导入或执行插件代码。
3. 处理方案绑定插件摘要和配置版本，保存草稿；引用中的方案不能被归档。
4. 视频库真实流式上传 MP4 / WebM，保存 SHA-256 和文件，支持持久授权读取、HEAD、Range 和浏览器预览。
   失败上传可取消后重试。仅校验容器签名；“待媒体准入”不等于可解码或已获 Runtime 准入。
5. 创建引用已上传视频和方案的任务草稿，或归档草稿；不会自动创建 Runtime 任务或模型结果。
6. 查询数据库中的真实素材、详情、历史 revision、时间锚点、来源和置信度；缺失置信度显示未知。
   素材与本次上传的媒体尚未建立来源映射，详情明确显示原片定位待接入。
7. 管理员可创建用户、调整角色、停用和重设密码；权限或密码变化撤销该账号的会话与凭据，
   不能移除自己的管理权限或停用自己。
8. 创建、限期和撤销业务访问凭据；秘密仅创建时显示，数据库只保存 hash。API 凭据不能管理其他凭据，
   也不能获得 Admin scopes。审计保存操作者、动作、目标、时间，不记录密码、令牌和媒体内容。

## 仍依赖底座接线的部分

| 能力 | 当前行为 |
| --- | --- |
| 媒体准入 / 解码 / 推理 / 时间轴融合 | 工作台没有发起 Runtime 调用；上传保留待准入 |
| 任务执行、取消、恢复 | `/v1/jobs` 明确返回 501；界面只管理草稿 |
| 插件安装、启停、卸载、已安装清单 | 管理 API 返回明确 501；安装按钮禁用并说明原因 |
| 方案发布、运行时能力校验 | 返回 501，不能发布草稿 |
| 素材原片时间点回看 | 待 Runtime 素材来源与媒体资产映射 |
| 语义检索 | 已接线（ADR-023）：未配置检索面时 503 + `semantic_search_unavailable`，检索面不可达/令牌不符也是 503（`retryable` 区分）；只支持查询文字与条数，筛选条件显式 422，不自动转成关键词成功 |
| MCP / 对外查询 gRPC | 仍未接线；REST 契约和权限可供后续 adapter 复用 |

本地受控目录不是公共插件市场。签名校验、在线发行源、包下载、升级回滚与模型就绪校验均未实现。

## 使用已有业务数据库

此路径会显式升级目标数据库；先备份并确认 `SENSORYPLEX_DATABASE_URL` 指向的目标。
不要将预览账户当作生产预置账号。

```sh
uv sync --frozen
make proto
make migrate
uv run sensoryplex-user admin --roles admin,operator
make console-build
uv run uvicorn sensoryplex_api.app:create_app --factory --host 127.0.0.1 --port 8091 --no-access-log --limit-concurrency 64
```

用户命令交互读取密码，不将密码放入 argv。部署设置沿用 `SENSORYPLEX_` 环境前缀：
`DATABASE_URL`、`BLOB_ROOT`、`CONSOLE_DIST`、`PLUGIN_ROOT`、`ALLOWED_ORIGINS`、`COOKIE_SECURE`。
非本机部署应配置 TLS、精确 Origin 和 Secure cookie。旧 `API_TOKEN` 仅作为显式业务兼容凭据，不能访问管理接口。
升级 Gateway 镜像前也需显式应用 `0002_console`；旧 Python import 路径保留，不维护第二套实现。

`services/api/Dockerfile` 包含前端构建、非 root API 和静态页面；数据目录需持久化。此次本机镜像构建
在拉取 Node 基础镜像时被 Docker 镜像站 EOF 阻断，尚无容器启动验收结论。

## 资源边界与运维限制

- JSON 请求体 64 KiB；查询分页默认 50、最多 100；offset 最多 10000。不会无限重试。
- 单文件默认最多 1 GiB，上传最多 900 秒；异步上传连接池最多 2 个连接、2 个等待者。
  PostgreSQL advisory lock 将共享数据库上的活动上传限制为 1 个，其余请求明确拒绝。
- 元数据连接池默认 8 个连接、最多 32 个等待者；SQL timeout 5 秒、锁等待 2 秒。
- 每账号最多 20 个待上传记录、100 个活动任务草稿、20 个活动访问凭据、20 个登录会话。
- 未上传记录可取消；已经保存的文件不提供删除接口。进程硬终止可能留下 staging 或未引用 Blob，
  当前没有自动垃圾回收，维护前必须核对数据库引用并停止写入；不要按时间直接删除已有 Blob。
- 数据与文件需一起备份。迁移只追加，回退应用版本前先确认 schema 兼容；不自动删除 0002 表。
- 当前用户和凭据列表最多展示 100 条，任务选择器最多列出最近 100 个资产 / 方案。
  大规模目录检索、集群共享存储、容量压测、后台清理和远端部署尚未验收。

## 验证

```sh
make proto
make check
make integration
npm --prefix apps/console run format:check
make console-build
```

`tests/integration/test_console.py` 使用真实 PostgreSQL 的临时 schema，覆盖身份、CSRF、跨 owner 访问、
内容摘要、Range、重启后读取、取消失败上传、不可变配置、草稿引用、能力拒绝和令牌撤销。
其中构造的文件头仅用于传输测试，不作为可解码样本或模型成功证据。
可重复浏览器验收（需要已启动的本地 API、预览管理员密码文件及真实授权视频）：

```sh
make console-check MEDIA="$PWD/video/samples/sasebo-basketball.480p.vp9.webm"
```

脚本固定使用 `agent-browser@0.38.1`，只允许 localhost；会真实创建带唯一 `ui-` 前缀的配置、
方案、上传记录、任务草稿、已撤销凭据和只读账户，保留审计证据，不调用 Runtime。
截图与结果存入 `.data/console-preview/ui-*/`，不保存密码或令牌快照。
实际浏览器验收记录见 `docs/verification.md` 的 Console 小节。
