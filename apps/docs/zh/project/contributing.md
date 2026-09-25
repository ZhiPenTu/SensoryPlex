# 贡献与翻译

## 写代码之前

按顺序读：仓库根的两份需求文档、`docs/implementation-status.md`，以及覆盖你要改的那块区域的 ADR。与 ADR 冲突的
改动，要么附带一份 ADR 修订，要么重新考虑——ADR 是承重结构，不是历史记录。

## 代码在哪里运行

| 层面 | 规则 |
| --- | --- |
| 控制面（`api`、`gateway`、`console`、`postgres`、`nats`、`relay`、`index`） | 构建、lint、迁移与验证一律**在容器内**通过 `docker compose exec -T <service> ...` 执行 |
| 主机例外 | `cargo`/Rust 构建、`make configure`、`launchd`、模型插件、GStreamer 与 HF 权重、MediaMTX、授权媒体 |

容器规则的目的是消除“本地能跑、生产起不来”的漂移。不要给控制面路径引入宿主依赖，也不要给需要宿主加速器的
路径套上容器步骤。

## 不可让步的规则

1. **`proto/` 是唯一的跨语言契约源。** 改契约就运行 `make proto`，并提交重新生成的产物。禁止手改生成代码。
2. **字段号是永久的。** 删除必须 `reserved`；破坏语义需要新的协议 major。
3. **迁移只追加**，并通过 `tools/migrate.py` 显式执行。绝不要改写历史 revision 来“回滚”——新增一条迁移。
4. **禁止合成业务数据。** 不编造模型结果、图表序列或分钟值。空结果就返回空。
5. **未知保持未知。** 置信度缺失、PTS 未知、探测不出的加速器与未知容量都按未知上报并说明原因，绝不填默认值。
6. **原始媒体与密钥不出数据面。** 不进控制消息、不进事件、不进日志。
7. **所有队列与并发都有上限**，且每个失败都有可观察语义。
8. **注释语言：** 手写 Rust（`///`、`//!`、`//`）与 Python（docstring、`#`）注释默认中文；`proto/` 文件与生成
   代码保持英文。中文叙述里，API、协议、容器、库与 ADR 标识符保留英文原名。

## 验证期望

跑覆盖你改动范围的检查，并且说清它们证明了什么：

```sh
make check                # lint + Python 测试 + Rust fmt/clippy/test
make test-integration     # 真实 PostgreSQL（相关时还有 NATS）
make orchestration-p1-check
make event-pipeline-check
```

媒体端到端工作必须用真实授权样本。被跳过的测试、未执行的 CI 作业、`healthy` 的容器，都不构成证据。
如果某个检查你跑不了，请在变更说明里写明，而不是让它保持暗示。

## 持续集成

GitHub Actions 按设计**仅手动触发**：工作流不会在每次 push 或 PR 上消耗托管 runner 额度。日常门禁在本地完成。
手动派发时，昂贵的 macOS 作业只在勾选 `run_apple_silicon` 后才会启动——而没有执行的远端 macOS 作业绝不能被
描述成通过。

已发布的文档站遵循同一条规则：`docs-pages.yml` 只在被手动派发时才把 `apps/docs` 发布到 GitHub Pages，
所以线上站点的最新程度取决于它上一次运行，而不是 `master` 的当前提交。它的构建期常量与本机复现命令见
[部署](/zh/operations/deployment)。

## 参与本文档站

站点在 `apps/docs`，用 VitePress 构建。结构：

```text
apps/docs/
├── .vitepress/config.mts     # locales、nav、sidebar、edit link
├── public/                   # favicon.svg / logo.svg，原样拷进产物
├── scripts/check-docs.mjs    # 语言树对等 + 产物链接校验（无第三方依赖）
├── <page>.md                 # 英文页（服务于 /）
└── zh/<page>.md              # 中文页（服务于 /zh/）
```

站点静态资源放在 `public/`，**不是** `.vitepress/public/`：VitePress 把 public 目录解析为
`srcDir/public`。放错位置的文件会静默地不出现在产物里——这正是 `make docs-check` 要逐条校验构建产物
里每个链接与资源的原因。

### 新增或修改页面

1. 在英文树里创建页面（`apps/docs/<section>/<slug>.md`）。
2. 在 `zh/` 下同路径创建对应中文页——**两种语言都必须有该页**。VitePress 不做跨语言回退，缺文件就是 404，
   而不是显示未翻译文本。
3. 在 `.vitepress/config.mts` 的**两个** sidebar（`EN_SIDEBAR` 与 `ZH_SIDEBAR`）里都加上条目，路径形状一致
   （`/section/slug` 与 `/zh/section/slug`）。
4. 运行 `make docs-check`——它会构建站点并校验内部链接与资源。容器里还没有 `node_modules` 时先跑一次
   `make docs-install`：它是 docs 目标里唯一需要 npm registry 的一个，缺依赖时其余目标会显式失败并提示。

边写边看可以跑 `make docs-dev`（带热更新的 VitePress dev server，`http://127.0.0.1:5175`）；
`http://127.0.0.1:5174` 上跑的是已构建产物。

### 新增一门语言

1. 创建语言目录（例如 `ja/`），镜像整棵页面树。
2. 在 `.vitepress/config.mts` 加 `locales.<code>` 条目，包含 `label`、`lang`、`title`、`description`、
   自己的 `nav`、自己的 `sidebar`，以及带有该语言目录前缀的 `editLink.pattern`（`apps/docs/ja/:path`）。
3. 各语言的导航结构保持一致，让读者在任何页面切换语言后都停在同一主题上。
4. **状态宣称要重新陈述，而不是重新翻译。** 三个标签（已验证 / 未验收 / 未实现）与证据命令必须原义穿过翻译。
   不要把“未验收”软化得更乐观，也不要添加英文页没有的能力宣称。
5. 命令、文件路径、标识符与错误码保持原样；只翻译它们周围的叙述。

### 文档风格

- 先说读者能做什么，再说边界。
- 每条能力陈述都应带上它的证据命令，或明确写“未验收”。
- 宁给一条可运行命令，也不描述一条命令。
- 链接到 ADR 或源码文件，而不是用可能漂移的方式复述契约。
