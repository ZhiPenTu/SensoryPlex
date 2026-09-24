"""ADR-022 宿主加速器上报验收：报告必须与宿主实况逐条对账，三态不得互相塌陷。

这个脚本只在**主机**执行（`make accelerator-check` 用 `PY_HOST`）：它要起真实的
`sensoryplex-runtime serve`，而那是与主机同架构的原生二进制；同时它要直接读
`system_profiler` / CoreML framework / `nvidia-smi`，这些只有主机上才有。

四路对账，缺任何一路这个缺口就不算收口：

1. **基线**：报告里的 `host_accelerators` 与本脚本**独立解析**的宿主事实一致
   （状态与版本都要对上，证据必须无路径、有界）。
2. **与语言环境无关**：`LANG=zh_CN.UTF-8` 下判定与版本完全不变——macOS 会本地化显示文本，
   所以判定只能认 `spdisplays_*` / `sppci_*` 这些稳定键。
3. **探测不到 ≠ 不存在**：`PATH=/nonexistent` 时探测工具全部缺失，受影响的加速器必须落
   `unknown` 且原因是 `probe_tool_missing:<source>`，**绝不允许**写成 `unavailable`。
4. **宿主能力 ≠ 进程能力**：同一份报告里 `backends` 仍然全部 `unavailable`
   （本进程不做推理），加速器表不得把这件事读成"CoreML 在这台机器可用/不可用"。
"""

import argparse
import contextlib
import json
import os
import platform as host_platform
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import grpc
from edge_material_sdk.generated.runtime.v1.runtime_pb2 import (
    ACCELERATOR_STATE_AVAILABLE,
    ACCELERATOR_STATE_UNAVAILABLE,
    ACCELERATOR_STATE_UNKNOWN,
    CAPABILITY_STATE_UNAVAILABLE,
    DescribeCapabilitiesRequest,
)
from edge_material_sdk.generated.runtime.v1.runtime_pb2_grpc import RuntimeServiceStub

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINARY = ROOT / "target/release/sensoryplex-runtime"

SYSTEMS = {"darwin": "macos", "linux": "linux"}
ARCHITECTURES = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64"}

COREML_FRAMEWORK = Path("/System/Library/Frameworks/CoreML.framework")

# 与 crates/runtime/src/accelerator.rs 的 `expected()` 同源。
EXPECTED_ACCELERATORS = {
    "macos-aarch64": ("coreml", "metal"),
    "linux-x86_64": ("cuda",),
    "linux-aarch64": (),
}


def platform_name():
    system = host_platform.system().lower()
    machine = host_platform.machine().lower()
    return f"{SYSTEMS.get(system, system)}-{ARCHITECTURES.get(machine, machine)}"


def run_host_tool(argv):
    """直接执行宿主探测工具；这里只用它做**独立**对账，不复用 Rust 侧的任何解析。"""
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


@dataclass(frozen=True)
class DirectFact:
    """本脚本独立探测出的宿主事实。"""

    state: int
    version: str
    reason: str
    needs_tool: bool


def direct_metal():
    raw = run_host_tool(["system_profiler", "-json", "SPDisplaysDataType"])
    if raw is None:
        return DirectFact(ACCELERATOR_STATE_UNKNOWN, "", "probe_tool_missing:system_profiler", True)
    try:
        adapters = json.loads(raw).get("SPDisplaysDataType") or []
        adapter = adapters[0]
    except (ValueError, TypeError, IndexError, AttributeError):
        return DirectFact(ACCELERATOR_STATE_UNKNOWN, "", "displays_json_unparsable", True)
    family = str(adapter.get("spdisplays_mtlgpufamilysupport", "")).strip()
    if not family:
        return DirectFact(ACCELERATOR_STATE_UNAVAILABLE, "", "host_reports_no_metal_family", True)
    return DirectFact(ACCELERATOR_STATE_AVAILABLE, family.removeprefix("spdisplays_"), "", True)


def direct_coreml():
    if not COREML_FRAMEWORK.exists():
        return DirectFact(
            ACCELERATOR_STATE_UNAVAILABLE, "", "framework_absent:CoreML.framework", False
        )
    info = COREML_FRAMEWORK / "Versions/A/Resources/Info.plist"
    raw = run_host_tool(["plutil", "-extract", "CFBundleVersion", "raw", str(info)])
    if raw is None:
        return DirectFact(ACCELERATOR_STATE_UNKNOWN, "", "probe_tool_missing:framework_info", True)
    version = raw.strip()
    if not version:
        return DirectFact(
            ACCELERATOR_STATE_UNKNOWN, "", "framework_version_empty:framework_info", True
        )
    return DirectFact(ACCELERATOR_STATE_AVAILABLE, version, "", True)


def direct_cuda():
    raw = run_host_tool(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
    if raw is None:
        return DirectFact(ACCELERATOR_STATE_UNKNOWN, "", "probe_tool_missing:nvidia_smi", True)
    first = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    if not first:
        return DirectFact(
            ACCELERATOR_STATE_UNKNOWN, "", "nvidia_smi_reported_no_gpu:nvidia_smi", True
        )
    parts = [part.strip() for part in first.split(",")]
    if not parts[0]:
        return DirectFact(
            ACCELERATOR_STATE_UNKNOWN, "", "nvidia_smi_reported_no_model:nvidia_smi", True
        )
    return DirectFact(ACCELERATOR_STATE_AVAILABLE, parts[1] if len(parts) > 1 else "", "", True)


DIRECT_PROBES = {"metal": direct_metal, "coreml": direct_coreml, "cuda": direct_cuda}


@contextlib.contextmanager
def runtime_facts(binary, env_overrides=None):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        address = f"127.0.0.1:{listener.getsockname()[1]}"
    environment = os.environ | {"SENSORYPLEX_RUNTIME_ADDR": address} | (env_overrides or {})
    try:
        process = subprocess.Popen([str(binary), "serve"], env=environment)
    except OSError as error:
        raise SystemExit(f"无法启动 {binary}: {error}") from error
    try:
        with grpc.insecure_channel(address) as channel:
            grpc.channel_ready_future(channel).result(timeout=15)
            response = RuntimeServiceStub(channel).DescribeCapabilities(
                DescribeCapabilitiesRequest(), timeout=10
            )
            yield response
    finally:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def triples(facts):
    return [(fact.accelerator, fact.state, fact.runtime_version) for fact in facts]


def check_baseline(facts, platform):
    expected = EXPECTED_ACCELERATORS.get(platform, ())
    assert [fact.accelerator for fact in facts] == list(expected), (
        f"{platform} 上预期 {expected}，实际 {[fact.accelerator for fact in facts]}"
    )
    for fact in facts:
        assert fact.state != 0, f"{fact.accelerator} 不能留 unspecified"
        assert fact.platform == platform
        available = fact.state == ACCELERATOR_STATE_AVAILABLE
        assert bool(fact.unavailable_reason) != available, (
            f"{fact.accelerator} 的原因串与状态不匹配：{fact.unavailable_reason!r}"
        )
        for item in fact.evidence:
            assert "/" not in item, f"evidence 不得携带路径: {item}"
            assert len(item) <= 128, f"evidence 过长: {item}"
        direct = DIRECT_PROBES[fact.accelerator]()
        if direct.state == ACCELERATOR_STATE_UNKNOWN and direct.needs_tool:
            # 脚本自己都探不到时只能记下"本机无法独立对账"，不能用它去放宽断言。
            print(f"  note: {fact.accelerator} 本机无法独立对账（{direct.reason}）")
            continue
        assert fact.state == direct.state, (
            f"{fact.accelerator} 状态与宿主实况不符："
            f"报告={fact.state} 直读={direct.state}（宿主说 {direct.reason}）"
        )
        assert fact.runtime_version == direct.version, (
            f"{fact.accelerator} 版本与宿主实况不符："
            f"报告={fact.runtime_version!r} 直读={direct.version!r}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--binary", default=str(DEFAULT_BINARY))
    args = parser.parse_args()

    binary = Path(args.binary)
    if not binary.exists():
        raise SystemExit(
            f"{binary} 不存在；先运行 "
            "`cargo build --locked --release -p sensoryplex-runtime`（或 make accelerator-check）"
        )

    platform = platform_name()
    print(f"平台 {platform}；被测二进制 {binary}")

    with runtime_facts(binary) as baseline:
        assert baseline.platform == platform, baseline.platform
        facts = list(baseline.host_accelerators)
        print("① 基线与宿主直读对账")
        check_baseline(facts, platform)
        baseline_triples = triples(facts)
        print(f"  {baseline_triples}")
        for fact in facts:
            print(f"  {fact.accelerator}: {fact.detection_source} {list(fact.evidence)}")

    with runtime_facts(binary, {"LANG": "zh_CN.UTF-8", "LC_ALL": "zh_CN.UTF-8"}) as localized:
        print("② LANG=zh_CN.UTF-8 下判定不变")
        assert triples(localized.host_accelerators) == baseline_triples, (
            f"判定受语言环境影响：{triples(localized.host_accelerators)} != {baseline_triples}"
        )
        print(f"  {triples(localized.host_accelerators)}")

    with runtime_facts(binary, {"PATH": "/nonexistent"}) as starved:
        print("③ PATH=/nonexistent：探测工具缺失必须落 unknown，不能落 unavailable")
        for fact in starved.host_accelerators:
            assert fact.state != ACCELERATOR_STATE_AVAILABLE, (
                f"{fact.accelerator} 不可能在这个环境里可用"
            )
            if DIRECT_PROBES[fact.accelerator]().needs_tool:
                assert fact.state == ACCELERATOR_STATE_UNKNOWN, (
                    f"{fact.accelerator} 在没有探测工具时被判成 {fact.state}："
                    f"{fact.unavailable_reason}"
                )
                assert fact.unavailable_reason.startswith("probe_tool_missing:"), (
                    f"{fact.accelerator} 的原因必须点明是探测工具缺失：{fact.unavailable_reason}"
                )
                assert fact.detection_source == "unavailable"
            assert not fact.runtime_version and not fact.evidence
        print(f"  {triples(starved.host_accelerators)}")

    with runtime_facts(binary) as crossed:
        print("④ 宿主加速器表不得替执行后端说话")
        assert crossed.host_accelerators, "本目标预期至少有一块加速器"
        for backend in crossed.backends:
            assert backend.state == CAPABILITY_STATE_UNAVAILABLE, backend.backend
            assert backend.unavailable_reason
        print(
            f"  backends 全部 unavailable（{len(crossed.backends)} 条），"
            f"accelerators 见 ①：两者结论互不覆盖"
        )

    print(f"宿主加速器上报验收: PASS; platform={platform}; host_accelerators={baseline_triples}")


if __name__ == "__main__":
    sys.exit(main())
