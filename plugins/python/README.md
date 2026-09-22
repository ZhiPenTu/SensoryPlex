# Python 插件开发

`common/` 发布 `edge_material_sdk`，与 Gateway 同属 uv workspace，插件只能依赖 SDK。
导入的消息来自 `edge_material_sdk.generated`，不从需求文档手写 RPC 数据模型。

实现 `ProcessorPlugin.describe()` 与异步 `process(request, cancel_token)`，入口通过
`invoke()` 执行 deadline、上下文、输入/输出契约、取消与并发准入检查。返回值为
`runtime.v1.ProcessResponse`，失败使用标准错误码。空的成功响应会被拒绝。

这只是 SDK building block，不会自动启动 gRPC worker 或装载模型。插件实现必须使用
稳定输入和版本派生输出 ID，并在 worker 中完成 durable 幂等、生命周期、lease 回收。
CPU/GPU buffer 映射尚未实现，此版本 SDK 对 buffer 输入返回明确不支持。

Manifest 检查：

```sh
uv run python tools/validate_plugin.py /path/to/plugin.yaml
```

Schema 覆盖开放插件规范的 V1 本地 gRPC 形态。此检查只做结构预检，不声称完成
SDK 版本协商、镜像签名/SBOM 验证、沙箱或外发策略执行。Remote/native ABI 等形态待后续实现。
不预置带伪造 digest 的可注册 AI 插件；模型目录和发布 artifact 在真实接入时创建。
