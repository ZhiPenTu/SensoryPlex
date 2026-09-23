"""OCR 权重的身份：来自**实际被加载的那几个文件**的字节，而不是配置里写的版本号。

PP-OCR 是三个模型（检测 / 方向分类 / 识别）的组合，因此"模型身份"也必须覆盖三份权重：

- 每个文件单独算 SHA-256，并把相对角色（det/cls/rec）一起带出来；
- `artifact_digest` 是这三个（角色, 摘要）按名字排序后的规范摘要，任何一份权重换掉都会变；
- 容器先做一次真实读取（ONNX 是 protobuf，第一字节必须是 field 1 的 tag），
  损坏或截断的文件在 Start 就被拒，而不是等到第一次识别。

与 ASR 插件同一条规则（ADR-012 §2 / ADR-014 §3）：身份必须可被第三方**独立复算**。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import pathlib
from dataclasses import dataclass, field

HASH_CHUNK_BYTES = 1 << 20
# ONNX 是 protobuf：第一个字段是 `ir_version`（field 1, varint），tag 字节恒为 0x08。
ONNX_FIRST_TAG = 0x08


def installed_version(distribution: str) -> str:
    """真实安装的发行版版本：读不到包元数据就写 unknown，不猜、也不编一个好看的数字。"""
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - 依赖装了就一定在
        return "unknown"


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def probe_onnx_container(path: pathlib.Path) -> str:
    """真实读一次 ONNX 容器头：空文件、非 protobuf、被截断的文件都必须在这里失败。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(1)
    except OSError:
        raise ValueError("model_container_unreadable") from None
    if not head:
        raise ValueError("model_container_empty")
    if head[0] != ONNX_FIRST_TAG:
        raise ValueError("model_container_not_onnx")
    return "onnx"


def combined_digest(files: dict[str, str]) -> str:
    """把 {角色: 单文件摘要} 折成一个与顺序无关的规范摘要。"""
    digest = hashlib.sha256()
    for role in sorted(files):
        digest.update(role.encode())
        digest.update(b"\0")
        digest.update(files[role].encode())
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True)
class WeightsFile:
    role: str
    name: str
    digest: str
    size_bytes: int


@dataclass
class OcrModelIdentity:
    """OCR 组合模型的身份；`backend` 必须是实测值（会话真的在用哪个 provider）。"""

    model_id: str
    model_version: str
    release_id: str
    artifact_digest: str
    backend: str
    providers: dict[str, list[str]]
    runtime_version: str
    weights: list[WeightsFile]
    source: str
    directory: str = field(default="", repr=False)

    def weights_payload(self) -> list[dict]:
        return [
            {"role": item.role, "name": item.name, "sha256": item.digest, "bytes": item.size_bytes}
            for item in self.weights
        ]


def build_identity(
    *,
    model_id: str,
    revision: str,
    source: str,
    directory: pathlib.Path,
    weights: list[WeightsFile],
    providers: dict[str, list[str]],
    backend: str,
    runtime_version: str,
) -> OcrModelIdentity:
    files = {item.role: item.digest for item in weights}
    artifact_digest = combined_digest(files)
    return OcrModelIdentity(
        model_id=model_id,
        model_version=revision,
        # release id 里带组合摘要前缀：同一次配置换掉任一权重都会得到新的 release。
        release_id=f"ppocr:{model_id}@{artifact_digest.removeprefix('sha256:')[:12]}",
        artifact_digest=artifact_digest,
        backend=backend,
        providers=providers,
        runtime_version=runtime_version,
        weights=weights,
        source=source,
        directory=str(directory),
    )
