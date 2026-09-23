"""插件产物工具：复算插件包摘要、生成 SBOM、把摘要写回 manifest。

规则（AGENTS.md / 开放插件规范）：manifest 里的 digest 必须是**真实、可复算**的产物摘要，
不允许占位串。因此这个工具做三件事，并且 `--check` 模式只读不写：

- `--check <plugin-dir>`：复算摘要并与 `plugin.yaml` 中的值比对，不一致即非 0 退出；
- `<plugin-dir>`：把复算出的摘要写回 `plugin.yaml`（唯一的写入动作）；
- `--sbom <plugin-dir>`：生成/刷新 `sbom.cdx.json`。

摘要范围由插件包自己定义（`...artifact.package_digest`），这个工具只调用它，
不另立一套算法——否则"可复算"就变成两套口径。
"""

import argparse
import importlib
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 已知的本地常驻插件包（路径 → 提供 digest/sbom 的模块）。
KNOWN_PLUGINS = {
    "plugins/python/processors/asr-whisper-mlx": "edge_material_plugin_asr_whisper_mlx.artifact",
    "plugins/python/processors/embed-bge-onnx": "edge_material_plugin_embed_bge_onnx.artifact",
    "plugins/python/processors/ocr-rapidocr": "edge_material_plugin_ocr_rapidocr.artifact",
    "plugins/python/processors/vlm-moondream": "edge_material_plugin_vlm_moondream.artifact",
}


def _artifact_module(plugin_dir: pathlib.Path):
    try:
        relative = plugin_dir.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        raise SystemExit(f"plugin dir must live inside the repo: {plugin_dir}") from None
    module_name = KNOWN_PLUGINS.get(relative)
    if module_name is None:
        raise SystemExit(f"no artifact module registered for {relative}")
    return importlib.import_module(module_name)


def manifest_digest(path: pathlib.Path) -> str:
    return str(yaml.safe_load(path.read_text())["spec"]["artifacts"]["digest"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plugin_dir", type=pathlib.Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--sbom", action="store_true")
    arguments = parser.parse_args()

    artifact = _artifact_module(arguments.plugin_dir)
    digest = artifact.package_digest(arguments.plugin_dir)
    manifest_path = arguments.plugin_dir / "plugin.yaml"

    if arguments.sbom:
        document = artifact.write_sbom(arguments.plugin_dir / "sbom.cdx.json", arguments.plugin_dir)
        print(f"sbom written components={len(document['components'])}")
        return 0

    written = manifest_digest(manifest_path)
    if arguments.check:
        if written != digest:
            print(f"digest drift: manifest={written} actual={digest}")
            return 1
        print(f"digest matches: {digest}")
        return 0

    # 只改 digest 那一行，保留 manifest 的人工排版与注释意图。
    lines = manifest_path.read_text().splitlines()
    replaced = 0
    for index, line in enumerate(lines):
        if line.strip().startswith("digest:"):
            indent = line[: len(line) - len(line.lstrip())]
            lines[index] = f"{indent}digest: {digest}"
            replaced += 1
    if replaced != 1:
        print(f"expected exactly one digest line in {manifest_path}, found {replaced}")
        return 1
    manifest_path.write_text("\n".join(lines) + "\n")
    print(f"digest written: {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
