# 模型总览

数据模型刻意做得很小。框架存储的东西只有四种：**stream**、不可变的**素材 revision**、针对某个 stream
某段时间范围的**观测**，以及把观测与产出者绑起来的**血缘**。

## 对象

| 对象 | 含义 | 可变性 |
| --- | --- | --- |
| `MediaSourceRef` | 媒体来源：secret 名称加内容摘要。本地文件带 `sha256:` 摘要，直播流摘要为空。 | 不可变 |
| `stream_id` / `source_id` | 由文件摘要派生，因此同一文件重复回放保持幂等 | 派生 |
| `TimestampRange` | 同一 stream 上的 `[start_ms, end_ms)` | 值 |
| `TimelineAnchor` | 同一 stream 上的呈现顺序区间；`pts_ms` 等于区间起点；不携带媒体字节 | 值 |
| `MaterialUnit` + `revision` | 某条 stream 上可寻址的一段及其事实 | 只追加；revision 永不被改写 |
| `Observation` | 针对某段时间范围的事实（文字块、转写、描述、向量） | 只追加 |
| `source.owner` | 拥有该来源的主体；查询按它过滤 | 不可变 |
| `model_release_id` / `processor_release_id` | 产出该观测的模型或处理器版本，含配置 hash | 不可变 |
| `BufferDescriptor` + lease | 数据面内部使用的有界共享内存引用 | 受 TTL 约束 |

## Revision 规则

- Revision 从 `1` 开始连续递增。
- **同版本同内容可重放。** 再写一次是幂等的，不是错误。
- **同版本不同内容被拒绝。** 静默覆盖 revision 会摧毁审计轨迹，所以这里必须是显式错误。
- 查询旧 revision 时**在查询期动态**标记 `superseded`；已存储的快照本身永不被改写。

## 血缘规则

只有来源与产出身份都校验通过，观测才会被接受：

- 被引用的素材/stream 必须存在，且属于调用方主体的 `source.owner`；
- 产出它的模型/处理器发布版本必须已知，且配置 hash 已折进发布身份——改了 prompt、语言或采样阈值，
  就会得到不同的身份；
- 返回向量命中之前，查询结果会回查 PostgreSQL 里的 `ready` 状态、素材与 `source.owner`，
  过期的索引条目无法泄漏结果。

## 事件携带引用，不携带媒体

事务性 outbox 里只存 `EventEnvelope`。帧、PCM 与 tensor 绝不进入事件、控制消息或日志。正因如此，
事件链路才能安全地重放与去重：载荷是数据，不是字节。

## 置信度要么已知，要么显式未知

`confidence` 是 optional。缺失时必须说明缺失原因。框架不会用 `0` 或 `1` 代替“没有意见”——那会把
未知变成一句假话。

## 继续阅读

- [时间轴语义](/zh/concepts/timeline) —— 时间规则的细节。
- [能力模型](/zh/concepts/capability-model) —— 框架如何表达“我做不了这件事”。
- [契约与代码生成](/zh/architecture/contracts) —— 字段级规则。
