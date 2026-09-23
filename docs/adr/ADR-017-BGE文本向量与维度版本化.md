# ADR-017：BGE 文本向量、维度版本化与"不接数据面"的下游插件

**状态：** Accepted（2026-09-24）
**上游决策：** ADR-008（Apple Silicon 一等目标）、ADR-010（跨进程数据面安全边界）、ADR-012（模型插件与端侧推理边界）、ADR-015（macOS 常驻形态与统一内存分级）、ADR-016（OCR 与 ONNX 执行后端）
**相关文档：** [实现状态](../implementation-status.md)、[验证记录](../verification.md)、[契约](../contracts/README.md)、[待办](../TODO.md)

---

## 1. 背景与问题

BGE 是本项目第四个端侧模型插件，也是第一个**消费上游观测而不是字节**的插件。它接在 OCR 后面：
把 `observation.ocr_blocks` 里的真实文字编码成向量。不能照抄 VLM/ASR/OCR 的三条先例，因为它在
五处不同：

1. **输入不是字节。** 帧、音频段是"要读的字节"，而文字块是"已经算出来的事实"。这意味着本插件
   **不挂数据面**：没有 `LeaseBufferReader`、没有 lease、没有 arena。于是出现一个新的语义问题——
   "这条路径本来就没有 lease"与"忘了归还 lease"在报告里长得一样，必须能被区分。
2. **维度是身份的一部分。** 同一段文字用 `dimension=512` 与另一版模型得到的是**两个不同的空间**。
   向量写进同一个 collection 会让相似度搜索给出无意义结果，而且**不会有任何报错**。
3. **`content_hash` 的含义变了。** 在上游插件里它等于"这段字节"的摘要（与 lease 窗口对账）；
   在这里最诚实的意思是"**实际被编码的那段文本**"的摘要。上游观测的身份不能因此被吞掉，
   必须另写一组 `input.*` 字段。
4. **相似度不是校准置信度**（沿用 ADR-016 §4）：0.7 的余弦相似度不是 70% 的把握，不能填进
   `confidence`，必须显式写"本插件不提供校准置信度"及原因。
5. **"没有文字"与"处理失败"必须分开。** 给空文本算一个向量，等于给"没有内容"编造语义。

目标平台仍是 ADR-008/015 定下的一等目标：Apple Silicon macOS（本轮验收机 M2 Max，与 OCR 同一台）。
本决策的落点是 `plugins/python/processors/embed-bge-onnx`，只承诺"链路语义、身份与维度版本化正确"，
**不承诺向量质量**，也不承诺 CoreML 加速（见 §5/§6/§8）。

## 2. 决策一：本插件不接数据面，并把"零 lease"写进对账

- manifest 声明 `acceptsMemoryKinds: []`、`network: none`、`writablePaths: []`：本插件不读字节、
  不落盘、不联网。权重由运营**预置**在 `model_dir`，插件自己从不下载。
- 拒绝路径在两处都存在且都在活进程上验过：SDK 层没有 reader 时是 `buffer_reader_not_attached`，
  插件自己收到 buffer 输入时是 `unsupported_input_kind:buffer`。**不会退化成"顺手读本地文件"**。
- worker（`tools/ai_worker.py`）支持契约的两条输入路径，并在报告里写明用的是哪条：
  - `buffer`：`--data-plane` + `--input-kind`（原有的 VLM/ASR/OCR 路径，有 lease）；
  - `observation`：`--input-observations <报告或数组>`（BGE 路径，**没有**数据面）。
  报告里 `input_mode` 明确写 `observation`，并且：
  - `drain = {"discarded": 0, "failures": [], "leases": 0}`
  - `runtime_stats_after = {"leased": 0, "leases": 0}`
  - **没有** `runtime_stats` 键（连一个数据面 RPC 都没发过）。

一个刻意的实现细节：worker 只在 `buffer` 模式下注入 `handoff_endpoint`。`observation` 模式的插件
配置 schema 是 `additionalProperties: false`，多塞一个它不认识的键会被它自己拒绝——这是好事，
说明"配置边界是插件说了算"（见 §7 的缺陷记录）。

## 3. 决策二：维度版本化——`dimension` 必须实测，并且是身份的一部分

- `dimension` 取自模型结构配置 `config.json` 的 `hidden_size`，并在 **Start 时跑一次真实前向探针**
  实测输出维度与之对账：不一致即拒绝启动（`model_dimension_mismatch:<declared>!=<measured>`）。
- payload 里写 `dimension_source = "config.json:hidden_size+probe_forward"`：读者能看到这个数字
  是"配置声明 + 实测确认"，不是从模型名猜的。
- `vector_index_key = material_text_<model_id slug>_d<dimension>_v1`，**维度来自实测值**而不是写死：
  换成 1024 维权重会得到另一个 key，"混索引"在结构上就不可能发生。
- 本切片**不落向量库**：payload 写 `storage = "inline_payload"`、`vector_ref = null`。这不是"忘了写
  引用"，而是把"没有库"这件事写清楚；Milvus 写入属于后续切片（见 §8）。

本机实测（2026-09-24，`onnxruntime 1.30.0` + `tokenizers`，任何第三方可用同一批文件复算）：

| 角色 | 文件 | 字节 | SHA-256 |
| --- | --- | --- | --- |
| encoder | `onnx/model_quantized.onnx` | 24 010 842 | `15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc` |
| tokenizer | `tokenizer.json` | 439 125 | `48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26` |
| model_config | `config.json` | 716 | `d4193ead3a810fd694fa8a31d7fc72fbaebc0668b603e398734bf2f6538ff42f` |
| **组合** | — | — | `1d01788f181300ac127dd2d323fbb234b560936aa991c8b22f4e3462aa117271` |

维度 = `512`（`config.json:hidden_size`，并被前向探针实测确认）。`release_id` =
`bge:bge-small-zh-v1.5@1d01788f1813`（组合摘要前 12 位）。身份**不含 `model_dir` 路径**：位置是配置，
身份是字节。

## 4. 决策三：`content_hash` = 实际被编码文本的摘要，上游身份另写 `input.*`

- 抽取规则只认一种形态：上游观测的 `payload.blocks[].text`；modality 不是 `ocr_blocks` 就显式拒绝
  （`unsupported_input_modality:<actual>`），不做"尽力而为"的猜测。
- 拼接规则写进 payload，读者不必反推：`join_separator = "\n"`、`block_count`、`blank_blocks`
  （空块被跳过并计数）、`char_count`、`source_modality`。
- **边界是失败而不是截断**：块数 > `MAX_TEXTS`(256) → `input_block_count_exceeds_bound`；
  拼接后 > `MAX_TOTAL_CHARS`(4096) → `input_text_exceeds_bound`；一段文字都没有 → `input_text_empty`。
- `content_hash` 与 payload 的 `text_sha256` 是同一个值，且**可被任何持有该文本的人复算**；
  验收脚本按同一条规则**独立重拼**一遍再比对，不采信插件自己的说法。
- 上游观测的身份与锚点另写 `input.*`：`observation_id` / `modality` / `content_hash` / `stream_id` /
  `source_item_id` / `quality_state` / `time_range` / `model_release_id`。
- 一个刻意的结果：**同一段文字来自不同帧时 `content_hash` 相同、`observation_id` 不同**
  （ID 的种子含上游 observation id、锚点、模型 release 与配置摘要）。"同一段字"与"同一次识别"
  是两件事，不能因为文字相同就把观测合并。

## 5. 决策四：池化与归一化写进结果；相似度不是置信度

- 池化 = CLS（`last_hidden_state[:, 0]`）、归一化 = L2，两者都写进 payload
  （`pooling = "cls"`、`normalize = "l2"`），不让读者猜。
- `norm` 实测 `1.0`；`vector_sha256` 是**向量 float32 小端字节**的 SHA-256，与 JSON 里的十进制
  表示无关，可被独立复算（验收脚本用自己的实现复算）。
- `confidence` 缺省，并写 `confidence_unavailable_reason =
  embedding_similarity_is_not_calibrated_confidence`。
- 质量只做**合理性**检查，不做基准：本机实测 `cos("今天天气不错", "明天天气很好") = 0.8094`，
  而同句与无关句的相似度为 0.2745（端侧推理在本地运行）与 0.4988（股票市场今日下跌）。
  这说明向量空间**顺序合理**，**不说明**检索质量达标——没有召回/排序基准（见 §8）。

## 6. 决策五：执行后端必须真的被选中；CoreML 与 Metal 的边界

- 配置 `provider: cpu | coreml`。请求 `coreml` 时会话的**首选** provider 必须是
  `CoreMLExecutionProvider`，否则拒绝启动（`execution_provider_not_selected:<actual>`），
  CPU 只作为后备出现在列表后面。
- 请求值走**显式入参**而不是从可变状态反推：ADR-016 缺陷 1（"请求 CoreML 被拿默认 CPU 比对"）
  不重犯。
- 本机实测（M2 Max，同一份权重，短文本 5 条 + 链路内 2 条 observation）：

| 口径 | cpu | coreml |
| --- | --- | --- |
| 单条短文本编码 | 0.78 ms | 3.16 ms |
| 链路内单条 observation（含分词/池化） | 1.03–1.26 ms | 5.01–9.19 ms |
| session providers | `['CPUExecutionProvider']` | `['CoreMLExecutionProvider', 'CPUExecutionProvider']` |
| 同文本两次编码 | 向量摘要相同（确定性） | 向量摘要相同（确定性） |
| 跨 provider 一致性 | — | `cos ≈ 1.0`；最长文本 0.996986（`max|Δ| = 1.08e-2`） |

  结论与 OCR 一致：`coreml` 是**可选择且可观测**的后端，在这个量化模型上**更慢**（约 4–8 倍），
  因此默认仍是 `cpu`，且本 ADR **不宣称 CoreML 加速**。跨 provider 的微小差异来自量化算子在不同
  EP 上的分区/求值顺序，同 provider 内则是确定的。
- **Metal：本 ADR 不引入 `metal` 后端。** ONNX Runtime 在 macOS 上没有独立的 Metal EP
  （ADR-016 §5 已定）；Apple GPU 在本项目里是**间接**使用的：ONNX 路径走 CoreML EP，
  ASR 走 MLX（原生 Metal），VLM 走 ollama。
- **运行时（Rust）侧的能力表仍然把加速后端记为"不可用"**，这是事实而不是遗漏：`crates/runtime`
  本版本没有任何 in-process `ExecutionBackend` 实现，`model_inference` 仍在
  `unavailable_capabilities` 里。因此**不得**把这张表读成"CoreML 不可用"——它表达的是
  "运行时自己不做推理"。把后端能力上报做得更精确（按后端给出不同原因、而不是一句话）仍是
  未完成项，见 §8。

## 7. 本轮暴露并修掉的真实缺陷（不是"一次就过"）

| # | 现象 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | 用**真实** HF 权重目录启动时 `model_file_not_found` | 权重目录不是平的：Hugging Face 快照把 ONNX 放在 `onnx/` 子目录，`tokenizer.json`/`config.json` 在快照根。插件原来只把 `model_file` 当文件名拼在 `model_dir` 下 | 允许 `model_file` 是 `model_dir` 内的**相对路径**，同时拒绝绝对路径与 `..` 越界（`model_file_must_be_relative` / `model_file_outside_model_dir`）；契约测试覆盖子目录与三种越界 |
| 2 | worker 用 `--input-observations` 时插件拒绝配置 | observation 路径的插件配置没有数据面，worker 却仍注入 `handoff_endpoint`（以及 `endpoint`/`model` 默认键），被插件 schema 的 `additionalProperties: false` 拒绝 | worker 只在 `buffer` 模式注入 `handoff_endpoint`，并按输入路径决定是否填模型服务默认键 |
| 3 | 验收脚本误判两次：`token_count` 与 `input.time_range` | payload 是 protobuf `Struct`：(a) 所有数字在 JSON 里都是 double，脚本却要求 `isinstance(int)`；(b) `Struct` 的键**原样保留 snake_case**，而真实 proto 字段经 `MessageToDict` 变成 camelCase，脚本直接比较两个 dict 必然不等 | 脚本按"整数性"而不是类型比较；`input.time_range` 显式按键取值比较 |

缺陷 1 值得单独记一笔：**它只在真实权重目录上才会暴露**——用测试自造的平目录永远跑不出来。

## 8. 后果与仍未验证范围

- 交付物：`plugins/python/processors/embed-bge-onnx`（插件包，含 `config.schema.json`、`plugin.yaml`、
  `sbom.cdx.json`）、`tools/verify_embed.py`（四角色验收：编排 / 真实 OCR 链路 / BGE 插件进程 / worker）、
  `tools/ai_worker.py` 的 observation 输入路径、`Makefile` 的 `embed-check`、契约测试 **42 项**、
  根 `pyproject.toml` / `uv.lock` 的工作区登记。
- 验收：`make embed-check MEDIA=... [PROVIDER=cpu|coreml]`。它会**先跑一遍真实 OCR 链路**产出
  `ocr_blocks`，再让 BGE 插件消费它；`OBSERVATIONS=<既有 ai-worker.json>` 可复用上游报告跳过重跑。
  本项与 `cargo` 一样固定在**主机**执行：权重来自主机 HF 缓存，CoreML EP 也只在 macOS 主机上存在，
  compose 容器里两者都没有（`.env` 只挂了 `MEDIA_DIR`）——不做"容器里假装跑过"。
- **仍未验证（不得当作完成）：**
  - 向量**质量**：没有检索/排序基准（召回、MRR），没有中文长文本、跨语言或领域文本评测；
    仓库登记样本里的屏幕文字是拉丁/俄文，中文界面样本尚未覆盖；§5 只证明排序合理。
  - 向量库：本切片不落库（`storage = inline_payload`）；`vector_index_key` 只**命名**了 collection，
    Milvus 建索引、写入与检索都没有验证。
  - 维度版本化的**迁移**：换模型或换维度后旧向量是重建还是并存，尚未决策。
  - CoreML 的实际收益：见 §6，实测更慢；动态 shape 与量化算子的分区回退未解决。
  - 运行时加速后端的能力上报仍把加速后端记为不可用（§6）；`metal` 在 ONNX 路径上不存在。
  - `linux-x86_64`、Mac mini / 跨机均未验证；插件仍未签名（只在 manifest 写明白原因），
    SBOM 只有结构预检；"绝不联网"只有 manifest 声明（`network: none`、`writablePaths: []`），
    没有 DNS/egress 层的强制执行。
  - worker 的 durable 幂等与 lease 崩溃回收仍未做；observation 路径没有 lease，但这不改变
    buffer 路径的结论（ADR-012 §7）。
