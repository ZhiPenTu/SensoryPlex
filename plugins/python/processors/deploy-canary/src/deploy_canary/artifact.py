"""自检探针的包摘要：与首方插件同一口径（`pyproject.toml` + `src`），可复算。

探针不带任何业务数据，但它必须和真插件一样能**自证**可执行内容，否则热部署执行器的
"候选身份/摘要核对"就验不到东西。
"""

from __future__ import annotations

import hashlib
import pathlib

DIGEST_INPUTS = ("pyproject.toml", "src")
EXCLUDED_PARTS = {"__pycache__", ".venv", ".DS_Store"}


def package_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def package_digest(root: pathlib.Path | None = None) -> str:
    root = root or package_root()
    digest = hashlib.sha256()
    for path in sorted(_iter_files(root)):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return "sha256:" + digest.hexdigest()


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


def _split_requirement(requirement: str) -> tuple[str, str]:
    for index, character in enumerate(requirement):
        if character in "<>=!~[; ":
            return requirement[:index].strip(), requirement[index:].strip()
    return requirement.strip(), ""


def sbom(root: pathlib.Path | None = None) -> dict:
    """最小 SBOM：只声明真实存在的东西——探针组件与它声明的运行时依赖。"""
    import tomllib

    root = root or package_root()
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    components = []
    for requirement in project.get("dependencies", []):
        name, specifier = _split_requirement(requirement)
        pinned = specifier.startswith("==") and "," not in specifier
        component = {
            "type": "library",
            "name": name,
            "version": specifier[2:] if pinned else "unspecified",
        }
        if specifier and not pinned:
            component["properties"] = [
                {"name": "sensoryplex:version_specifier", "value": specifier}
            ]
        components.append(component)
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
    import json

    document = sbom(root)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return document
