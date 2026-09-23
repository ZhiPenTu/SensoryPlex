"""VLM 场景描述插件（org.sensoryplex.vlm-moondream）。"""

from .plugin import PLUGIN_NAME, PLUGIN_VERSION, VisionVlmPlugin, describe, validate_config

__all__ = [
    "PLUGIN_NAME",
    "PLUGIN_VERSION",
    "VisionVlmPlugin",
    "describe",
    "validate_config",
]
