# ADR-024：outbox 分发接线与消费去重边界（发布确认、`Nats-Msg-Id` 去重、漂移不静默）

**状态：** Accepted（2026-09-24）
**上游决策：** ADR-010（跨进程数据面的安全边界）、ADR-019（运行时消费分级队列上限）、ADR-020（向量索引落库与检索闭环）、ADR-023（网关语义检索接线与索引检索面）
**相关文档：** [实现状态](../implementation-status.md)、[验证记录](../verification.md)、[待办](../TODO.md)、[契约与查询语义](../contracts/README.md)

---

## 1. 背景与问题

事务性 outbox 的规矩是"事实与事件同事务、发布与提交分开"。`0001_initial.sql` 里
`event_outbox` 与 `consumed_event` 两张表从第一天就在，事实侧也一直在写：

| 事实 | 位置 |
| --- | --- |
| `append_material` 在**同一事务**里写 material 事实与一条 `material.upserted` 事件（`event_id = material:<id>:<rev>`） | `services/api/.../infrastructure/materials.py` |
| `event_outbox(event_id PK, event_type, contract_bytes, published_at, attempt, created_at)` | `db/migrations/0001_initial.sql` |
| `consumed_event(event_id, consumer_name, consumed_at)`，主键 `(event_id, consumer_name)` | 同上 |
| 全仓此前**没有**任何生产者、消费者或 relay：`nats` 只出现在 compose 与 Makefile 里 | `rg nats` |

于是有一句必须钉死的话：**"已写 outbox"不等于"已发布"**，更不等于"已被消费"。
`published_at` 是这一跳唯一的账；它一旦被写错，后面的对账就永远不可能正确。

同时要承认一个上游缺口：**observation 根本没有事件**（`Observation` proto 里没有 `event_id`），
而 BGE 的输入模态是上游 OCR 观测里的 `ocr_blocks`
（`plugins/python/processors/embed-bge-onnx/.../text.py` 的 `INPUT_MODALITY`）。也就是说，
即便 relay 通了，`material.upserted` 也**携带不了可编码的文本**——"消费到 sink"不是接线问题，
而是契约缺口。本切片只做**发布这一跳**，并在 §9 把这条边界写死。

## 2. 决策一：relay 是独立服务，且只做发布

新增 `services/outbox-relay`（`sensoryplex-relay`），`python -m sensoryplex_relay.cli` 既是
常驻进程也是短命进程（`--once`）：

- **不塞进 index-worker**：链路上游是"事件发布"，下游才是 sink；而 index-worker 依赖
  `pymilvus` / `milvus-lite`，relay 不该背这堆依赖，也不该因此只能在 macOS 上跑；
- **不做消费循环**：理由见 §1 的契约缺口。relay 的职责边界就是 `outbox → JetStream`；
- 依赖只有 `psycopg` 与 `nats-py`（`nats-py` 由 relay 自己声明，不进 index-worker 的闭包）；
- 镜像内验证：api 镜像里 `uv pip install ./services/outbox-relay`（**不加** `|| true`——
  装不上就让镜像构建失败，而不是让容器里的测试事后红成一片，同 index-worker 的口径）。

## 3. 决策二：`published_at` 只在确认之后写；认领不跨网络持锁

四步写死：

1. **只在 JetStream 确认收到之后**才写 `published_at`。发布失败时这一列必须保持 `NULL`——
   否则"压根没发出去"会被后来的进程读成"已经发过"，事件永久消失（验收场景 5 就钉这一条）；
2. 认领用 `FOR UPDATE SKIP LOCKED` **且立刻提交**：认领事务不跨网络 I/O 持锁。代价是同一批
   可能被两个 relay 实例同时看到（可能重复发布一次），而这个代价由 §4 吸收；反过来
   "持锁发布"会让一个慢的 NATS 拖住行锁，那才是更贵的错误；
3. `mark_published` 带 `published_at IS NULL` 条件，是真正的并发保护：
   **同一行只可能被记成"已发布"一次**；
4. 失败只加 `attempt`，不碰 `published_at`（`record_failure`）。发不出去就留在待发状态并计数，
   **不丢**：`pending` / `max_attempt` / `oldest_pending_age_s` 每轮都进状态行。

## 4. 决策三：`Nats-Msg-Id = event_id`，去重交给 JetStream

发布带 `Nats-Msg-Id = <outbox 的 event_id>`。于是"发布确认了、提交前崩了"这类重发会被
JetStream 的 duplicate window 吸收，relay **不需要**去猜"到底发没发"。验收场景 6 用真实
JetStream 重放同一 `Nats-Msg-Id`，断言 `PubAck.duplicate is True` 且流里的消息数不变。

事件类型字符串保持原样（`material.upserted`），subject 规则是
`<prefix>.<event_type>`，`event_type` 用正则**显式校验**（拒绝 `*`、`>`、空格、大写、
超过 4 层）：subject 里出现通配符不是"换了个名字"，而是把一条事件投成了通配订阅。

## 5. 决策四：stream 由 relay 建，漂移只报不改

relay 启动时 `ensure_stream`：不存在就用**有界**契约建（`sensoryplex-events`，
subjects `["sensoryplex.events.>"]`，FILE 存储，`max_msgs=200000`、`max_bytes=256MiB`、
`max_age=7d`、`duplicate_window=2h`）；已存在则**只校验**，逐字段比较，漂移即
`event_stream_contract_mismatch` **报错退出**。

刻意**不自动改**保留策略：把 `max_age` 或 `duplicate_window` 静默改小会丢掉还没被消费的事件，
或者让重发变成真重复——两者都属于"把配置错误伪装成正常"，与 §3 的边界同源。

## 6. 决策五：消费去重是 `(event_id, consumer_name)`，且**先干活后记账**

`consumed_event` 的主键就是幂等键；但它的 `consumed_at` 是 `NOT NULL DEFAULT now()`，
**没有**"在飞（in-flight）"这一态。因此 `sensoryplex_index_worker.records` 提供的是：

- `is_consumed(conn, event_id, consumer_name)`：本 consumer 是否已处理完（用于跳过重复工作）；
- `record_consumed(...) -> bool`：干完活之后记账，`True` = 首次完成，`False` = 重复完成（幂等，**不是**错误）；
- `consumed_state(...)`：`consumed_at` 可观察。

刻意**不**提供"先认领、再干活、最后收尾"的两相接口：那样会在"插了行、还没干完"的窗口里崩溃，
而重投时那行会让事件被永久跳过——把 at-least-once 悄悄变成 at-most-once，事件丢了也没人知道。
正确顺序是反的：**先做幂等工作**（`embedding_id` 是确定性的，`begin_pending` 撞 PK 会校验身份，
重跑得到同一份向量），**干完了再记账**。于是崩溃最多让工作重做一遍，绝不会让事件消失。

## 7. 验收（`make outbox-check`）

`tools/verify_outbox_relay.py` **在 api 容器内**执行（与 `index-check` / `semantic-check` 的理由不同：
这里只要真实 PostgreSQL 与真实 JetStream，两者都在 compose 里，Milvus Lite / HF 权重 / CoreML
一个都不用）。它自建**独立** stream 与 subject 前缀（`sensoryplex-events-acceptance`），
用完删掉，不碰开发用的 `sensoryplex-events`。场景：

| # | 场景 | 断言要点 |
| --- | --- | --- |
| 1 | 目标描述 | `--describe` 只回 `host:port/db`，不含凭据 |
| 2 | 真实建 stream | 契约逐字段读回核对：subjects / FILE / `max_age` / `duplicate_window` / 上下界 |
| 3 | 真实发布并对账 | 拉回来的 subject、`Nats-Msg-Id`、载荷**逐字节**等于 outbox 行 |
| 4 | 确认之后才记账 | `published_at` 非空且 `attempt=1`；`pending` 归零 |
| 5 | 发不出去就不写 | 契约缺陷 → `failed=1` + `event_envelope_mismatch`，行仍未发布、`attempt=1`；NATS 不可达 → exit 1 + `nats_unreachable`，**一行状态都不打**、账目不变 |
| 6 | 重放去重 | 同一 `Nats-Msg-Id` 重发 → `duplicate=True` 且流内消息数不变 |
| 7 | 漂移不静默 | 手工造一个 `max_age` 漂移的 stream → `event_stream_contract_mismatch`，且**读回后仍是原值**（没被修好）；删掉后 relay 能按契约重建 |
| 8 | sink 去重 | 真 PostgreSQL 上 `record_consumed` 第一次 `True`、第二次 `False`，`consumed_at` 可观察，且只写了 1 行 |
| 9 | 不外泄 | 状态行无 DSN / schema 名 / 主机路径 / 载荷，字段集合与契约一致 |

NATS 或 PostgreSQL 不可达时**异常退出，不跳过**；`make outbox-run` 用同一入口起常驻形态。

同切片的其余证据：`tests/contracts/test_outbox_relay_contract.py`（subject 与 event_type 准入、
`RelayOptions` 逐项边界、envelope 与行一致性、`stream_contract_diff` 的秒口径、启动连接**有界**、
状态行形状与 `describe_target` 抹凭据）与 `tests/integration/test_outbox_relay.py`
（真实 PostgreSQL + 真实迁移：确认后才写、失败不写 `published_at`、`attempt` 计数、
一批里一条坏事件不拖累其余、`mark_published` 只翻一次、`(event_id, consumer_name)` 作用域）。

## 8. 顺带修掉/确认的缺陷

三个都是**真机跑出来**的，记在案以免下次重犯：

1. **`max_age` / `duplicate_window` 的单位是秒，不是纳秒**。nats-py 的 `StreamConfig` 声明
   `max_age: Optional[float] = None  # in seconds`，由库的 `as_dict()` 换算成服务端的纳秒
   `time.Duration`。按"proto 是纳秒"的直觉写 `7d * 1e9` 会被**再乘一次 1e9**，服务端直接
   `invalid JSON: cannot unmarshal number ... into Go struct field StreamConfigRequest.StreamConfig.max_age`。
2. **`nats.connect(max_reconnect_attempts=-1)` 连"首次连接"都是无限重试**
   （`_select_next_server` 只在 `max_reconnect_attempts > 0` 时才放弃服务器）。于是"地址填错 /
   NATS 没起"在只靠库参数的写法里表现为**进程静默挂着、一行状态都不打**——最难排查的一类失败。
   现在启动连接有上限（`--connect-timeout-s`，默认 10s）并显式报 `nats_unreachable`；
   连上之后仍交给库做无限重连（稳态断线不该让常驻进程退出）。
3. **`duplicate_window > max_age` 的 stream 建不出来**：JetStream 直接
   `duplicates window can not be larger then max age`。验收场景 7 最初就踩在这上面，
   漂移只改 `max_age` 且仍大于 `duplicate_window`。

另外确认了一条**既有 schema 事实**：`consumed_event.consumed_at` 是 `NOT NULL DEFAULT now()`，
所以"两相认领"在没有迁移的情况下根本表达不出来（见 §6）。本切片**不加**迁移——
加一个可空列只为支持"认领态"，会把 §6 那个危险方向重新打开。

## 9. 后果与仍未验证范围

已落地：`services/outbox-relay`（`relay.py` / `cli.py`）、`make outbox-check` / `make outbox-run`、
`tools/verify_outbox_relay.py`、`records.is_consumed` / `record_consumed` / `consumed_state`、
契约与集成测试、api/gateway 镜像与 uv workspace 接线。

**仍未验证（不得声称完成）：**

- **NATS → sink 的常驻消费仍未接线**：本轮做的是"发布这一跳"。而且它**不只是接线问题**——
  唯一存在的事件 `material.upserted` 不携带可编码文本，BGE 需要 `ocr_blocks`，而 observation
  连 `event_id` 都没有。补齐 observation 契约（谁为哪种观测发事件、事件里带什么引用）是**后续切片**；
  在此之前 `outbox-check` 通过**不等于**"向量已被事件驱动地写进去了"；
- **relay 尚未进 compose**：常驻形态目前靠 `make outbox-run` 显式起（JetStream 里没有消费者，
  先常驻一个只发不收的进程没有意义，也会让开发栈默认产生无人消费的事件）；
- **没有 dead-letter 与重试上限**：`attempt` 只被计数，超过阈值的处理方式未定义（TODO）；
- **NATS 无鉴权、无 TLS**：本切片沿用 compose 里回环暴露的本地 NATS（ADR-010 的受控引用边界不变）；
- **未做端到端背压**（ADR-019 的队列上限没有接到 relay）；`--batch` 是固定上限；
- **macOS/Apple Silicon 未在这里单独验收**：本目标在容器内跑，NATS/PostgreSQL 与主机架构无关；
- `golden_path_verified` 仍恒为 false。
