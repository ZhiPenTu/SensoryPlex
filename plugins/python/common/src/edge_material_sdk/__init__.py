"""Public SDK. Cross-process messages are generated exclusively from proto/."""

from .processor import CancelToken, PluginError, ProcessorPlugin

__all__ = ["CancelToken", "PluginError", "ProcessorPlugin"]
