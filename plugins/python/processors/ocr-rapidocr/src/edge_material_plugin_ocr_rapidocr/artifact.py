"""插件包内容的规范摘要与 SBOM：都是可复算的真实产物，不是占位符。

摘要范围刻意**不含** `plugin.yaml`：manifest 要写这个摘要，把 manifest 本身算进去就自指了。
README 与解释器缓存同样不算（它们不改变执行行为）。因此"改一行代码 → 摘要变化，
改一段说明文字 → 摘要不变"是可以被复算验证的。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import tomllib

DIGEST_INPUTS = ("pyproject.toml", "src")
EXCLUDED_PARTS = {"__pycache__", ".venv", ".DS_Store"}


def package_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _iter_files(root: pathlib.Path):
    for name in DIGEST_INPUTS:
        target = root / name
        if target.is_file():
            yield target
        elif target.is_dir():
            for path in sorted(target.rglob("*")):
                if (
                    path.is_file()
                    and not EXCLUDED_PARTS.intersection(path.parts)
                    and path.suffix != ".pyc"
                ):
                    yield path


def package_digest(root: pathlib.Path | None = None) -> str:
    """按"相对路径 + 文件内容摘要"顺序无关地复算出插件包摘要。"""
    root = root or package_root()
    digest = hashlib.sha256()
    for path in sorted(_iter_files(root)):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return "sha256:" + digest.hexdigest()


def sbom(root: pathlib.Path | None = None) -> dict:
    """最小 SBOM：只声明真实存在的东西——插件组件与它声明的运行时依赖。"""
    root = root or package_root()
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    components = []
    for requirement in project.get("dependencies", []):
        name, _, version = requirement.partition("==")
        components.append(
            {"type": "library", "name": name.strip(), "version": version.strip() or "unspecified"}
        )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": project["name"],
                "version": project["version"],
                "properties": [
                    {"name": "sensoryplex:package_digest", "value": package_digest(root)}
                ],
            },
            "tools": [{"name": "sensoryplex-plugin-artifact", "version": "0.1.0"}],
        },
        "components": components,
    }


def write_sbom(path: pathlib.Path, root: pathlib.Path | None = None) -> dict:
    document = sbom(root)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return document
