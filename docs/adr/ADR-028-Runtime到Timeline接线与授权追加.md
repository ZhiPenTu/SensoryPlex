# ADR-028：Runtime → Timeline 接线（窗口选择、原片映射、授权事务性追加）

**状态：** Accepted（2026-09-24）
**上游决策：** ADR-003（媒体锚点与半开区间）、ADR-010（跨进程数据面的安全边界）、ADR-020（向量索引落库与检索闭环）、ADR-023（网关语义检索接线与索引检索面）、ADR-024（outbox 分发接线）、ADR-025（常驻消费循环与 sink 接线）、ADR-027（事件链路分级背压与容器化常驻）
**相关文档：** [实现状态](../implementation-status.md)、[验证记录](../verification.md)、[待办](../TODO.md)、[Timeline 融合核心](../../crates/timeline/README.md)、[契约与查询语义](../contracts/README.md)

---

## 1. 背景与问题

Timeline 融合核心（`crates/timeline`）已经合并主线，但它**是一个没有任何调用方的叶子 crate**：
workspace 里只有它自己的 `Cargo.toml` 提到 `sensoryplex-timeline`，`crates/runtime` 既不依赖它、
源码里也没有 `timeline` 字样。写入侧同样是断链的：
`services/api/src/sensoryplex_api/infrastructure/materials.py` 的 `append_material()`
自述 *"The append entry point is for an authorized timeline worker, never public HTTP."*，
而 `services/api` 内部**零调用方**——那个 timeline worker 并不存在。

同时，两条 pipeline（`config/pipelines/file-material.yaml` / `srt-live.yaml`）都已经声明了
`- type: timeline_fusion`，但没有任何代码实现它。于是有三句话在本文档之前都是**假的或没有证据的**：

1. 「pipeline 声明了 `timeline_fusion`」≠「这个处理器被执行了」；
2. 「Timeline 核心合并了」≠「真实媒体能被聚合成素材」；
3. 「`append_material` 是唯一写入口」≠「有一条授权路径真的在用它」。

本切片回答的问题是：**真实媒体从 Runtime 走到素材事实，中间那几跳由谁负责、按什么契约、失败怎么算。**

## 2. 决策一：窗口选择与原片映射归 Runtime，不归融合核心，也不归 Python

融合核心的调用边界（`crates/timeline/README.md`）已经写死：核心**不切窗、不合并、不裁剪**，
只处理调用方明确给出的时间窗。所以"从媒体里选出素材窗口"这一跳必须有人做。选定 Runtime，理由：

- **只有 Runtime 见过真实字节与原片身份**：`FileSource::open` 通过 ffprobe 得到
  `stream_id = stream-<digest12>`、`source_id = file-<digest12>`、整文件 SHA-256 与时长；
  这三者是"可信的 source_id / stream_id / 原片 SHA-256"的唯一来源（ADR-003）。
- **Python 侧不能是这条信息的来源**：让编排脚本自己算文件摘要、自己编 stream id，
  等于把"原片映射"降级成调用方的断言——正是 ADR-010 要挡的那类越权。

新增 Runtime 子命令 `sensoryplex-runtime timeline`，只做四件事：真探测 → 读真运行报告 →
选窗 + 调用 `FusionEngine::fuse()` → 写出 MaterialUnit（二进制 protobuf）与一份 JSON 报告。
**它不打开数据库、不发事件、不做网络 RPC**（§5）。

派生规则（全部稳定、可从同一份输入复算，不含路径、不含时钟）：

| 值 | 派生式 |
| --- | --- |
| `stream_id` / `source_id` | 真探测结果（`file-<digest12>`） |
| `asset_id` | `asset-<digest12>` |
| 素材窗口 | 固定栅格 `[k*window_ms, (k+1)*window_ms)` 与媒体时长求交，`window_ms` 来自 pipeline 策略 |
| `material_unit_id` | `material-<digest12>-<start_ms>` |
| `created_at_unix_ms` | 窗口内观测 `created_at_unix_ms` 的最大值（**不是**取当前时间） |
| `pipeline_version` | `<pipeline 名>:timeline-fusion-v1:<策略摘要前 16 位>`（§4） |

三条写死的边界：

1. **跨窗观测一律显式拒绝，不裁剪**：半开的窗口是事实的一部分，把 `[4000,6000)` 裁成
   `[4000,5000)` 会造出一条**从未被观测的时间区间**。拒绝原因 `observation_crosses_window`。
2. **素材窗口上限 = 融合核心的 `max_window_ms`（300 s）**，越界的 pipeline 声明在解析期就被拒绝。
3. **`created_at` 取观测的最大值**：同一批事实重放必须得到**逐字节相同**的素材，
   否则 `append_material` 的幂等判定（内容摘要相同 ⇒ 未新增）永远不成立。

## 3. 决策二：观测必须绑定到"Runtime 签发的不透明标识"上

`timeline` 的两个输入都是**真运行产物**，不是调用方手写的 JSON：

- `--report <replay.pb>`：Runtime 自己对同一文件写的探测/解码报告（ADR-003）；
- `--worker-report <ai-worker.json>`：`tools/ai_worker.py` 的真实运行报告，里面有
  `frames[]`（每条保留项的 `buffer_id` / `kind` / **`source_time_range_ms`** / `source_digest` /
  `observation_id`）与 `observations[]`。

由 `frames[]` 派生 timeline item，标识符**与 SDK 的 `describe_source_item()` 同一算法独立复算**：

```
item_id = "<buffer_id>@<source_digest 去掉 'sha256:' 后前 16 位>"
item 窗口 = frames[].source_time_range_ms     # Runtime 签发的描述符窗口，不是观测自己的窗口
```

四条校验（任一条不成立即显式拒绝，不猜、不改写）：

| 校验 | 原因串 |
| --- | --- |
| 报告里的 `stream_id`/`source_id`/`content_hash` 与本进程重新探测同一文件的结果一致 | `report_probe_mismatch` |
| 观测的 `source_item_id` 能在描述符账本里找到，且等于按上面公式复算的值 | `observation_item_mismatch` |
| 观测的 `time_range` 等于账本里该条目声明的 `source_time_range_ms` | `observation_ledger_range_mismatch` |
| 观测的 `stream_id`/`source_id` 等于探测结果；同一批观测必须一致 | `observation_stream_mismatch` / `observation_source_mismatch` |

`buffer_id` 是 Runtime 在保留表里**签发**的句柄（ADR-010），消费者拿不到、也伪造不出；
"观测确实来自那条被授予的窗口"因此是可复算的，而不是自报的。
`SourceReference.content_hash` 用**整文件摘要**（原片引用不能用帧摘要冒充，融合核心也会拒），
`SourceReference.time_range` 用账本里的描述符窗口。

**诚实边界（不得含糊）**：`--worker-report` 与 `--report` 的对应关系由操作者提供，
本切片校验的是"同一条 stream + 描述符账本自洽 + 摘要形状"，**不是**密码学绑定：
一份构造的 worker 报告只要满足上述四条校验就能被本命令接受。真正的绑定是
`make handoff-check`（lease 由 Runtime 授予）+ 本命令的四条校验两件事**相加**，
而这两件事都只证明"这个窗口被授予过"，不证明"模型真的看懂了画面"。

## 4. 决策三：pipeline 声明即实现（`timeline_fusion` 必须有策略）

既然 pipeline 已经声明了 `timeline_fusion`，就让"声明"有后果：

```yaml
  timeline_fusion:
    window_ms: 5000
    fast_modalities:
      - vision.scene_description
    enrichment_modalities: []
```

`Pipeline::parse` 增加三条校验，**越界即拒绝，不夹取、不降级、不静默忽略**：

- 声明了 `timeline_fusion` 处理器但缺 `timeline_fusion` 策略 → `timeline_fusion_declared_without_policy`；
- 有策略但没有任何处理器声明 `timeline_fusion` → `timeline_policy_without_declaration`；
- 策略本身非法（`window_ms` 越界、`fast_modalities` 为空、快慢两组重叠或超过 32 个、
  标签为空或超长）→ `invalid_timeline_policy`，并且**复用 `FusionEngine::new` 的同一份校验**，
  不在 Runtime 里另写一套规则。

`pipeline_version` 必须把策略纳入不可变身份（融合核心 README 的硬要求）：
`<pipeline 名>:timeline-fusion-v1:<sha256(规范化策略 JSON) 前 16 位>`。
换了 `window_ms` 或模态集合就是**另一个 pipeline 版本**，同一条素材不可能被两个策略各自写成
同一个版本号——这正是"同名策略内容被悄悄替换"要挡的东西。

## 5. 决策四：Rust 不碰数据库，追加走 Python 的授权写入口

`FusionOutcome.material` 只写到文件（`<material_unit_id>.material.pb`）；把它变成事实的那一步
是 `tools/timeline_handoff.py`，它以**授权 timeline worker**的身份做两件事：

1. **登记引用事实**（只为通过写侧校验而登记，字段全部来自 Runtime 报告，不自造）：
   `media_source(source_id, type='file', uri_redacted='private://not-exposed', owner)`、
   `stream_session(stream_id, source_id)`、`media_asset(asset_id, stream_id, sha256, duration_ms)`、
   `timeline_item(item_id, stream_id, kind, start_ms, end_ms)`；
2. **调 `append_material()`**：素材 + 观测 + lineage + outbox 行在**同一个事务**里落下
   （ADR-024），事件由它产生，不是脚本手写的。

`owner` 与该授权是显式参数（不默认成任何"看起来合理"的值）；重放同一份素材时
`append_material` 返回 `False`（未新增）并如实计入报告；revision 冲突
（`immutable_revision_conflict` / `non_sequential_revision`）原样上抛，不吞。
Rust 侧因此**不新增任何数据库/网络依赖**，`crates/storage` 的 `MetadataStore` 端口仍无 adapter——
它是另一个切片的形态（常驻 timeline worker），本切片不假装拥有它。

## 6. 决策五：只做一条链，且明确"还没做"的部分

本切片只做**文件源 → 单次运行 → 素材**。以下都在报告里作为 blocker 写死，不留给读者推断：

- `metadata_append_not_exercised`：Runtime 自己**没有**追加（追加发生在 `timeline_handoff.py`）；
- `vector_index_not_exercised` / `semantic_search_not_exercised`：`timeline` 命令与
  `timeline_handoff.py` 都**不**发事件、不写向量；向量那一跳由 ADR-020/023/025 的既有证据负责，
  真实媒体素材接进去的那一次端到端由 `make timeline-check` 单独出证据；
- `golden_path_verified` 仍恒为 `false`：本命令不改变这个事实。

## 7. 失败语义（稳定原因串）

| 原因串 | 含义 | 可否重试 |
| --- | --- | --- |
| `report_missing` / `report_unreadable` | `--report` 文件不存在或不是有效的 `ReplayReport` | 否（修输入） |
| `worker_report_missing` / `worker_report_unreadable` | worker 报告缺失或 JSON 不可解析 | 否 |
| `worker_report_failed` | worker 报告里 `failures` 非空（那一轮模型链路本身没成功） | 是（重跑模型链路） |
| `report_probe_mismatch` | 报告的 stream/source/摘要与本进程重新探测同一文件的结果不一致 | 否 |
| `observation_without_descriptor_ledger` | 观测的 `source_item_id` 不在描述符账本里 | 否 |
| `observation_item_mismatch` | `source_item_id` ≠ `<buffer_id>@<digest16>` 复算值 | 否 |
| `observation_ledger_range_mismatch` | 观测区间 ≠ 账本声明的描述符窗口 | 否 |
| `observation_stream_mismatch` / `observation_source_mismatch` | 观测身份与探测结果不一致 | 否 |
| `observation_crosses_window` | 观测跨了两个素材窗口 | 否（调整 `window_ms`） |
| `source_reference_crosses_material_boundary` | 描述符窗口跨窗（观测本身没跨） | 否 |
| `fusion_*` | 融合核心的显式拒绝（`fusion_observation_outside_window` 等原样上抛） | 否 |
| `timeline_no_observations` | 报告里没有任何观测 | 否 |
| `immutable_revision_conflict` / `non_sequential_revision` | 追加期 revision 冲突（写侧仲裁） | 是（重新读取最新 revision） |

## 8. 验收

`make timeline-check MEDIA=<授权样本>`（容器内执行 Python、主机执行 cargo，理由同既有媒体目标）：

| 场景 | 判定标准 |
| --- | --- |
| 1 | 真实媒体 → 真实 replay（解码 + lease）→ 真实插件观测 → `timeline` → 素材事实：观测区间等于描述符窗口、素材窗口包含其观测、`SourceReference` 用整文件摘要、`pipeline_version` 带策略摘要 |
| 2 | 真实 PostgreSQL（隔离 schema + 真实迁移）→ `timeline_handoff.py` 追加：素材/观测/lineage/outbox 行落库，事件 `payload_ref` 指向真实 revision，重放第二次返回"未新增" |
| 3 | 拒绝语义：跨窗观测、账本缺失、其余摘要不一致都必须显式失败且**不追加任何事实** |
| 4 | 不外泄：报告里没有媒体路径、没有共享内存段名、没有 DSN |

## 9. 后果与仍未验证范围

已落地：`crates/runtime` 的 `timeline` 子命令与 pipeline `timeline_fusion` 策略、
`tools/timeline_handoff.py`、`tools/verify_timeline_handoff.py`、`make timeline-check`。

**仍未验证（不得声称完成）：**

- **revision 前进**：本切片一律 `previous=None` ⇒ `revision=1`；"读回最新 revision 再融合下一版"
  未实现（需要一个能读事实的调用方），因此同一素材的**第二次不同内容**写入会以
  `immutable_revision_conflict` 显式失败而不是自动升版；
- **常驻 timeline worker**：真正的常驻形态（消费描述符/观测流 → 融合 → 追加）未实现，
  本切片是**单次运行**；
- **实时源（SRT/RTMP）**：`timeline` 命令只接文件源；实时接入的窗口选择与 clock 对齐未设计；
- **多模态同窗**：本切片的验收只用 VLM 观测；ASR/OCR/BGE 与 VLM 混窗时的
  `fast_modalities`/`enrichment_modalities` 组合未被真实验收；
- **素材粒度策略**：固定栅格只是"先搭基座"的确定性选择；按内容切窗（场景变化、
  语音段边界）未做，也不在本切片假装做过；
- `golden_path_verified` 仍恒为 `false`（完整 Golden Path 还要求查询侧与回看侧的验收）。
