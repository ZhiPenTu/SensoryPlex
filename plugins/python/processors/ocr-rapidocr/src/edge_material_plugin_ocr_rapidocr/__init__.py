"""OCR 插件（org.sensoryplex.ocr-rapidocr）。"""

from .plugin import PLUGIN_NAME, PLUGIN_VERSION, OcrPlugin, describe, validate_config

__all__ = [
    "PLUGIN_NAME",
    "PLUGIN_VERSION",
    "OcrPlugin",
    "describe",
    "validate_config",
]
