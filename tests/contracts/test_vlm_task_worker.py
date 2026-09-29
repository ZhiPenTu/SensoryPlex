"""宿主准入规则：可回收缓存可用，wired/compressed 不得计入余量。"""

import pytest

from tools.vlm_task_worker import macos_available_memory


def test_cached_memory_does_not_permanently_starve_the_queue():
    output = """Pages free: 100.
Pages inactive: 50000.
Pages speculative: 50.
Pages wired down: 9999999.
Pages occupied by compressor: 9999999.
"""
    assert macos_available_memory(output, 16384) == 50150 * 16384


def test_missing_memory_probe_is_not_unlimited_capacity():
    with pytest.raises(RuntimeError, match="vlm_consumer_memory_probe_unavailable"):
        macos_available_memory("Pages free: 10.", 16384)
