# ADR-021：模型 worker 按分级并发上限限流（准入、重试与账目）

**状态：** Accepted（2026-09-24）
**上游决策：** ADR-008（Apple Silicon 一等目标与内存分级"必须遵守"）、ADR-012（模型插件与端侧推理边界）、ADR-015（macOS 常驻形态与统一内存分级）、ADR-019（运行时消费分级队列上限）
**相关文档：** [实现状态](../implementation-status.md)、[验证记录](../verification.md)、[待办](../TODO.md)、[macOS 常驻手册](../runbooks/macos-resident.md)

---

## 1. 背景与问题

ADR-015 把"模型 worker 的并发预算"写进了统一内存分级表（`small` 档为 1，`large` 为 3），
ADR-019 收口了队列那一半（`queue_capacity` 越界即失败），但两份 ADR 都自认了**同一个缺口**：
`SENSORYPLEX_MODEL_PARALLELISM` 只有"已声明"这一层事实。本轮把它核实并收口。改动前的事实是：

| 事实 | 位置 |
| --- | --- |
| 变量只在 Rust 侧解析与**转述**：`ResidentLimits.model_parallelism` 的字段注释直接写着"当前没有任何 worker 读它，因此只能被报成'已声明'" | `crates/runtime/src/lib.rs` |
| `tools/ai_worker.py` 的输入处理是 `for job in jobs` 的**串行**循环，没有任何并发上限 | `tools/ai_worker.py` |
| 并发闸门其实**已经存在**，但在插件侧：在飞调用达到 `max_concurrency` 时返回 `RESOURCE_EXHAUSTED`、原因 `concurrency_limit`、`retryable=True` | `plugins/python/common/src/edge_material_sdk/processor.py` |
| worker 收到可重试拒绝时只往报告里记一行 `error` 就继续，那条输入既不算成功也不算失败 | `tools/ai_worker.py` |

于是有两条坏结局，而且都不会报错：

1. **"已按分级限流"是假事实。** 读文档的人以为 worker 已受分级约束，而实际并发路数由调用方式决定：
   串行调用时永远等于 1（与档位无关）；将来接上并发调用时又会毫无上限地冲进插件——而分级表正是
   为避免这件事写的（ADR-008 禁止 16 GiB 机型并行加载 ASR + OCR + VLM）。
2. **可重试拒绝静默消失。** 插件的 `concurrency_limit` / `deadline_expired` / `processing_timeout`
   都会把那条输入写一行 `error` 就丢掉，运行照样以 0 退出——账面上"提交了 4 条"，实际只产出 3 条观测。

## 2. 决策一：单位是**并发在飞的插件调用数**，`limit == 1` 与旧语义完全一致

分级表的措辞是"N 个 worker"，本切片把它落成 worker 侧的**在飞插件调用数**上限：
`ThreadPoolExecutor(max_workers=limit)` + 显式提交窗口，报告里的 `peak_in_flight` 是**实测峰值**，
不是配置值——`limit=4` 的报告只有在真的出现过 4 路在飞时才会写 4。

`limit == 1` 时提交窗口只有一个槽位，语义与改动前的 `for job in jobs` 串行**完全一致**，
所以**默认行为不变**：没有注入 `SENSORYPLEX_MODEL_PARALLELISM`（开发机上没有 `resident.env` 是常态）
时报 `not_injected` 并按 1 跑，不填一个"看起来合理"的默认值。

## 3. 决策二：请求值与分级上限是两件事，**运行时的上限是权威**

worker 把"这次想要几路"和"这一档允许几路"分开：

- **请求值**来自 `--model-parallelism`（flag）或 `SENSORYPLEX_MODEL_PARALLELISM`（环境变量）；
- **上限**来自运行时：`--runtime <addr>` 给出时先调 `DescribeCapabilities`，从
  `residency.model_parallelism` 读这一档的允许路数（ADR-015 §5 / ADR-019 §5 已把这条转述路径锁好）。

| 输入组合 | 结果 |
| --- | --- |
| flag 与 env 都在且**不相等** | `model_parallelism_conflict: env=<n> flag=<n>`：拒绝，exit 2。静默选一个等于把冲突藏起来 |
| 请求值 > 运行时转述的分级上限 | `model_parallelism_exceeds_tier_cap: requested=<n> tier_capacity=<n> tier=<name>`：拒绝，exit 2，**不夹取、不降级、不改写配置** |
| 只有请求值（无 `--runtime`） | 采用它，`source=flag` 或 `source=env`；`tier=not_checked`、`tier_capacity=null` |
| 只有运行时上限（没有请求值） | 采用它，`source=runtime` |
| 两者都没有 | `not_injected`，`limit=1`，`source=none` |
| 空串 / `0` / 非整数 | `invalid_resident_limit: <来源>`，exit 2 |
| `--runtime` 连不上 | `runtime_capabilities_unavailable:<CODE>`，exit 2 |

两条必须写死的语义：

- **连不上运行时不能退化成"没有上限"。** 那正好是这条链要禁止的静默降级，所以 `--runtime` 一旦给出
  就必须拿到答案，拿不到就拒绝。
- **报告里不出现"被夹到上限的成功"。** 越界的运行在**连插件之前**就退出，报告里只有
  `{"state": "rejected", "reason": "..."}`；只有通过准入的运行才有 `admitted` 的账目。
  同一组 `admit()` 判定也用在同一棵树上跑的双语对照（见 §6 的 `bad_resident_limit_parity`）：
  Rust 侧 `str::parse::<usize>()` 与 Python 侧刻意不用裸 `int()`（`int("３")` 会通过而 Rust 不认），
  两边对同一个坏值给出**同一个**原因串。

单位与字段的口径在报告里就叫 `model_concurrency`（不是 `model_parallelism`）：它记的是**执行账目**，
而 `model_parallelism` 是常驻注入的**预算声明**，两者不该混成一个名字。

## 4. 决策三：可重试拒绝不再静默消失

旧实现把插件的可重试拒绝当成单条终态失败。新语义：

- **每轮尝试都刷新 deadline。** 否则第 2 次会带着一条已经过期的 deadline 上去，插件的
  `deadline_expired`（可重试）会让"重试"退化成永远失败。
- **重试次数有上限**：`--max-attempts`（默认 3，含首次调用），退避指数增长并**封顶 1s**
  （`--retry-backoff-ms`，默认 100，0 表示不等待）。只有插件**自己声明** `retryable` 的拒绝才有下一次。
- **预算用尽才落终态**：`retry_exhausted:<原因>`，并计进 `exhausted`；不可重试的拒绝仍记
  `failed`；**成功但没有观测**（违反"没有 error 就至少有一条观测"这条契约）记 `failed` 且原因
  `empty_plugin_result`——否则这条输入在账目里既不算成功也不算失败。
- **账目按原因码分开记**：`throttle_events` 里 `concurrency_limit`（自己撞上插件闸门）与
  `deadline_expired`（时间预算）说明的东西不一样，合并成一个数字就把区别抹掉了。
- **报告按输入顺序回填**：并发执行打乱完成顺序，`frames` 与 `observations` 仍然按提交顺序写，
  这样既有断言（顺序相关）与人的阅读习惯都不受影响。

## 5. 本轮改动的落点

| 文件 | 改动 |
| --- | --- |
| `tools/model_limits.py` | **新增**：`parse_limit` / `tier_from_residency` / `admit` / `Admission`、`should_retry` / `retry_delay_ms`、`InFlightLedger`（在飞峰值、重试、终态归宿） |
| `tools/ai_worker.py` | 接入准入（在连插件之前）、`ThreadPoolExecutor` 限流、有界重试与退避、报告新增 `model_concurrency`；`--runtime` / `--model-parallelism` / `--max-attempts` / `--retry-backoff-ms` 四个参数 |
| `tests/contracts/test_model_limits.py` | **新增** 32 项：纯判定 + 契约形状替身（`ScriptedPlugin`、用 barrier 验跨线程峰值），不启真实模型 |
| `tools/verify_model_parallelism.py` | **新增** 真实验收脚本：多样本上游（真实 replay → 真实 OCR → worker），真起 `sensoryplex-runtime serve` 做运行时对账 |
| `Makefile` | 新增 `parallelism-check`（主机执行：HF 权重、CoreML EP 只存在于 macOS 主机，与 `*-check` 同类例外） |
| `crates/runtime/src/lib.rs` | **只改注释**（Rust 行为不变）：把"当前没有任何 worker 读它"改为指向本 ADR；Rust 侧仍然是转述，不参与判定 |

## 6. 验收证据（真机 M2 Max / 32 GiB / macOS，4 个真实授权样本）

命令：

```bash
make parallelism-check INPUTS=4 \
  MEDIA="/abs/…/screencast-video2commons.480p.vp9.webm \
         /abs/…/slides-vrt-nodiscussion.480p.vp9.webm \
         /abs/…/slides-vrt-discussion.480p.vp9.webm"
```

工作区 `/var/folders/.../sensoryplex-parallelism-1wbv1tv7`，上游批次是 **4 条真实观测**
（`obs_13bf4277…`、`obs_71d8cfcc…`、`obs_1dbea528…`、`obs_5a9cde84…`，来自
`screencast-video2commons` / `slides-vrt-nodiscussion` / `slides-vrt-discussion`），
每条链路都是**未改动**的 `tools/verify_ocr.py`（真实 replay → 真实 OCR 插件 → worker）。

| # | 场景 | 实测（逐字取自各 `*.json`） |
| --- | --- | --- |
| 1 | 未注入 | `state=not_injected limit=1 source=none tier=not_checked tier_capacity=null peak_in_flight=1 retries=0 throttle_events={}` |
| 2 | `--model-parallelism 1` | `state=admitted limit=1 source=flag peak_in_flight=1 retries=0`：与旧串行语义一致 |
| 3 | flag 全开（`=4`） | `state=admitted limit=4 source=flag peak_in_flight=4 attempts=10 completed=4 failed=0 exhausted=0 retries=6`，`throttle_events={"concurrency_limit": 6}` |
| 4 | env 全开（`=4`） | 与 #3 同形，只有 `source=env` 不同 |
| 5 | `--model-parallelism 2` | `limit=2 source=flag peak_in_flight=2 retries=1 throttle_events={"concurrency_limit": 1}` |
| 6 | env = `0` / 空串 / `abc` / flag = `abc` | `invalid_resident_limit: SENSORYPLEX_MODEL_PARALLELISM=0`、`… is set but empty`、`…=abc`、`invalid_resident_limit: --model-parallelism=abc`（各 exit 2，`model_concurrency.state=rejected`，一条输入都没跑） |
| 7 | env=3 且 flag=2 | `model_parallelism_conflict: env=3 flag=2` |
| 8 | `--runtime` 起的 `large` 档、无请求值 | `state=admitted limit=3 source=runtime tier=large tier_capacity=3 peak_in_flight=3 retries=3 throttle_events={"concurrency_limit": 3}` |
| 9 | 同一次运行里 flag=2 ≤ 档位上限 3 | `limit=2 source=flag tier=large tier_capacity=3 peak_in_flight=2`：请求值不被上限顶掉，但 **`tier`/`tier_capacity` 仍被带出** |
| 10 | 同一次运行里 flag=8 > 档位上限 3 | `model_parallelism_exceeds_tier_cap: requested=8 tier_capacity=3 tier=large`（拒绝，不夹取到 3） |
| 11 | 运行时转述 `medium`（上限 2）、无请求值 | `limit=2 source=runtime tier=medium tier_capacity=2 peak_in_flight=2` |
| 12 | `--runtime` 指向不可达端点 | `runtime_capabilities_unavailable:UNAVAILABLE` |
| 13 | 同一个坏值在 Rust 与 Python 两侧 | `bad_resident_limit_parity: rust==python: invalid_resident_limit: SENSORYPLEX_MODEL_PARALLELISM=abc` |
| 14 | `make check EXEC_MODE=container` | ruff 通过、契约 **230 passed**、集成 **31 passed**、cargo fmt/clippy（`-D warnings`）/test 全过 |
| 15 | `make check EXEC_MODE=host` | 同上（宿主 `DSN` 注入 `SENSORYPLEX_TEST_DATABASE_URL`），exit 0 |

`flag_all`（#3）报告里的关键细节：
`frames[].attempts = [1, 2, 3, 4]`、`drain={'discarded': 0, 'failures': [], 'leases': 0}`、
`runtime_stats_after={'leased': 0, 'leases': 0}`——**4 条输入全部产出观测、没有一条消失**。

**本轮拿到的关键新证据**：插件侧那道 `concurrency_limit` 闸门**真实发生**了（BGE 插件
`max_concurrency=1`，而 worker 侧并发 4），并且被重试吸收到 4/4 全产出。上一轮 ADR-019 写下
"未吃透"的那一点，至此有了实测。

## 7. 后果与仍未验证范围

- **已收口**：`SENSORYPLEX_MODEL_PARALLELISM` 从"只被声明与转述"变成**被模型 worker 消费**——
  它是准入上限，越界与坏值都让 worker 在连插件之前以 exit 2 失败；未注入时按既有串行语义跑并显式
  记 `not_injected`。ADR-015 §5/§7/§8 与 ADR-019 §4/§7 的这条缺口不再成立。
- **已收口（缺陷）**：可重试拒绝不再让输入静默消失——有上限的重试、退避、每轮刷新 deadline，
  预算用尽落 `retry_exhausted:<原因>`，并按原因码分开计数。
- **仍未验证**：`small` 档（16 GiB）与 Mac mini 各档位没有真机跑过（本轮全部证据来自
  `macos-aarch64` / M2 Max / 32 GiB 一台机器）；`linux-x86_64` 未验证；`xlarge` 档未跑。
- **仍未验证**：worker 的并发上限与**上游产帧**没有联合压测——`file-material` 回放按 `min_interval_ms=1000`
  抽帧，单样本只交付 1～2 帧，所以本轮 4 路并发的"压力"来自多给样本，不是单样本高吞吐。
  高帧率下的背压（上游满、worker 追不上）仍未验证。
- **仍未验证**：`retry_exhausted` 的路径只在契约测试里覆盖（替身插件），没有在真实插件上跑到预算耗尽。
- **仍未验证**：worker 仍是**验收脚本形态**（CLI），没有常驻服务、没有跨进程队列节流；
  `MODEL_PARALLELISM` 目前只约束单次 worker 进程内的在飞调用数，不约束"同时起几个 worker"。
- **不在本轮范围**：把 `model_parallelism` 接进运行时自己的调度（运行时目前只转述它，不跑模型）；
  ADR-012 里"运行时把加速后端记为不可用"的能力上报问题也未动。
- **既有现象（未修复，与本 ADR 无关）**：`tools/verify_ocr.py` 会把 `drain.discarded == 0`
  判成失败（"每一帧都被消费"在它那里是失败条件）；`video/samples/screencast-watchlist` 在
  1 秒抽帧下常产出 0 文本块。两者本轮都未触及，登记在此供后续单独处理。
- `golden_path_verified` 恒为 false，本 ADR 不改变这一结论。
