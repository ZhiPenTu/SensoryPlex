# 契约与代码生成

`proto/` 是**唯一的跨语言契约源**。一条规则如果无法在 Protobuf 契约里表达，它就不属于接口的一部分——
而生成代码从来不是修这个问题的地方。

## 契约包

| 包 | 文件 | 覆盖范围 |
| --- | --- | --- |
| `common/v1` | `proto/common/v1/common.proto` | 共享值类型与枚举 |
| `material/v1` | `proto/material/v1/material.proto` | 素材、revision、观测、血缘 |
| `media/v1` | `media.proto`、`live.proto`、`handoff.proto` | 来源、锚点、回放报告、实时接入、跨进程交接 |
| `runtime/v1` | `proto/runtime/v1/runtime.proto` | Health、能力描述、处理器插件服务 |
| `gateway/v1` | `gateway.proto`、`console.proto` | 查询入口与控制台 API |
| `index/v1` | `proto/index/v1/index.proto` | 向量写入与检索面 |
| `node/v1` | `proto/node/v1/node.proto` | 节点注册、心跳、预检、部署意图 |
| `orchestration/v1` | `proto/orchestration/v1/orchestration.proto` | 方案 revision、Run、Task、调度 |

## 代码生成

| 目标 | 方式 | 是否提交 |
| --- | --- | --- |
| Rust（`tonic`/`prost`） | 构建期 `build.rs` | 否——构建时生成 |
| Python SDK 消息 | `make proto` → `tools/generate_proto.py` | 是——提交进 SDK，禁止手改 |
| 控制台类型 | `make proto` → `tools/generate_console_types.py` | 是——同一规则 |

```sh
make proto     # 在 api 容器内执行；需要先跑起 ./deploy/up.sh
```

## 字段规则

- **禁止重编号。** 字段号是永久的。
- **删除必须 `reserved`。** 已删除字段的编号不可复用。
- **破坏语义的修改进入新的协议 major。** 当前是开发预览，尚未发布稳定 SDK。
- **时间范围**是同一 stream 上的 `[start_ms, end_ms)`，且 `end_ms` 严格大于 `start_ms`。
- **`confidence` 是 optional。** 缺失时必须说明原因；绝不用 `0`/`1` 代替“未知”。
- **两个不同的 SHA-256 字段**分别对应插件产物与模型产物。两者不可互换：描述代码的产物不是描述权重的产物。

## REST 映射

HTTP 面遵循 Protobuf JSON 映射，另有几条项目自己的决定：

| 规则 | 取值 |
| --- | --- |
| 字段命名 | `snake_case` |
| `int64` | 以**字符串**返回 |
| `optional` | 缺失表示未知，绝不填默认值 |
| 查询时间条件 | 区间**重叠** |
| 标签 | 按 **AND** 组合 |
| `modalities` | 按 **OR** 组合 |
| `keyword` | PostgreSQL 字面子串检索，**没有相关性排名** |
| `semantic` | 对检索面的真实向量检索 |

`keyword` 与 `semantic` 的区别很重要：关键词查询没有“最佳匹配”的概念，因此它的顺序不能当作相关性来呈现。

## 媒体契约要点

- `MediaSourceRef` 只携带 secret 名称与内容摘要；本地文件是 `sha256:` 摘要，直播流摘要为空。
  `stream_id`/`source_id` 由摘要派生，因此文件重复回放保持幂等。
- 锚点是呈现顺序区间，不带媒体字节；解码顺序的 PTS 会被重排并计入 `out_of_order_items`。
- 每个被丢弃的抽样点都带原因码，不夹取。
- 只有真实授权样本通过完整验收，`ReplayReport.golden_path_verified` 才能置真；报告永不包含媒体路径。

## 解码数据平面契约

- 全零等于“没有解码发生”：`arena_id` 为空时不允许出现 `tracks`、`evidence_descriptors` 或非零
  `descriptors_built`。
- `first_pts_ms` / `last_end_ms` 用 `-1` 表示“未观察到”——绝不用 `0`。
- `timeline_offset_ms` 让 descriptor 与 `ffprobe` 锚点落在同一条呈现时间轴上。
- `overlapping_samples` 统计重复上一段的真实载荷：保留，但不静默丢弃。

## 能力契约

`RuntimeService.DescribeCapabilities`（ADR-008）是规范性的：平台标识、必填的 `unavailable_reason`、
未知保持未知、`admitted_memory_kinds`，以及 `backends` 与 `host_accelerators` 的分离。
`Health.unavailable_capabilities` 与 `DescribeCapabilities.unavailable_capabilities` 必须同源——
两处不一致属于契约缺陷，不是外观问题。语义见 [能力模型](/zh/concepts/capability-model)。

## 测试

```sh
make test-contracts    # pytest tests/contracts，不依赖任何外部服务
```

契约测试不需要数据库，因此是项目里最便宜的一道门禁，也是唯一能在没有任何基础设施的情况下把字段级规则
钉死的地方。

## 继续阅读

- [服务与节点](/zh/architecture/services)
- [时间轴语义](/zh/concepts/timeline)
