"""平台服务适配器与受控 bundle 安装（ADR-030）。

这一层只做**机械动作**：校验并解包受控 bundle、离线装出带 venv 的版本化安装目录、把插件进程
托管给平台服务。它不碰 HTTP、不认识控制面：状态机推进与回报在 `node_agent_hot_deploy` 里。

平台服务（macOS 用户级 LaunchAgent / Linux systemd user unit）的选型理由：插件是独立进程，需要
"崩了才重启、正常退出不重启"、版本化 unit 名与私有日志；`launchd`/`systemd` 的 KeepAlive/
Restart=on-failure 正好是这个语义，自己写守护循环会把"异常退出才重启"退化成"无条件拉起"。

macOS 适配器已在本机实机验收；**Linux 适配器尚未在 Linux 节点验收**，按 ADR-030 的范围声明，
在 Linux 实机跑通前不得宣称支持 Linux。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import platform
import plistlib
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass

# 解包硬上限：超过即拒绝，绝不"截断后继续"或"跳过这一项"。
MAX_BUNDLE_BYTES = 4 << 30
MAX_MEMBER_BYTES = 2 << 30
MAX_MEMBERS = 20_000
MAX_PATH_DEPTH = 12
ENDPOINT_FORMAT = "sensoryplex.plugin-endpoint/1"

UNIT_PREFIX = "org.sensoryplex.plugin"
# 插件运行期只允许看到这些环境变量：宿主环境里的凭据不会被顺手带进子进程。
ENV_ALLOWLIST = ("HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME")
PATH_VALUE = "/usr/bin:/bin:/usr/sbin:/sbin"


class HotDeployError(RuntimeError):
    """带**稳定错误码**的执行期失败。控制面、Console 与审计都直接消费这个码。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class UnitState:
    """平台服务的**实际**状态；未知一律表达为 loaded=False，不猜成功。"""

    loaded: bool
    running: bool
    pid: int = 0
    last_exit_code: int | None = None
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "loaded": self.loaded,
            "running": self.running,
            "pid": self.pid,
            "last_exit_code": self.last_exit_code,
            "detail": self.detail,
        }


@dataclass
class RuntimeSpec:
    """托管一个版本化实例所需的全部事实（进程、路径、日志）。"""

    plugin_id: str
    release_id: str
    runtime_instance_id: str
    artifact_digest: str
    python_module: str
    interpreter: pathlib.Path
    payload_src: pathlib.Path
    runtime_dir: pathlib.Path
    endpoint_file: pathlib.Path
    stdout_log: pathlib.Path
    stderr_log: pathlib.Path


def sha256_file(path: pathlib.Path) -> str:
    """落盘字节的 `sha256:<hex>` 摘要；与控制面的 `bundle_digest` 同一口径。"""
    return "sha256:" + sha256_hex(path)


def sha256_hex(path: pathlib.Path) -> str:
    """裸十六进制摘要。bundle.manifest.json 里的逐文件摘要就是这个口径，不做二次加前缀。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def platform_name() -> str:
    system = platform.system().lower()
    return {"darwin": "macos", "linux": "linux"}.get(system, system)


def arch_name() -> str:
    machine = platform.machine().lower()
    return {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64"}.get(machine, machine)


def sanitize_label(value: str) -> str:
    """平台服务标识只允许 `[A-Za-z0-9._-]`；其余字符替换，保证 unit 名可安全拼接。"""
    return "".join(char if char.isalnum() or char in "._-" else "-" for char in value)


def unit_name(plugin_id: str, release_id: str, runtime_instance_id: str) -> str:
    """版本化 unit 名：同一 release 的不同运行实例互不覆盖，回滚实例也不会撞名。"""
    return ".".join(
        (
            UNIT_PREFIX,
            sanitize_label(plugin_id),
            sanitize_label(release_id),
            sanitize_label(runtime_instance_id),
        )
    )


def resolve_plugin_python() -> pathlib.Path:
    """找一个 3.12 解释器给插件 venv 用（`requires-python = >=3.12,<3.13`）。

    先信当前解释器：Agent 自己如果就跑在 3.12 上，那它一定可用。否则按顺序探测候选，
    全都不可用就显式失败 —— 不去猜"也许能跑"。
    """
    candidates: list[str] = []
    if sys.version_info[:2] == (3, 12):
        candidates.append(sys.executable)
    candidates += ["python3.12", "/opt/homebrew/bin/python3.12", "/usr/local/bin/python3.12"]
    home = pathlib.Path.home()
    candidates += [str(home / ".local/bin/python3.12"), "/usr/bin/python3.12"]
    for candidate in candidates:
        resolved = shutil.which(candidate) if os.sep not in candidate else candidate
        if not resolved or not pathlib.Path(resolved).is_file():
            continue
        probe = subprocess.run(
            [resolved, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0 and probe.stdout.strip() == "3.12":
            return pathlib.Path(resolved).resolve()
    raise HotDeployError(
        "plugin_runtime_python_unavailable",
        "no CPython 3.12 interpreter found for the plugin virtualenv",
    )


def verify_bundle_bytes(bundle_path: pathlib.Path, expected_digest: str) -> str:
    """复算落盘 bundle 字节摘要。只信落盘字节，不信下载响应头或意图里的声明。"""
    if not bundle_path.is_file():
        raise HotDeployError("release_bundle_unavailable", str(bundle_path))
    size = bundle_path.stat().st_size
    if size <= 0 or size > MAX_BUNDLE_BYTES:
        raise HotDeployError("release_bundle_size_rejected", f"bytes={size}")
    actual = sha256_file(bundle_path)
    if actual != expected_digest:
        raise HotDeployError(
            "release_bundle_digest_mismatch", f"expected={expected_digest} actual={actual}"
        )
    return actual


def safe_extract_bundle(bundle_path: pathlib.Path, target: pathlib.Path) -> dict:
    """解包 bundle，并在**写盘之前**拒绝一切越界形状。

    拒绝清单（任一条命中即 `release_bundle_unsafe_member`）：绝对路径、`..` 逃逸、符号链接、
    硬链接、设备/管道等非普通成员、超过成员/总大小上限、路径层级过深。先校验再落盘，避免
    "已经写了半个恶意包"。
    """
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle_path, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise HotDeployError("release_bundle_too_many_members", str(len(members)))
        planned = []
        total = 0
        for member in members:
            name = member.name
            if member.issym() or member.islnk():
                raise HotDeployError("release_bundle_unsafe_member", f"link:{name}")
            if not (member.isfile() or member.isdir()):
                raise HotDeployError("release_bundle_unsafe_member", f"type:{name}")
            pure = pathlib.PurePosixPath(name)
            if pure.is_absolute() or any(part == ".." for part in pure.parts):
                raise HotDeployError("release_bundle_path_escape", name)
            if len(pure.parts) > MAX_PATH_DEPTH:
                raise HotDeployError("release_bundle_path_too_deep", name)
            if member.isfile():
                if member.size > MAX_MEMBER_BYTES:
                    raise HotDeployError("release_bundle_member_too_large", name)
                total += member.size
                if total > MAX_BUNDLE_BYTES:
                    raise HotDeployError("release_bundle_uncompressed_too_large", str(total))
            planned.append((member, target.joinpath(*pure.parts)))
        for member, destination in planned:
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise HotDeployError("release_bundle_unreadable_member", member.name)
            with destination.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            destination.chmod(0o755 if member.mode & 0o100 else 0o644)
    manifest_path = target / "bundle.manifest.json"
    if not manifest_path.is_file():
        raise HotDeployError("release_bundle_manifest_missing", str(manifest_path))
    return json.loads(manifest_path.read_text())


def verify_bundle_tree(target: pathlib.Path, manifest: dict) -> dict:
    """逐文件复算摘要，并核对 manifest/schema/SBOM 的自声明摘要。

    多一个文件、少一个文件、改一个字节都会失败：bundle 是**内容寻址**的，不是"目录大致相同"。
    """
    declared = {entry["path"]: entry for entry in manifest["files"]}
    actual = {
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if path.is_file() and path.relative_to(target).as_posix() != "bundle.manifest.json"
    }
    if set(declared) != actual:
        raise HotDeployError(
            "release_bundle_member_mismatch",
            f"missing={sorted(set(declared) - actual)} unexpected={sorted(actual - set(declared))}",
        )
    for relative, entry in sorted(declared.items()):
        path = target / relative
        if path.stat().st_size != int(entry["bytes"]):
            raise HotDeployError("release_bundle_size_mismatch", relative)
        if sha256_hex(path) != entry["sha256"]:
            raise HotDeployError("release_bundle_content_digest_mismatch", relative)
    payload = target / "payload"
    for key, expected, label in (
        ("manifest_digest", manifest.get("manifest_digest"), payload / "plugin.yaml"),
        (
            "config_schema_digest",
            manifest.get("config_schema_digest"),
            payload / "config.schema.json",
        ),
        ("sbom_digest", manifest.get("sbom_digest"), payload / "sbom.cdx.json"),
    ):
        if not label.is_file():
            raise HotDeployError("release_bundle_descriptor_missing", label.name)
        if not expected or sha256_hex(label) != expected:
            raise HotDeployError(f"release_bundle_{key}_mismatch", label.name)
    return manifest


def install_release(
    *,
    payload: pathlib.Path,
    install_dir: pathlib.Path,
    manifest: dict,
    release: dict,
    timeout_s: float = 900.0,
) -> pathlib.Path:
    """在**离线**条件下把 payload 装成一个版本化 venv，返回解释器路径。

    只用 bundle 自带 wheelhouse（`--no-index`），不带 `--deps`、不看 `~/.cache`、不联网；
    安装完成后写 `INSTALLED.json` 作为"这个目录已经装好且属于哪个 release"的证据，重复
    安装（例如回滚复用同一 release）直接命中缓存。
    """
    marker = install_dir / "INSTALLED.json"
    interpreter = install_dir / "venv" / "bin" / "python"
    if marker.is_file() and interpreter.is_file():
        recorded = json.loads(marker.read_text())
        if recorded.get("artifact_digest") == manifest.get("artifact_digest") and recorded.get(
            "bundle_digest"
        ) == release.get("bundle_digest"):
            return interpreter
    lock_file = payload / "requirements.lock.txt"
    if not lock_file.is_file():
        raise HotDeployError("release_lock_file_missing", str(lock_file))
    if not (payload / "wheelhouse").is_dir():
        raise HotDeployError("release_wheelhouse_missing", str(payload / "wheelhouse"))

    install_dir.mkdir(parents=True, exist_ok=True)
    if interpreter.exists():
        shutil.rmtree(install_dir / "venv", ignore_errors=True)
    python = resolve_plugin_python()
    _run(
        [str(python), "-m", "venv", str(install_dir / "venv")],
        cwd=install_dir,
        timeout_s=timeout_s,
        code="plugin_venv_creation_failed",
    )
    # 离线安装：显式禁用索引与网络，只有 wheelhouse 里的轮子是合法输入。
    _run(
        [
            str(interpreter),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-warn-script-location",
            "-r",
            "requirements.lock.txt",
        ],
        cwd=payload,
        timeout_s=timeout_s,
        code="plugin_offline_install_failed",
    )
    marker.write_text(
        json.dumps(
            {
                "format": "sensoryplex.plugin-install/1",
                "plugin_id": manifest["plugin"]["plugin_id"],
                "plugin_version": manifest["plugin"]["plugin_version"],
                "release_id": release.get("release_id", ""),
                "artifact_digest": manifest.get("artifact_digest", ""),
                "bundle_digest": release.get("bundle_digest", ""),
                "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return interpreter


def _run(argv: list[str], *, cwd: pathlib.Path, timeout_s: float, code: str) -> str:
    try:
        completed = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired as error:
        raise HotDeployError(code, f"timeout after {timeout_s}s") from error
    except OSError as error:
        raise HotDeployError(code, str(error)) from error
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise HotDeployError(code, " | ".join(tail[-3:])[:400])
    return completed.stdout


def read_endpoint_file(path: pathlib.Path) -> dict:
    """读插件原子写出的 endpoint 契约；形状或身份不符一律显式失败。"""
    if not path.is_file():
        raise HotDeployError("plugin_endpoint_file_missing", str(path))
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise HotDeployError("plugin_endpoint_file_invalid", str(error)) from error
    if payload.get("format") != ENDPOINT_FORMAT:
        raise HotDeployError("plugin_endpoint_file_invalid", f"format={payload.get('format')}")
    endpoint = str(payload.get("endpoint", ""))
    host, _, port = endpoint.rpartition(":")
    if host not in {"127.0.0.1", "localhost"} or not port.isdigit() or int(port) == 0:
        raise HotDeployError("plugin_endpoint_not_loopback", endpoint)
    return payload


def wait_for_endpoint_file(
    path: pathlib.Path, *, deadline_s: float, interval_s: float = 0.2
) -> dict:
    """轮询 endpoint 文件直到出现或超时。**不从 stdout 推断端口**。

    文件"还没出现"才继续等（进程可能仍在装载）；文件**已经存在但内容违反契约**时不重试：
    契约要求原子写出，读到半个 JSON 或非 loopback 地址就说明插件真的写错了，继续重试
    只会把精确错误码拖成一个笼统的超时。
    """
    deadline = time.monotonic() + deadline_s
    last: HotDeployError | None = None
    while time.monotonic() < deadline:
        try:
            return read_endpoint_file(path)
        except HotDeployError as error:
            if error.code in {"plugin_endpoint_file_invalid", "plugin_endpoint_not_loopback"}:
                raise
            last = error
        time.sleep(interval_s)
    raise HotDeployError("plugin_endpoint_file_timeout", last.detail if last else "")


class Supervisor:
    """平台服务适配器接口。子类只实现"写 unit / 装载 / 停止 / 查状态"四件事。"""

    name = ""

    def unit_path(self, unit: str) -> pathlib.Path:
        raise NotImplementedError

    def write_unit(self, unit: str, spec: RuntimeSpec) -> pathlib.Path:
        raise NotImplementedError

    def load(self, unit: str, spec: RuntimeSpec) -> None:
        raise NotImplementedError

    def stop(self, unit: str) -> UnitState:
        raise NotImplementedError

    def state(self, unit: str) -> UnitState:
        raise NotImplementedError

    def limited_environment(self) -> dict[str, str]:
        """受限环境：只透传白名单变量，再补上运行必需的固定项。"""
        allowed = {name: os.environ[name] for name in ENV_ALLOWLIST if os.environ.get(name)}
        allowed["PATH"] = PATH_VALUE
        allowed["PYTHONUNBUFFERED"] = "1"
        allowed["PYTHONDONTWRITEBYTECODE"] = "1"
        return allowed


class LaunchAgentSupervisor(Supervisor):
    """macOS 用户级 LaunchAgent 适配器（已实机验收）。"""

    name = "launchagent"

    def __init__(self) -> None:
        self.uid = os.getuid()

    def unit_path(self, unit: str) -> pathlib.Path:
        return pathlib.Path.home() / "Library" / "LaunchAgents" / f"{unit}.plist"

    def _domain(self) -> str:
        return f"gui/{self.uid}"

    def write_unit(self, unit: str, spec: RuntimeSpec) -> pathlib.Path:
        environment = self.limited_environment()
        # PYTHONPATH 指向 payload 源码树：插件对自身可执行内容的摘要（artifact_digest）
        # 是按"pyproject.toml + src"复算的，必须让进程真的从这份源码树加载自己。
        environment["PYTHONPATH"] = str(spec.payload_src)
        payload = {
            "Label": unit,
            "ProgramArguments": [
                str(spec.interpreter),
                "-m",
                spec.python_module,
                "--port",
                "0",
                "--expect-digest",
                spec.artifact_digest,
                "--endpoint-file",
                str(spec.endpoint_file),
            ],
            "WorkingDirectory": str(spec.runtime_dir),
            "EnvironmentVariables": environment,
            "RunAtLoad": True,
            # 只"异常退出才重启"：正常退出（0）不拉起，避免"停不掉的实例"。
            "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Background",
            "StandardOutPath": str(spec.stdout_log),
            "StandardErrorPath": str(spec.stderr_log),
        }
        path = self.unit_path(unit)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(payload))
        return path

    def load(self, unit: str, spec: RuntimeSpec) -> None:
        # 先清掉上一次尝试留下的服务与 plist（`stop` 会删 unit 文件），**再**写新的 plist：
        # 顺序反了就会拿着一个刚被自己删掉的路径去 bootstrap，报成误导性的 EIO。
        self.stop(unit)
        path = self.write_unit(unit, spec)
        result = subprocess.run(
            ["launchctl", "bootstrap", self._domain(), str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise HotDeployError(
                "candidate_start_failed",
                f"launchctl bootstrap rc={result.returncode}: {result.stderr.strip()[:200]}",
            )

    def stop(self, unit: str) -> UnitState:
        before = self.state(unit)
        if before.loaded:
            result = subprocess.run(
                ["launchctl", "bootout", f"{self._domain()}/{unit}"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0 and "No such process" not in result.stderr:
                raise HotDeployError(
                    "plugin_unit_unload_failed",
                    f"launchctl bootout rc={result.returncode}: {result.stderr.strip()[:200]}",
                )
        self.unit_path(unit).unlink(missing_ok=True)
        return before

    def state(self, unit: str) -> UnitState:
        result = subprocess.run(
            ["launchctl", "print", f"{self._domain()}/{unit}"], capture_output=True, text=True
        )
        if result.returncode != 0:
            return UnitState(loaded=False, running=False, detail="not_loaded")
        running = False
        pid = 0
        last_exit: int | None = None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped == "state = running":
                running = True
            elif stripped == "state = not running":
                running = False
            elif stripped.startswith("pid = "):
                pid = int(stripped.split("=", 1)[1].strip() or 0)
            elif stripped.startswith("last exit code = "):
                value = stripped.split("=", 1)[1].strip()
                last_exit = None if value == "(never exited)" else int(value)
        return UnitState(
            loaded=True,
            running=running,
            pid=pid,
            last_exit_code=last_exit,
            detail="loaded",
        )


class SystemdUserSupervisor(Supervisor):
    """Linux systemd user unit 适配器（**尚未在 Linux 节点验收**，见模块说明）。"""

    name = "systemd_user"

    def unit_path(self, unit: str) -> pathlib.Path:
        return pathlib.Path.home() / ".config" / "systemd" / "user" / f"{unit}.service"

    def write_unit(self, unit: str, spec: RuntimeSpec) -> pathlib.Path:
        environment = self.limited_environment()
        environment["PYTHONPATH"] = str(spec.payload_src)
        environment_lines = [f"Environment={name}={value}" for name, value in environment.items()]
        lines = [
            "[Unit]",
            f"Description=SensoryPlex plugin {spec.plugin_id} {spec.release_id}",
            "",
            "[Service]",
            "Type=simple",
            "ExecStart="
            + " ".join(
                [
                    str(spec.interpreter),
                    "-m",
                    spec.python_module,
                    "--port",
                    "0",
                    "--expect-digest",
                    spec.artifact_digest,
                    "--endpoint-file",
                    str(spec.endpoint_file),
                ]
            ),
            f"WorkingDirectory={spec.runtime_dir}",
            *environment_lines,
            # 与 LaunchAgent 的 KeepAlive{SuccessfulExit:false} 等价：异常退出才重启。
            "Restart=on-failure",
            "RestartSec=2",
            f"StandardOutput=append:{spec.stdout_log}",
            f"StandardError=append:{spec.stderr_log}",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
        path = self.unit_path(unit)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines))
        return path

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)

    def load(self, unit: str, spec: RuntimeSpec) -> None:
        self.write_unit(unit, spec)
        self._systemctl("daemon-reload")
        result = self._systemctl("enable", "--now", f"{unit}.service")
        if result.returncode != 0:
            raise HotDeployError(
                "candidate_start_failed",
                f"systemctl enable rc={result.returncode}: {result.stderr.strip()[:200]}",
            )

    def stop(self, unit: str) -> UnitState:
        before = self.state(unit)
        self._systemctl("disable", "--now", f"{unit}.service")
        self._systemctl("reset-failed", f"{unit}.service")
        self.unit_path(unit).unlink(missing_ok=True)
        self._systemctl("daemon-reload")
        return before

    def state(self, unit: str) -> UnitState:
        result = self._systemctl(
            "show",
            f"{unit}.service",
            "-p",
            "LoadState",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "MainPID",
            "-p",
            "ExecMainStatus",
        )
        if result.returncode != 0:
            return UnitState(loaded=False, running=False, detail="systemctl_failed")
        values = {}
        for line in result.stdout.splitlines():
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
        loaded = values.get("LoadState") == "loaded"
        if not loaded:
            return UnitState(loaded=False, running=False, detail=values.get("LoadState", ""))
        active = values.get("ActiveState") == "active"
        substate = values.get("SubState", "")
        return UnitState(
            loaded=True,
            running=active and substate == "running",
            pid=int(values.get("MainPID") or 0),
            last_exit_code=int(values.get("ExecMainStatus") or 0),
            detail=substate,
        )


def supervisor_for_platform(name: str = "") -> Supervisor:
    """按平台选适配器；不支持的平台显式失败，不静默退化成"无托管"。"""
    resolved = name or platform_name()
    if resolved == "macos":
        return LaunchAgentSupervisor()
    if resolved == "linux":
        return SystemdUserSupervisor()
    raise HotDeployError("plugin_runtime_platform_unsupported", resolved)
