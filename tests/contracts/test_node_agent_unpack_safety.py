"""`tools/node_agent_platform.safe_extract_bundle` 的解包硬上限与「先校验再落盘」（ADR-030）。

设计要求 Agent 在**写盘之前**拒绝路径穿越、符号链接、超限成员与过深路径。真实尺寸的上限
（单个成员 2GiB、整包解压 4GiB、20000 个成员）不可能在单元测试里构造，所以这里把上限常量
临时调小，验证「命中上限即拒绝」的判定、稳定错误码，以及目标目录里**没有留下半个包**。

宿主 `--scope native` 覆盖的是真实归档的**形状类**拒收（符号链接 / `..` / 绝对路径 / 成员不符 /
摘要不符）；上限类拒收由本文件负责，两者互补，谁都不能替代谁。
"""

import io
import pathlib
import tarfile

import pytest

from tools import node_agent_platform as platform


def bundle_with(tmp_path: pathlib.Path, members: dict[str, bytes]) -> pathlib.Path:
    """把 `{成员名: 内容}` 打成 `.tar.gz`，充当受控 bundle。"""
    path = tmp_path / "bundle.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def test_member_count_limit_is_rejected_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "MAX_MEMBERS", 2)
    bundle = bundle_with(tmp_path, {"a.txt": b"a", "b.txt": b"b", "c.txt": b"c"})
    target = tmp_path / "unpacked"

    with pytest.raises(platform.HotDeployError) as failure:
        platform.safe_extract_bundle(bundle, target)

    assert failure.value.code == "release_bundle_too_many_members"
    # 拒绝必须发生在写盘之前：目录里不能有半个包。
    assert list(target.rglob("*")) == []


def test_oversize_member_is_rejected_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "MAX_MEMBER_BYTES", 4)
    bundle = bundle_with(tmp_path, {"big.bin": b"12345"})
    target = tmp_path / "unpacked"

    with pytest.raises(platform.HotDeployError) as failure:
        platform.safe_extract_bundle(bundle, target)

    assert failure.value.code == "release_bundle_member_too_large"
    assert list(target.rglob("*")) == []


def test_uncompressed_total_limit_is_rejected_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "MAX_MEMBER_BYTES", 1 << 20)
    monkeypatch.setattr(platform, "MAX_BUNDLE_BYTES", 4)
    bundle = bundle_with(tmp_path, {"a.bin": b"aaa", "b.bin": b"bbb"})
    target = tmp_path / "unpacked"

    with pytest.raises(platform.HotDeployError) as failure:
        platform.safe_extract_bundle(bundle, target)

    assert failure.value.code == "release_bundle_uncompressed_too_large"
    assert list(target.rglob("*")) == []


def test_deep_path_is_rejected_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "MAX_PATH_DEPTH", 2)
    bundle = bundle_with(tmp_path, {"a/b/c.txt": b"x"})
    target = tmp_path / "unpacked"

    with pytest.raises(platform.HotDeployError) as failure:
        platform.safe_extract_bundle(bundle, target)

    assert failure.value.code == "release_bundle_path_too_deep"
    assert list(target.rglob("*")) == []


def test_valid_bundle_extracts_and_returns_manifest(tmp_path):
    """对照组：合法小包必须真的落盘并返回 manifest，证明上面的拒收不是"什么都拒绝"。"""
    manifest = b'{"format": "sensoryplex.release-bundle/1", "files": []}\n'
    bundle = bundle_with(
        tmp_path,
        {"bundle.manifest.json": manifest, "payload/plugin.yaml": b"metadata: {}\n"},
    )
    target = tmp_path / "unpacked"

    assert platform.safe_extract_bundle(bundle, target) == {
        "format": "sensoryplex.release-bundle/1",
        "files": [],
    }
    assert (target / "bundle.manifest.json").read_bytes() == manifest
    assert (target / "payload/plugin.yaml").read_bytes() == b"metadata: {}\n"
