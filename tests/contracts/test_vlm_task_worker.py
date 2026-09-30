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


def test_decode_anchor_falls_back_when_start_ms_is_at_stream_boundary(monkeypatch):
    import subprocess
    from pathlib import Path

    from edge_material_plugin_vlm_moondream import slow_consumer as consumer

    from tools.vlm_task_worker import decode_anchor

    calls = []

    def mock_decode(media, start_ms, timeout):
        calls.append(("primary", start_ms))
        raise consumer.SlowConsumerError("vlm_consumer_anchor_decode_failed")

    def mock_run(args, **kwargs):
        calls.append(("subprocess", args))
        png = b"\x89PNG\r\n\x1a\nfake_png"
        return subprocess.CompletedProcess(args, 0, stdout=png, stderr=b"")

    monkeypatch.setattr(consumer, "_decode_anchor", mock_decode)
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = decode_anchor(Path("/fake/media.mp4"), 717000, 30)
    assert result == b"\x89PNG\r\n\x1a\nfake_png"
    assert calls[0] == ("primary", 717000)
    assert calls[1][0] == "subprocess"
    assert "-ss" in calls[1][1] and "716.900" in calls[1][1]
