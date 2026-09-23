"""BGE 文本向量插件（org.sensoryplex.embed-bge-onnx）。"""

from .plugin import PLUGIN_NAME, PLUGIN_VERSION, EmbedPlugin, describe, validate_config

__all__ = [
    "PLUGIN_NAME",
    "PLUGIN_VERSION",
    "EmbedPlugin",
    "describe",
    "validate_config",
]
