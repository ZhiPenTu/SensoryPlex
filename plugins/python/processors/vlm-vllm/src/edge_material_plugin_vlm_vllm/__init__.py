"""VLM 远程场景描述插件（org.sensoryplex.vlm-vllm）。"""

from .plugin import (
    PLUGIN_NAME,
    PLUGIN_VERSION,
    VisionVllmPlugin,
    VllmConfig,
    describe,
    normalize_base_url,
    validate_config,
)

__all__ = [
    "PLUGIN_NAME",
    "PLUGIN_VERSION",
    "VisionVllmPlugin",
    "VllmConfig",
    "describe",
    "normalize_base_url",
    "validate_config",
]
