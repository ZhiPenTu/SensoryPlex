"""独立仓库制品构建、分离式签名与离线字节验证。"""

import base64
import gzip
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from packaging.specifiers import SpecifierSet
from packaging.utils import parse_wheel_filename

from .manifest import artifact_digest, canonical, digest, load_manifest

MAX_BUNDLE_BYTES = 512 << 20
MAX_EXPANDED_BYTES = 2 << 30
MAX_MEMBERS = 20000
SIGNING_CONTEXT = b"sensoryplex.plugin-release/v2\0"


def file_digest(path):
    hashed = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            hashed.update(chunk)
    return "sha256:" + hashed.hexdigest()


def keygen(private_path, public_path):
    key = Ed25519PrivateKey.generate()
    # 创建不覆盖已有密钥，并在创建时限制权限。
    import os

    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    Path(public_path).write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )


def public_identity(public_pem):
    key = serialization.load_pem_public_key(public_pem)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("signer_key_not_ed25519")
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "signer_" + hashlib.sha256(raw).hexdigest()[:32]


def sign(descriptor, private_path):
    key = serialization.load_pem_private_key(Path(private_path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("signer_key_not_ed25519")
    return base64.b64encode(key.sign(SIGNING_CONTEXT + canonical(descriptor))).decode()


def verify_signature(descriptor, signature, public_pem):
    key = serialization.load_pem_public_key(public_pem)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("signer_key_not_ed25519")
    try:
        key.verify(
            base64.b64decode(signature, validate=True), SIGNING_CONTEXT + canonical(descriptor)
        )
    except Exception as error:
        raise ValueError("release_signature_invalid") from error


def run(argv, cwd):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=900)
    if result.returncode:
        # 发布错误不会回显可能含凭据的依赖 URL 或环境。
        raise ValueError("build_command_failed:" + Path(argv[0]).name)
    return result.stdout


def require_locked_sdk(project, sdk_wheel, compatibility):
    """SDK 字节也必须来自插件的锁文件，不能用同名或不同版本 wheel 绕过依赖身份。"""
    packages = tomllib.loads((Path(project) / "uv.lock").read_text()).get("package", [])
    locked = next((p for p in packages if p["name"] == "edge-material-sdk"), None)
    name, version, _, _ = parse_wheel_filename(Path(sdk_wheel).name)
    if (
        name != "edge-material-sdk"
        or not locked
        or str(version) != locked["version"]
        or version not in SpecifierSet(compatibility)
        or file_digest(sdk_wheel) not in {w.get("hash") for w in locked.get("wheels", [])}
    ):
        raise ValueError("sdk_wheel_lock_mismatch")


def build(project, out, sdk_wheel):
    project, out, sdk_wheel = (
        Path(project).resolve(),
        Path(out).resolve(),
        Path(sdk_wheel).resolve(),
    )
    registration = load_manifest(project)
    if not registration["v2"] or not (project / "uv.lock").is_file():
        raise ValueError("v2_manifest_and_project_lock_required")
    manifest = registration["manifest"]
    spec, meta = manifest["spec"], manifest["metadata"]
    require_locked_sdk(project, sdk_wheel, spec["sdk"]["runtime"])
    artifact_digest(project)
    if any(path.is_symlink() for path in project.rglob("*") if ".venv" not in path.parts):
        raise ValueError("project_symlink_forbidden")
    with tempfile.TemporaryDirectory(prefix="sensoryplex-build-") as temporary:
        staging = Path(temporary)
        payload = staging / "payload"
        payload.mkdir()
        for name in ("src", "pyproject.toml", "uv.lock", "plugin.yaml", spec["config"]["schema"]):
            source = project / name
            if source.is_dir():
                shutil.copytree(
                    source, payload / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
                )
            else:
                (payload / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, payload / name)
        for side in ("inputs", "outputs"):
            for contract in spec[side]:
                if "schema" in contract:
                    name = contract["schema"]["path"]
                    (payload / name).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(project / name, payload / name)
        (payload / "plugin.yaml").write_text(
            __import__("yaml").safe_dump(manifest, sort_keys=False)
        )
        (payload / "config.schema.json").write_bytes(canonical(registration["config_schema"]))
        wheelhouse = payload / "wheelhouse"
        wheelhouse.mkdir()
        shutil.copyfile(sdk_wheel, wheelhouse / sdk_wheel.name)
        run(["uv", "build", "--wheel", "--out-dir", str(wheelhouse)], project)
        requirements = run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-emit-project",
                "--no-emit-package",
                "edge-material-sdk",
            ],
            project,
        )
        lock_input = staging / "third-party.txt"
        lock_input.write_bytes(requirements)
        run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--wheel-dir",
                str(wheelhouse),
                "-r",
                str(lock_input),
            ],
            project,
        )
        wheels = sorted(wheelhouse.glob("*.whl"))
        if not wheels or not any(path.name.startswith("edge_material_sdk-") for path in wheels):
            raise ValueError("sdk_wheel_missing")
        (payload / "requirements.lock.txt").write_text(
            "\n".join("wheelhouse/" + path.name for path in wheels) + "\n"
        )
        sbom = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "version": 1,
            "components": [
                {
                    "type": "library",
                    "name": path.name,
                    "hashes": [{"alg": "SHA-256", "content": file_digest(path)[7:]}],
                }
                for path in wheels
            ],
        }
        (payload / "sbom.cdx.json").write_bytes(canonical(sbom))
        os_name = {"darwin": "macos", "linux": "linux"}.get(platform.system().lower())
        arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64"}.get(
            platform.machine().lower()
        )
        if not os_name or not arch:
            raise ValueError("unsupported_build_platform")
        entry = {
            "transport": "grpc",
            "protocol": "v1",
            "python_module": "edge_material_sdk.server",
            "bind_port": 0,
            "port_argument": "--port",
            "expect_digest_argument": "--expect-digest",
            "endpoint_file_argument": "--endpoint-file",
        }
        runtime = {
            "python": ">=3.12,<3.13",
            "local_native": True,
            "network_at_deploy": "denied",
            "wheel_count": len(wheels),
            "lock_file": "payload/requirements.lock.txt",
        }
        bundle_manifest = {
            "format": "sensoryplex.plugin-bundle/1",
            "built_at": "1970-01-01T00:00:00Z",
            "plugin": {
                "plugin_id": meta["name"],
                "plugin_version": str(meta["version"]),
                "platform": os_name,
                "arch": arch,
                "form": "local_native",
            },
            "artifact_digest": artifact_digest(payload),
            "entrypoint": entry,
            "runtime_requirements": runtime,
            "default_deadline_ms": spec["resources"]["defaultDeadlineMs"],
            "files": [
                {
                    "path": path.relative_to(staging).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": file_digest(path)[7:],
                }
                for path in sorted(payload.rglob("*"))
                if path.is_file()
            ],
        }
        for key, name in (
            ("manifest_digest", "plugin.yaml"),
            ("config_schema_digest", "config.schema.json"),
            ("sbom_digest", "sbom.cdx.json"),
        ):
            bundle_manifest[key] = file_digest(payload / name)[7:]
        (staging / "bundle.manifest.json").write_bytes(canonical(bundle_manifest))
        out.mkdir(parents=True, exist_ok=True)
        bundle = out / "bundle.tar.gz"
        if bundle.exists() or (out / "release.json").exists():
            raise ValueError("output_release_already_exists")
        with (
            bundle.open("wb") as target,
            gzip.GzipFile(fileobj=target, mode="wb", mtime=0, filename="") as compressed,
            tarfile.open(fileobj=compressed, mode="w") as archive,
        ):
            for path in sorted(staging.rglob("*")):
                if path == lock_input:
                    continue
                info = archive.gettarinfo(str(path), arcname=path.relative_to(staging).as_posix())
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                info.mode = 0o755 if path.is_dir() else 0o644
                if path.is_file():
                    with path.open("rb") as source:
                        archive.addfile(info, source)
                else:
                    archive.addfile(info)
        bundle_digest = file_digest(bundle)
        descriptor = {
            key: bundle_manifest[key]
            for key in (
                "artifact_digest",
                "manifest_digest",
                "config_schema_digest",
                "sbom_digest",
                "entrypoint",
                "runtime_requirements",
                "default_deadline_ms",
            )
        }
        descriptor.update(
            {
                "plugin_id": meta["name"],
                "plugin_version": str(meta["version"]),
                "platform": os_name,
                "arch": arch,
                "form": "local_native",
                "bundle_digest": bundle_digest,
                "bundle_bytes": bundle.stat().st_size,
                "sbom_components": len(wheels),
                "declared_memory_bytes": int(spec["resources"]["memoryBytes"]),
                "declared_cpu_millicores": int(spec["resources"]["cpuMillicores"]),
            }
        )
        descriptor["release_id"] = (
            "rel_"
            + digest(f"{meta['name']}:{meta['version']}:{os_name}:{arch}:{bundle_digest}".encode())[
                7:39
            ]
        )
        (out / "release.json").write_bytes(canonical(descriptor))
        verify_bundle(bundle, descriptor)
        return descriptor


def verify_bundle(bundle, descriptor):
    """不解包、不执行代码；全部成员、字节和声明都重新核验。"""
    bundle = Path(bundle)
    if (
        not 0 < bundle.stat().st_size <= MAX_BUNDLE_BYTES
        or bundle.stat().st_size != descriptor["bundle_bytes"]
    ):
        raise ValueError("release_bundle_size_rejected")
    if file_digest(bundle) != descriptor["bundle_digest"]:
        raise ValueError("bundle_digest_mismatch")
    with tempfile.TemporaryDirectory(prefix="sensoryplex-verify-") as temporary:
        root = Path(temporary)
        seen, total = set(), 0
        with tarfile.open(bundle, "r:gz") as archive:
            for member in archive:
                name = member.name
                parts = Path(name).parts
                if (
                    len(seen) >= MAX_MEMBERS
                    or name in seen
                    or not parts
                    or len(parts) > 12
                    or Path(name).is_absolute()
                    or ".." in parts
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError("release_bundle_unsafe_member")
                seen.add(name)
                total += member.size
                if member.size > MAX_BUNDLE_BYTES or total > MAX_EXPANDED_BYTES:
                    raise ValueError("release_bundle_expansion_rejected")
                path = root / name
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    with path.open("wb") as target:
                        shutil.copyfileobj(source, target, 1 << 20)
        bundle_manifest = json.loads((root / "bundle.manifest.json").read_bytes())
        declared = {item["path"]: item for item in bundle_manifest["files"]}
        actual = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and path.relative_to(root).as_posix() != "bundle.manifest.json"
        }
        if len(declared) != len(bundle_manifest["files"]) or set(declared) != actual:
            raise ValueError("release_bundle_member_mismatch")
        for relative, item in declared.items():
            if (root / relative).stat().st_size != item["bytes"] or file_digest(root / relative)[
                7:
            ] != item["sha256"]:
                raise ValueError("release_bundle_content_digest_mismatch")
        for key, name in (
            ("manifest_digest", "plugin.yaml"),
            ("config_schema_digest", "config.schema.json"),
            ("sbom_digest", "sbom.cdx.json"),
        ):
            if (
                file_digest(root / "payload" / name)[7:] != descriptor[key]
                or bundle_manifest[key] != descriptor[key]
            ):
                raise ValueError("release_descriptor_digest_mismatch")
        if artifact_digest(root / "payload") != descriptor["artifact_digest"]:
            raise ValueError("release_artifact_digest_mismatch")
        registration = load_manifest(root / "payload")
        if (
            bundle_manifest.get("format") != "sensoryplex.plugin-bundle/1"
            or bundle_manifest["artifact_digest"] != descriptor["artifact_digest"]
        ):
            raise ValueError("release_bundle_format_invalid")
        resources = registration["manifest"]["spec"]["resources"]
        for field, key in (
            ("declared_memory_bytes", "memoryBytes"),
            ("declared_cpu_millicores", "cpuMillicores"),
            ("default_deadline_ms", "defaultDeadlineMs"),
        ):
            if descriptor[field] != resources[key]:
                raise ValueError("release_resource_declaration_mismatch")
        meta = registration["manifest"]["metadata"]
        if not registration["v2"] or (meta["name"], str(meta["version"])) != (
            descriptor["plugin_id"],
            descriptor["plugin_version"],
        ):
            raise ValueError("release_manifest_identity_mismatch")
        if (
            descriptor["form"] != "local_native"
            or descriptor["platform"] not in {"macos", "linux"}
            or descriptor["arch"] not in {"aarch64", "x86_64"}
        ):
            raise ValueError("release_platform_invalid")
        expected_plugin = {
            key: descriptor[key]
            for key in ("plugin_id", "plugin_version", "platform", "arch", "form")
        }
        if bundle_manifest["plugin"] != expected_plugin:
            raise ValueError("release_bundle_platform_mismatch")
        wheels = sorted((root / "payload/wheelhouse").glob("*.whl"))
        if not wheels or not any(path.name.startswith("edge_material_sdk-") for path in wheels):
            raise ValueError("sdk_wheel_missing")
        lock = (root / "payload/requirements.lock.txt").read_text().splitlines()
        if lock != ["wheelhouse/" + path.name for path in wheels]:
            raise ValueError("release_wheel_lock_invalid")
        sbom = json.loads((root / "payload/sbom.cdx.json").read_bytes())
        expected_components = [
            {
                "type": "library",
                "name": path.name,
                "hashes": [{"alg": "SHA-256", "content": file_digest(path)[7:]}],
            }
            for path in wheels
        ]
        if sbom["components"] != expected_components or descriptor["sbom_components"] != len(
            wheels
        ):
            raise ValueError("release_sbom_mismatch")
        if (
            descriptor["entrypoint"].get("python_module") != "edge_material_sdk.server"
            or descriptor["entrypoint"] != bundle_manifest["entrypoint"]
            or descriptor["runtime_requirements"] != bundle_manifest["runtime_requirements"]
        ):
            raise ValueError("release_entrypoint_invalid")
        expected_id = (
            "rel_"
            + digest(
                f"{meta['name']}:{meta['version']}:{descriptor['platform']}:{descriptor['arch']}:{descriptor['bundle_digest']}".encode()
            )[7:39]
        )
        if descriptor["release_id"] != expected_id:
            raise ValueError("release_identity_invalid")
        return registration
