# 服务与节点

## 进程

| 服务 | 端口 | 技术栈 | 职责 | 健康检查 |
| --- | --- | --- | --- | --- |
| `console` | `5173` | nginx + React/Vite | 托管 SPA，并把 `/v1`、`/auth`、`/admin` 反代到 `api:8091`，让浏览器保持同源 | `GET /` |
| `docs` | `5174` | nginx + VitePress | 托管静态框架使用文档站（英文在 `/`，中文在 `/zh/`）；不反代、不持凭据、无 `depends_on` | `GET /` |
| `api` | `8091` | FastAPI | 业务、管理与认证端点（`/v1`、`/auth`、`/admin`），另有 `/livez`、`/v1/health`、`/v1/capabilities` | `GET /livez` |
| `gateway` | `8090` | FastAPI | 保留的旧查询入口，作为迁移兼容层；冒烟验证与 api 同口径 | `GET /v1/health` |
| `migrate` | — | Python | 一次性执行 `tools/migrate.py`，带严格版本校验与事务锁；完成后退出 | — |
| `relay` | — | Python | 认领 outbox 记录并发布到 NATS JetStream | stdout 的 ready 行 |
| `index` | `50077` | Python | 向量写入、JetStream 常驻消费、gRPC 检索面 | `50077` 的 TCP 连接 |
| `postgres` | `25432` | PostgreSQL 16 | 元数据、事实、outbox | `pg_isready` |
| `nats` | `24222` | NATS 2.11 | JetStream 事件总线；监控端口 `28222` | — |

`docs` 是纯静态站点：它在默认栈里，但不持凭据、也不反代任何东西，因此不影响控制面的状态。

`relay` 与 `index` 属于 compose 的 `events` profile，由 `./deploy/up-events.sh` 显式启动，默认的
`./deploy/up.sh` 从不拉起它们。检索面端口**只**在 compose 网络内暴露，宿主不监听。

## Gateway 与 API

两者都是 FastAPI 进程，共享同一份 schema 与凭据。`api` 是当前面（业务、管理、认证）；`gateway` 保留为
兼容入口。两者的能力声明刻意保持同源——一个能力是否可用如果是“这次部署有没有恰好挂上仓库 `.env`”的函数，
那是 bug，不是特性。

## 节点拓扑（ADR-026）

主节点持有**唯一控制面权威**。子节点是 worker 主机，可与主节点同机，也可在同一局域网内的其它位置。

| 概念 | 行为 |
| --- | --- |
| 节点 Agent | `tools/node_agent.py` 跑在子节点上，完成注册并发送认证心跳 |
| 节点生命周期 | `candidate` → `enrolling` → `ready`，另有 `draining`、`offline`、`revoked` |
| 插件实例生命周期 | `planned` → `installing` → `ready`，另有 `degraded`、`draining`、`stopped`、`failed`、`rolled_back`、`uninstalled` |
| 安装位置 | 在 Web 控制台按节点选择，不由插件文件布局推导 |
| 部署意图 | 不可变、可审计，并有显式回滚动作 |
| 审计 | 每次状态变化都记录其操作者 |

### 预检

部署与分发都要过硬性预检，覆盖：

- **节点身份与心跳新鲜度** —— 心跳超过 60 秒即判定陈旧，以 `node_heartbeat_stale` 拒绝；
- **数据本地性** —— 接受共享内存类型的插件必须跑在持有数据面的节点上，否则 `data_locality_violation`；
- **产物形态与平台** —— 在声明的形态与平台上跑不起来的产物被拒绝
  （`container_runtime_unsupported`、`native_runtime_unsupported`、`unsupported_platform`）；
- **加速器可用性** —— 宿主没有的请求方被以 `accelerator_not_available` 拒绝；
- **资源预算** —— 声明的 CPU 与内存必须放得下（`insufficient_cpu`、`insufficient_memory`）；
- **产物 digest** —— 摘要不匹配被拒绝（`digest_mismatch`）。

### 数据本地性是硬过滤

共享内存、`dma_buf` 与硬件句柄都是 host-local，因此：

- 与某条 stream 绑定的 `data_plane_local` 任务只能指派给同机节点；
- 跨主机的 raw `BufferDescriptor` 边被拒绝两次——解析图时一次，调度时再一次；
- 远程 GPU/边缘节点只处理观测与对象引用；
- `draining` 或 `offline` 的节点在预检与任务认领两处都以 `409` 拒绝。框架绝不静默降级，也不把工作改派给
  没有被告知的节点。

### 故障转移是可审计的

当节点失联或租约过期，恢复循环原子地检测、回收任务并重新派发到备用节点——同时在持久账目中保留
**第一任与第二任**两份 assignment 记录。

::: warning 还不是生产级
跨机 mTLS 证书**轮换**与主节点高可用选主尚未实现。请把当前拓扑当作可用的单权威设计，而不是加固过的集群。
:::

## 验收

```sh
make node-check              # 节点拓扑 6 个验收场景
make orchestration-p2-check  # 分布式编排 6 个场景
```
