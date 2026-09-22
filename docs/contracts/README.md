# V1 契约（0.1.0 开发预览）

唯一跨语言消息源是 `proto/`。Rust 由 `build.rs` 生成 tonic/prost 代码；Python 由
`make proto` 生成并提交到 SDK，禁止手工修改生成文件。字段不得重编号，删除时必须
reserved，破坏语义的修改进入新的协议 major。当前为开发预览，尚未发布稳定 SDK。

- 时间范围：同一 stream 内的 `[start_ms, end_ms)`，毫秒偏移，终点必须大于起点。
- confidence 为 optional；缺失时必须说明原因，不能用 0 或 1 代替未知值。
- plugin artifact 与 model artifact 使用两个不同的 SHA-256 字段。
- 材料 revision 从 1 连续递增；同版本同内容可重放，同版本不同内容拒绝。
- 查询旧 revision 时动态标记 superseded，原始快照保持不可变。
- 原始媒体只以受控引用传递。事件不携带帧、PCM、tensor；outbox 仅保存 EventEnvelope。
- REST 使用 Protobuf JSON 映射，字段采用 snake_case；int64 返回字符串，optional
  缺失表示未知；查询时间条件为区间重叠，标签是 AND，modalities 是 OR。
- `keyword` 为 PostgreSQL 字面子串检索，无相关性排名；`semantic` 未接入时返回 501。
- 单一部署 API token 映射到 `SENSORYPLEX_PRINCIPAL`，数据库按 source.owner 二次过滤。
  外部身份提供方、多用户 token、管理角色与审计日志不在当前底座实现范围内。
- 默认不注册模型插件，不导入测试 observation，不把测试 fixture 作为媒体验收。

`append_material` 是受信 timeline/storage 进程的内部入口；当前无公共写入 API。
事实写入和 outbox 在同一事务完成。outbox 分发、NATS 消费去重和重试器尚待实现，
因此不能把“已写 outbox”解释为“已发布 NATS”或“可语义检索”。
