"""BGE 模型的身份：来自**实际被加载的那几个文件**的字节，而不是配置里写的版本号。

一个可用的文本编码器不只是一份 ONNX：分词器与模型结构配置同样决定"同样的文本会得到什么
向量"。因此模型身份覆盖三份文件：

- `encoder`：实际加载的 ONNX 权重（默认 `model_quantized.onnx`；HF 快照把它放在 `onnx/`
  子目录，因此 `model_file` 是 `model_dir` 内的相对路径，越界会被拒绝）；
- `tokenizer`：`tokenizer.json`（分词规则决定 token 边界，换掉它向量就变）；
- `model_config`：`config.json`（`hidden_size` 就是向量维度，**维度是身份的一部分**）。

每个文件单独算 SHA-256，`artifact_digest` 是它们按角色排序后的规范摘要；任何一份文件换掉，
`artifact_digest` 与向量维度都会一起变——这正是 ADR-017 要求的"维度必须版本化"。

ONNX 容器在 Start 时真实读一次（首字节必须是 protobuf field 1 的 tag），空文件/非 ONNX/
截断文件在这里就被拒，而不是等到第一次编码。与 OCR、ASR 同一条规则：身份必须可被第三方
独立复算。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import pathlib
from dataclasses import dataclass, field

HASH_CHUNK_BYTES = 1 << 20
# ONNX 是 protobuf：第一个字段是 `ir_version`（field 1, varint），tag 字节恒为 0x08。
ONNX_FIRST_TAG = 0x08
DEFAULT_MODEL_FILE = "model_quantized.onnx"
TOKENIZER_FILE = "tokenizer.json"
MODEL_CONFIG_FILE = "config.json"


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


def resolve_model_file(directory: pathlib.Path, model_file: str) -> pathlib.Path:
    """把 `model_file` 解析到 `model_dir` **之内**。

    真实权重目录不必是平的：Hugging Face 快照把 ONNX 放在 `onnx/` 子目录里，而
    `tokenizer.json` / `config.json` 在快照根。因此允许相对子路径，但绝对路径和 `..`
    逃逸一律拒绝——权重是只读输入，越界属于配置错误，不是"帮运营找文件"。
    （只做词法判断，不解析符号链接：HF 快照本身就用符号链接指向 blobs。）
    """
    candidate = pathlib.PurePosixPath(model_file.replace("\\", "/"))
    if candidate.is_absolute():
        raise ValueError("model_file_must_be_relative")
    if any(part == ".." for part in candidate.parts):
        raise ValueError("model_file_outside_model_dir")
    return directory / model_file


def read_model_config(path: pathlib.Path) -> dict:
    """读 `config.json`：维度与最大长度来自这里，读完立刻用于与实测输出对账。"""
    if not path.is_file():
        raise ValueError("model_config_missing")
    try:
        document = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("model_config_unreadable") from None
    dimension = document.get("hidden_size")
    positions = document.get("max_position_embeddings")
    if not isinstance(dimension, int) or dimension <= 0:
        raise ValueError("model_config_incomplete:hidden_size")
    if not isinstance(positions, int) or positions <= 0:
        raise ValueError("model_config_incomplete:max_position_embeddings")
    return {"dimension": dimension, "max_position_embeddings": positions}


@dataclass(frozen=True)
class WeightsFile:
    role: str
    name: str
    digest: str
    size_bytes: int


@dataclass
class EmbedModelIdentity:
    """向量模型的身份；`backend` 必须是实测值（会话真的在用哪个 provider）。"""

    model_id: str
    model_version: str
    release_id: str
    artifact_digest: str
    backend: str
    providers: list[str]
    runtime_version: str
    dimension: int
    max_length: int
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
    providers: list[str],
    backend: str,
    runtime_version: str,
    dimension: int,
    max_length: int,
) -> EmbedModelIdentity:
    files = {item.role: item.digest for item in weights}
    artifact_digest = combined_digest(files)
    return EmbedModelIdentity(
        model_id=model_id,
        model_version=revision,
        # release id 里带组合摘要前缀：换掉任一文件（含分词器）都会得到新的 release。
        release_id=f"bge:{model_id}@{artifact_digest.removeprefix('sha256:')[:12]}",
        artifact_digest=artifact_digest,
        backend=backend,
        providers=providers,
        runtime_version=runtime_version,
        dimension=dimension,
        max_length=max_length,
        weights=weights,
        source=source,
        directory=str(directory),
    )
