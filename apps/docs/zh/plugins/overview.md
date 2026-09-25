# 插件体系

插件是框架获得能力的方式，而设计前提是：插件是**跑在别人端侧硬件上的不可信第三方代码**。因此“开放”的边界
远在“直接加载任意代码”之前就停住了。

## 插件可以扩展什么

| `kind` | 扩展的环节 | 典型用途 |
| --- | --- | --- |
| `SourcePlugin` | 接入 | 新的容器格式、采集设备或流协议 |
| `ProcessorPlugin` | 感知 | OCR、ASR、VLM 描述、向量、归一化 |
| `SinkPlugin` | 导出 | 新的下游存储或通知目标 |
| `EnricherPlugin` | 事实增强 | 从既有事实派生新的观测 |
| `ExecutionBackendPlugin` | 硬件执行 | 供其它插件使用的厂商运行时（TensorRT、厂商 NPU SDK） |

## 插件绝不允许改变什么

- **时间语义。** 时间轴是同一 stream 上的 `[start_ms, end_ms)`。插件不能自造偏移，也不能把未知值夹取成
  合法区间。
- **血缘。** 每条观测都必须带产出它的发布身份，以及影响其输出语义的配置 hash。
- **置信度语义。** 未知就是未知，并且要说明原因——绝不用 `0` 或 `1` 顶替。
- **数据边界。** 不把原始 buffer、PCM 或 tensor 送上控制面，也不把密钥写进 manifest、镜像层、日志、观测或事件。
- **资源声明。** 它是准入与隔离依据，不是尽力而为的建议。

## 生命周期

```text
Discover → Validate → Register → Start → Ready
                                   │
                         Process / Cancel / Health
                                   │
                    Drain → Stop → Unregister
```

| 阶段 | 运行时期望 | 插件必须保证 |
| --- | --- | --- |
| `Describe` | 读取能力、协议与配置 schema | 不加载模型、无副作用、快速返回 |
| `ValidateConfig` | 部署前校验配置与依赖 | 字段级错误，不泄露密钥 |
| `Start` | 创建实例与受控资源 | 限时初始化；未 ready 不接收业务请求 |
| `Health` | 定期探测存活与可服务状态 | 区分 `healthy`、`degraded`、`unhealthy` |
| `Process` | 投递一个或一批带 deadline 的输入 | 幂等、可取消、结果可溯源 |
| `Cancel` | 取消尚未结束的请求 | 尽快停止；绝不回写成功结果 |
| `Drain` | 停止新任务，等待在途任务 | 在 grace period 内完成或明确中止 |
| `Stop` | 回收模型、句柄与临时资源 | 释放资源，不丢失已确认结果 |

## 错误码

错误是分类型的。捕获异常后返回空成功结果的插件是缺陷，不是韧性特性。

| 错误码 | 含义 | 运行时默认处理 |
| --- | --- | --- |
| `INVALID_INPUT` | 输入不满足声明的 format/schema | 标记失败，不重试 |
| `UNSUPPORTED_CAPABILITY` | 不支持该 modality、语言、模型或配置 | 重新路由或标记不支持 |
| `UNSUPPORTED_MEMORY_KIND` | 读不了这种 buffer 类型 | 复制/转换或重新路由 |
| `DEADLINE_EXCEEDED` | 在 deadline 前未完成 | 取消，再按策略重试/降级 |
| `RESOURCE_EXHAUSTED` | GPU、内存或队列资源不足 | 背压、排队或路由 |
| `TRANSIENT_BACKEND_FAILURE` | 可恢复的模型/设备失败 | 有界重试 |
| `DATA_POLICY_DENIED` | 隐私、地域或外发策略禁止 | 不重试；审计并告警 |
| `INTERNAL_PLUGIN_ERROR` | 插件内部错误 | 隔离实例、记录诊断、按策略重启 |

错误响应带安全的 `reason_code`，适用时还带可操作的 `retry_after_ms`。access token、原始对象 URL 与内部堆栈
绝不返回给上游。

## 投递语义

- **至少一次。** 同一个 `idempotency_key` 下，`Process` 必须返回语义相同的结果，或显式返回先前结果的引用。
- **输出身份由稳定输入派生**，绝不用当前时钟或随机数。
- **默认不保证顺序。** 需要严格单流顺序的模型要声明 `ordering: per_stream`，并说明随之而来的吞吐与恢复代价。
- **可重试与不可重试必须区分。** 临时显存不足可重试；输入格式违规不可。

## 资源与背压

manifest 里的 `resources` 是调度器的准入与隔离依据。若插件持续超过声明，运行时可以降低并发、暂停、迁移或
终止该实例。

无上限的内部队列被视为缺陷：插件必须上报队列深度、在飞、拒绝与 deadline miss。背压按顺序施加：

```text
队列水位上升
  → 降低可选采样 / 合并 batch
  → 暂停慢路径 enrichment
  → 降低非关键任务优先级
  → 返还 RESOURCE_EXHAUSTED / runtime.backpressure
  → 有策略地丢弃可重建的低价值任务
```

任何丢弃都有计数与原因码。直播接入与已确认的事实写入优先级高于高成本慢路径推理。

## 性能宣称

一个吞吐数字只有带上上下文才有意义：硬件与驱动、模型版本、量化方式、输入尺寸、流数量、并发、batch、
数据面内存类型、P50/P95/P99、预热状态与失败率。没有这些，“每秒 N 帧”和任何东西都不可比。

## 形态与签名

- `artifacts.form` 是 `container` 或 `local_native`（宿主常驻进程，例如 macOS 上的 `launchd` 服务）。
  随包的四个处理器当前都是 `local_native`。
- **v1 里插件未签名。** 没有签名时必须写明确的 `signatureUnavailableReason`；绝不用占位 digest 或占位签名字符串
  冒充签名。
- 签名/SBOM 目前只有结构预检；沙箱与外发策略的**执行**尚未实现。

## 继续阅读

- [打包与 Manifest](/zh/plugins/package-and-manifest)
- [Python SDK](/zh/plugins/python-sdk)
