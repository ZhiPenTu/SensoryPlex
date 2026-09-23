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
- `arena_peak_bytes` 是**已提交容量**的高水位，不是并发存活字节：arena 为 bump 分配 + 空闲链，
  当一个请求放不进任何空闲区时才会新增提交。交替出现"单帧"与"整段音频"这类尺寸时，提交量会接近
  两者之和；接近 `arena_capacity_bytes` 表示该流在使 arena 碎片化。`decoded_bytes` 是累计流量，
  既不驻留也不是内存上界。
- `AudioSegment` 区间覆盖其携带的样本，`Σlisted.bytes == audio_segments.bytes ==` 音频轨字节
  （当 `listed == segments` 时）；跨段 ms 取整允许 1 ms 偏差，尾部不足一段时 `partial=true`。

抽帧契约（`DecodedDataPlane.sampling`）：

- 空 `sampling` 表示"本次没有抽帧执行"，不得读作"所有帧都被保留"。
- 每个被观测的帧恰好记一次：`observed == kept + Σ skipped_*`，且
  `kept == kept_first_frame + kept_content_change + kept_static_heartbeat`。
- `kept` 等于该视频轨的 `samples`；轨道上 `samples + dropped_samples` 仍是该轨解码到的样本数。
  抽帧跳过既进 `sampling` 明细，也进轨道 `dropped_samples` 与原因集，两处视角不可相加。
- `kept <= max_keeps_bound`（仅由速率上限决定的硬上界），
  `max_gap_ms <= static_hold_ms + max_frame_interval_ms`（静止段不会无限期不采样）。
- `observed` 必须等于 ffprobe 路径的视频锚点数：两条独立路径看到的帧数一致，覆盖率才有意义。
- 跳过必须在 `drop_reasons` 里有对应原因（`rate_limited`、`no_change_yet`、`non_monotonic_pts`、
  `missing_signature`）；禁止静默丢帧。
- `DecodedTrackStat.last_end_ms` 描述该轨解码到哪里，抽帧只缩短交接，不缩短它。

实时接入契约（`media/v1/live.proto`，`sensoryplex-runtime ingest`）：

- 直播与离线是两条语义不同的路径：直播**没有已知时长**，因此不产出 `TimelineAnchor`，也不写
  `ReplayReport`；它写 `LiveIngestReport`，`MediaSourceDescription.duration_ms` 固定为 0。
  读数者不得把 0 当作"0 秒素材"。
- URI 可能带 streamid 或凭据，因此**只从环境变量读**：变量名由 pipeline 的 `uri_secret_ref` 派生
  （`SRT_LIVE_URI` → `SENSORYPLEX_SRT_LIVE_URI`）。命令行、日志与报告里只出现引用名；
  缺变量时以 `live_uri_env_missing: <变量名>` 失败，不做默认值兜底。
- 直播不可复现：`MediaSourceRef.content_hash` 保持为空，绝不用占位摘要冒充内容寻址；
  arena/stream 身份来自"本次会话"（引用名 + 时刻 + 运行种子），同一毫秒内并发的两次接入也不相同。
- `LiveStreamStats.samples` 是**从源收到**的样本数（视频帧 + 音频帧），落到 descriptor 的数量是
  `decoded.tracks[].samples`，两者不是一回事，也不可互相替代。
- 断流是正常事件而不是错误，但必须可观察：`stalls` / `stalled_ms` / `max_stall_ms` /
  `stall_events[]`（有界明细）全部来自本进程的墙钟测量。断流起点是**最后一个样本的时刻**，
  不是"发现超阈值那一刻"；窗口结束时仍在断流也必须计入，并记 `stream_gap_at_window_end`。
- `StreamStall.pts_jump_ms` 是"媒体时间缺了多少"，与墙钟 `gap_ms` 不是一回事；两端未知时写 `-1`，
  绝不填 0。恢复后新发布者从 0 重新计时会让它是负数，这同样是事实。
- `reconnect_owner` 如实写明重连归属：当前为 `srtsrc auto-reconnect`，即**解码元素负责重连**，
  Runtime 只测量断流与恢复，不声称自己控制重连。
- 重试有上限：卡顿次数超过 `--max-stalls` 即以 `live_stall_budget_exceeded` 显式失败，
  不无限等待。窗口长度/阈值/预算越界一律拒绝启动，不做夹取。
- 窗口里一个样本都没有**不是**成功：必须记 `live_window_produced_no_samples` 并以非 0 退出；
  `LiveIngestReport.golden_path_verified` 与 `ReplayReport` 同义，恒为 false。
- `handoff_state` 与 `ReplayReport` 同义（`exposed_on=<addr>` 或 `not_exercised`）；
  `ingest` 与 `replay` 共用同一条 arena / descriptor / lease / 交接链路与同一套运行参数解析。
- `replay` 读不了 SRT：anchor 路径要求已知时长，遇到 `type: srt` 的 pipeline 会以
  `srt_source_requires_ingest_command` 显式拒绝，绝不返回一条被清空的、看起来正常的时间轴。
- 样本时长按**来源**区分，不许混为一谈：buffer 自带的时长照用；缺失时由驱动用**同一轨下一个
  样本的 PTS 差分**补出（真实测量值），并计入 `DecodedTrackStat.duration_derived_samples`。
  差分 ≤ 0 记 `duration_delta_nonpositive`，超过 `MAX_DERIVED_DURATION_MS`（5000 ms）记
  `duration_delta_out_of_range`，窗口/流结束时仍挂起的最后一个样本记
  `duration_unresolved_at_end`——三条都是显式丢弃并计数，不是静默消失。
  这条路径来自真实采集端：OBS（Apple VideoToolbox H.264）的码流不带 timing，接收端 buffer
  没有 duration，按"缺时长就丢"会把整条视频轨丢掉。

跨进程数据面契约（`media/v1/handoff.proto`，**BufferHandoffService**；安全边界见
[ADR-010](../adr/ADR-010-跨进程数据面的安全边界.md)）：

- `ReplayReport.handoff_state` 必须显式说明数据面这次有没有被使用：带 `--handoff-listen` 时为
  `exposed_on=<addr>`，否则为 `not_exercised`。一次没有消费方的 replay 不得被读成"数据面已验证"。
- 控制消息只带**不透明引用**：`buffer_id`、字节偏移与长度、`sha256:` 摘要、时间区间、
  `BufferFormat`、lease 标识以及共享内存段名。**不出现**帧、PCM、tensor、宿主路径或密钥；
  原始字节只留在共享段里，靠 lease 授权窗口读取。
- 段名按运行随机派生（`/sp.<12 位十六进制>`），**不可从 arena handle 或报告推导**，
  且不出现在 `ReplayReport` 中；它是不透明句柄，不是路径，也不是授权凭证。
- gRPC 只允许绑定回环地址：共享内存不可跨主机映射，`--handoff-listen` 给非回环地址直接拒绝启动。
- `List` / `Stats` 返回 `HandoffStats`，其中两条恒等式必须成立，消费者与验收脚本各算一遍：
  `retained_total = retained + released_total + expired_total`（每条保留的 buffer 都有归宿）、
  `offered_total = retained_total + retain_rejections`（每个保留请求都有结果）。
- `offered_total` 必须等于本次解码交接的样本数（`Σtrack.samples`）：数据面与报告的计数对不上即为缺陷。
- 保留表上限 `<= 4096`、lease TTL 限定 `[50, 60000] ms`、单条 buffer 区间不超过 60 s，越界一律拒绝，
  不做夹取；写侧容量拒绝是稳定字符串，只有三种原因：`handoff_backlog_full`（保留表满）、
  `handoff_kind_quota_full`（单一 buffer 种类到配额）、`arena_capacity_exceeded`（共享段满）。
- 保留表是**所有种类共用**的一张表，因此上限分两层：总上限 `retained_limit`，以及单一
  buffer 种类的上限 `retained_kind_limit = max(1, retained_limit / 2)`（**ADR-011**）。
  实时流里音频块约 47 Hz、视频 keep 只有几 Hz，没有第二层上限时先到的种类会把整张表占满：
  实测 10 秒直播窗口的 32 条保留全部是音频块，视频帧一帧也交不出去。
  `retained_by_kind` 说明窗口由谁组成，`retained_kind_peak` 是单一种类的水位高水位。
- 消费期拒绝码是稳定字符串：`mapping_out_of_range`（越界，含跨到相邻 buffer）、`ambiguous_window`
  （只给 offset 不给 length 或反之）、`unknown_buffer`、`invalid_lease_ttl`、`buffer_already_leased`、
  `unknown_or_released_lease`（迟到释放）；一律通过 `ProcessingError.reason_code` 返回，不只写日志。
- `Acquire` 返回的 `BufferDescriptor.content_hash` 只覆盖**被授予的窗口**，不是整条 buffer；
  lease 为只读且带过期时间，`leased` 与 `request_rejections` 分别统计在途与拒绝。
- 同一 buffer 同时只允许一个消费者：重复领取是显式拒绝，不是静默共享。
- **lease 不是内存隔离**：同 UID 进程映射整段后仍能看到相邻 buffer 的字节；消费进程属于受信组件，
  这一点在验收输出里以 `same_uid_segment_visibility` 显式上报，不得当作已实现的安全边界。
- 生产者退出时 `shm_unlink` 段名；消费者只解除自己的映射，不删名。

背压与队列可观察契约（`media/v1/media.proto` 的 **BackpressureReport**，装配于 `DecodedDataPlane.backpressure`；
设计决策见 [ADR-011](../adr/ADR-011-保留窗口按种类分配.md)）：

- `observed` 不是"指标为零"，而是"这次运行到底有没有可测量的有界队列"：未暴露数据面的
  replay 写 `observed=false`，此时**其余字段一律无意义**，不得读成"压力为零"。
- `state` 只有 `ok | degraded | saturated`，由队列深度对阈值推导，**不由健康检查推导**。
  `degraded_entries`/`saturated_entries` 是进入次数，`state` 是结束时所处档位。
- `queues[]` 三条，`name[unit]=current/peak/capacity`：`handoff_retained_table[items]`（保留表总深度）、
  `handoff_retained_kind[items]`（最深的单一种类，容量 = `retained_kind_limit`）、
  `handoff_arena_bytes[bytes]`（共享段已用）。`peak` 是真实达到过的最大深度，不是配置上限。
- 两条恒等式必须成立：`dropped_total == Σ drop_reasons[].count == Σ drop_kinds[].count`。
  原因是稳定字符串（`handoff_backlog_full` / `handoff_kind_quota_full` / `arena_capacity_exceeded`），
  种类是 descriptor 携带的 kind 字符串（`video_frame` / `audio_pcm` / `audio_segment`）；
  只报总数会让人把"某一类被限流"误读成"视频帧全丢了"。
- `timeouts_total` / `residency_*` 来自 lease：`residency_*_ms` 是"保留 → 释放或过期"的等待时间，
  超时是**真实测量事件**，不是缺失样本。
- 处理顺序是**先降级、再拒绝**：进入 `degraded` 先按 `throttle_factor` 放大采样最小间隔
  （有 `throttle_cap_ms` 上限），被抑制的 keep 记 `sampling_throttled_samples` 与
  `sampling.skipped_backpressure_throttled`；进入 `saturated` 才由保留表拒绝。
- 覆盖边界：三条队列覆盖 arena 与保留表；GStreamer `queue` 元素与 `appsink max_buffers`
  **没有计数出口**，不在本报告内，不能据此宣称"全链路队列都可观察"。

模型插件契约（`runtime/v1/plugin.proto` + `docs/contracts/plugin.schema.json`，
工作样例见 `plugins/python/processors/vlm-moondream`，设计决策见
[ADR-012](../adr/ADR-012-模型插件与端侧推理边界.md)）：

- 模型身份来自**模型服务实测**（`GET /api/tags` 的 `digest`），不是插件写死的版本号；
  条目缺失或摘要不可用即拒绝启动（`model_not_available` / `model_artifact_digest_unavailable`）。
  `provenance.modelArtifactDigest` 必须与模型服务当前报告的摘要逐位相等。
- 模型不提供校准置信度时，`confidence` **留空**并写 `confidence_unavailable_reason`
  （本插件为 `model_does_not_report_calibrated_confidence`）；**禁止**填一个看似合理的数字。
- observation 必须绑定到具体字节：`time_range` 等于源 descriptor 的半开区间（`timing_source=media_pts`，
  不重新计时）、`content_hash` 等于该帧 lease 窗口摘要、`observation_id` 由稳定输入派生（可复现）。
- 插件产物摘要必须**可复算**：范围为 `pyproject.toml` + `src/**`（排除 `__pycache__`/`.pyc`），
  由 `tools/plugin_artifact.py` 生成与 `--check` 校验；manifest 里的 `digest` 不允许占位串，
  启动时 `--expect-digest` 不匹配即 `exit 2`。`artifacts.form` 为 `container`（缺省，强制 `signature`）
  或 `local_native`（要求 `image` 以 `local:` 开头且必须写 `signatureUnavailableReason`）。
- buffer 输入只允许 `cpu_shared_memory`，且必须经 `edge_material_sdk.LeaseBufferReader`
  （Acquire → `shm_open`+`mmap` → 摘要校验 → Release）；SDK 未挂 reader 时返回
  `buffer_reader_not_attached`，非该 kind 返回 `unsupported_memory_kind:*`，均不静默跳过。
- 帧字节不进日志/控制消息/返回 payload；消费方必须为每条保留给出归宿
  （读完即 Release，不消费的条目显式 `discard`），保证 `released+expired+retained == retained_total`。

媒体格式准入契约（ADR-009，**[媒体格式支持矩阵与拒绝语义](../adr/ADR-009-媒体格式支持矩阵与拒绝语义.md)**，
以下为待实现要求，当前状态见 [实现状态](../implementation-status.md)）：

- v1 只承诺 ADR-009 §2 的矩阵：容器 MP4/MOV、MKV/WebM、MPEG-TS；视频 H.264/HEVC/VP8/VP9 的
  8-bit SDR；音频 AAC-LC/Opus/Vorbis/PCM；声道只承诺 mono/stereo。矩阵之外的组合一律显式拒绝。
- 拒绝码是稳定字符串，必须进入 `blockers`（构建级）或 `drop_reasons` / `failure_reasons`（流级）
  之一，禁止只写日志；被拒轨道不得产生 `BufferDescriptor`，也不得让计数看起来像源里没有这条轨道。
- 准入依据按轨道关联的源格式上下文与解码后格式；容器、编码 profile、源位深不能从 `decodebin`
  输出的 raw caps 反推。源信息缺失须在有界等待后显式拒绝，CAPS 变化须重新判定。
- 禁止降级成功：`pixel_format=RGBA` 只说明归一化目标，不说明源位深；源侧位深与色彩由
  `bit_depth`、`color_primaries`、`transfer_characteristics`、`matrix_coefficients` 显式表达，
  取不到就按未知表达，不得填默认值。
- 几何必须显式：`display_rotation_deg` 只承诺 0/90/180/270，SAR 必须可表达；旋转要在归一化阶段
  真正应用，下游拿到的必须是呈现后的画面。
- 帧率模式必须显式（`CONSTANT | VARIABLE | UNKNOWN`）：未知帧率不得按 CFR 处理；v1 不承诺 VFR
  抽帧语义，只承诺能声明它。
- 字段归属：`common/v1/common.proto` 的 `BufferFormat` 承载 descriptor 的几何与源位深/色彩字段；
  `media/v1/media.proto` 承载源格式、轨道关联、时间基、帧率模式与应用的旋转角度等报告证据。
  仅修改媒体报告不能补齐插件收到的 `BufferDescriptor.format`；新增字段须保留显式未知语义。
- 实际选中的解码器元素（如 `vtdec_hw`、`avdec_h264`）属于证据：跨平台结论必须同时给出平台标识与
  解码器元素。`avdec_hevc` 在 macOS 上实测不存在，本机 HEVC 走 VideoToolbox，不得外推到 Linux。

`append_material` 是受信 timeline/storage 进程的内部入口；当前无公共写入 API。
事实写入和 outbox 在同一事务完成。outbox 分发、NATS 消费去重和重试器尚待实现，
因此不能把“已写 outbox”解释为“已发布 NATS”或“可语义检索”。
