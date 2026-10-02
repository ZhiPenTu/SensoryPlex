import { defineConfig } from 'vitepress'

// 站点常量集中在这里：仓库地址、默认分支与外部入口，避免散落在各页手写。
const REPO_URL = 'https://github.com/ZhiPenTu/SensoryPlex'
const REPO_BRANCH = 'master'
// 部署域名由环境变量注入。默认空值表示“尚未发布到公网”，此时不产出 sitemap/OG 的绝对地址，
// 也不编造域名；静态产物本身与是否发布无关。
const SITE_URL = process.env.DOCS_SITE_URL ?? ''
// 站点基础路径（子路径部署，例如 GitHub Pages 项目页）。VitePress 会自己把 `base` 规范化成
// 前后都带斜杠的形式，但 `head` 里的绝对 URL 不经过它的处理，所以先在这里统一规范化。
const normalizeBase = (value) =>
  !value || value === '/' ? '/' : `/${value.replace(/^\/+|\/+$/g, '')}/`
const BASE = normalizeBase(process.env.DOCS_BASE)
// sitemap 条目补基路径用：把 `/SensoryPlex/` 与 `/guide/x` 拼成 `SensoryPlex/guide/x`。
// 拼出来的值**不能**带前导 `/`——sitemap 包按 `new URL(条目, hostname)` 语义解析，
// 带前导 `/` 的路径会把 hostname 里的子路径整体丢掉。
const withBase = (url: string) => BASE.replace(/^\//, '') + url.replace(/^\//, '')
// sitemap 条目的最小结构：校验器只关心 url 与 hreflang 交替链接，其余字段原样透传。
type SitemapEntry = { url: string; links?: { lang: string; url: string }[] }
// “最后更新”时间来自 `git log`，而构建镜像里没有 git 二进制。默认关闭，让任何环境下的构建产物一致；
// 需要时用 DOCS_LAST_UPDATED=1 打开，并要求构建环境真的有 git（否则 VitePress 会直接构建失败）。
const LAST_UPDATED = process.env.DOCS_LAST_UPDATED === '1'

const EN_SIDEBAR = [
  {
    text: 'Getting started',
    items: [
      { text: 'Introduction', link: '/guide/introduction' },
      { text: 'Quickstart', link: '/guide/quickstart' },
      { text: 'Installation', link: '/guide/installation' },
      { text: 'Console walkthrough', link: '/guide/console-walkthrough' },
      { text: 'AI Agents (MCP & Skills)', link: '/guide/mcp-and-skills' }
    ]
  },
  {
    text: 'Core concepts',
    items: [
      { text: 'Model overview', link: '/concepts/overview' },
      { text: 'Timeline semantics', link: '/concepts/timeline' },
      { text: 'Capability model', link: '/concepts/capability-model' }
    ]
  },
  {
    text: 'Architecture',
    items: [
      { text: 'System overview', link: '/architecture/overview' },
      { text: 'Contracts & codegen', link: '/architecture/contracts' },
      { text: 'Services & nodes', link: '/architecture/services' },
      { text: 'Orchestration core', link: '/architecture/orchestration' },
      { text: 'Event pipeline & retrieval', link: '/architecture/event-pipeline' }
    ]
  },
  {
    text: 'Plugin development',
    items: [
      { text: 'Plugin system', link: '/plugins/overview' },
      { text: 'Packaging & manifest', link: '/plugins/package-and-manifest' },
      { text: 'Python SDK', link: '/plugins/python-sdk' }
    ]
  },
  {
    text: 'Operations',
    items: [
      { text: 'Deployment', link: '/operations/deployment' },
      { text: 'Configuration', link: '/operations/configuration' },
      { text: 'Troubleshooting', link: '/operations/troubleshooting' }
    ]
  },
  {
    text: 'Reference',
    items: [
      { text: 'Capability status', link: '/reference/status' },
      { text: 'Make targets', link: '/reference/make-targets' },
      { text: 'ADR index', link: '/reference/adr-index' }
    ]
  },
  {
    text: 'Project & Community',
    items: [{ text: 'Contributing & Governance', link: '/project/contributing' }]
  }
]

const ZH_SIDEBAR = [
  {
    text: '快速开始',
    items: [
      { text: '项目简介', link: '/zh/guide/introduction' },
      { text: '快速上手', link: '/zh/guide/quickstart' },
      { text: '安装与前置要求', link: '/zh/guide/installation' },
      { text: '控制台全流程', link: '/zh/guide/console-walkthrough' },
      { text: '大模型集成 (MCP & Skills)', link: '/zh/guide/mcp-and-skills' }
    ]
  },
  {
    text: '核心概念',
    items: [
      { text: '模型总览', link: '/zh/concepts/overview' },
      { text: '时间轴语义', link: '/zh/concepts/timeline' },
      { text: '能力模型', link: '/zh/concepts/capability-model' }
    ]
  },
  {
    text: '架构',
    items: [
      { text: '系统总览', link: '/zh/architecture/overview' },
      { text: '契约与代码生成', link: '/zh/architecture/contracts' },
      { text: '服务与节点', link: '/zh/architecture/services' },
      { text: '可编排执行核心', link: '/zh/architecture/orchestration' },
      { text: '事件链路与检索', link: '/zh/architecture/event-pipeline' }
    ]
  },
  {
    text: '插件开发',
    items: [
      { text: '插件体系', link: '/zh/plugins/overview' },
      { text: '打包与 Manifest', link: '/zh/plugins/package-and-manifest' },
      { text: 'Python SDK', link: '/zh/plugins/python-sdk' }
    ]
  },
  {
    text: '运维',
    items: [
      { text: '部署', link: '/zh/operations/deployment' },
      { text: '配置参考', link: '/zh/operations/configuration' },
      { text: '故障排查', link: '/zh/operations/troubleshooting' }
    ]
  },
  {
    text: '参考',
    items: [
      { text: '能力实现状态', link: '/zh/reference/status' },
      { text: 'Make 目标', link: '/zh/reference/make-targets' },
      { text: 'ADR 索引', link: '/zh/reference/adr-index' }
    ]
  },
  {
    text: '项目与社区',
    items: [{ text: '贡献指南与社区治理', link: '/zh/project/contributing' }]
  }
]

export default defineConfig({
  title: 'SensoryPlex',
  description:
    'Edge-side multimodal material preprocessing framework: Rust core, Python AI SDK, Protobuf/gRPC contracts.',
  base: BASE,
  cleanUrls: true,
  // `README.md` 是写给贡献者的仓库文件，不是站点页面：排除它，避免产物里多出一个没人链接的
  // `/README` 页面。（`scripts/check-docs.mjs` 的语言树对等校验同样跳过根 README。）
  srcExclude: ['README.md'],
  lastUpdated: LAST_UPDATED,
  head: [
    // 站点图标与 logo 都放在 `public/`（VitePress 的 publicDir 是 srcDir 下的 `public/`，
    // 不是 `.vitepress/public/`），构建时原样拷到产物根目录。
    ['link', { rel: 'icon', type: 'image/svg+xml', href: `${BASE}favicon.svg` }],
    ['meta', { name: 'theme-color', content: '#4f46e5' }],
    ['meta', { property: 'og:type', content: 'website' }],
    ['meta', { property: 'og:site_name', content: 'SensoryPlex' }],
    [
      'meta',
      {
        property: 'og:description',
        content:
          'Edge-side multimodal material preprocessing framework: Rust core, Python AI SDK, Protobuf/gRPC contracts.'
      }
    ]
  ],
  ...(SITE_URL
    ? {
        sitemap: {
          hostname: SITE_URL,
          // 子路径部署（GitHub Pages 项目页）下必须补基路径：VitePress 交给 sitemap 包的条目是
          // 站点内相对路径（如 `/guide/quickstart`），而 sitemap 包按 `new URL(条目, hostname)`
          // 解析——以 `/` 开头的路径会把 hostname 里的子路径整体丢掉，于是 sitemap 里每条 URL
          // 都指向 404。hreflang 的 alternate 链接是同一批路径，同样要补。
          transformItems: (items: SitemapEntry[]): SitemapEntry[] =>
            items.map((item) => ({
              ...item,
              url: withBase(item.url),
              ...(item.links
                ? { links: item.links.map((link) => ({ ...link, url: withBase(link.url) })) }
                : {})
            }))
        }
      }
    : {}),
  themeConfig: {
    logo: '/logo.svg',
    siteTitle: 'SensoryPlex',
    search: { provider: 'local' },
    socialLinks: [{ icon: 'github', link: REPO_URL }],
    outline: { level: [2, 3] },
    darkModeSwitchLabel: 'Appearance',
    sidebarMenuLabel: 'Menu',
    returnToTopLabel: 'Return to top'
  },
  locales: {
    root: {
      label: 'English',
      lang: 'en-US',
      title: 'SensoryPlex',
      description:
        'Edge-side multimodal material preprocessing framework: Rust core, Python AI SDK, Protobuf/gRPC contracts.',
      themeConfig: {
        nav: [
          { text: 'Guide', link: '/guide/introduction', activeMatch: '^/guide/' },
          { text: 'Architecture', link: '/architecture/overview', activeMatch: '^/architecture/' },
          { text: 'Plugins', link: '/plugins/overview', activeMatch: '^/plugins/' },
          { text: 'Operations', link: '/operations/deployment', activeMatch: '^/operations/' },
          { text: 'Reference', link: '/reference/status', activeMatch: '^/reference/' }
        ],
        sidebar: EN_SIDEBAR,
        editLink: {
          pattern: `${REPO_URL}/edit/${REPO_BRANCH}/apps/docs/:path`,
          text: 'Edit this page on GitHub'
        },
        outline: { label: 'On this page', level: [2, 3] },
        docFooter: { prev: 'Previous page', next: 'Next page' },
        lastUpdated: { text: 'Last updated' },
        footer: {
          message: 'Documentation is licensed with the project. Status statements reflect verified evidence only.',
          copyright: 'SensoryPlex — engineering base, version 0.1.0'
        }
      }
    },
    zh: {
      label: '简体中文',
      lang: 'zh-CN',
      title: 'SensoryPlex',
      description:
        '端侧 AI 多模态素材预处理框架：Rust 核心、Python AI SDK、Protobuf/gRPC 契约。',
      themeConfig: {
        nav: [
          { text: '指南', link: '/zh/guide/introduction', activeMatch: '^/zh/guide/' },
          { text: '架构', link: '/zh/architecture/overview', activeMatch: '^/zh/architecture/' },
          { text: '插件', link: '/zh/plugins/overview', activeMatch: '^/zh/plugins/' },
          { text: '运维', link: '/zh/operations/deployment', activeMatch: '^/zh/operations/' },
          { text: '参考', link: '/zh/reference/status', activeMatch: '^/zh/reference/' }
        ],
        sidebar: ZH_SIDEBAR,
        // `:path` 是相对站点根（apps/docs）的路径，**已经包含** `zh/` 前缀；
        // 因此这里与默认语言用同一个 pattern，不能再加一层语言目录。
        editLink: {
          pattern: `${REPO_URL}/edit/${REPO_BRANCH}/apps/docs/:path`,
          text: '在 GitHub 上编辑本页'
        },
        outline: { label: '本页目录', level: [2, 3] },
        docFooter: { prev: '上一页', next: '下一页' },
        lastUpdated: { text: '最后更新' },
        darkModeSwitchLabel: '外观',
        sidebarMenuLabel: '目录',
        returnToTopLabel: '回到顶部',
        footer: {
          message: '文档与项目同许可。状态描述只基于已验证的证据。',
          copyright: 'SensoryPlex — 可运行工程底座，版本 0.1.0'
        }
      }
    }
  }
})
