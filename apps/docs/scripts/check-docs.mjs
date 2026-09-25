#!/usr/bin/env node
// 文档站结构校验：语言对等 + 构建产物内部链接。
//
// VitePress 在构建期已经会报 markdown 死链，但有两件事它不覆盖：
//   1. 语言树是否对等——少了文件就是 404，而不是“显示未翻译文本”；
//   2. 构建产物里由 front matter / 组件拼出来的内部 href/src 是否真的有对应文件。
// 两者都是纯结构检查，因此不依赖网络，也不依赖任何第三方包。
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

// 新增语言时，把语言目录名加到这里，对等校验会自动覆盖它。
const LOCALES = ['zh']
// 这些目录不属于内容树：站点配置、依赖、脚本自身、构建产物。
const SKIP_DIRS = new Set(['.vitepress', 'node_modules', 'scripts'])

const siteRoot = fileURLToPath(new URL('..', import.meta.url))
const distDir = join(siteRoot, '.vitepress', 'dist')

// 站点基础路径：DOCS_BASE 注入后，产物里所有绝对链接都带这个前缀。检查对象是**产物本身**，
// 所以这里只认构建时同源的 DOCS_BASE，并用与 .vitepress/config.mts 完全相同的规则规范化；
// 不设置时等价于根路径，行为与以前一致。
const normalizeBase = (value) =>
  !value || value === '/' ? '/' : `/${String(value).replace(/^\/+|\/+$/g, '')}/`
const siteBase = normalizeBase(process.env.DOCS_BASE)

const errors = []
const notes = []

function walk(dir, predicate, out = [], skip = SKIP_DIRS) {
  for (const entry of readdirSync(dir)) {
    if (skip.has(entry)) continue
    const full = join(dir, entry)
    const stat = statSync(full)
    if (stat.isDirectory()) walk(full, predicate, out, skip)
    else if (predicate(full)) out.push(full)
  }
  return out
}

const toPosix = (p) => relative(siteRoot, p).split(sep).join('/')
const isMarkdown = (p) => p.endsWith('.md')

// ── 1. 语言对等 ────────────────────────────────────────────────────────────
const defaultPages = new Set(
  walk(
    siteRoot,
    (p) => isMarkdown(p) && !toPosix(p).startsWith('README'),
    [],
    new Set([...SKIP_DIRS, ...LOCALES])
  ).map(toPosix)
)

for (const locale of LOCALES) {
  const localeDir = join(siteRoot, locale)
  if (!existsSync(localeDir)) {
    errors.push(`语言目录缺失：${locale}/`)
    continue
  }
  const localePages = new Set(
    walk(localeDir, (p) => isMarkdown(p) && !toPosix(p).startsWith(`${locale}/README`)).map(
      (p) => toPosix(p).slice(locale.length + 1)
    )
  )
  const missing = [...defaultPages].filter((p) => !localePages.has(p))
  const extra = [...localePages].filter((p) => !defaultPages.has(p))
  for (const p of missing) errors.push(`${locale}/ 缺少页面：${p}`)
  for (const p of extra) errors.push(`${locale}/${p} 没有对应的默认语言页面`)
  notes.push(`${locale}: ${localePages.size} 页 / 默认语言 ${defaultPages.size} 页`)
}

// ── 2. 构建产物内部链接 ────────────────────────────────────────────────────
if (!existsSync(distDir)) {
  errors.push('构建产物不存在：先运行 npm run build，再运行本检查')
} else {
  const htmlFiles = walk(distDir, (p) => p.endsWith('.html'))
  const attrPattern = /(?:href|src)="([^"]+)"/g
  const isExternal = (url) =>
    /^(?:[a-z][a-z0-9+.-]*:)?\/\//i.test(url) || url.startsWith('mailto:') || url.startsWith('data:')

  // 返回目标绝对路径；null 表示"绝对链接不在站点基础路径下"（前缀没被构建进去，
  // 产物拿去子路径托管就是整站 404），由调用方带上下文报错。
  const resolveTarget = (href) => {
    const clean = href.split('#')[0].split('?')[0]
    if (!clean) return join(distDir, 'index.html')
    if (!clean.startsWith('/')) return join(distDir, clean.replace(/^(\.\/)+/, ''))
    if (siteBase === '/') return clean === '/' ? join(distDir, 'index.html') : join(distDir, clean)
    if (!clean.startsWith(siteBase)) return null
    const rest = clean.slice(siteBase.length)
    return rest ? join(distDir, rest) : join(distDir, 'index.html')
  }

  const candidatesFor = (target) => [
    target,
    `${target}.html`,
    join(target, 'index.html')
  ]

  for (const file of htmlFiles) {
    const html = readFileSync(file, 'utf8')
    for (const match of html.matchAll(attrPattern)) {
      const url = match[1]
      if (!url || url.startsWith('#') || isExternal(url)) continue
      const target = resolveTarget(url)
      if (target === null) {
        errors.push(`${toPosix(file)} → 绝对链接不在站点基础路径 ${siteBase} 下：${url}`)
        continue
      }
      // 尾斜杠链接必须命中**目录索引**本身，不能只靠 `.html` 兜底：本机 nginx 会把 `/guide/x/`
      // 落到 `guide/x.html`，而 GitHub Pages 不做这个回退（实测 `/zh/reference/status/` 是 404、
      // `/zh/reference/status` 是 200）。产物要同时能在两种形态下托管，所以按更严的那种卡。
      const candidates = candidatesFor(target)
      const targetExists = url.endsWith('/')
        ? candidates.some((candidate) => candidate.endsWith('index.html') && existsSync(candidate))
        : candidates.some((candidate) => existsSync(candidate))
      if (!targetExists) {
        errors.push(
          url.endsWith('/')
            ? `${toPosix(file)} → 尾斜杠链接没有目录索引（GitHub Pages 上会是 404）：${url}`
            : `${toPosix(file)} → 链接目标不存在：${url}`
        )
      }
    }
  }
  notes.push(`构建产物：${htmlFiles.length} 个 HTML 文件（站点基础路径 ${siteBase}）`)
}

// ── 3. sitemap（只在设置了 DOCS_SITE_URL 时才产出）─────────────────────────
// VitePress 默认把站点内相对路径直接交给 sitemap 包，子路径部署时基路径会被 URL 解析丢掉，
// 结果是 sitemap 里每条链接都指向 404。这里逐条确认 URL 落在站点基础路径下，防止它悄悄退化。
const sitemapPath = join(distDir, 'sitemap.xml')
if (existsSync(sitemapPath)) {
  const xml = readFileSync(sitemapPath, 'utf8')
  const urls = [
    ...xml.matchAll(/<loc>([^<]+)<\/loc>/g),
    ...xml.matchAll(/hreflang="[^"]*"\s+href="([^"]+)"/g)
  ].map((match) => match[1])
  const violations = []
  let checked = 0
  for (const url of urls) {
    let pathname
    try {
      pathname = new URL(url).pathname
    } catch {
      violations.push(`不是绝对地址：${url}`)
      continue
    }
    checked += 1
    if (!pathname.startsWith(siteBase)) {
      violations.push(`不在站点基础路径 ${siteBase} 下：${url}`)
    }
  }
  if (!urls.length) errors.push('sitemap.xml 没有任何可校验的 URL')
  if (violations.length) {
    const examples = violations.slice(0, 3).join('；')
    errors.push(`sitemap.xml 有 ${violations.length} 条 URL 不合格：${examples}`)
  }
  notes.push(`sitemap：校验 ${checked} 条绝对 URL（站点基础路径 ${siteBase}）`)
}

for (const note of notes) console.log(`[docs] ${note}`)
if (errors.length) {
  for (const error of errors) console.error(`[docs] FAIL ${error}`)
  process.exit(1)
}
console.log('[docs] OK 语言对等与内部链接检查通过')
