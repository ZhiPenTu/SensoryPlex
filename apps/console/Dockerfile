# 多阶段构建：先在 node:20-alpine 里装依赖并执行 vite build，再把 dist/ 静态产物与
# node/npm 一起拷进 nginx:1.27-alpine。nginx 同时承担静态托管与 /v1 /auth /admin
# 反向代理；node 保留下来供开发期在容器内跑 `npm run dev`。
FROM docker.m.daocloud.io/library/node@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build
WORKDIR /console

COPY apps/console/package.json apps/console/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY apps/console ./
RUN npm run build

FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10
RUN rm -f /etc/nginx/conf.d/default.conf && \
    # 把 pid 与 error_log 重定向到 /tmp/容器内可写目录，避开 read_only 根的限制；
    # sed -i 的两种语法都被 alpine busybox sed 支持。
    sed -i 's|^pid .*|pid /tmp/nginx.pid;|' /etc/nginx/nginx.conf && \
    sed -i 's|^error_log .*|error_log /dev/stderr info;|' /etc/nginx/nginx.conf && \
    grep -q 'pid /tmp/nginx.pid;' /etc/nginx/nginx.conf && \
    grep -q 'error_log /dev/stderr' /etc/nginx/nginx.conf
COPY deploy/compose/console.nginx.conf /etc/nginx/conf.d/console.conf

COPY --from=build /console/dist /usr/share/nginx/html
# 保留 node 与 npm，让 `make console-build` / `make console-dev` 在容器内执行。
COPY --from=build /usr/local/bin/node /usr/local/bin/node
COPY --from=build /usr/local/lib/node_modules /usr/local/lib/node_modules
# nginx:alpine 默认不带 libstdc++；node 的 C++ ABI 依赖 libstdc++.so.6。
# 仅在 dev 期间需要 node，所以按需安装；apk add 与 sed 是 alpine busybox 支持的。
RUN apk add --no-cache libstdc++ && \
    ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \
    node --version && npm --version

EXPOSE 5173
HEALTHCHECK --interval=10s --timeout=3s --retries=6 CMD wget -q -O- http://127.0.0.1:5173/ >/dev/null || exit 1
CMD ["nginx", "-g", "daemon off;"]
