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
- `keyword` 为 PostgreSQL 字面子串检索，无相关性排名；`semantic` 为真实向量检索（契约见下文
  "网关语义检索契约"）。
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
- `host_accelerators` 是**宿主**加速器事实（ADR-022），与 `backends` 分开：`backends` 回答
  "本进程能不能执行推理"，`host_accelerators` 回答"这台宿主有没有这块加速器"。
  三态 `ACCELERATOR_STATE_AVAILABLE / _UNAVAILABLE / _UNKNOWN` 不得互相塌陷：探测工具缺失、
  超时或输出读不懂一律落 `_UNKNOWN` 并带上 `probe_*:<source>` 原因，**不许**写成"不存在"；
  `state != AVAILABLE` 的每一条必须带 `unavailable_reason`，`AVAILABLE` 的那条不允许带。
  `runtime_version` / `evidence` 只能放真读到的值（读不到就留空），`evidence` 里不出现文件系统路径。
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
- 音频**样本布局**是契约的一部分：`BufferFormat.sample_format` 与 `AudioSegment.sample_format`
  缺省为空串只表示**未知**，读者不得假设宽度或字节序（按猜的宽度解释字节会静默改变下游模型看到的内容）。
  解码链只承认 `F32LE`（`crates/media/src/segment.rs` 的 `AUDIO_SAMPLE_FORMAT`，与链上 capsfilter 同源），
  其它布局在切段前被显式记账丢弃（`audio_unsupported_sample_format`），既不猜宽度也不按 4 字节/样本硬读。
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
- `offered_total` 必须等于本次解码**亲手交接**的 descriptor 数
  （`descriptors_built == Σtrack.samples + audio_segments.segments`）：数据面与报告的计数对不上即为缺陷。
  逐样本 buffer 与音频**段**描述符都进同一张保留表（M10），因此"样本数"不再等于"被 offer 的 buffer 数"，
  这个差必须被报告解释，不能当成误差抹掉。
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
工作样例见 `plugins/python/processors/vlm-moondream`（VLM）与
`plugins/python/processors/asr-whisper-mlx`（ASR），设计决策见
[ADR-012](../adr/ADR-012-模型插件与端侧推理边界.md) 与
[ADR-014](../adr/ADR-014-ASR插件与音频样本布局契约.md)）：

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
- **本地权重类插件的模型身份**来自**即将加载的权重文件本身**（逐块 SHA-256），不是配置里的版本号：
  必须真读一次容器头（safetensors / npz）并校验 `config.json` 的维度字段能被加载器读入，
  摘要算得出但加载器读不了同样拒绝启动（ADR-014 §4）。验收脚本必须**独立复算**一遍摘要，
  不得调用插件代码自证。
- **解码诊断量不是校准置信度**：`avg_logprob` / `no_speech_prob` / `compression_ratio` /
  `temperature` 原样进 payload（缺项写 `null`），`confidence` 仍然留空 + 写明原因；
  下游不得把诊断量当概率用（ADR-014 §5）。
- 音频类插件的 `payload` 必须写明子段时间的换算方式
  （`segment_timing=media_pts_window_relative_plus_window_start`）与输入事实
  （`sample_format` / `input_sample_rate` / `input_channels` / `input_samples` / `whisper_samples`）；
  空转写不是失败，但必须带 `empty_transcript_reason` 说明是哪一种空。
- **越窗的子段时间戳不得被夹取或丢弃**：模型可能给出越出窗口的时间（Whisper 退化时实测到
  5 秒窗口上的 `[940, 29880]`），必须逐子段标记 `timing_outside_window` 并给出
  `segments_outside_window` 计数，observation 的锚点仍是**源段区间**；夹取会让越界时序看起来
  像测得值（ADR-014 §6）。

媒体格式准入契约（ADR-009，**[媒体格式支持矩阵与拒绝语义](../adr/ADR-009-媒体格式支持矩阵与拒绝语义.md)**，
**以下语义已实现**：`crates/media/src/capability.rs` 是矩阵与判定的唯一归属，
`crates/media/src/decode.rs` 负责采集与上报；证据见 [验证记录](../verification.md) 的"M9"一节，
仍未验证范围见 ADR-009 §10）：

- v1 只承诺 ADR-009 §2 的矩阵：容器 MP4/MOV、MKV/WebM、MPEG-TS；视频 H.264/HEVC/VP8/VP9 的
  8-bit SDR；音频 AAC-LC/Opus/Vorbis/PCM；声道只承诺 mono/stereo。矩阵之外的组合一律显式拒绝。
  这些行都已有公开授权正样本（`make capability-check` 6 个正样本）。
  有容器头的音频容器单独命名拒绝（`unsupported_container_ogg` / `unsupported_container_wav` /
  `unsupported_container_flac`），不与 `unsupported_container_raw_es`（真正没有容器头的裸 ES）混用。
- 拒绝码是稳定字符串（`unsupported_<维度>_<取值>`，见 ADR-009 §3），进
  `DecodedDataPlane.rejected_tracks`：每条给出 `track_kind`（`video|audio|other`）、`code`、
  被观测到的 `detail`、`container` 与本次运行实际选中的 `decoder_element`；命令行同时打
  `rejected=<条数>`。被拒轨道不进 `blockers`（那是构建级缺失），不产生 `BufferDescriptor`、
  不产生 track stat，也不得让计数看起来像源里没有这条轨道——被拒的**唯一原因位置**是这个列表。
  同一流里没被拒的轨道照常解码，全轨被拒的运行仍然 exit 0（拒绝不是运行失败）。
- 准入依据按轨道关联的源格式上下文与解码后格式；容器、编码 profile、源位深不能从 `decodebin`
  输出的 raw caps 反推。容器结论取自 typefind（`have-type`，demuxer 的 sink caps 作兜底），
  轨道源格式取自 demuxer/parser/decoder/capsfilter 的 sink caps，一律按 GStreamer **stream ID**
  关联（禁止按 pad 顺序或只按 video/audio 类型配对）。源信息缺失即在链头拒绝
  （`unknown_source_*`）：判不出来就不放行。known 字段被后到的 caps 覆盖即重新判定。
- 禁止降级成功：`pixel_format=RGBA` 只说明归一化目标，不说明源位深；源侧位深与色彩由
  `bit_depth`、`color_primaries`、`transfer_characteristics`、`matrix_coefficients` 显式表达，
  取不到就按未知表达，不得填默认值。
- 几何必须显式：`display_rotation_deg` 只承诺 0/90/180/270，SAR 必须可表达；旋转要在归一化阶段
  真正应用，下游拿到的必须是呈现后的画面。**v1 尚未实现这一条**：本机实测 `qtdemux` 对
  `rotate=90` / DisplayMatrix 不暴露旋转信息，因此这两个字段保持**缺省**（绝不填 0 冒充"未旋转"），
  旋转的采集与应用属于 `docs/TODO.md` 的剩余子项。
- 帧率模式必须显式（`CONSTANT | VARIABLE | UNKNOWN`）：未知帧率不得按 CFR 处理；v1 不承诺 VFR
  抽帧语义，只承诺能声明它。
- 字段归属：`common/v1/common.proto` 的 `BufferFormat` 承载 descriptor 的几何与源位深/色彩字段；
  `media/v1/media.proto` 承载源格式、轨道关联、时间基、帧率模式与应用的旋转角度等报告证据。
  仅修改媒体报告不能补齐插件收到的 `BufferDescriptor.format`；新增字段须保留显式未知语义。
- 实际选中的解码器元素（如 `vtdec_hw`、`avdec_h264`）属于证据：跨平台结论必须同时给出平台标识与
  解码器元素。`avdec_hevc` 在 macOS 上实测不存在，本机 HEVC 走 VideoToolbox，不得外推到 Linux。
  `decoder_element` 为空串只表示**本次运行没能归因到解码器**；容器里直存 raw 采样（例如 MOV 里的
  `pcm_s16le`）没有 parser/decoder，此时的取值是 `demuxer_passthrough`——这是一个确定的答案，
  不是空串，也不得被读成"用了内置解码器"。该语义写在 `media.proto` 的 `decoder_element=17` 注释里。

向量索引落库契约（ADR-020，**[向量索引落库与检索闭环](../adr/ADR-020-向量索引落库与检索闭环.md)**，
**以下语义已实现并实测**：`services/index-worker`，证据见 [验证记录](../verification.md) 的
"M8 剩余：向量索引落库与检索闭环（ADR-020）"一节）：

- `embedding_record` 是向量落库的**事实行**；向量本体在向量库里，本表只存引用与重建依据。
  迁移 `0003_embedding_index.sql` 追加 `observation_id`、`vector_index_key`、`error_code`、
  `indexed_at`、`created_at`、`updated_at`，并把顺序写成行不变式（由数据库强制，不是靠代码自觉）：
  `ready` ⇒ `vector_ref IS NOT NULL AND indexed_at IS NOT NULL`；`failed` ⇒ `error_code IS NOT NULL`；
  `ready` ⇒ `error_code IS NULL`。
- `state` 描述**最近一次尝试**，`vector_ref`/`indexed_at` 描述**最近一次确认写入**：重跑失败会把行改成
  `failed` 但**不清空**已有引用（库里那份向量确实还在），而检索只认 `state='ready'`，
  所以它不会被当成成功命中返回。
- `embedding_id` 是**确定性**主键：`emb_` + `sha256(<observation_id>|<material_unit_id>|<revision>)`
  前 32 位十六进制。同一份输入重跑得到同一个 id（幂等），不同 material/revision/observation 必不相同。
  identity 冲突（同一 id 换了观察、key、模型或摘要）显式报 `embedding_identity_conflict` /
  `embedding_payload_conflict`，不覆盖别人的行。
- **collection 名就是 `vector_index_key`**（`material_<slug>_d<实测维度>_v<契约版本>`）：换模型或换维度
  得到另一个 collection，旧向量留在旧 collection，不原地迁移、不补零、不截断。写入前要求三方维度一致
  ——key 后缀、payload 自称的维度、向量实际长度——否则 `vector_dimension_mismatch`；已存在的同名
  collection 必须与当前契约逐字段一致，否则 `vector_collection_contract_mismatch`。
- **`vector_ref` 是逻辑引用**：`milvus://<collection>/<embedding_id>`，**不含**主机、端口、本地路径或
  库文件名（宿主路径属于部署配置）。Lite 形态与服务端形态产生同一个引用。
- **向量库不是事实源，也不是鉴权依据**：命中必须回查 PostgreSQL——`state='ready'` + material 行存在
  且 `status <> 'failed'` + `source.owner` 等于当前 principal——才允许作为结果返回；
  查不到的回查计入 `unindexed_hits`（是"被丢弃的命中"，不是"没有命中"）。
- 稳定原因码（顶层与逐条都只用这些码，不解析异常文本）：`vector_store_unavailable`（路径/凭据/客户端
  不可用）、`vector_store_locked`（Lite 数据目录被同机另一进程 flock 持有，可重试）、
  `vector_collection_contract_mismatch` / `vector_index_type_mismatch`、`vector_collection_load_failed`、
  `vector_upsert_failed` / `vector_query_failed` / `vector_search_failed`、`vector_confirm_mismatch`、
  `invalid_embedding_payload`、`invalid_vector_index_key`、`invalid_vector_ref`、`index_key_mismatch`、
  `vector_dimension_mismatch`、`embedding_identity_conflict`、`embedding_payload_conflict`。
- 形态边界：本机验收用 Milvus **Lite 文件形态**，它是**进程独占**的（数据目录 flock），
  因此 edge 是"单写进程"；`deploy/compose/docker-compose.vector.yml` 的服务端拓扑因本机 Docker Hub
  不可达**未验收**，不得据此声称服务端可用。

网关语义检索契约（ADR-023，**[网关语义检索接线与索引检索面](../adr/ADR-023-网关语义检索接线与索引检索面.md)**，
**以下语义已实现并实测**，证据见 [验证记录](../verification.md) 的"网关语义检索接线（ADR-023）"一节）：

- **`mode=semantic` 只接受 `query` + `limit`**；`stream_id` / `modalities` / `tags` / `start_ms` /
  `end_ms` / `min_confidence` 显式拒绝（`semantic_filters_not_supported`）。静默忽略筛选条件会返回
  "像是筛过"的结果，比拒绝更糟。空查询在 API 侧即拒（`invalid_query`）。
- **keyword 不排名，`hits` 必须为空**（"没有排名"与"排名为 0"不同）；semantic 的 `hits` 与
  `materials` **同序同长**，`index_version` 由检索面给出（keyword 恒为 `postgres-literal-v1`）。
- **命中是引用，不是事实源也不是鉴权依据**：调用方按 `(material_unit_id, material_revision)`
  重新水合事实；水合不出来的命中计入 `unresolved_hits`，检索面丢弃的命中计入 `unindexed_hits`，
  结果集不静默变小。
- **`distance` 是 COSINE 距离**（FLAT 精确检索，越小越近），**不是置信度**：本切片不做相关性校准，
  RRF / 混合检索未做，筛选条件只在 keyword 模式生效。
- **同源守卫**：collection 里的 `model_release_id` 必须**唯一且等于**本次查询编码器，否则整请求拒绝
  （`vector_index_model_release_mixed` / `query_model_release_mismatch`）。刻意不按 principal 过滤
  ——collection 是所有 owner 共用的，混装会让**所有人**的距离失去可比性。
- **进程边界**：向量库由常驻检索面（`sensoryplex-index serve`）独占持有，API **不**打开向量库
  （Milvus Lite 数据目录是进程独占的，ADR-020 §7）。检索面共享令牌 + 常量时间比较
  （`authorization: Bearer <token>`，缺失与带错一律拒绝且不区分），无令牌或令牌不足 32 字符拒绝启动，
  默认只绑回环 `127.0.0.1:50077`。
- **HTTP 状态码只表示失败落在哪一环**：没走到检索面 → 503（未配置 `semantic_search_unavailable`、
  令牌不符 `semantic_index_unauthenticated`、连不上 `semantic_index_unreachable`）；检索面答了但不是
  本契约 → 502（`semantic_index_protocol_error` 或检索面自报的不可重试原因码）。
  **`retryable` 是独立标记**：503 也可能不可重试（配置错误），502 也不代表"重试会变好"。
- **原因码原样上抛**，API 不改写检索面的码；API 自有的两个码（`semantic_search_unavailable`、
  `semantic_index_unreachable`）与检索面的码是**两套词汇**，不得混用。
- 能力上报只报配置事实：配了检索面 ⇒ `semantic_search` 报 available（`reason` 为空），否则报
  `semantic_search_unavailable`——**不是 501，也不再是 `semantic_index_not_configured`**。
  终结点与令牌必须都配齐，终结点写成 URL（`http://...`）直接拒绝：它要的是 gRPC target。

模型 worker 并发上限契约（ADR-021，**[模型 worker 按分级并发上限限流](../adr/ADR-021-模型worker按分级并发上限限流.md)**，
**以下语义已实现并实测**；`tools/ai_worker.py`，证据见 [验证记录](../verification.md) 的
"M8 剩余：模型 worker 按分级并发上限限流（ADR-021）"一节）：

- 报告字段是 `model_concurrency`（**不是** `model_parallelism`）：前者是这一次运行的**执行账目**，
  后者是常驻分级注入的**预算声明**。两者混成一个名字就等于把"声称"和"实测"混掉。
- 单位是**并发在飞的插件调用数**；`peak_in_flight` 是**实测峰值**，`limit == 1` 与串行语义完全一致，
  因此未注入（`not_injected` / `limit=1` / `source=none`）时默认行为不变。
- `source` 写明上限来自哪里：`flag`（`--model-parallelism`）、`env`（`SENSORYPLEX_MODEL_PARALLELISM`）
  或 `runtime`（`DescribeCapabilities.residency.model_parallelism`）。**不做"两个来源谁大用谁"这类推断。**
- `tier` / `tier_capacity` 在没接运行时时写成 `not_checked` / `null`——`null` 而不是 0，
  因为 0 在 `ResidencyLimits` 里已经被"未声明"占用，报告里不该再借它表示未知。
- 稳定原因码（顶层与逐条都只用这些码，不解析异常文本）：`invalid_resident_limit`
  （空串 / `0` / 非整数 / 非 UTF-8）、`model_parallelism_conflict`（flag 与 env 都在且不相等）、
  `model_parallelism_exceeds_tier_cap`（请求值超这一档上限，**不夹取**）、
  `runtime_capabilities_unavailable:<CODE>`（`--runtime` 给出的端点拿不到答案）、
  `invalid_max_attempts` / `invalid_retry_backoff_ms`、`empty_plugin_result`、
  `plugin_process_failed:<CODE>`、`retry_exhausted:<原因>`。
- **越界与坏值在连插件之前就失败**（exit 2），报告里只有 `{"state": "rejected", "reason": "..."}`，
  且一条输入都不会跑：报告里**不可能**出现"被夹到上限的成功"。
- **可重试拒绝不再是终态**：插件的 `concurrency_limit` / `deadline_expired` / `processing_timeout`
  按 `--max-attempts`（默认 3，含首次调用）重试，指数退避封顶 1s，**每轮刷新 deadline**；
  预算用尽才落 `retry_exhausted:<原因>`。`throttle_events` 按原因码分开计数——把它们合成一个数字
  就把"自己撞上插件闸门"与"时间预算不够"抹成同一件事。
- Rust 侧 `ResidentLimits.model_parallelism` 只**转述**（`serve` 不跑模型）；消费方是 worker 进程。
  两边的解析必须给出**逐字相同**的原因串（Python 侧刻意不用裸 `int()`，见该 ADR §3）。

`append_material` 是受信 timeline/storage 进程的内部入口；当前无公共写入 API。
事实写入和 outbox 在同一事务完成。**分发**已按
[ADR-024](../adr/ADR-024-outbox分发接线与消费去重边界.md) 接上（`services/outbox-relay`：
`published_at` 只在 JetStream 确认之后写、`Nats-Msg-Id = event_id` 由 duplicate window 吸收重发、
stream 漂移只报不改）；消费去重原语在 `records.is_consumed` / `record_consumed`，键是
`(event_id, consumer_name)`。

**消费（JetStream → sink）**已按 [ADR-025](../adr/ADR-025-常驻消费循环与sink接线.md) 接上
（`sensoryplex-index serve --consume`：消费与检索面**同进程**，因为 Milvus Lite 的数据目录是
进程级 flock）。这一侧的契约面写死为：

- 事件只是**通知**：`payload_ref` 必须是 `material:<material_unit_id>:<revision>`（revision 为正整数），
  形状不符即 `invalid_payload_ref`；可编码文本按引用回查 `observation.payload_jsonb`
  （读 payload 的规则仍只有 BGE 插件那一份实现），回查不到即 `event_missing_facts`
  ——事实与事件同事务，查不到是写侧缺陷，不是"没数据"；
- 订阅是**精确 subject**（`<prefix>.material.upserted`），不用 `>` 通配；不支持的 `event_type`
  记账跳过（`unsupported_event_type`）并 ack，不重投到天荒地老；
- durable 契约六项逐字比：`durable_name` / `filter_subject` / `ack_policy=explicit` / `ack_wait` /
  `max_deliver` / `max_ack_pending=batch`。**消费端不建 stream**（缺失即 `event_stream_missing`），
  只建自己的 durable；stream 与 durable 漂移一律**只报不改**
  （`event_stream_contract_mismatch` / `event_consumer_contract_mismatch`）；
- 顺序是**先干活后记账**：编码 → 落库 → 确认写入 → `record_consumed` → `ack`。
  少一条向量就不算消费完成：重投到 `max_deliver` 上限后以 `event_retry_exhausted` **退出码 3**
  显式停止（本切片没有 dead-letter）；
- 状态行 `consume.status` 只有 16 个字段（计数 + 标识 + 稳定原因码），不放载荷、文本、向量、
  令牌、DSN 或主机路径。

因此"已写 outbox"仍**不能**解释为"已被消费"——它只表示事件被可靠地记了下来；
"已发布 NATS"由 `make outbox-check` 出证据（subject / `Nats-Msg-Id` / 载荷逐字节对回来），
"已被消费成向量、并且能被同一个检索面检索到"由 `make consume-check` 出证据。
语义检索本身已接线；它的输入除了显式调用（`sensoryplex-index index`），现在也可以来自
NATS 消费（走同一个进程）。
