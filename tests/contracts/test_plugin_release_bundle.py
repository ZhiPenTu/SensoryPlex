"""`tools/plugin_release.py` 的 bundle 摘要口径与拒收语义（纯离线：不构建 wheel、不联网）。

这里验的是"制品构建侧的自我对账"：可复现归档、逐成员摘要对账、描述符与 manifest 的摘要一致性。
Agent 侧的**解包安全**由 `tools/node_agent_platform` 承担，两处验收互不替代：**形状类**拒收
（符号链接 / `..` / 绝对路径 / 成员不符 / 摘要不符）由 `tools/verify_plugin_hot_deploy.py
--scope native` 在宿主上验收；**上限类**拒收（成员数 / 单成员大小 / 解压总大小 / 路径深度）由
`tests/contracts/test_node_agent_unpack_safety.py` 覆盖。本文件通过不等于制品已装到节点。
"""

import argparse
import json
import pathlib

import pytest

from tools import plugin_release as release

ARTIFACT_DIGEST = "sha256:" + "a" * 64


def layout(tmp_path):
    """铺一个最小 bundle 树：manifest + payload（plugin.yaml/schema/sbom）。"""
    staging = tmp_path / "staging"
    payload = staging / "payload"
    payload.mkdir(parents=True)
    contents = {
        "plugin.yaml": "metadata:\n  name: org.sensoryplex.fixture\n",
        "config.schema.json": "{}\n",
        "sbom.cdx.json": '{"components": []}\n',
    }
    for name, text in contents.items():
        (payload / name).write_text(text)
    manifest = {
        "format": release.BUNDLE_FORMAT,
        "artifact_digest": ARTIFACT_DIGEST,
        "manifest_digest": release.sha256_bytes(contents["plugin.yaml"].encode()),
        "config_schema_digest": release.sha256_bytes(contents["config.schema.json"].encode()),
        "sbom_digest": release.sha256_bytes(contents["sbom.cdx.json"].encode()),
        "files": [
            {
                "path": f"payload/{name}",
                "bytes": (payload / name).stat().st_size,
                "sha256": release.sha256_bytes((payload / name).read_bytes()),
            }
            for name in sorted(contents)
        ],
    }
    write_manifest(staging, manifest)
    return staging, manifest


def write_manifest(staging, manifest):
    (staging / "bundle.manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))


def pack(staging, tmp_path, *, name="bundle.tar.gz"):
    bundle = tmp_path / name
    release._write_tar(staging, bundle)
    return bundle


def write_descriptor(bundle, manifest, *, digest=None, artifact_digest=None):
    descriptor = {
        "bundle_digest": digest or ("sha256:" + release.sha256_file(bundle)),
        "artifact_digest": artifact_digest or manifest["artifact_digest"],
    }
    (bundle.parent / "release.json").write_text(json.dumps(descriptor))
    return descriptor


def verify(bundle):
    return release.verify(argparse.Namespace(bundle=str(bundle)))


def test_bundle_verify_accepts_intact_release(tmp_path):
    staging, manifest = layout(tmp_path)
    bundle = pack(staging, tmp_path)
    descriptor = write_descriptor(bundle, manifest)
    assert verify(bundle) == 0
    assert descriptor["bundle_digest"] == "sha256:" + release.sha256_file(bundle)


def test_bundle_archive_is_reproducible(tmp_path):
    """固定 mtime/uid/gid 与成员排序：同一份内容必须给出同一个 bundle 摘要。"""
    staging, _ = layout(tmp_path)
    first = pack(staging, tmp_path, name="a.tar.gz")
    second = pack(staging, tmp_path, name="b.tar.gz")
    assert release.sha256_file(first) == release.sha256_file(second)


def test_bundle_verify_rejects_missing_bundle(tmp_path):
    with pytest.raises(release.BuildError, match="bundle_missing"):
        verify(tmp_path / "absent.tar.gz")


def test_bundle_verify_rejects_missing_manifest(tmp_path):
    staging, _ = layout(tmp_path)
    (staging / "bundle.manifest.json").unlink()
    bundle = pack(staging, tmp_path)
    with pytest.raises(release.BuildError, match="bundle_manifest_missing"):
        verify(bundle)


def test_bundle_verify_rejects_undeclared_or_missing_members(tmp_path):
    staging, manifest = layout(tmp_path)
    (staging / "payload" / "smuggled.txt").write_text("not in manifest\n")
    bundle = pack(staging, tmp_path)
    with pytest.raises(release.BuildError, match="bundle_member_mismatch.*smuggled"):
        verify(bundle)

    payload = staging / "payload"
    (payload / "smuggled.txt").unlink()
    (payload / "plugin.yaml").unlink()
    write_manifest(staging, manifest)
    bundle = pack(staging, tmp_path, name="short.tar.gz")
    with pytest.raises(release.BuildError, match="bundle_member_mismatch.*missing"):
        verify(bundle)


def test_bundle_verify_rejects_size_and_content_digest_drift(tmp_path):
    staging, manifest = layout(tmp_path)
    tampered = json.loads(json.dumps(manifest))
    tampered["files"][0]["bytes"] += 1
    write_manifest(staging, tampered)
    bundle = pack(staging, tmp_path, name="size.tar.gz")
    with pytest.raises(release.BuildError, match="bundle_size_mismatch"):
        verify(bundle)

    tampered = json.loads(json.dumps(manifest))
    tampered["files"][0]["sha256"] = "b" * 64
    write_manifest(staging, tampered)
    bundle = pack(staging, tmp_path, name="digest.tar.gz")
    with pytest.raises(release.BuildError, match="bundle_content_digest_mismatch"):
        verify(bundle)


def test_bundle_verify_rejects_descriptor_disagreement(tmp_path):
    staging, manifest = layout(tmp_path)
    bundle = pack(staging, tmp_path)
    # 描述符声明与落盘字节不一致：这是"传输内容被替换"的判据（API 导入时用同一口径复算）。
    write_descriptor(bundle, manifest, digest="sha256:" + "0" * 64)
    with pytest.raises(release.BuildError, match="bundle_digest_mismatch"):
        verify(bundle)

    # 描述符与 manifest 对"可执行代码身份"必须逐字一致。
    write_descriptor(bundle, manifest, artifact_digest="sha256:" + "1" * 64)
    with pytest.raises(release.BuildError, match="artifact_digest_mismatch"):
        verify(bundle)

    # 描述符缺省（尚未写出）时只对账 bundle 内部一致性。
    (bundle.parent / "release.json").unlink()
    assert verify(bundle) == 0


def test_manifest_value_parsing_helpers():
    assert release._memory_bytes("512Mi") == 512 * 1024 * 1024
    assert release._memory_bytes("1Gi") == 1024 * 1024 * 1024
    assert release._memory_bytes("2Ki") == 2048
    assert release._memory_bytes("") == 0
    assert release._cpu_millicores("1") == 1000
    assert release._cpu_millicores("0.5") == 500
    assert release._python_module({"command": ["python", "-m", "deploy_canary"]}) == "deploy_canary"
    with pytest.raises(release.BuildError, match="entrypoint_command_without_module"):
        release._python_module({"command": ["python", "server.py"]})


def test_known_plugins_registry_matches_repository_layout():
    """构建器与制品工具必须共用同一份"已知本地插件包"注册表口径。"""
    for relative, module in release.KNOWN_PLUGINS.items():
        directory = pathlib.Path(release.ROOT) / relative
        assert (directory / "plugin.yaml").is_file(), relative
        assert module.endswith(".artifact"), module
    assert (pathlib.Path(release.ROOT) / release.SDK_DIR).is_dir()
