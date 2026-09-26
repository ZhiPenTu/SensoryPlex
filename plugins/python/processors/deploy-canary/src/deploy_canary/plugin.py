"""自检探针的插件声明与配置校验：只有生命周期契约，没有任何业务处理。"""

from __future__ import annotations

from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime

PLUGIN_ID = "org.sensoryplex.deploy-canary"
PLUGIN_VERSION = "0.1.0"
SELF_CHECK_SECRET_FIELD = "batch_size"


def describe(artifact_digest: str = "", *, plugin_id: str = PLUGIN_ID) -> runtime.PluginDescription:
    return runtime.PluginDescription(
        name=plugin_id,
        version=PLUGIN_VERSION,
        protocol="v1",
        artifact_digest=artifact_digest,
    )


def validate_config(config: dict) -> runtime.ValidationResult:
    """`batch_size` 必须是正整数；`scenario` 只是场景标签，不改变插件行为。

    数值经 protobuf `Struct` 往返后会变成浮点（`1` → `1.0`），所以这里按"整数值"判定，
    而不是按 Python 的 `int` 类型判定 —— 否则合法配置会在真实链路上被判非法。
    """
    errors = []
    value = config.get(SELF_CHECK_SECRET_FIELD, 1)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append("batch_size_not_integer")
    elif float(value) != int(float(value)):
        errors.append("batch_size_not_integer")
    elif float(value) < 1:
        errors.append("batch_size_must_be_positive")
    return runtime.ValidationResult(valid=not errors, field_errors=errors)
