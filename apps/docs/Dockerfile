# 文档站容器：多阶段构建，先在 node:20-alpine 里构建 VitePress 静态产物，再交给
# nginx:1.27-alpine 静态托管。
#
# 这是**纯静态**站点：不反代任何后端、不持有数据库凭据、不依赖 api 是否在跑。
# 文档站不属于控制面，唯一的运行产物就是 apps/docs/.vitepress/dist。
# 保留 node 与 npm（同 console 的约定），让 `make docs-build` / `make docs-check` /
# `make docs-dev` 在容器内执行——宿主机不需要安装 node 工具链。
FROM docker.m.daocloud.io/library/node@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build
WORKDIR /docs

# 站点常量在构建期注入（compose 的 build.args 传入）：
# - DOCS_BASE：子路径部署（例如 GitHub Pages 项目页）时用；
# - DOCS_SITE_URL：留空表示“尚未发布”，此时不产出 sitemap/绝对地址，也不编造域名。
# 两者默认值都等于 config.mts 里的默认，因此本地构建与 .env 未配置时行为一致。
ARG DOCS_BASE=/
ARG DOCS_SITE_URL=
ENV DOCS_BASE=${DOCS_BASE} DOCS_SITE_URL=${DOCS_SITE_URL}

COPY apps/docs/package.json apps/docs/package-lock.json ./
# 带重试的 npm ci：构建期只有这一步需要公网，registry 抖动不应该把镜像构建变成随机失败。
# 只加传输层重试参数，不换 registry、不改依赖来源。
RUN npm ci --no-audit --no-fund \
      --fetch-retries=5 \
      --fetch-retry-mintimeout=10000 \
      --fetch-retry-maxtimeout=60000 \
      --fetch-timeout=300000

COPY apps/docs ./
RUN npm run build

FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10
RUN rm -f /etc/nginx/conf.d/default.conf && \
    # pid 与 error_log 重定向到 /tmp 与 stderr，避开 read_only 根无法写 /var/run、/var/log/nginx 的限制。
    sed -i 's|^pid .*|pid /tmp/nginx.pid;|' /etc/nginx/nginx.conf && \
    sed -i 's|^error_log .*|error_log /dev/stderr info;|' /etc/nginx/nginx.conf && \
    grep -q 'pid /tmp/nginx.pid;' /etc/nginx/nginx.conf && \
    grep -q 'error_log /dev/stderr' /etc/nginx/nginx.conf
COPY deploy/compose/docs.nginx.conf /etc/nginx/conf.d/docs.conf

COPY --from=build /docs/.vitepress/dist /usr/share/nginx/html
# 保留 node 与 npm，让文档站也能在容器内构建/调试（与 apps/console 同一约定）。
COPY --from=build /usr/local/bin/node /usr/local/bin/node
COPY --from=build /usr/local/lib/node_modules /usr/local/lib/node_modules
# nginx:alpine 不带 libstdc++，而 node 的 C++ ABI 依赖 libstdc++.so.6。
# apk 走 dl-cdn.alpinelinux.org，弱网下这一步会偶发 `IO ERROR`。这里只加有限重试，
# **不换镜像源**：5 次都失败仍然显式失败，只是不再把一次网络抖动当成构建失败。
RUN set -eux; \
    ok=0; \
    for attempt in 1 2 3 4 5; do \
        if apk add --no-cache libstdc++; then ok=1; break; fi; \
        echo "apk add libstdc++ 第 ${attempt} 次失败，5s 后重试" >&2; \
        sleep 5; \
    done; \
    test "${ok}" = 1; \
    ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm; \
    ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx; \
    node --version; npm --version

EXPOSE 5174
HEALTHCHECK --interval=10s --timeout=3s --retries=6 CMD wget -q -O- http://127.0.0.1:5174/ >/dev/null || exit 1
CMD ["nginx", "-g", "daemon off;"]
