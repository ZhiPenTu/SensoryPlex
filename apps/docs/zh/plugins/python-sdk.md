# Python SDK

Python SDK 位于 `plugins/python/common/`，发布 `edge_material_sdk`。它与 gateway 同属一个 uv workspace，而
**插件只允许依赖 SDK**，绝不允许依赖 `services/` 内部模块。

生成消息来自 `edge_material_sdk.generated`。不要照本文档手写 RPC 数据模型：真正的 `.proto` 文件是唯一的契约源。

## SDK 做什么、不做什么

它是一个**building block**：提供插件基类、buffer lease reader 与生成消息。

它**不会**替你启动 gRPC worker，也不会替你加载模型。插件要自己负责生命周期服务、持久幂等与 lease 回收。
worker 侧的可用参考实现是 `plugins/python/processors/vlm-moondream`（见 ADR-012）。

## 真正重要的两个方法

实现 `describe()` 与异步的 `process(request, cancel_token)`。入口经 `invoke()` 执行，它在调用你的代码之前
负责 deadline、请求上下文、输入/输出契约、取消与并发准入。

```python
from edge_material_sdk import Observation, ProcessorPlugin, ProcessResponse


class SubtitleNormalizer(ProcessorPlugin):
    def describe(self):
        return {
            "consumes": ["observation.asr_segment"],
            "produces": ["observation.asr_normalized"],
            "supports": {"cancellation": True, "retry": "idempotent"},
        }

    async def process(self, request, cancel_token):
        results = []
        for item in request.inputs:
            cancel_token.raise_if_cancelled()
            results.append(
                Observation.from_input(
                    item,
                    modality="asr_normalized",
                    payload={"text": normalize(item.payload["text"])},
                    confidence=item.confidence,
                    provenance=self.provenance(request),
                )
            )
        return ProcessResponse(observations=results)
```

这是结构示例——确切的 import 与生成类以 SDK 发布包为准。真正重要的是它演示的几件事：

- **结果继承输入的时间锚点与来源**，而不是自造新的；
- **检查取消**，因此被取消的请求会停下，而不是悄悄跑完；
- **输出带 provenance**；
- 同一个 `idempotency_key` 绝不产出互相矛盾的结果。

返回值是 `runtime.v1.ProcessResponse`；失败使用标准错误码。**空的成功响应会被拒绝**——“我什么都没找到”必须
表达为真实结果或有类型的失败，而不是一次静默空转。

## 从数据面读 buffer

插件只允许访问 `cpu_shared_memory`，并且**必须**经 `LeaseBufferReader`——`BufferDescriptor` 是地址，不是数据。

```python
from edge_material_sdk import LeaseBufferReader, ProcessorPlugin


class MyPlugin(ProcessorPlugin):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)  # buffer_reader 在 gRPC Start 时按 config 附加


# 在 gRPC 服务的 Start 处理里（数据面地址来自 Start(config) 的 handoff_endpoint）：
plugin.buffer_reader = LeaseBufferReader(config["handoff_endpoint"], ttl_ms=ttl_ms)
```

reader 走真实路径——gRPC `Acquire` → `shm_open` + `mmap` → 摘要校验 → `Release`——而在 `process()` 里，
你不消费的每一条都必须显式 `discard()`。数据面的要求是**每条保留都有归宿**。

失败路径都是显式的，绝不静默：

| 情形 | 结果 |
| --- | --- |
| 未挂 reader 却来了 buffer 输入 | `buffer_reader_not_attached` |
| descriptor 的内存类型不是 `cpu_shared_memory` | `unsupported_memory_kind:*` |
| 音频采样布局未知 | 显式拒绝 |
| 子段越出所属窗口 | 只标记，不夹取 |

音频字节严格按 descriptor 的 `sample_format` 解释。既没有默认采样宽度，也没有“按 4 字节/样本硬读”的兜底——
见 ADR-014。

::: warning 不要声明自己映射不了的内存类型
GPU 与统一内存映射**尚未实现**。消费字节的处理器声明 `cpu_shared_memory`；消费上游事实的处理器什么都不声明，
数据面也不会给它挂 lease。
:::

## 并发与准入

`tools/ai_worker.py` 用 `--model-parallelism` / `SENSORYPLEX_MODEL_PARALLELISM` 对照运行时转述的档位上限做准入
与在飞调用限流。坏值、来源冲突与越界在**连插件之前**就以退出码 `2` 结束——不夹取。`concurrency_limit` 与
`deadline_expired` 这类可重试拒绝不会让输入静默消失：报告记录实测 `peak_in_flight` 与重试账目（ADR-021）。

## 验收插件

```sh
make model-check MEDIA=/absolute/authorized-sample.mp4     # VLM（本机 ollama）
make asr-check   MEDIA=/absolute/authorized-speech.webm    # ASR（MLX Whisper）
make ocr-check   MEDIA=/absolute/authorized-video.webm     # OCR（随包 PP-OCR ONNX）
make embed-check MEDIA=/absolute/authorized-video.webm     # BGE 文本向量
make parallelism-check MEDIA="/abs/a.webm /abs/b.webm" INPUTS=4
```

两个参数值得知道：

- `make ocr-check EXPECT=empty` —— 在无文字样本上，“0 块 + 一个 `empty_reason`”就是**正确**结果。项目正是用
  它证明“模型没找到文字”与“处理失败”可区分；
- `PROVIDER=coreml` —— 走 CoreML 执行后端。拿不到 CoreML 会话就显式失败，**绝不静默退回 CPU**（ADR-016）。

这些验收各自都是四个进程（编排 / 生产者 runtime / 插件 / worker），并需要一份真实授权样本。
