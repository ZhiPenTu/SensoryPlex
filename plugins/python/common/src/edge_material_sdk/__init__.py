"""公共 SDK。跨进程消息仅由 proto/ 生成。"""

from .processor import CancelToken, PluginError, ProcessorPlugin

__all__ = ["CancelToken", "PluginError", "ProcessorPlugin"]
