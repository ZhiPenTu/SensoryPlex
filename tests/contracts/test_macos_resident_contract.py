"""M5 常驻形态的契约：统一内存分级、plist 渲染与 media 作业的上限注入。

这些用例不安装任何 launchd job（那属于真机验收的一部分），但分级边界、渲染严格性、
`resident.env` 的内容与包装脚本的拒绝语义都是**真实代码路径**：
用例里的数字必须能和 `crates/media/src/handoff.rs`、插件 manifest 对上。
"""

import pathlib
import plistlib
import stat
import subprocess
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import macos_resident as resident  # noqa: E402

WRAPPER = ROOT / "deploy/macos/bin/sensoryplex-media-run"
HANDOFF_RS = ROOT / "crates/media/src/handoff.rs"


def fake_tier(name: str = "large") -> resident.Tier:
    for tier in resident.TIERS:
        if tier.name == name:
            return tier
    raise AssertionError(f"no tier {name}")


def measured_probe(memory_bytes: int = 34359738368) -> resident.MemoryProbe:
    return resident.MemoryProbe(memory_bytes, "sysctl", "sysctl -n hw.memsize")


class Completed:
    """替身：只保留被测代码真正读的两个字段。"""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def test_tier_boundaries_are_explicit_and_never_snap():
    def tier_name(gib: float) -> str | None:
        tier = resident.select_tier(int(gib * resident.GIB))
        return None if tier is None else tier.name

    # 低于最低档必须是"不能常驻"，不是"取最近一档"。
    assert tier_name(8) is None
    assert tier_name(15.9) is None
    assert tier_name(16) == "small"
    assert tier_name(23.9) == "small"
    assert tier_name(24) == "medium"
    assert tier_name(31.9) == "medium"
    assert tier_name(32) == "large"
    assert tier_name(63.9) == "large"
    assert tier_name(64) == "xlarge"
    assert tier_name(512) == "xlarge"
    assert resident.select_tier(None) is None


def test_tier_table_is_contiguous_and_anchored_to_the_rust_defaults():
    rust = HANDOFF_RS.read_text(encoding="utf-8")

    def rust_constant(name: str) -> int:
        for line in rust.splitlines():
            if line.startswith(f"pub const {name}: usize ="):
                raw = line.split("=", 1)[1].strip().rstrip(";")
                return int(raw.replace("_", "").split("*")[0].strip()) * (
                    64 * 1024 * 1024 if "*" in raw else 1
                )
        raise AssertionError(f"{name} not found in handoff.rs")

    default_retained = rust_constant("DEFAULT_RETAINED_LIMIT")
    max_retained = rust_constant("MAX_RETAINED_LIMIT")
    medium = fake_tier("medium")
    # 锚点：medium 档就是今天的默认值，安装它不改变现有行为。
    assert medium.handoff_retained_limit == default_retained
    assert medium.handoff_arena_bytes == 64 * resident.MIB
    for tier in resident.TIERS:
        assert 0 < tier.handoff_retained_limit <= max_retained
        assert tier.handoff_retained_limit >= 2
        assert tier.handoff_arena_bytes > 0
        assert tier.model_parallelism >= 1
        assert tier.media_queue_capacity >= 1
    # 档位必须连续：上一档的上界就是下一档的下界，避免出现"没有档"的空隙。
    for lower, upper in zip(resident.TIERS, resident.TIERS[1:], strict=False):
        assert lower.max_bytes == upper.min_bytes
    limits = [tier.handoff_retained_limit for tier in resident.TIERS]
    arenas = [tier.handoff_arena_bytes for tier in resident.TIERS]
    assert limits == sorted(limits)
    assert arenas == sorted(arenas)


def test_probe_prefers_host_measurement_and_labels_the_source():
    measured = resident.probe_memory(
        env={}, run=lambda *_, **__: Completed("34359738368\n"), system="Darwin"
    )
    assert (measured.memory_bytes, measured.source) == (34359738368, "sysctl")

    declared = resident.probe_memory(
        env={resident.MEMORY_ENV: "17179869184"},
        run=lambda *_, **__: Completed(returncode=1),
        system="Darwin",
    )
    assert (declared.memory_bytes, declared.source) == (17179869184, "env")
    assert resident.MEMORY_ENV in declared.detail

    missing = resident.probe_memory(
        env={}, run=lambda *_, **__: Completed(returncode=1), system="Darwin"
    )
    assert (missing.memory_bytes, missing.source) == (None, "unavailable")

    # 声明值必须真是正整数：垃圾值不能被静默忽略，也不能被当成 0。
    with pytest.raises(ValueError):
        resident.probe_memory(
            env={resident.MEMORY_ENV: "32gb"},
            run=lambda *_, **__: Completed(returncode=1),
            system="Darwin",
        )


def test_unsupported_reason_names_the_reason_not_a_fallback():
    too_small = resident.probe_memory(
        env={}, run=lambda *_, **__: Completed("8589934592"), system="Darwin"
    )
    assert resident.select_tier(too_small.memory_bytes) is None
    reason = resident.unsupported_reason(too_small)
    assert "8.0 GiB" in reason
    assert "不猜" in reason
    assert "探测不可用" in resident.unsupported_reason(
        resident.MemoryProbe(None, "unavailable", "sysctl 退出码 1")
    )


def test_templates_render_strictly_and_stay_valid_plists():
    probe = measured_probe()
    paths = resident.ResidentPaths(home=pathlib.Path("/tmp/sensoryplex-test-home"))
    values = resident.plist_values(
        fake_tier(), probe, paths, pathlib.Path("/bin/echo"), resident.DEFAULT_RUNTIME_ADDR
    )
    runtime = plistlib.loads(
        resident.render_plist(
            ROOT / "deploy/macos/launchd/org.sensoryplex.runtime.plist.template", values
        )
    )
    assert runtime["Label"] == resident.RUNTIME_LABEL
    assert runtime["ProgramArguments"][-1] == "serve"
    assert runtime["RunAtLoad"] is True
    assert runtime["KeepAlive"] == {"SuccessfulExit": False}
    tier = fake_tier()
    assert runtime["EnvironmentVariables"]["SENSORYPLEX_HANDOFF_RETAINED_LIMIT"] == str(
        tier.handoff_retained_limit
    )
    # 运行时从环境变量读分级（ADR-019），所以 plist 必须把分级上限一并注入；
    # 否则常驻进程只能报"未注入"，与安装时的分级脱钩。
    assert runtime["EnvironmentVariables"]["SENSORYPLEX_MEDIA_QUEUE_CAPACITY"] == str(
        tier.media_queue_capacity
    )
    assert runtime["EnvironmentVariables"]["SENSORYPLEX_MODEL_PARALLELISM"] == str(
        tier.model_parallelism
    )
    assert runtime["EnvironmentVariables"]["SENSORYPLEX_MEMORY_SOURCE"] == "sysctl"
    assert runtime["StandardOutPath"].endswith("runtime.out.log")

    caffeinate = plistlib.loads(
        resident.render_plist(
            ROOT / "deploy/macos/launchd/org.sensoryplex.caffeinate.plist.template", values
        )
    )
    # -s 只在交流电下阻止系统睡眠；不带 -d，屏幕仍可熄灭。
    assert caffeinate["ProgramArguments"] == ["/usr/bin/caffeinate", "-ims"]
    assert caffeinate["KeepAlive"] is True

    with pytest.raises(ValueError, match="占位符"):
        resident.render_template("<plist>{{MISSING}}</plist>", {})
    # 小写/未知形态的花括号不是本工具的占位符，但渲染后仍留下花括号同样必须失败。
    with pytest.raises(ValueError, match="花括号"):
        resident.render_template("{{TIER}} {{tier}}", {"TIER": "large"})


def test_resident_env_carries_tier_limits_and_memory_source():
    memory = 34359738368
    tier = fake_tier()
    lines = resident.resident_env_text(
        tier, measured_probe(memory), resident.DEFAULT_RUNTIME_ADDR
    ).splitlines()
    values = {key: value for key, value in (line.split("=", 1) for line in lines if "=" in line)}
    assert values["SENSORYPLEX_RESIDENT_TIER"] == tier.name
    assert values["SENSORYPLEX_HANDOFF_RETAINED_LIMIT"] == str(tier.handoff_retained_limit)
    assert values["SENSORYPLEX_HANDOFF_ARENA_BYTES"] == str(tier.handoff_arena_bytes)
    assert values["SENSORYPLEX_MEDIA_QUEUE_CAPACITY"] == str(tier.media_queue_capacity)
    assert values["SENSORYPLEX_MODEL_PARALLELISM"] == str(tier.model_parallelism)
    assert values["SENSORYPLEX_MODEL_BUDGET_BYTES"] == str(memory // 3)
    assert values["SENSORYPLEX_MEMORY_SOURCE"] == "sysctl"
    assert values["SENSORYPLEX_UNIFIED_MEMORY_BYTES"] == str(memory)


def test_declared_plugin_memory_comes_from_the_manifests():
    declared = resident.declared_plugin_memory()
    assert declared["asr-whisper-mlx"] == 4 * resident.GIB
    assert declared["vlm-moondream"] == 1 * resident.GIB
    assert resident.parse_size("4Gi") == 4 * resident.GIB
    assert resident.parse_size("512Mi") == 512 * resident.MIB
    with pytest.raises(ValueError):
        resident.parse_size("4GB")


def test_pipeline_queue_capacity_is_reported_not_rewritten():
    capacities = resident.pipeline_queue_capacities()
    assert capacities, "至少应读到 file-material.yaml 与 srt-live.yaml"
    assert set(capacities.values()) == {32}
    # 工具只报告口径，不替调用方改写配置文件。
    text = (ROOT / "config/pipelines/file-material.yaml").read_text(encoding="utf-8")
    assert text.count("queue_capacity: 32") == 1
    # 报告的措辞必须说明上限在运行时是**准入条件**，而不是一句"已按分级调整"。
    lines = resident.summarize(fake_tier(), measured_probe(), resident.DEFAULT_RUNTIME_ADDR)
    assert any("运行时按分级上限准入" in line for line in lines)


def write_wrapper_fixture(tmp_path: pathlib.Path, tier: resident.Tier) -> dict[str, str]:
    env_file = tmp_path / "resident.env"
    env_file.write_text(
        resident.resident_env_text(tier, measured_probe(), resident.DEFAULT_RUNTIME_ADDR),
        encoding="utf-8",
    )
    runtime = tmp_path / "fake-runtime"
    # 既回显参数（检查保留窗口注入），也回显运行时真正读的那三个环境变量（ADR-019）。
    runtime.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do printf \'%s\\n\' "$a"; done\n'
        "printf 'tier=%s queue_capacity=%s parallelism=%s\\n'"
        ' "${SENSORYPLEX_RESIDENT_TIER:-}" "${SENSORYPLEX_MEDIA_QUEUE_CAPACITY:-}"'
        ' "${SENSORYPLEX_MODEL_PARALLELISM:-}"\n'
    )
    runtime.chmod(runtime.stat().st_mode | stat.S_IXUSR)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "SENSORYPLEX_RESIDENT_ENV": str(env_file),
        "SENSORYPLEX_RUNTIME_BINARY": str(runtime),
    }


def test_media_wrapper_injects_tier_limits_and_refuses_silent_override(tmp_path):
    tier = fake_tier()
    environment = write_wrapper_fixture(tmp_path, tier)
    completed = subprocess.run(
        [str(WRAPPER), "replay", "pipeline.yaml", "media.mp4", "--report", "report.pb"],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    arguments = completed.stdout.split()
    assert arguments[:4] == ["replay", "pipeline.yaml", "media.mp4", "--report"]
    assert arguments[arguments.index("--handoff-retained-limit") + 1] == str(
        tier.handoff_retained_limit
    )
    assert arguments[arguments.index("--handoff-arena-bytes") + 1] == str(tier.handoff_arena_bytes)
    assert f"分级 {tier.name}" in completed.stderr
    # 分级上限必须真的进到子进程环境里：wrapper 只打印自己那一行不算证据。
    assert (
        f"tier={tier.name} queue_capacity={tier.media_queue_capacity} "
        f"parallelism={tier.model_parallelism}"
    ) in completed.stdout
    assert f"queue_capacity_cap={tier.media_queue_capacity}" in completed.stderr

    overridden = subprocess.run(
        [str(WRAPPER), "replay", "pipeline.yaml", "media.mp4", "--handoff-retained-limit", "8"],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert overridden.returncode == 2
    assert "由常驻分级决定" in overridden.stderr

    missing = subprocess.run(
        [str(WRAPPER), "replay", "pipeline.yaml", "media.mp4"],
        capture_output=True,
        text=True,
        check=False,
        env=environment | {"SENSORYPLEX_RESIDENT_ENV": str(tmp_path / "absent.env")},
    )
    assert missing.returncode == 1
    assert "缺少分级文件" in missing.stderr


def test_media_wrapper_refuses_a_pre_set_tier_environment(tmp_path):
    """运行时从环境变量读分级，所以"预置环境变量"与"命令行覆盖"是同一件事。"""

    environment = write_wrapper_fixture(tmp_path, fake_tier())
    for variable, value in (
        ("SENSORYPLEX_MEDIA_QUEUE_CAPACITY", "4096"),
        ("SENSORYPLEX_MODEL_PARALLELISM", "8"),
        ("SENSORYPLEX_RESIDENT_TIER", "xlarge"),
    ):
        blocked = subprocess.run(
            [str(WRAPPER), "replay", "pipeline.yaml", "media.mp4"],
            capture_output=True,
            text=True,
            check=False,
            env=environment | {variable: value},
        )
        assert blocked.returncode == 2, blocked.stderr
        assert variable in blocked.stderr
        assert "由常驻分级决定" in blocked.stderr


def test_launchctl_print_is_parsed_for_state_pid_and_last_exit():
    sample = textwrap.dedent(
        """
        gui/501/org.sensoryplex.runtime = {
            active count = 1
            state = running
            pid = 4242
            last exit code = 0
        }
        """
    )
    assert resident.parse_launchctl_print(sample) == {
        "state": "running",
        "pid": "4242",
        "last_exit_code": "0",
    }
    assert resident.parse_launchctl_print("no such process") == {}


def test_pmset_check_reports_and_never_changes_settings():
    sleeping = resident.pmset_guidance(run=lambda *_, **__: Completed(" AC Power:\n  sleep 10\n"))
    assert "sleep 10" in sleeping[0]
    assert "sudo pmset" in sleeping[0]
    awake = resident.pmset_guidance(run=lambda *_, **__: Completed("  sleep 0\n"))
    assert "已关闭" in awake[0]
