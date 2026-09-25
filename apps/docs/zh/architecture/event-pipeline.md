# 事件链路与检索

事件链路负责把一条已写入的事实变成可以语义检索的东西。它由四跳组成，而项目是一跳一跳分别验收的——
这样前一跳的绿灯就不会被误当成后一跳的绿灯。

```text
事实写入（素材 + 观测 + outbox，同一事务）
   → services/outbox-relay            发布到 NATS JetStream
   → services/index-worker --consume  durable 消费 → BGE 编码 → 向量库
   → index 检索面（gRPC，index:50077）
   → api / gateway                    转发查询，用 PostgreSQL 水合命中
```

## 第一跳 —— outbox → JetStream（ADR-024）

- outbox 里**只**存 `EventEnvelope`。没有帧、PCM、tensor 或密钥。
- `published_at` **只在** JetStream 确认发布之后才写。
- `Nats-Msg-Id = event_id`，stream 的 duplicate window 会吸收重发，而不是把它们重复计数。
- stream 漂移**只报不改**：relay 说清它看到了什么，绝不静默对账。
- NATS 不可达时，relay **一行都不写**。
- `make outbox-check` 把消息从 JetStream 拉回来逐字段对账。

::: tip 这一跳不等于全链路
`make outbox-check` 通过证明的是**发布**这一跳，不代表向量已经被事件驱动的消费者写进去了。
那要用下一跳的验收。
:::

## 第二跳 —— 常驻消费（ADR-025）

`sensoryplex-index serve --consume` 是常驻进程，把 JetStream 消费进向量 sink。已验证的性质：

- 换 durable 重放**不会**重复写入；
- 坏事件重投到上限后**fail-stop**，退出码 `3`——不 ack 也不记账，因此毒丸事件不会被静默吞掉；
- 启动期三类失败都是显式的：stream 缺失、durable 漂移、NATS 不可达；
- 状态行不外泄密钥。

`make consume-check` 在**宿主**执行（Milvus Lite 是进程独占的本地文件，BGE 权重也只在主机上），验证单进程内的
消费正确性：真实写侧 → 真实 relay → 真 JetStream → 常驻消费 → 真实 BGE → 真实 Milvus Lite →
**同一个进程**的 gRPC 检索面立刻能检索到。

## 第三跳 —— 向量落库与检索面（ADR-020、ADR-023）

- 索引进程写入 Milvus（本机是 **Lite 文件形态**），读回确认之后才把状态置 `ready`。
- 检索面在**编码器与向量库契约确定之后**才开端口——端口可连只说明“检索面就绪”，另有一条 ready 行才说明
  “消费真的接上了”。
- 命中会先回查 PostgreSQL（`ready` 状态、素材、`source.owner`）再返回，因此过期索引条目无法泄漏结果。
- 查询向量由 BGE 插件自己的 `Start` 编码产出，并做 **collection 级同源守卫**：模型发布版本与 collection
  建设时不一致时，整个请求被拒绝。

### 失败分类

| 情形 | 应答 |
| --- | --- |
| 能力未实现 | `501` |
| 检索面未配置 / 不可达 / 令牌不符 | `503` |
| 检索面答了但不符合契约 | `502` |
| 令牌不对（配置错误） | `503` **且 `retryable = false`** |

`retryable` 是独立标记，不要从状态码反推。

## 第四跳 —— API 与网关

`mode=semantic` 是转发到检索面的真实向量查询，不是 `501` 占位。API 只转发查询并水合事实，排序决策留在持有
向量库的索引进程里。

## 分级准入（ADR-027）

事件链路按机型档位准入。`SENSORYPLEX_EVENT_QUEUE_CAPACITY` 设定该档上限，两个批深度**必须小于等于**它：

| 变量 | 含义 |
| --- | --- |
| `SENSORYPLEX_EVENT_QUEUE_CAPACITY` | 该档的在飞上限 |
| `SENSORYPLEX_EVENT_RELAY_BATCH` | relay 每轮认领的行数 |
| `SENSORYPLEX_EVENT_CONSUME_BATCH` | 消费侧在飞未 ack 的消息数 |

越界配置会在**连任何东西之前**以 `event_inflight_exceeds_tier_cap` 拒绝启动；缺变量则是显式的 `not_injected`。
既不夹取也不降级——与媒体数据面同一口径。

## 验收阶梯

```sh
make outbox-check          # 第一跳，api 容器内
make consume-check         # 第二跳，宿主、单进程
make event-pipeline-check  # 两个常驻服务在 compose 里的端到端
make index-check           # 向量库的写后回读确认
make semantic-check        # 网关语义检索 13 个场景
```

## 已知边界

- **NATS 任务分发未接线。** 目前只有 事实 → 索引 这一个方向。
- 服务端 Milvus 拓扑**未验收**（本机 Docker Hub 不可达）；已验证形态是 Milvus Lite，且它不能被两个进程同时打开。
- RRF / 混合检索与相关性校准未实现。
