# Python 插件开发

`common/` 发布 `edge_material_sdk`，与 Gateway 同属 uv workspace，插件只能依赖 SDK。
导入的消息来自 `edge_material_sdk.generated`，不从需求文档手写 RPC 数据模型。

实现 `ProcessorPlugin.describe()` 与异步 `process(request, cancel_token)`，入口通过
`invoke()` 执行 deadline、上下文、输入/输出契约、取消与并发准入检查。返回值为
`runtime.v1.ProcessResponse`，失败使用标准错误码。空的成功响应会被拒绝。

这只是 SDK building block，不会自动启动 gRPC worker 或装载模型。插件实现必须使用
稳定输入和版本派生输出 ID，并在 worker 中完成 durable 幂等、生命周期、lease 回收。

## buffer 输入

插件只能访问 `cpu_shared_memory`，且 **必须** 经 `edge_material_sdk.LeaseBufferReader`
读字节，不能直接拿 descriptor 当数据：

```python
from edge_material_sdk import LeaseBufferReader, ProcessorPlugin


class MyPlugin(ProcessorPlugin):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)  # buffer_reader 在 gRPC Start 时按 config 附加


# gRPC 服务的 Start 处理里（数据面地址来自 Start(config) 的 handoff_endpoint）：
plugin.buffer_reader = LeaseBufferReader(config["handoff_endpoint"], ttl_ms=ttl_ms)
```

reader 走真实路径（gRPC Acquire → `shm_open`+`mmap` → 摘要校验 → Release），并在
`process()` 里对不消费的条目显式 `discard()`——数据面要求每条保留都有归宿。SDK 未挂
reader 时对 buffer 输入返回 `buffer_reader_not_attached`；非 `cpu_shared_memory` 的
descriptor 返回 `unsupported_memory_kind:*`；两者都不静默跳过。GPU / unified memory
映射尚未实现，因此插件**不得**声明这些 kind。完整工作样例见
`plugins/python/processors/vlm-moondream`，边界见 [ADR-012](../../docs/adr/ADR-012-模型插件与端侧推理边界.md)。

Manifest 检查：

```sh
uv run python tools/validate_plugin.py /path/to/plugin.yaml
```

Schema 覆盖开放插件规范的 V1 本地 gRPC 形态。此检查只做结构预检，不声称完成
SDK 版本协商、镜像签名/SBOM 验证、沙箱或外发策略执行。Remote/native ABI 等形态待后续实现。
不预置带伪造 digest 的可注册 AI 插件；模型目录和发布 artifact 在真实接入时创建。
