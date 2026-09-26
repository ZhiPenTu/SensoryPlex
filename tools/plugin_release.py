"""插件热部署 release 构建器（ADR-030）。

把某平台/架构的首方 `local_native` 插件打成**受控 bundle**，并写出制品仓描述符。它做四件事：

- `build`：解析 manifest -> 生成 bundle.manifest.json -> 打 tar.gz -> 写 release.json；
- `verify`：独立复算一个 bundle 的字节摘要与逐文件摘要（Agent 之外的第二次校验）；
- `list`：列出制品仓里已构建的 release 描述符。

bundle 内部**不含自身摘要**：逐文件摘要在 `bundle.manifest.json` 里，整包摘要是 tar.gz 文件字节的
sha256。这样"manifest 写摘要"不会变成自引用，而"改一行说明文字 → artifact_digest 不变、改一行代码 →
artifact_digest 变化"仍然可以被复算验证。

构建期允许联网（这是构建机），**部署期不允许**：wheelhouse + requirements.lock.txt 是 Agent 离线安装
的全部输入。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tomllib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLE_FORMAT = "sensoryplex.plugin-bundle/1"
DEFAULT_INDEX_URL = "https://pypi.org/simple"
# 与 tools/plugin_artifact.py 共用同一份"已知本地插件包"注册表，避免两套口径。
KNOWN_PLUGINS = {
    "plugins/python/processors/asr-whisper-mlx": "edge_material_plugin_asr_whisper_mlx.artifact",
    "plugins/python/processors/deploy-canary": "deploy_canary.artifact",
    "plugins/python/processors/embed-bge-onnx": "edge_material_plugin_embed_bge_onnx.artifact",
    "plugins/python/processors/ocr-rapidocr": "edge_material_plugin_ocr_rapidocr.artifact",
    "plugins/python/processors/vlm-moondream": "edge_material_plugin_vlm_moondream.artifact",
}
SDK_DIR = "plugins/python/common"
SDK_PACKAGE = "edge-material-sdk"
EXCLUDED_PARTS = {"__pycache__", ".venv", ".DS_Store", ".git"}


class BuildError(RuntimeError):
    """构建期显式失败：没有静默降级，也没有"先跳过这一项"。"""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def host_platform() -> tuple[str, str]:
    import platform

    system = platform.system().lower()
    name = {"darwin": "macos", "linux": "linux"}.get(system, system)
    machine = platform.machine().lower()
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64"}.get(machine, machine)
    return name, arch


def load_manifest(plugin_dir: pathlib.Path) -> dict:
    manifest = yaml.safe_load((plugin_dir / "plugin.yaml").read_text())
    if manifest.get("apiVersion") != "edge.material.plugin/v1":
        raise BuildError(f"unsupported_api_version:{manifest.get('apiVersion')}")
    return manifest


def artifact_module(plugin_dir: pathlib.Path):
    relative = plugin_dir.resolve().relative_to(ROOT).as_posix()
    module_name = KNOWN_PLUGINS.get(relative)
    if module_name is None:
        raise BuildError(f"no_artifact_module_registered:{relative}")
    sys.path.insert(0, str(plugin_dir / "src"))
    try:
        return importlib.import_module(module_name)
    finally:
        sys.path.pop(0)


def copy_tree(source: pathlib.Path, target: pathlib.Path) -> None:
    for path in sorted(source.rglob("*")):
        if EXCLUDED_PARTS.intersection(path.parts) or path.suffix == ".pyc":
            continue
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def exported_requirements(plugin_package: str) -> str:
    """用 uv 的锁文件导出第三方依赖（含 hash），保证与开发环境逐字一致。"""
    completed = subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--package",
            plugin_package,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise BuildError(f"uv_export_failed:{completed.stderr.strip()[:400]}")
    return completed.stdout


def build_local_wheels(plugin_dir: pathlib.Path, wheelhouse: pathlib.Path) -> list[pathlib.Path]:
    """构建 SDK 与插件自身的轮子：部署期没有构建后端，也不允许联网取构建依赖。"""
    plugin_package = tomllib.loads((plugin_dir / "pyproject.toml").read_text())["project"]["name"]
    for package in (SDK_PACKAGE, plugin_package):
        completed = subprocess.run(
            ["uv", "build", "--wheel", "--package", package, "--out-dir", str(wheelhouse)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise BuildError(f"uv_build_failed:{package}:{completed.stderr.strip()[:400]}")
    wheels = sorted(wheelhouse.glob("*.whl"))
    if len(wheels) < 2:
        raise BuildError(f"local_wheels_missing:{[path.name for path in wheels]}")
    return wheels


def pip_capable_python(requested: str) -> str:
    """返回一个**带 pip** 的解释器；构建机可以联网，部署机不行，两者不能混为一谈。"""
    probe = subprocess.run(
        [requested, "-c", "import pip"],
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        return requested
    build_venv = ROOT / ".data/build-venv"
    build_python = build_venv / "bin/python"
    reusable = (
        build_python.is_file()
        and subprocess.run([str(build_python), "-c", "import pip"], capture_output=True).returncode
        == 0
    )
    if not reusable:
        subprocess.run(
            ["uv", "venv", str(build_venv), "--python", "3.12", "--clear"], cwd=ROOT, check=True
        )
        subprocess.run(
            ["uv", "pip", "install", "--python", str(build_python), "pip"], cwd=ROOT, check=True
        )
    return str(build_python)


def download_wheelhouse(
    plugin_package: str, wheelhouse: pathlib.Path, build_python: str
) -> list[pathlib.Path]:
    """下载第三方轮子。显式指定 PyPI：本机环境变量里的镜像地址会让这一步静默变慢/变空。"""
    # `pip wheel` 会把 sdist 构建出的轮子也放进同一目录。
    requirements = wheelhouse / "third-party.txt"
    text = exported_requirements(plugin_package)
    lines = [line for line in text.splitlines() if not line.startswith("-e ")]
    requirements.write_text("\n".join(lines) + "\n")
    # 用 `pip wheel` 而不是 `pip download`：少数依赖（例如 rapidocr 链上的
    # antlr4-python3-runtime==4.9.3）在 PyPI 上只有 sdist，必须先在本机构建成轮子，
    # 部署机才有东西可离线安装。构建期联网是本工具的既定前提。
    completed = subprocess.run(
        [
            build_python,
            "-m",
            "pip",
            "wheel",
            "--index-url",
            DEFAULT_INDEX_URL,
            "--wheel-dir",
            str(wheelhouse),
            "-r",
            str(requirements),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise BuildError(f"wheelhouse_download_failed:{completed.stderr.strip()[-600:]}")
    requirements.unlink()
    return sorted(wheelhouse.glob("*.whl"))


def build(args) -> int:
    plugin_dir = pathlib.Path(args.plugin_dir).resolve()
    if not plugin_dir.is_dir():
        raise BuildError(f"plugin_dir_missing:{plugin_dir}")
    manifest = load_manifest(plugin_dir)
    meta = manifest["metadata"]
    spec = manifest["spec"]
    entrypoint_spec = spec["entrypoint"]

    if entrypoint_spec.get("transport") != "grpc":
        raise BuildError(f"unsupported_transport:{entrypoint_spec.get('transport')}")
    if spec["artifacts"].get("form") != "local_native":
        raise BuildError(f"unsupported_form:{spec['artifacts'].get('form')}")

    platform_name, arch = host_platform()
    if args.platform and args.platform != platform_name:
        raise BuildError(f"cross_platform_bundle_not_supported:{args.platform}!={platform_name}")
    if args.arch and args.arch != arch:
        raise BuildError(f"cross_arch_bundle_not_supported:{args.arch}!={arch}")

    artifact = artifact_module(plugin_dir)
    artifact_digest = artifact.package_digest(plugin_dir)
    manifest_digest_spec = spec["artifacts"]["digest"]
    if artifact_digest != manifest_digest_spec:
        raise BuildError(
            f"artifact_digest_drift:manifest={manifest_digest_spec} actual={artifact_digest}"
        )

    # 每个插件一个独立 staging 子目录：并发构建或上一次残留不会互相清空 wheelhouse。
    staging = pathlib.Path(args.staging_dir).resolve() / f"{meta['name']}-{meta['version']}"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "payload").mkdir(parents=True)
    payload = staging / "payload"
    copy_tree(plugin_dir, payload)
    # 权重与 SBOM 一起进包：Agent 解包后不需要任何额外网络或宿主路径。
    (payload / "config.schema.json").write_text(
        json.dumps(json.loads((plugin_dir / spec["config"]["schema"]).read_text()), indent=2) + "\n"
    )

    wheelhouse = payload / "wheelhouse"
    wheelhouse.mkdir()
    build_local_wheels(plugin_dir, wheelhouse)
    build_python = pip_capable_python(args.build_python)
    download_wheelhouse(
        tomllib.loads((plugin_dir / "pyproject.toml").read_text())["project"]["name"],
        wheelhouse,
        build_python,
    )

    wheel_paths = sorted(wheelhouse.glob("*.whl"))
    if not wheel_paths:
        raise BuildError("empty_wheelhouse")
    lock_lines = [
        "# 离线安装输入：Agent 只用这些轮子，不带 --deps、不联网。",
        *[f"wheelhouse/{path.name}" for path in wheel_paths],
    ]
    (payload / "requirements.lock.txt").write_text("\n".join(lock_lines) + "\n")

    entrypoint = {
        "transport": "grpc",
        "protocol": entrypoint_spec.get("protocol", "v1"),
        "python_module": _python_module(entrypoint_spec),
        "port_argument": "--port",
        "expect_digest_argument": "--expect-digest",
        "endpoint_file_argument": "--endpoint-file",
        "bind_port": 0,
    }
    resources = spec.get("resources", {})
    runtime_requirements = {
        "python": ">=3.12,<3.13",
        "local_native": True,
        "network_at_deploy": "denied",
        "wheel_count": len(wheel_paths),
        "declared_memory": str(resources.get("memory", "")),
        "lock_file": "payload/requirements.lock.txt",
    }

    files = []
    for path in sorted(payload.rglob("*")):
        if path.is_dir():
            continue
        files.append(
            {
                "path": f"payload/{path.relative_to(payload).as_posix()}",
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "mode": oct(path.stat().st_mode & 0o777),
            }
        )

    manifest_digest = sha256_file(payload / "plugin.yaml")
    schema_digest = sha256_file(payload / "config.schema.json")
    sbom_path = payload / "sbom.cdx.json"
    sbom_digest = sha256_file(sbom_path)
    sbom_components = len(json.loads(sbom_path.read_text()).get("components", []))

    bundle_manifest = {
        "format": BUNDLE_FORMAT,
        # bundle digest 是 release 身份的一部分，不能因为构建时钟变了就改变。
        # 实际导入时间由控制面 `published_at` 记账；bundle 内只保留稳定内容。
        "built_at": "1970-01-01T00:00:00Z",
        "plugin": {
            "plugin_id": meta["name"],
            "plugin_version": meta["version"],
            "display_name": meta.get("displayName", meta["name"]),
            "vendor": meta.get("vendor", ""),
            "license": meta.get("license", ""),
            "form": "local_native",
            "platform": platform_name,
            "arch": arch,
        },
        "artifact_digest": artifact_digest,
        "manifest_digest": manifest_digest,
        "config_schema_digest": schema_digest,
        "sbom_digest": sbom_digest,
        "entrypoint": entrypoint,
        "runtime_requirements": runtime_requirements,
        "default_deadline_ms": int(resources.get("defaultDeadlineMs", 120000)),
        "files": files,
    }
    manifest_path = staging / "bundle.manifest.json"
    manifest_path.write_text(json.dumps(bundle_manifest, indent=2, sort_keys=True) + "\n")

    release_dir = (
        pathlib.Path(args.out).resolve()
        / meta["name"]
        / str(meta["version"])
        / f"{platform_name}-{arch}"
    )
    candidate_bundle = staging.parent / f"{staging.name}.bundle.tar.gz"
    candidate_bundle.unlink(missing_ok=True)
    _write_tar(staging, candidate_bundle)

    bundle_digest = "sha256:" + sha256_file(candidate_bundle)
    release_id = (
        "rel_"
        + sha256_bytes(
            f"{meta['name']}:{meta['version']}:{platform_name}:{arch}:{bundle_digest}".encode()
        )[:32]
    )

    descriptor = {
        "release_id": release_id,
        "plugin_id": meta["name"],
        "plugin_version": str(meta["version"]),
        "platform": platform_name,
        "arch": arch,
        "form": "local_native",
        "artifact_digest": artifact_digest,
        "bundle_digest": bundle_digest,
        "manifest_digest": manifest_digest,
        "config_schema_digest": schema_digest,
        "sbom_digest": sbom_digest,
        "bundle_bytes": candidate_bundle.stat().st_size,
        "entrypoint": entrypoint,
        "runtime_requirements": runtime_requirements,
        "default_deadline_ms": bundle_manifest["default_deadline_ms"],
        "sbom_components": sbom_components,
        "declared_memory_bytes": _memory_bytes(str(resources.get("memory", "0"))),
        "declared_cpu_millicores": _cpu_millicores(str(resources.get("cpu", "0"))),
        "trust": "first_party",
        "authenticated": True,
        "authentication_method": "controlled_artifact_repository",
        "signature_status": spec["artifacts"].get("signatureUnavailableReason", "not_signed"),
        "bundle_path": (
            pathlib.Path(meta["name"])
            / str(meta["version"])
            / f"{platform_name}-{arch}"
            / "bundle.tar.gz"
        ).as_posix(),
        "trust_repository": pathlib.Path(args.out).resolve().relative_to(ROOT).as_posix()
        if pathlib.Path(args.out).resolve().is_relative_to(ROOT)
        else str(pathlib.Path(args.out).resolve()),
    }
    existing_descriptor = release_dir / "release.json"
    existing_bundle = release_dir / "bundle.tar.gz"
    if existing_descriptor.exists() or existing_bundle.exists():
        if not (existing_descriptor.is_file() and existing_bundle.is_file()):
            raise BuildError("existing_release_incomplete")
        try:
            existing = json.loads(existing_descriptor.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise BuildError("existing_release_descriptor_unreadable") from error
        actual_existing = "sha256:" + sha256_file(existing_bundle)
        if existing.get("bundle_digest") != actual_existing:
            raise BuildError("existing_release_bundle_digest_mismatch")
        if existing.get("bundle_digest") != bundle_digest:
            raise BuildError("immutable_release_content_conflict")
        print(json.dumps(existing, indent=2))
        print(f"\nrelease unchanged: {existing.get('release_id', '')}")
        print(f"bundle:            {existing_bundle}")
        candidate_bundle.unlink(missing_ok=True)
        return 0

    release_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = release_dir / "bundle.tar.gz"
    shutil.copyfile(candidate_bundle, bundle_path)
    candidate_bundle.unlink(missing_ok=True)
    (release_dir / "release.json").write_text(json.dumps(descriptor, indent=2) + "\n")

    print(json.dumps(descriptor, indent=2))
    print(f"\nrelease built: {release_id}")
    print(f"bundle:        {bundle_path}")
    print(f"wheels:        {len(wheel_paths)} ({sum(p.stat().st_size for p in wheel_paths)} bytes)")
    return 0


def _python_module(entrypoint_spec: dict) -> str:
    command = entrypoint_spec.get("command") or []
    if "-m" not in command:
        raise BuildError("entrypoint_command_without_module")
    return command[command.index("-m") + 1]


def _memory_bytes(value: str) -> int:
    text = value.strip().lower()
    for suffix, factor in (("gi", 1 << 30), ("mi", 1 << 20), ("ki", 1 << 10)):
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * factor)
    return int(float(text or 0))


def _cpu_millicores(value: str) -> int:
    text = value.strip() or "0"
    return int(float(text) * 1000)


def _write_tar(staging: pathlib.Path, target: pathlib.Path) -> None:
    """写可复现的 tar.gz：固定 mtime/uid/gid/成员排序，让同一份内容摘要稳定。

    gzip 头自己也带 mtime（`GzipFile` 默认取当前时间）。不给它显式归零，同一份内容隔几秒重建
    就会得到不同的 `bundle_digest`，进而算出不同的 `release_id` —— 制品仓会因此把"同一版本重建"
    判成 `plugin_release_content_conflict`。所以这里把 gzip 层也固定成 mtime=0、无文件名的头。
    """
    members = sorted(staging.rglob("*"))
    with (
        target.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", compresslevel=6, fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for path in members:
            info = archive.gettarinfo(str(path), arcname=path.relative_to(staging).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            if path.is_file():
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
            else:
                archive.addfile(info)


def verify(args) -> int:
    bundle = pathlib.Path(args.bundle).resolve()
    if not bundle.is_file():
        raise BuildError(f"bundle_missing:{bundle}")
    bundle_digest = "sha256:" + sha256_file(bundle)
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
        names = {member.name for member in members}
        if "bundle.manifest.json" not in names:
            raise BuildError("bundle_manifest_missing")
        manifest = json.loads(archive.extractfile("bundle.manifest.json").read())
        declared = {entry["path"] for entry in manifest["files"]}
        # 目录不是内容成员；只有普通文件参与逐文件摘要对账。
        actual = {
            member.name
            for member in members
            if member.isfile() and member.name != "bundle.manifest.json"
        }
        if declared != actual:
            raise BuildError(
                f"bundle_member_mismatch:missing={sorted(declared - actual)}"
                f":unexpected={sorted(actual - declared)}"
            )
        for entry in manifest["files"]:
            payload = archive.extractfile(entry["path"]).read()
            if len(payload) != entry["bytes"]:
                raise BuildError(f"bundle_size_mismatch:{entry['path']}")
            if sha256_bytes(payload) != entry["sha256"]:
                raise BuildError(f"bundle_content_digest_mismatch:{entry['path']}")
    descriptor_path = bundle.parent / "release.json"
    if descriptor_path.is_file():
        descriptor = json.loads(descriptor_path.read_text())
        if descriptor["bundle_digest"] != bundle_digest:
            raise BuildError(
                f"bundle_digest_mismatch:descriptor={descriptor['bundle_digest']}"
                f" actual={bundle_digest}"
            )
        if descriptor["artifact_digest"] != manifest["artifact_digest"]:
            raise BuildError("artifact_digest_mismatch_between_descriptor_and_manifest")
    print(f"bundle ok: {bundle_digest} files={len(manifest['files'])}")
    return 0


def list_releases(args) -> int:
    repository = pathlib.Path(args.out).resolve()
    count = 0
    for descriptor_path in sorted(repository.glob("*/*/*/release.json")):
        descriptor = json.loads(descriptor_path.read_text())
        count += 1
        print(
            f"{descriptor['release_id']}  {descriptor['plugin_id']}@{descriptor['plugin_version']}"
            f"  {descriptor['platform']}-{descriptor['arch']}"
            f"  bytes={descriptor['bundle_bytes']}  artifact={descriptor['artifact_digest'][:19]}…"
        )
    if not count:
        print(f"no releases in {repository}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SensoryPlex plugin release builder (ADR-030)")
    sub = parser.add_subparsers(dest="command", required=True)

    builder = sub.add_parser("build", help="构建一个平台定向的受控 bundle")
    builder.add_argument("plugin_dir")
    builder.add_argument("--platform", default="")
    builder.add_argument("--arch", default="")
    builder.add_argument("--out", default=".data/releases")
    builder.add_argument("--staging-dir", default="/tmp/sensoryplex-release-staging")
    builder.add_argument(
        "--build-python",
        default=os.environ.get("SENSORYPLEX_BUILD_PYTHON", sys.executable),
        help="用于 pip download 的解释器（构建机专属；部署期不使用）",
    )
    builder.set_defaults(func=build)

    verifier = sub.add_parser("verify", help="独立复算 bundle 的整包与逐文件摘要")
    verifier.add_argument("bundle")
    verifier.set_defaults(func=verify)

    lister = sub.add_parser("list", help="列出制品仓内的 release 描述符")
    lister.add_argument("--out", default=".data/releases")
    lister.set_defaults(func=list_releases)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
