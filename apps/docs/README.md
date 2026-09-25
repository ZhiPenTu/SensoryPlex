# SensoryPlex 文档站（apps/docs）

开源框架使用文档的静态源站点：VitePress 1.6.4，英文默认在 `/`、简体中文在 `/zh/`。
面向读者的是产物本身（本机 `http://127.0.0.1:5174`，或任何静态服务器），本文件只写给改文档的人。

## 常用命令（全部在容器内执行，宿主机不需要 node 工具链）

```sh
make docs-install   # 一次性安装依赖（唯一需要 npm registry 的步骤）
make docs-dev       # 带热更新的本地预览：http://127.0.0.1:5175
make docs-check     # 构建 + 语言树对等 + 产物内部链接/资源校验（提交前门禁）
make docs-build     # 产出产物到 apps/docs/.vitepress/dist（可托管到任何静态服务器）
make docs-serve     # 预览已构建产物（与 docs-dev 共用 5175，二者互斥）
```

需要先有容器栈：`./deploy/up.sh`（`./deploy/up.sh docs` 可只起文档站）。

## 目录约定

```text
apps/docs/
├── .vitepress/config.mts     # locales、nav、sidebar、edit link、站点常量
├── public/                   # logo.svg / favicon.svg，原样拷进产物（不是 .vitepress/public）
├── scripts/check-docs.mjs    # 语言树对等 + 产物链接校验（无第三方依赖）
├── <page>.md                 # 英文页（服务于 /）
└── zh/<page>.md              # 中文页（服务于 /zh/）
```

- 两种语言必须**逐页对等**：VitePress 不做跨语言回退，缺页就是 404。`make docs-check` 会直接报出缺哪一页。
- 站点静态资源必须放 `public/`：VitePress 解析的 public 目录是 `srcDir/public`，放进 `.vitepress/public/`
  会静默丢失（`make docs-check` 的链接校验就是为了拦住这类问题）。
- 新增页面/新增语言的具体步骤见站点上的
  [贡献与翻译](project/contributing.md)。

## 编辑纪律

- 站点上的**能力描述只写已验证结论**，并带三态标签（已验证 / 未验收 / 未实现）与证据命令；
  `ReplayReport.golden_path_verified=false` 等未验收边界不允许被软化。
- 命令、路径、标识符、错误码原样保留（GStreamer、ffprobe、gRPC、ADR-003 等英文原名不翻译），
  只翻译叙述。
- 英文页与中文页的**状态宣称强度必须一致**：中文页不得比英文页更乐观。

## 站点常量（构建期，只改变产物内容）

```sh
make docs-build DOCS_BASE=/sensoryplex/                 # 供子路径托管的产物
make docs-build DOCS_SITE_URL=https://docs.example.com   # 带 sitemap/绝对地址的产物
```

本机 `docs` 服务始终按根路径托管，因此子路径产物只落在 `apps/docs/.vitepress/dist`，不会被覆盖进容器。
