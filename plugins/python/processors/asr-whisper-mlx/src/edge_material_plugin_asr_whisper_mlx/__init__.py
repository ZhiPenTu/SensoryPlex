"""ASR 转写插件（org.sensoryplex.asr-whisper-mlx）。"""

from .plugin import PLUGIN_NAME, PLUGIN_VERSION, WhisperAsrPlugin, describe, validate_config

__all__ = [
    "PLUGIN_NAME",
    "PLUGIN_VERSION",
    "WhisperAsrPlugin",
    "describe",
    "validate_config",
]
