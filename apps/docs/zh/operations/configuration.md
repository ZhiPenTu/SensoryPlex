# 配置参考

所有运行时配置都在仓库根的 `.env` 里，它由 `make configure` 生成、被 Git 忽略，且绝不能提交。
`.env.example` 列出了完整键集合；本页解释每个键改变什么，以及取值不对时系统会怎么表现。

## 端口与绑定地址

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CONSOLE_PORT` | `5173` | Web 控制台 |
| `API_PORT` | `8091` | 业务/管理/认证 API |
| `GATEWAY_PORT` | `8090` | 兼容入口 |
| `DOCS_PORT` | `5174` | 本文档站 |
| `DOCS_DEV_PORT` | `5175` | 仅供 `make docs-dev` / `docs-serve`；站点本身不使用 |
| `POSTGRES_PORT` | `25432` | 元数据存储 |
| `NATS_PORT` | `24222` | 事件总线客户端端口（监控：`28222`） |

宿主绑定一律回环。索引检索面（`index:50077`）在 compose 网络内可达，且刻意不发布到宿主。

## 凭据

| 变量 | 是否必需 | 用途 |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | 必需，无默认值 | Postgres 密码；未设置时 compose 直接中止 |
| `SENSORYPLEX_DATABASE_URL` | 必需 | 含密码的完整 DSN |
| `SENSORYPLEX_API_TOKEN` | 必需，无默认值 | 单部署 API token；未设置时 compose 直接中止 |
| `SENSORYPLEX_PRINCIPAL` | `local-developer` | 部署 token 映射到的主体 |
| `SENSORYPLEX_INDEX_AUTH_TOKEN` | events profile 必需 | 检索面读取；长度 ≥ 32 |
| `SENSORYPLEX_INDEX_SEARCH_TOKEN` | events profile 必需 | API 读取；与上面是**同一个** token |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | 预留 | 示例里为对象存储路径预留，该路径尚未实现 |

随机值由 `make configure` 填入。两个“必需且无默认值”的键用了 compose 的 `:?Run make configure` 守卫，
因此缺失会立刻让整个栈失败，而不是带着空凭据启动。

::: warning 一个 token，两个名字
`SENSORYPLEX_INDEX_AUTH_TOKEN` 与 `SENSORYPLEX_INDEX_SEARCH_TOKEN` 是**同一个** token 在两个进程里的视角。
只给其中一个会让 API 拒绝启动；两个都不给是允许的，此时 API 会把语义检索显式报成不可用，而不是假装能用。
:::

## 事件链路档位

档位决定在飞上限，两个批深度**必须小于等于**它：

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `SENSORYPLEX_RESIDENT_TIER` | `medium` | 用于准入的机型档位 |
| `SENSORYPLEX_EVENT_QUEUE_CAPACITY` | `32` | 该档的在飞上限 |
| `SENSORYPLEX_EVENT_RELAY_BATCH` | `16` | relay 每轮认领的行数 |
| `SENSORYPLEX_EVENT_CONSUME_BATCH` | `32` | 消费侧在飞未 ack 的消息数 |

失败语义刻意严格：

- 批深度越过上限 ⇒ 服务**在连任何东西之前**拒绝启动，原因 `event_inflight_exceeds_tier_cap`；
- 变量缺失 ⇒ 显式的 `not_injected`；
- 取值从不夹取，档位从不静默降级。

## 检索

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `SENSORYPLEX_INDEX_SEARCH_ENDPOINT` | `index:50077` | 检索面地址。容器内必须是 compose 服务名——那里的 `127.0.0.1` 指容器自己 |
| `SENSORYPLEX_INDEX_VECTOR_INDEX_KEY` | `material_text_bge_small_zh_v1_5_d512_v1` | collection 键；编码了模型与维度契约 |

collection 键是契约而不是标签：检索面会按建设 collection 的模型发布版本做同源守卫，因此换了编码器却不换键会被拒绝，
而不是被混装。

## 宿主集成

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `REPO_ROOT` | `../../` | bind mount 进 `api`/`gateway`/`console` 的仓库根；写成绝对路径可让容器内验证可复现 |
| `MEDIA_DIR` | `/tmp` | 授权样本目录，以只读方式挂到 `/host-media` |
| `SENSORYPLEX_TEST_DATABASE_URL` | — | `make test-integration` 的测试库；缺失时集成测试**报错而不是跳过** |
| `SENSORYPLEX_RUNTIME_ADDR` | `127.0.0.1:50051` | 宿主工具使用的 runtime gRPC 地址 |
| `RUST_LOG` | `info` | Rust 日志过滤 |

## 演示账号

| 变量 | 说明 |
| --- | --- |
| `SENSORYPLEX_DEMO_USERNAME` | 由 `make demo-seed` 写入；只有它存在时登录页才显示演示按钮 |
| `SENSORYPLEX_DEMO_PASSWORD` | 随机密码，写入 `.data/demo-password` |

演示凭据是特权账号（`admin`、`operator`）。生产性质节点上请把两者留空，登录页会自动回退到
“首次使用需由节点管理员创建账户”。

## 文档站

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `DOCS_PORT` | `5174` | `docs` 服务的宿主端口 |
| `DOCS_DEV_PORT` | `5175` | `make docs-dev` / `make docs-serve` 的宿主端口 |
| `DOCS_BASE` | `/` | **构建期**：产物要被托管在子路径下时的前缀 |
| `DOCS_SITE_URL` | 空 | **构建期**：设置后构建会产出带绝对地址的 sitemap；空值表示“未发布” |

`DOCS_BASE` 与 `DOCS_SITE_URL` 从命令行传入（`make docs-build DOCS_BASE=/repo/`），`docs` 服务**不**
从 `.env` 读它们：本机服务始终从 `127.0.0.1:5174` 的根路径托管。子路径产物落在
`apps/docs/.vitepress/dist`，且刻意**不**覆盖进运行中的容器——它的绝对链接在根路径 nginx 后面会全 404。
镜像构建只注入 `DOCS_SITE_URL`（它只增加 sitemap/绝对地址），不注入 `DOCS_BASE`。

## 运维规则

- `.env` 被 Git 忽略，且必须保持如此。绝不要把生成的 `.env` 贴进 issue、截图或容器日志。
- 轮换凭据意味着在 `.env` 里重新生成，并重启读取它的服务——有些服务（如索引检索面）在启动时校验，
  旧进程会继续使用旧值。
- 容器里的 `/workspace` bind mount 是宿主树的视图。容器无法把新凭据写回宿主 `.env`；这正是
  `make configure` 必须是宿主目标的原因。
