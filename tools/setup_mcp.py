"""Compatibility shim forwarding to tools.ops.setup_mcp."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.ops.setup_mcp as _target_module  # noqa: E402

sys.modules[__name__] = _target_module

if __name__ == "__main__":
    if hasattr(_target_module, "main"):
        sys.exit(_target_module.main())
    else:
        target_path = str(Path(__file__).parent / "ops" / "setup_mcp.py")
        runpy.run_path(target_path, run_name="__main__")
