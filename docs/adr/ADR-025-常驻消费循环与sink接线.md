# ADR-025：常驻消费循环与 sink 接线（事件驱动写入、同进程检索、坏事件 fail-stop）

**状态：** Accepted（2026-09-24）
**上游决策：** ADR-010（跨进程数据面的安全边界）、ADR-019（运行时消费分级队列上限）、ADR-020（向量索引落库与检索闭环）、ADR-023（网关语义检索接线与索引检索面）、ADR-024（outbox 分发接线与消费去重边界）
**相关文档：** [实现状态](../implementation-status.md)、[验证记录](../verification.md)、[待办](../TODO.md)、[契约与查询语义](../contracts/README.md)

---

## 1. 背景与问题

ADR-024 把 outbox 的**发布这一跳**接上了（`outbox → NATS JetStream`，`Nats-Msg-Id = event_id`，
确认之后才写 `published_at`），sink 侧的消费去重原语（`records.is_consumed` /
`record_consumed`）也备好了，但**没有任何常驻消费者在用它**。于是那句必须钉死的话仍然是
半截的："已写 outbox"≠"已发布"，而"已发布"也还**不等于**"已被消费成向量"。

ADR-024 §9 把这条边界记成"不只是接线问题"，理由是：上游 observation 没有 `event_id`，
唯一存在的事件 `material.upserted` 携带不了 BGE 需要的 `ocr_blocks` 文本。本切片回答的就是
这件事，而答案是**不去改事件契约**（见 §3）。同时有两条既有约束决定了形态：

- **Milvus Lite 的数据目录是进程级 flock 独占的**（ADR-020 §7）：全机只能有一个进程持有向量库，
  所以"谁来消费"不是自由选择；
- **重建状态机是不可能的**：`consumed_event.consumed_at` 是 `NOT NULL DEFAULT now()`
  （ADR-024 §6），所以消费侧只有"完成"这一态可表达。

因此本切片要回答三个问题：**消费跑在哪个进程上？事件里的受控引用怎么变成可编码文本？
失败怎么算？**

## 2. 决策一：消费与检索面同进程（`serve --consume`）

`sensoryplex-index serve` 新增 `--consume`：**常驻消费挂在持有向量库的那个进程上**
（`ConsumerRunner` 起一个线程跑 `asyncio.run(consume_loop)`）。理由只有一条但是硬理由：

- 拆成两个进程**不是**"更解耦"，而是二选一——要么消费写进的向量对检索面不可见（检索面拿着
  自己的 Milvus 连接看不到别人的写入，且本地文件形态下根本打不开同一个目录），要么第二个进程
  直接 `vector_store_locked`。

配套写死的三条：

1. **有界启动**：`serve` 在 `connect_timeout_s * 3` 内等"消费侧真的接上了"（NATS 连接 +
   stream/durable 契约全部对完）；超时或失败就 emit `{command: "consume", error_code, error_detail}`
   并以退出码 1 结束。**绝不带着一个没接上的消费侧对外服务**——半启动的常驻进程是最难排查的失败形态；
2. **加不加 `--consume` 是两个事实**：就绪行里的 `consume` 要么是 `null`（只做检索面），
   要么把 subject / stream / durable / batch / `max_deliver` / `idle_exit_cycles` 原样写出来；
3. **fatal = 进程 fatal**：消费侧以退出码 3 结束时（§5）`on_fatal` 停掉检索面，进程按 3 退出，
   "为什么停了"要能被进程边界之外看见。

服务端 Milvus 形态下才有条件把两者拆开——那是后续拓扑，不是本切片的猜测。

## 3. 决策二：事件只是通知，事实在库里

不改事件契约，也不新增 observation 事件：`material.upserted.payload_ref = material:<id>:<rev>`
是**受控引用**（ADR-010：控制面不传载荷），可编码文本仍在 `observation.payload_jsonb` 里，
消费侧按引用**回查事实**。四条写死的语义：

1. **引用形状显式校验**：先摘固定前缀 `material:`，再从右往左切一刀（素材 id 允许含 `:`），
   revision 必须是正整数。形状不对 → `invalid_payload_ref`。把 `material:m1:x` 当成
   "revision 是 x" 去查库，失败会以"查不到素材"的形式出现——把一件契约缺陷伪装成一次数据缺失；
2. **回查不到不是"没数据"，是写侧缺陷**：事实与事件同事务（ADR-024 §1），查不到只能是缺陷 →
   `event_missing_facts`，**在任何写入之前**失败；
3. **观测顺序稳定**：`load_observations` 按 `observation_id` 排序。同一份事实两次消费必须得到
   同一批 `embedding_id` 与同一条文本，否则"重跑得到同一份向量"这条幂等性就没有依据；
4. **payload 的读法只有插件那一份实现**：把 `payload_jsonb` 用 `ParseDict` 还原成**真的
   `google.protobuf.Struct`**，再交给 `bge_text.collect_text`（块在不在、块有没有 `text`、
   数量与字符数上限）。自己写一层 dict 适配会让"插件进程编码"与"消费侧编码"各自漂移，
   而且漂移只在部分素材上显现。

## 4. 决策三：先干活后记账

`consumed_event` 只有"已完成"这一态，所以顺序是**编码 → 落库 → 确认写入 → `record_consumed`
→ commit → ack**：

- 崩在中间只会让工作重做一遍——`embedding_id` 是确定性的（`observation_id` + 素材 + revision），
  重做得到同一份向量，`begin_pending` 撞主键会校验身份；
- **sink 失败时先 `commit` 再抛**：`index_embedding` 已经留下 `failed` 行（带原因码），
  不提交就等于"拒绝了但库里查不到"，那是静默丢弃（ADR-020 §2 同一条规则）；
- **重复投递提前返回**：`is_consumed` 命中就直接 ack（`duplicate=True`）。不 ack 的话，
  队列里会永远留着一条已完成的事件；
- **新 durable = 新消费视角**：去重键是 `(event_id, consumer_name)`，换一个 durable 名等于换一个
  视角，两套视角的账互不顶掉——这是 JetStream 的语义，不是本层新加的规则。

## 5. 决策四：少一条向量就不算消费完成（fail-stop）

本事件应产出的向量没有全部写出来，事件就**不记账、不 ack**，交给 JetStream 重投：

- 失败走 **延迟 nak**（`--nak-delay-s`，默认 5s）：不加延迟的 nak 会把"坏事件 + 常驻进程"
  变成忙循环；
- 重投次数到达 `--max-deliver`（默认 5，上限 20）→ `ConsumeExit(code=3, fatal_code="event_retry_exhausted")`
  → 进程按 **3** 退出，事件**留在队列里**等人处理。

理由：把"丢了一条向量"写成"事件已消费"是静默变质；而 at-least-once 的重投上限到了就必须有人来看。

> **修订（2026-09-25，实测）：** 上面那句"事件**留在队列里**等人处理"**是错的**，本节按实测订正。
> `max_deliver` 用尽后 JetStream 把这条消息**终止**（terminated）：`num_pending=0`、
> `num_ack_pending=0`，而 `ack_floor` 停在第一条没被 ack 的序号上。实测凭据（真链路）：
> 4 条事件重投 5 次后被终止后**再也没有投递过**（`delivered.stream_seq` 不再前进），它们对应的
> `material_unit` 因此**永久**拿不到向量，重启消费侧也换不回来。这正是"没有 dead-letter"的代价，
> 也是下面这条修订要解决的问题的一部分。
>
> **修订（2026-09-25，`应产出` 的口径）：** fail-stop 只适用于**契约/事实缺陷**，
> 不能由一条**合法的真实帧**触发。本事件"应产出"的向量由**文本契约**决定：一条观测给不出可编码
> 文本，就**不产出**向量、按原因计数后继续，而不是让整条素材陪着它耗尽重投：
>
> | 消费侧原因类 | 触发（插件原因码，ADR-017 §4） | 语义 |
> | --- | --- | --- |
> | `input_text_empty` → `skipped_empty_text` | `input_text_empty`（`blocks=[]`） | 模型这一帧**没找到文字**（黑场、转场、纯画面帧），是真实媒体里的常态 |
> | `input_text_over_bound` → `skipped_over_bound` | `input_text_exceeds_bound` / `input_block_count_exceeds_bound` | 这一帧的文字**取值**越过插件上限（`MAX_TOTAL_CHARS` 4096 / `MAX_TEXTS` 256） |
>
> 形状错误（缺 `blocks`、块里没有 `text`、`text` 不是字符串）**仍然**抛
> `observation_text_rejected`、仍然不 ack、仍然 fail-stop——"真实媒体的取值"与"契约缺陷"是两件事。
> 分类不靠字面量：契约测试读插件**实际抛出的** `reason_code` 再映射到计数字段，插件改了原因码就会红。
>
> 触发这次修订的真实故障（`video/samples/screencast-watchlist.480p.vp9.webm`，Wikipedia 监视列表
> 密集屏录）：一帧 `ocr_blocks` 有 **109 块 / 4158 字符**，越过 4096 → 插件照 ADR-017 §4 失败 →
> 旧消费侧把它当坏事件 → 重投耗尽 → `consume.fatal` / `event_retry_exhausted` / `exit_code: 3` →
> **常驻 index 容器按 3 退出重启**（`RestartCount=1`），那 4 条事件被终止、素材永久缺向量。
> 一条真实帧让整条链路 fail-stop，就是"取值被当成缺陷"最贵的形态。

## 6. 决策五：消费端绝不建 stream，但建自己的 durable；漂移一律只报不改

- `require_stream`（消费端）：流必须**已存在且**符合契约，缺失即 `event_stream_missing`，
  **绝不自动创建**——消费端凭空建一个流会把"发布端还没部署"伪装成"链路已经通了"；
- `ensure_consumer`（消费端自己的 durable）：不存在就按契约建，已存在则**只校验**，漂移即
  `event_consumer_contract_mismatch`。durable 描述"我怎么消费"，发布端不认识它，所以这里允许创建；
- 契约六项逐字比：`durable_name` / `filter_subject` / `ack_policy == explicit` / `ack_wait` /
  `max_deliver` / `max_ack_pending == batch`。**`max_deliver` 一起比是刻意的**：它决定坏事件会不会
  被无限重投——被改大就把 fail-stop 变成潜在死循环，被改小会让重投还没试完就停，两者都是静默变质；
- 枚举取值与字符串取值统一成小写再比（nats-py 里 `AckPolicy` 是枚举，服务端读回来可能是串）。
  形状不同就判漂移是**假警报**，而假警报会让人开始忽略真警报——漂移检测最贵的失败形态。

## 7. 决策六：精确 subject、串行处理、有界在飞

- **精确订阅 `sensoryplex.events.material.upserted`**，不用 `>` 通配：通配订阅会让另一种事件被当成
  "待消费的素材"反复重投；
- 不支持的 `event_type` → `unsupported_event_type` 记账跳过并 ack（不重投到天荒地老）；
- 每批**串行**处理，在飞推理恒为 1；`max_ack_pending = batch`（有界）。
  这是 ADR-019 / ADR-021"并发必须有上限"口径里上限取 1 的那一端。

## 8. 决策七：状态行是可观测契约，不是日志

`consume.status` 的字段集合固定为 16 项：`event` / `cycle` / `stream` / `subject` / `durable` /
`received` / `consumed` / `skipped` / `failed` / `embedded_total` / `consumed_total` /
`duplicate_total` / `skipped_total` / `failed_total` / `error_code` / `error_detail`。

> **修订（[ADR-027](ADR-027-事件链路分级背压与容器化常驻.md)，2026-09-24）：** 字段集合现在是 **20 项**
> ——分级背压准入接上后追加 `inflight_state` / `inflight_declared` / `inflight_capacity` /
> `resident_tier`（取值只有状态字符串、整数与档位名，`error_detail` 的口径不变）。
>
> **修订（2026-09-25）：** 字段集合现在是 **24 项**——§5 的"按原因计数跳过"接上后追加
> `skipped_empty_text` / `skipped_over_bound`（本轮）与 `skipped_empty_text_total` /
> `skipped_over_bound_total`（累计）。**按原因分开而不是一个总数**是刻意的：空文本与越界要解决的
> 问题不同（前者是"这一帧没有字"，后者是"上限对密集屏录偏紧"），混成一个数字就查不出该动哪一头。
> 字段名在这里逐项写死、不做动态拼名：新增一类"不产出向量的取值"时，只改
> `TEXT_SKIP_FIELDS` 一处并同步这一节与状态行，不会出现"某几个字段悄悄消失"。

- **只有计数、标识与稳定原因码**：不放载荷、文本、向量、令牌、DSN 或主机路径；`error_detail`
  只放稳定原因码与异常**类名**（异常文本可能带地址、DSN 或载荷片段）；
- `--consume-status-out` 是**最新快照**（临时文件 + `rename` 原子替换），stdout 才是逐行流。
  要历史曲线就把 stdout 接走。

## 9. 验收

`make consume-check`（`tools/verify_index_consume.py`，**主机执行**：Milvus Lite 的目录是进程独占的
本地文件、BGE 权重也只在 macOS 主机上）用真实写侧、真实 relay、真实 JetStream、真实 BGE 权重、
真实 Milvus Lite、真实 gRPC 检索面跑完：

| # | 场景 | 钉住的事实 |
| --- | --- | --- |
| 1 | 事件驱动写入 + 同进程检索 | 真实 `append_material` 落 outbox → relay 发到 JetStream → `serve --consume` 消费 → `embedding_record` ready（带 `vector_ref` / `indexed_at`）；查询文本经真实 BGE 编码后**在同一个进程**检索到这条新向量并排第一；非 owner 命中被回查丢弃并计数；状态行与就绪行不外泄 |
| 2 | 重放不重复 | 换一个 durable（新消费视角）重投同一批事件：向量行数不变，记账按视角各自留痕（N 事件 × 2 视角） |
| 3 | 锁与优雅停止 | 消费进程持锁期间第二个进程 `vector_store_locked`；SIGTERM 后退出码 0 且目录可用（`inspect` 行数不变） |
| 4 | 坏事件 fail-stop | 事实回查不到的事件重投到上限 → 退出码 3 + `event_retry_exhausted` + `event_missing_facts`；不 ack（`num_ack_pending >= 1`）、不记账、不写向量行 |
| 5 | 启动期显式失败 | stream 缺失 → `event_stream_missing` 且**流没有被建出来**；durable 漂移 → `event_consumer_contract_mismatch`；NATS 不可达 → `nats_unreachable`；三者都以退出码 1 失败且不泄露 DSN/路径 |

同切片的其余证据：`tests/contracts/test_index_consumer_contract.py`（56 项：subject 精确绑定、
17 条参数越界、`parse_payload_ref` 正反例、durable 契约六种漂移、枚举归一、`ensure_consumer`
三条路径、payload→插件文本契约往返、不可编码 payload 必须拒绝、真实媒体取值按原因计数跳过
（分类取自插件**实际抛出的**原因码，不写死常量）、状态行字段集合与不外泄）与
`tests/integration/test_index_consumer.py`（真 PostgreSQL + 真迁移，含真 JetStream 的投递→ack、
坏事件 fail-stop、stream 缺失必须报错；向量库与编码器在这一层是替身，**不算**向量真的写进 Milvus）。

## 10. 顺带修掉/确认的缺陷

1. **`failed_total` 从不累加**：状态行只报本轮计数，累计值恒为 0 → 常驻进程永远显示
   "从没失败过"，而实际已经失败过若干轮。契约测试只钉字段集合、集成测试钉的是本轮计数，
   累计口径没人钉。现在循环里累加，集成测试补了断言；
2. **验收脚本自己会撒谎**（真机跑出来，记在案）：protobuf 的 JSON 视图会**省略 0 与空 repeated**，
   于是"没这个键"被读成"丢了 `None` 条命中"、非 owner 的空命中被读成"返回了命中"。
   读 protobuf JSON 时必须把缺键当默认值；
3. **状态行是先写、进程后退出**：验收脚本看到失败原因就立刻读退出码，读到的还是 `None`。
   判码前必须先等进程真的退出；
4. **"应产出"当初没有定义，于是一条真实帧把整条链路打停**（2026-09-25 修）：见 §5 的修订——
   越界文本原先是 `observation_text_rejected`（事件级失败），现在按原因计数跳过。同一批修掉的还有
   `failed_total` 式的观测盲区：跳过计数必须**分原因**且**有累计值**，否则"这一轮刚好没有跳过的观测"
   会让运维看到的永远是 0；
5. **`distance` 不是"距离"**（2026-09-25 修文档）：`proto/index/v1/index.proto` 与本 ADR 之外的
   检索文档把它写成"COSINE 距离（越小越近）"，而它是 COSINE **相似度**（越大越近、按降序返回）——
   取一条观测的整段文本当查询，它自己的值就是 `1.0`。行为一直是对的（`tools/verify_index.py` 的
   "self query == 1.0" 断言钉着），错的是注释与文档，代价是下游验收写错了名次断言（见
   [ADR-023](ADR-023-网关语义检索接线与索引检索面.md) §6 的订正）。

## 11. 后果与仍未验证范围

已落地：`services/index-worker` 的 `consumer.py`（消费循环 / 契约校验 / 状态行 / `ConsumerRunner`）、
`services/outbox-relay` 的 `contract.py`（发布端与消费端**共用**的事件契约，含 `require_stream` /
`ensure_consumer` 的区别）、`cli serve --consume` 的 12 个参数、
`records.load_material` / `load_observations` / `ensure_model_release`、`make consume-check`、
`tools/verify_index_consume.py`、契约与集成测试。本切片**不加**数据库迁移。

**仍未验证（不得声称完成）：**

- **服务端 Milvus 形态下的多进程拓扑**（消费与检索拆成两个进程）未验收——本机 Docker Hub 不可达，
  Milvus standalone 起不来（ADR-020 §6/§7）；
- **没有 dead-letter**：重投到上限就 fail-stop，事件被 JetStream **终止**（不是"留在队列里"，
  见 §5 的订正）→ 素材永久缺向量。元数据库不可用（`psycopg.Error`）走的也是 nak，同样会撞到
  `max_deliver`；
- ~~**没有按原因分流**~~ **部分收口（2026-09-25）**：文本类的"不产出向量"已按原因计数跳过
  （§5 的表）。**仍未做**的是"同一事件里只有**部分**观测给不出向量"之外的场景：形状错误、
  事实缺失、向量库故障仍然只有 fail-stop 一条路；
- **`ack_wait` 到期后的自动重投路径未单独验收**（验收走的是 nak 路径）；
- ~~**消费侧与 relay 都还没有进 compose**：常驻形态靠 `make outbox-run` / `serve --consume` 显式起；~~
  **已由 [ADR-027](ADR-027-事件链路分级背压与容器化常驻.md) 收口**（`events` profile 里的 `relay` /
  `index` 两个服务 + `make event-pipeline-check` 的容器内闭环验收）；
- **只支持 `material.upserted` 一种事件**；observation 级的事件契约仍未定义（本切片证明它不必要，
  但"未来要不要"是另一个问题）；
- ~~**吞吐与背压未测**：单批串行、在飞恒为 1，ADR-019 的队列上限没有接到这一层，也没有背压指标；~~
  **部分收口（[ADR-027](ADR-027-事件链路分级背压与容器化常驻.md)）**：ADR-019 的队列上限已作为
  **准入**接到两层（`--batch` / `--consume-batch` 越本档上限即拒绝启动，状态行带
  `inflight_state` / `inflight_declared` / `inflight_capacity` / `resident_tier`）。
  **仍未测**：吞吐与延迟曲线（本切片接的是准入，不是吞吐达标）；
- ~~**真实媒体端到端未联调**：真实视频 → Runtime/Timeline → metadata writer 的 outbox 那条路仍未验收，
  所以"真实素材被抽帧后自动产出向量并检索到"还没有证据；~~
  **已收口**：写侧那一半由 [ADR-028](ADR-028-Runtime到Timeline接线与授权追加.md) 的
  `make timeline-check` 验收；"融合出的素材经**常驻** relay/index 变成向量并被 api 语义检索命中"
  由 `make timeline-resident-check`（`tools/verify_timeline_semantic.py`）验收，实测见
  [验证记录](../verification.md)。**两个授权样本都过**，其中 `officehours-panel.480p.vp9.webm`
  的 6 条 `ocr_blocks` 观测里真的出现 1 条空文本 → 按 `skipped_empty_text` 计数跳过、素材照旧拿到
  其余向量、两个常驻容器 `RestartCount` 为 0（即 §5 的修法在真实媒体上被走到，而不是只在契约测试里）。
  **仍未验**：revision 前进、SRT 实时源、查询回看、向量 GC；
- `golden_path_verified` 仍恒为 false。
