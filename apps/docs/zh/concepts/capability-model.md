# 能力模型

多数媒体框架的失败是静默的：模型缺失就产出零结果，而零结果看起来就像一次成功的空查询。SensoryPlex 把
每个能力问题都当作**需要上报的事实**，而不是可以有默认兜底的东西。

## 两个不同的问题

运行时回答两个绝不允许混在一起的问题：

| 字段 | 它回答的问题 |
| --- | --- |
| `backends` | **本进程**能不能用这个后端执行推理？ |
| `host_accelerators` | **这台宿主**物理上有没有这块加速器？ |

Rust 进程今天没有任何 in-process `ExecutionBackend`，所以 `model_inference` 恒在
`unavailable_capabilities` 里——而同一台宿主仍可能如实报告 Metal 或 CUDA 加速器可用。因为“没有执行后端”
就报“没有加速器”，是反方向的另一句假话。

## 平台标识

能力报告带 `<os>-<arch>` 形式的平台标识，例如 `macos-aarch64` 或 `linux-x86_64`。任何性能或抽样结论都必须
带同一个标识，不同平台的结果绝不合并成一条统计。

## 未知保持未知

探测失败时，`runtime_version`、`precisions`、`max_concurrency` 与宿主内存一律留空或用 0。用猜测值或
“合理的默认容量”去填是被禁止的——那正是伪造的容量最终进入容量规划的方式。

## 加速器是三态

| 状态 | 含义 |
| --- | --- |
| `ACCELERATOR_STATE_AVAILABLE` | 宿主有，且**不允许**附原因 |
| `ACCELERATOR_STATE_UNAVAILABLE` | 宿主确实没有，且必填原因 |
| `ACCELERATOR_STATE_UNKNOWN` | 探测不出来——工具缺失、超时、输出读不懂 |

`UNKNOWN` 绝不允许塌陷成 `UNAVAILABLE`。找不到 `nvidia-smi` 不是“这台机器没有 GPU”的证据，只是“我们没
查出来”的证据。`UNKNOWN` 条目带 `probe_*:<source>` 原因，`evidence` 里只放真读到的值——绝不放文件系统
路径。

## 内存类型是准入输入

`admitted_memory_kinds` 是该平台允许准入的内存类型集合，媒体准入不得超过它。Apple Silicon 额外允许零拷贝
`unified_memory`；其它平台只有 `cpu_shared_memory`。插件声明自己接受什么，而且**不得**声明自己实际映射
不了的种类——GPU 与统一内存映射尚未实现，因此处理器插件声明 `cpu_shared_memory`（如果它消费的是上游事实
而不是字节，则什么都不声明）。

## 失败按发生位置分类

| 信号 | 含义 |
| --- | --- |
| `501` | 该能力在本构建里没有实现 |
| `503` | 能力存在，但依赖未配置、不可达或已失败 |
| `502` | 检索面答了，但答案不符合契约 |

`retryable` 是独立标记，不是状态码的同义词：令牌不对导致的 `503` 是配置错误，**不可重试**。调用方必须
两个都读。

## 唯一不允许漂移的标志

`ReplayReport.golden_path_verified` 默认为——且当前保持为——`false`。只有真实授权样本走完完整验收路径才允许
置真。未实现的能力必须出现在 `blockers` 中，且报告永不包含媒体的文件系统路径。

## 怎么验证这些说法

```sh
make capability-check      # 不可用原因 + 宿主加速器三态
make accelerator-check     # 加速器探测的四路对账
make gateway-smoke         # 经网关的能力上报
```

## 继续阅读

- [契约与代码生成](/zh/architecture/contracts) —— 这些字段定义在哪。
- [能力实现状态](/zh/reference/status) —— 各领域当前已验证的真实状态。
