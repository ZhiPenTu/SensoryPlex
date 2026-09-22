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

能力契约（`RuntimeService.DescribeCapabilities`，ADR-008）：

- 平台标识为 `<os>-<arch>` 构建目标口径，例如 `macos-aarch64`、`linux-x86_64`；
  性能与抽样结论必须带该标识，不同平台的结果不得合并统计。
- `unavailable_reason` 是必填语义：`state` 为 `CAPABILITY_STATE_UNAVAILABLE` 的后端必须说明原因，
  禁止把"尚未接入"表现为"零结果成功"。
- 未知保持未知：`runtime_version`、`precisions`、`max_concurrency` 与宿主内存探测失败时，
  使用空值或 0，禁止用猜测值或默认容量填充。
- `admitted_memory_kinds` 是该平台允许的 memory kind 集合，媒体准入不得超过它；
  Apple Silicon 额外允许零拷贝 `unified_memory`，其余平台只有 `cpu_shared_memory`。
- `Health.unavailable_capabilities` 与 `DescribeCapabilities.unavailable_capabilities` 同源，
  两处不一致视为契约缺陷。

媒体源契约（`media/v1/media.proto`）：

- `MediaSourceRef` 只携带 secret 名称与内容摘要：本地文件为 `sha256:` 摘要，直播流为空；
  文件源的 `stream_id`/`source_id` 由摘要派生，同一文件重复回放保持幂等。
- `TimelineAnchor` 使用同一 stream 的 `[start_ms, end_ms)` 呈现顺序区间，`pts_ms` 等于区间起点；
  锚点不携带媒体字节。
- ffprobe 按解码顺序输出帧：含 B 帧的流会出现 PTS 非单调，Runtime 负责重排为呈现顺序并把
  重排数量记入 `out_of_order_items`；重排是显式动作，不是静默修复。
- 每个被丢弃的点都要有原因（`drop_reasons`，如 `collapsed_interval`、`duration_unknown_last_frame_interval`、
  `pts_unavailable`）；禁止把未知时长、未知 PTS 或超出时长的点夹取成合法区间。
- `ReplayReport.golden_path_verified` 默认且当前恒为 false，只有真实授权样本通过完整验收才能置真；
  未实现的能力必须出现在 `blockers` 中，且报告不得包含媒体路径。

解码数据平面契约（`ReplayReport.decoded`，仅在本次构建真的解码时才填充）：

- 全零即"没有解码发生"，不得读作成功：`arena_id` 为空时不允许出现 `tracks`、`evidence_descriptors`
  或非零 `descriptors_built`。
- `DecodedTrackStat.first_pts_ms` / `last_end_ms` 用 `-1` 表示"未观察到"，禁止用 0 冒充起点。
  `timeline_offset_ms` 记录从原始媒体时间戳减去的呈现原点（容器 edit list 场景下非零），
  使 descriptor 时间轴与 ffprobe 锚点同处呈现时间轴。
- `overlapping_samples` 统计区间重复上一段的 buffer：载荷真实，因此保留，但必须计数；
  锚点路径对同一现象按 `collapsed_interval` 丢弃并计入 `dropped_items`，两处计数不可相加。
- `descriptors_built == Σtrack.samples + audio_segments.segments`，`descriptors_validated` 必须等于
  `descriptors_built`，`descriptor_failures` 非零时必须给出 `failure_reasons`。
- `leases_issued == descriptors_built`，`leases_released == leases_issued`；未释放即泄漏，属于契约缺陷。
- 交接证据只含受控引用：`memory_kind` 必须在该平台 `admitted_memory_kinds` 内，
  `locator.handle` 等于 arena id 且绝不是宿主路径，`content_hash` 为 `sha256:` 前缀的十六进制摘要，
  lease 为只读且带过期时间。证据里不出现帧或 PCM 字节。
- `AudioSegment` 区间覆盖其携带的样本，`Σlisted.bytes == audio_segments.bytes ==` 音频轨字节
  （当 `listed == segments` 时）；跨段 ms 取整允许 1 ms 偏差，尾部不足一段时 `partial=true`。

`append_material` 是受信 timeline/storage 进程的内部入口；当前无公共写入 API。
事实写入和 outbox 在同一事务完成。outbox 分发、NATS 消费去重和重试器尚待实现，
因此不能把“已写 outbox”解释为“已发布 NATS”或“可语义检索”。
