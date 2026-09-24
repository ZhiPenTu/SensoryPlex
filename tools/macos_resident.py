"""M5：macOS 常驻形态（launchd）与统一内存分级。

本工具只做三件可核对的事：

1. **探测宿主统一内存并映射到分级**。探测不到就显式失败（`unsupported`），不"取最近一档"；
   `SENSORYPLEX_TOTAL_MEMORY_BYTES` 只在宿主探测不可用时用于**声明**容量，输出必须带
   `source` 字段说明这份数字来自哪里（见 `docs/runbooks/development.md`）。
2. **用分级渲染 launchd plist 与 `resident.env`**。模板里缺失的占位符直接失败，渲染结果必须
   能被 `plistlib` 解析；只写用户级 `~/Library/LaunchAgents`，不碰系统级目录。
3. **通过 `launchctl` 安装/卸载/查看**，并可对已托管进程做一次真实 gRPC `Health` 调用。

不做什么：不调用 `pmset`（需要 root，只读检查并打印需要人工确认的命令）；不把插件 manifest 里
**声明**的内存占用当成实测 RSS；不替调用方改写 pipeline 的 `queue_capacity`——运行时会在
`replay`/`ingest` 按分级上限对声明值与保留窗口做准入，越界即显式失败（ADR-019），工具只报告
声明值。分级依据见 ADR-015。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import platform
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass

GIB = 1024**3
MIB = 1024**2
ROOT = pathlib.Path(__file__).resolve().parents[1]
SYSCTL = "/usr/sbin/sysctl"
PMSET = "/usr/bin/pmset"
LAUNCHCTL = "/bin/launchctl"
RUNTIME_LABEL = "org.sensoryplex.runtime"
CAFFEINATE_LABEL = "org.sensoryplex.caffeinate"
TEMPLATE_DIR = ROOT / "deploy/macos/launchd"
PLUGIN_DIR = ROOT / "plugins/python/processors"
PIPELINE_DIR = ROOT / "config/pipelines"
MEMORY_ENV = "SENSORYPLEX_TOTAL_MEMORY_BYTES"
DEFAULT_RUNTIME_ADDR = "127.0.0.1:50051"
DEFAULT_RUNTIME_BINARY = ROOT / "target/release/sensoryplex-runtime"

PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
LAUNCHCTL_STATE = re.compile(r"^\s*state = (\S+)$", re.MULTILINE)
LAUNCHCTL_PID = re.compile(r"^\s*pid = (\d+)$", re.MULTILINE)
LAUNCHCTL_LAST_EXIT = re.compile(r"^\s*last exit code = (-?\d+)$", re.MULTILINE)
DECLARED_SIZE = re.compile(r"^\s*(\d+)\s*([KMGT])i$")


@dataclass(frozen=True)
class Tier:
    """一档统一内存下的常驻参数。数值依据见 ADR-015 §2。"""

    name: str
    min_bytes: int
    max_bytes: int | None
    media_queue_capacity: int
    event_queue_capacity: int
    handoff_retained_limit: int
    handoff_arena_bytes: int
    model_parallelism: int
    note: str

    def covers(self, memory_bytes: int) -> bool:
        if memory_bytes < self.min_bytes:
            return False
        return self.max_bytes is None or memory_bytes < self.max_bytes

    def model_budget_bytes(self, memory_bytes: int) -> int:
        """模型 worker 的预算：统一内存的三分之一。

        其余留给系统、页缓存、aria/媒体缓冲与 Python 运行时。这是一个**预算上限**，
        不是实测占用；插件 manifest 里的 `resources.memory` 也只是声明值。
        """

        return memory_bytes // 3


# 锚点：`medium` 档等于今天的默认值（`crates/media/src/handoff.rs` 的
# DEFAULT_RETAINED_LIMIT=32 / DEFAULT_RETAIN_ARENA_BYTES=64 MiB），即"不改变现有行为"；
# 低于 16 GiB 不做静默降级——直接拒绝常驻（显式失败）。
TIERS: tuple[Tier, ...] = (
    Tier(
        name="small",
        min_bytes=16 * GIB,
        max_bytes=24 * GIB,
        media_queue_capacity=16,
        event_queue_capacity=16,
        handoff_retained_limit=16,
        handoff_arena_bytes=32 * MIB,
        model_parallelism=1,
        note="16GB 机型：模型只串行跑一个，不得并行 ASR+OCR+VLM（ADR-008）",
    ),
    Tier(
        name="medium",
        min_bytes=24 * GIB,
        max_bytes=32 * GIB,
        media_queue_capacity=32,
        event_queue_capacity=32,
        handoff_retained_limit=32,
        handoff_arena_bytes=64 * MIB,
        model_parallelism=2,
        note="等于当前默认上限（retained 32 / arena 64 MiB）",
    ),
    Tier(
        name="large",
        min_bytes=32 * GIB,
        max_bytes=64 * GIB,
        media_queue_capacity=64,
        event_queue_capacity=64,
        handoff_retained_limit=64,
        handoff_arena_bytes=128 * MIB,
        model_parallelism=3,
        note="默认上限的 2 倍；单一种类上限随之为 32 条",
    ),
    Tier(
        name="xlarge",
        min_bytes=64 * GIB,
        max_bytes=None,
        media_queue_capacity=128,
        event_queue_capacity=128,
        handoff_retained_limit=128,
        handoff_arena_bytes=256 * MIB,
        model_parallelism=4,
        note="默认上限的 4 倍；留给慢通道 VLM 与更大批次",
    ),
)


@dataclass(frozen=True)
class MemoryProbe:
    """统一内存探测结果。`source` 必须跟着数字一起走，避免把声明值当实测值。"""

    memory_bytes: int | None
    source: str
    detail: str


@dataclass(frozen=True)
class ResidentPaths:
    home: pathlib.Path

    @property
    def agent_dir(self) -> pathlib.Path:
        return self.home / "Library/LaunchAgents"

    @property
    def config_dir(self) -> pathlib.Path:
        return self.home / "Library/Application Support/SensoryPlex"

    @property
    def log_dir(self) -> pathlib.Path:
        return self.home / "Library/Logs/SensoryPlex"

    @property
    def resident_env(self) -> pathlib.Path:
        return self.config_dir / "resident.env"

    def plist(self, label: str) -> pathlib.Path:
        return self.agent_dir / f"{label}.plist"


def select_tier(memory_bytes: int | None) -> Tier | None:
    """分级之外的容量返回 `None`：调用方必须显式失败，不得取最近一档。"""

    if memory_bytes is None:
        return None
    for tier in TIERS:
        if tier.covers(memory_bytes):
            return tier
    return None


def unsupported_reason(probe: MemoryProbe) -> str:
    if probe.memory_bytes is None:
        return f"宿主统一内存探测不可用（{probe.detail}）"
    lowest = TIERS[0].min_bytes
    if probe.memory_bytes < lowest:
        return (
            f"统一内存 {probe.memory_bytes / GIB:.1f} GiB 低于最低档 {lowest / GIB:.0f} GiB，"
            "本工具不猜一档：请先确认机型，或改动 ADR-015 的分级本身"
        )
    return f"统一内存 {probe.memory_bytes} 字节不在任何一档内"


def probe_memory(
    env: dict[str, str] | None = None,
    run: object = subprocess.run,
    system: str | None = None,
) -> MemoryProbe:
    """优先读宿主实测值；只有探测失败才接受显式声明的容量，并标明来源。"""

    env = os.environ if env is None else env
    system = platform.system() if system is None else system
    if system == "Darwin":
        completed = run([SYSCTL, "-n", "hw.memsize"], capture_output=True, text=True, check=False)
        value = (completed.stdout or "").strip()
        if completed.returncode == 0 and value.isdigit() and int(value) > 0:
            return MemoryProbe(int(value), "sysctl", "sysctl -n hw.memsize")
        detail = f"sysctl 退出码 {completed.returncode}"
    else:
        detail = f"平台 {system} 不是 macOS；统一内存只对 Apple Silicon 有意义"
    declared = env.get(MEMORY_ENV)
    if declared is not None:
        if not declared.isdigit() or int(declared) <= 0:
            raise ValueError(f"{MEMORY_ENV} 必须是正整数字节数，收到 {declared!r}")
        return MemoryProbe(int(declared), "env", f"{detail}，改用 {MEMORY_ENV} 声明值")
    return MemoryProbe(None, "unavailable", detail)


def render_template(text: str, values: dict[str, str]) -> str:
    """严格渲染：缺值直接失败，渲染后不允许再出现占位符。"""

    missing = sorted({name for name in PLACEHOLDER.findall(text) if name not in values})
    if missing:
        raise ValueError(f"模板占位符没有对应取值：{missing}")
    rendered = PLACEHOLDER.sub(lambda match: str(values[match.group(1)]), text)
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("渲染结果里仍有未替换的花括号占位符")
    return rendered


def parse_size(text: str) -> int:
    match = DECLARED_SIZE.match(text)
    if match is None:
        raise ValueError(f"无法解析容量 {text!r}")
    amount, unit = int(match.group(1)), match.group(2)
    return amount * {"K": 1024, "M": MIB, "G": GIB, "T": 1024 * GIB}[unit]


def declared_plugin_memory(plugin_dir: pathlib.Path | None = None) -> dict[str, int]:
    """读插件 manifest 里**声明**的 `resources.memory`（声明值，不是实测 RSS）。"""

    plugin_dir = PLUGIN_DIR if plugin_dir is None else plugin_dir
    declared: dict[str, int] = {}
    for manifest in sorted(plugin_dir.glob("*/plugin.yaml")):
        text = manifest.read_text(encoding="utf-8")
        match = re.search(r"^\s*memory:\s*(\S+)\s*$", text, re.MULTILINE)
        if match is None:
            raise ValueError(f"{manifest} 没有声明 resources.memory")
        declared[manifest.parent.name] = parse_size(match.group(1))
    return declared


def pipeline_queue_capacities(directory: pathlib.Path | None = None) -> dict[str, int]:
    directory = PIPELINE_DIR if directory is None else directory
    capacities: dict[str, int] = {}
    for pipeline in sorted(directory.glob("*.yaml")):
        match = re.search(r"^\s*queue_capacity:\s*(\d+)\s*$", pipeline.read_text(), re.MULTILINE)
        if match is None:
            continue
        capacities[pipeline.name] = int(match.group(1))
    return capacities


def tier_values(tier: Tier) -> dict[str, str]:
    return {
        "TIER": tier.name,
        "MEDIA_QUEUE_CAPACITY": str(tier.media_queue_capacity),
        # 事件链路（relay 每轮认领 / 消费在飞）的深度上限；今天与媒体队列同值——没有证据说明
        # 两者应该不同，但它是**独立字段**，以后要分化就改这里，而不是悄悄复用媒体队列那个数。
        "EVENT_QUEUE_CAPACITY": str(tier.event_queue_capacity),
        "HANDOFF_RETAINED_LIMIT": str(tier.handoff_retained_limit),
        "HANDOFF_ARENA_BYTES": str(tier.handoff_arena_bytes),
        "MODEL_PARALLELISM": str(tier.model_parallelism),
    }


def resident_env_text(tier: Tier, probe: MemoryProbe, runtime_addr: str) -> str:
    if probe.memory_bytes is None:
        raise ValueError("resident.env 必须有可用内存数字")
    lines = [
        "# 由 tools/macos_resident.py install 生成；手改会被下一次 install 覆盖。",
        f"SENSORYPLEX_RESIDENT_TIER={tier.name}",
        f"SENSORYPLEX_UNIFIED_MEMORY_BYTES={probe.memory_bytes}",
        f"SENSORYPLEX_MEMORY_SOURCE={probe.source}",
        f"SENSORYPLEX_MEDIA_QUEUE_CAPACITY={tier.media_queue_capacity}",
        f"SENSORYPLEX_EVENT_QUEUE_CAPACITY={tier.event_queue_capacity}",
        f"SENSORYPLEX_HANDOFF_RETAINED_LIMIT={tier.handoff_retained_limit}",
        f"SENSORYPLEX_HANDOFF_ARENA_BYTES={tier.handoff_arena_bytes}",
        f"SENSORYPLEX_MODEL_PARALLELISM={tier.model_parallelism}",
        f"SENSORYPLEX_MODEL_BUDGET_BYTES={tier.model_budget_bytes(probe.memory_bytes)}",
        f"SENSORYPLEX_RUNTIME_ADDR={runtime_addr}",
    ]
    return "\n".join(lines) + "\n"


def render_plist(
    template: pathlib.Path,
    values: dict[str, str],
    *,
    verify: bool = True,
) -> bytes:
    rendered = render_template(template.read_text(encoding="utf-8"), values)
    if verify:
        try:
            plistlib.loads(rendered.encode("utf-8"))
        except Exception as error:  # noqa: BLE001 - 渲染失败必须以可读原因暴露
            raise ValueError(f"{template.name} 渲染结果不是合法 plist：{error}") from error
    return rendered.encode("utf-8")


def parse_launchctl_print(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    if (state := LAUNCHCTL_STATE.search(text)) is not None:
        fields["state"] = state.group(1)
    if (pid := LAUNCHCTL_PID.search(text)) is not None:
        fields["pid"] = pid.group(1)
    if (exit_code := LAUNCHCTL_LAST_EXIT.search(text)) is not None:
        fields["last_exit_code"] = exit_code.group(1)
    return fields


def pmset_sleep_value(run: object = subprocess.run) -> str | None:
    """只读检查：`sleep 0` 之外的系统睡眠会让常驻进程在无活动时暂停。"""

    completed = run([PMSET, "-g", "custom"], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        return None
    for line in (completed.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("sleep "):
            return stripped.split()[1]
    return None


def gui_domain() -> str:
    return f"gui/{os.getuid()}"


def launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([LAUNCHCTL, *arguments], capture_output=True, text=True, check=False)


def print_domain(label: str) -> dict[str, str]:
    completed = launchctl("print", f"{gui_domain()}/{label}")
    if completed.returncode != 0:
        return {"state": "not_loaded", "detail": (completed.stderr or "").strip()}
    return parse_launchctl_print(completed.stdout)


def verify_endpoint(address: str) -> dict[str, object]:
    """对已托管进程做一次真实 gRPC 调用：清单里的数字必须与进程自报的一致。"""

    import grpc
    from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime
    from edge_material_sdk.generated.runtime.v1.runtime_pb2_grpc import RuntimeServiceStub

    with grpc.insecure_channel(address) as channel:
        stub = RuntimeServiceStub(channel)
        health = stub.Health(runtime.HealthRequest(), timeout=5.0)
        describe = stub.DescribeCapabilities(runtime.DescribeCapabilitiesRequest(), timeout=5.0)
    return {
        "state": health.state,
        "unavailable_capabilities": list(health.unavailable_capabilities),
        "platform": describe.platform,
        "unified_memory_bytes": describe.host.unified_memory_bytes,
        "admitted_memory_kinds": list(describe.admitted_memory_kinds),
        # 分级值由进程从环境读入后原样转述（ADR-019）：这里对账的是"进程实际读到了什么"，
        # 不是"分级表里写了什么"。
        "resident_tier": describe.residency.tier,
        "media_queue_capacity": describe.residency.media_queue_capacity,
        "model_parallelism": describe.residency.model_parallelism,
    }


def plist_values(
    tier: Tier,
    probe: MemoryProbe,
    paths: ResidentPaths,
    runtime_binary: pathlib.Path,
    runtime_addr: str,
) -> dict[str, str]:
    return tier_values(tier) | {
        "RUNTIME_LABEL": RUNTIME_LABEL,
        "CAFFEINATE_LABEL": CAFFEINATE_LABEL,
        "RUNTIME_BINARY": str(runtime_binary),
        "RUNTIME_ADDR": runtime_addr,
        "WORKDIR": str(ROOT),
        "LOG_DIR": str(paths.log_dir),
        "UNIFIED_MEMORY_BYTES": str(probe.memory_bytes),
        "MEMORY_SOURCE": probe.source,
    }


def render_launchd_agents(
    paths: ResidentPaths,
    values: dict[str, str],
    *,
    with_caffeinate: bool,
) -> list[pathlib.Path]:
    written: list[pathlib.Path] = []
    templates = [("runtime", TEMPLATE_DIR / "org.sensoryplex.runtime.plist.template")]
    if with_caffeinate:
        templates.append(("caffeinate", TEMPLATE_DIR / "org.sensoryplex.caffeinate.plist.template"))
    for name, template in templates:
        label = RUNTIME_LABEL if name == "runtime" else CAFFEINATE_LABEL
        target = paths.plist(label)
        target.write_bytes(render_plist(template, values))
        written.append(target)
    return written


def wait_for_running(label: str, timeout_s: float = 15.0) -> dict[str, str]:
    import time

    deadline = time.monotonic() + timeout_s
    fields = print_domain(label)
    while fields.get("state") != "running" and time.monotonic() < deadline:
        time.sleep(0.5)
        fields = print_domain(label)
    return fields


def summarize(tier: Tier, probe: MemoryProbe, runtime_addr: str) -> list[str]:
    declared = declared_plugin_memory()
    budget = tier.model_budget_bytes(probe.memory_bytes or 0)
    declared_total = sum(declared.values())
    lines = [
        f"统一内存 {probe.memory_bytes / GIB:.1f} GiB"
        f"（来源 {probe.source}: {probe.detail}）→ 分级 {tier.name}",
        f"队列/保留：pipeline queue_capacity={tier.media_queue_capacity}、"
        f"handoff retained_limit={tier.handoff_retained_limit}"
        f"（单一种类 {tier.handoff_retained_limit // 2} 条）、"
        f"arena={tier.handoff_arena_bytes // MIB} MiB",
        f"事件链路：每进程在飞上限 event_queue_capacity={tier.event_queue_capacity}"
        "（relay 每轮认领 / 消费未 ack 深度，准入见 ADR-027）",
        f"模型：并发上限 {tier.model_parallelism} 个 worker，预算 {budget / GIB:.1f} GiB；"
        f"manifest 声明合计 {declared_total / GIB:.1f} GiB（声明值，不是实测 RSS）",
        f"运行控制端点 {runtime_addr}",
    ]
    for name, bytes_ in sorted(declared.items()):
        lines.append(f"  声明占用 {name}: {bytes_ / GIB:.1f} GiB")
    for pipeline, capacity in pipeline_queue_capacities().items():
        state = "匹配" if capacity <= tier.media_queue_capacity else "超过本档上限"
        lines.append(
            f"  pipeline {pipeline}: queue_capacity={capacity}"
            f"（{state}；运行时按分级上限准入，越界即失败，不改写配置）"
        )
    return lines


def pmset_guidance(run: object = subprocess.run) -> list[str]:
    value = pmset_sleep_value(run)
    if value is None:
        return ["pm-set：读不到睡眠设置（非 macOS 或 pmset 不可用），需要人工确认"]
    if value == "0":
        return ["pmset：系统睡眠已关闭（sleep 0），常驻进程不会被系统挂起"]
    return [
        f"pmset：系统睡眠仍是 sleep {value}，无活动时可能挂起常驻进程；"
        "本工具不修改系统设置，需要人工执行 `sudo pmset -a sleep 0 disksleep 0` 后再重启验证",
        "备选：随 launchd 常驻的 `caffeinate -ims`"
        "（install 默认安装，可用 --without-caffeinate 关闭）",
    ]


def command_probe(arguments: argparse.Namespace) -> int:
    probe = probe_memory()
    tier = select_tier(probe.memory_bytes)
    if tier is None:
        print(f"[resident] 无法常驻：{unsupported_reason(probe)}")
        return 1
    payload = {
        "tier": tier.name,
        "note": tier.note,
        "memory": {"bytes": probe.memory_bytes, "source": probe.source, "detail": probe.detail},
        "limits": {
            "media_queue_capacity": tier.media_queue_capacity,
            "handoff_retained_limit": tier.handoff_retained_limit,
            "handoff_arena_bytes": tier.handoff_arena_bytes,
            "model_parallelism": tier.model_parallelism,
            "model_budget_bytes": tier.model_budget_bytes(probe.memory_bytes),
        },
        "declared_plugin_memory": declared_plugin_memory(),
        "pipeline_queue_capacity": pipeline_queue_capacities(),
    }
    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    for line in summarize(tier, probe, DEFAULT_RUNTIME_ADDR):
        print(f"[resident] {line}")
    return 0


def resident_paths(arguments: argparse.Namespace) -> ResidentPaths:
    home = pathlib.Path(arguments.home).expanduser() if arguments.home else pathlib.Path.home()
    return ResidentPaths(home=home)


def resolve_tier() -> tuple[MemoryProbe, Tier] | None:
    probe = probe_memory()
    tier = select_tier(probe.memory_bytes)
    if tier is None:
        print(f"[resident] 无法常驻：{unsupported_reason(probe)}", file=sys.stderr)
        return None
    return probe, tier


def command_render(arguments: argparse.Namespace) -> int:
    resolved = resolve_tier()
    if resolved is None:
        return 1
    probe, tier = resolved
    paths = resident_paths(arguments)
    output = pathlib.Path(arguments.output).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    runtime_binary = pathlib.Path(arguments.runtime_binary).expanduser()
    values = plist_values(tier, probe, paths, runtime_binary, arguments.runtime_addr)
    for name, template in (
        ("runtime", TEMPLATE_DIR / "org.sensoryplex.runtime.plist.template"),
        ("caffeinate", TEMPLATE_DIR / "org.sensoryplex.caffeinate.plist.template"),
    ):
        (output / f"{name}.plist").write_bytes(render_plist(template, values))
    (output / "resident.env").write_text(
        resident_env_text(tier, probe, arguments.runtime_addr), encoding="utf-8"
    )
    print(f"[resident] 已渲染分级 {tier.name} 的 launchd 配置到 {output}")
    return 0


def command_install(arguments: argparse.Namespace) -> int:
    if platform.system() != "Darwin":
        print("[resident] install 只在 macOS 上有效（launchd 不存在于其他平台）", file=sys.stderr)
        return 1
    resolved = resolve_tier()
    if resolved is None:
        return 1
    probe, tier = resolved
    paths = resident_paths(arguments)
    runtime_binary = pathlib.Path(arguments.runtime_binary).expanduser()
    if not runtime_binary.is_file():
        print(
            f"[resident] 找不到 runtime 可执行文件 {runtime_binary}；"
            "先 `cargo build --locked --release -p sensoryplex-runtime --features gstreamer`"
            "或用 --runtime-binary 指定",
            file=sys.stderr,
        )
        return 1
    values = plist_values(tier, probe, paths, runtime_binary, arguments.runtime_addr)
    for directory in (paths.agent_dir, paths.config_dir, paths.log_dir):
        directory.mkdir(parents=True, exist_ok=True)
    paths.resident_env.write_text(
        resident_env_text(tier, probe, arguments.runtime_addr), encoding="utf-8"
    )
    labels = [RUNTIME_LABEL] + ([] if arguments.without_caffeinate else [CAFFEINATE_LABEL])
    written = render_launchd_agents(paths, values, with_caffeinate=not arguments.without_caffeinate)
    if arguments.dry_run:
        print(f"[resident] dry-run：已写入 {[str(path) for path in written]}，未调用 launchctl")
        return 0
    for label in labels:
        # 重复 install 是幂等的：先摘掉已加载的同名 job（未加载时 bootout 返回非 0，属正常）。
        launchctl("bootout", f"{gui_domain()}/{label}")
        bootstrapped = launchctl("bootstrap", gui_domain(), str(paths.plist(label)))
        if bootstrapped.returncode != 0:
            print(
                f"[resident] bootstrap {label} 失败：{(bootstrapped.stderr or '').strip()}",
                file=sys.stderr,
            )
            return 1
    runtime_state = wait_for_running(RUNTIME_LABEL)
    if runtime_state.get("state") != "running":
        print(f"[resident] {RUNTIME_LABEL} 未进入 running：{runtime_state}", file=sys.stderr)
        return 1
    for line in summarize(tier, probe, arguments.runtime_addr):
        print(f"[resident] {line}")
    for line in pmset_guidance():
        print(f"[resident] {line}")
    print(
        f"[resident] 已安装并启动：{RUNTIME_LABEL} pid={runtime_state.get('pid')}"
        f"（RunAtLoad 已生效；真机重启自启需在重启后再跑一次 `status`）"
    )
    return 0


def command_status(arguments: argparse.Namespace) -> int:
    paths = resident_paths(arguments)
    failed = False
    for label in (RUNTIME_LABEL, CAFFEINATE_LABEL):
        fields = print_domain(label)
        state = fields.get("state", "unknown")
        print(
            f"[resident] {label}: state={state} pid={fields.get('pid', '-')} "
            f"last_exit={fields.get('last_exit_code', '-')}"
        )
        if state != "running":
            failed = True
    if arguments.verify_endpoint:
        address = arguments.runtime_addr
        if paths.resident_env.is_file():
            for line in paths.resident_env.read_text(encoding="utf-8").splitlines():
                if line.startswith("SENSORYPLEX_RUNTIME_ADDR="):
                    address = line.split("=", 1)[1]
        try:
            result = verify_endpoint(address)
        except Exception as error:  # noqa: BLE001 - 端点不可达必须显式失败
            print(f"[resident] gRPC 端点 {address} 不可用：{error}", file=sys.stderr)
            return 1
        probe = probe_memory()
        print(f"[resident] gRPC {address}: {json.dumps(result, ensure_ascii=False)}")
        reported = int(result["unified_memory_bytes"])
        if probe.memory_bytes is not None and reported != probe.memory_bytes:
            print(
                f"[resident] 进程自报统一内存 {reported} != 宿主探测 {probe.memory_bytes}",
                file=sys.stderr,
            )
            return 1
        if not reported:
            print("[resident] 进程没有上报统一内存（0 表示未知，不是 0 字节）", file=sys.stderr)
            return 1
    return 1 if failed else 0


def command_uninstall(arguments: argparse.Namespace) -> int:
    paths = resident_paths(arguments)
    labels = [RUNTIME_LABEL, CAFFEINATE_LABEL]
    for label in labels:
        if platform.system() == "Darwin":
            launchctl("bootout", f"{gui_domain()}/{label}")
        print(f"[resident] 已卸载 {label}")
    for label in labels:
        plist = paths.plist(label)
        if plist.is_file():
            plist.unlink()
    if not arguments.keep_config and paths.resident_env.is_file():
        paths.resident_env.unlink()
        print("[resident] 已删除 resident.env")
    if arguments.purge_logs and paths.log_dir.is_dir():
        for entry in sorted(paths.log_dir.iterdir()):
            if entry.is_file():
                entry.unlink()
        print(f"[resident] 已清空日志 {paths.log_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="macOS 常驻形态（launchd）与统一内存分级")
    parser.add_argument("--home", default="", help="覆盖 HOME，仅用于测试或自定义安装位置")
    parser.add_argument(
        "--runtime-binary",
        default=str(DEFAULT_RUNTIME_BINARY),
        help="sensoryplex-runtime 可执行文件路径（默认 target/release）",
    )
    parser.add_argument("--runtime-addr", default=DEFAULT_RUNTIME_ADDR, help="控制端点地址")
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe = subparsers.add_parser("probe", help="打印分级与上限，不写任何文件")
    probe.add_argument("--json", action="store_true")
    probe.set_defaults(handler=command_probe)
    render = subparsers.add_parser("render", help="把 plist 与 resident.env 渲染到指定目录")
    render.add_argument("--output", required=True)
    render.set_defaults(handler=command_render)
    install = subparsers.add_parser("install", help="写入 ~/Library 并用 launchctl 启动")
    install.add_argument("--dry-run", action="store_true", help="只写文件，不调用 launchctl")
    install.add_argument("--without-caffeinate", action="store_true")
    install.set_defaults(handler=command_install)
    status = subparsers.add_parser("status", help="查看 job 状态，可选做一次真实 gRPC 调用")
    status.add_argument("--verify-endpoint", action="store_true")
    status.set_defaults(handler=command_status)
    uninstall = subparsers.add_parser("uninstall", help="卸载 job 并清理用户级文件")
    uninstall.add_argument("--keep-config", action="store_true")
    uninstall.add_argument("--purge-logs", action="store_true")
    uninstall.set_defaults(handler=command_uninstall)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    return int(arguments.handler(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
