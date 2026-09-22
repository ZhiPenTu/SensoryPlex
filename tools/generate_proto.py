"""Generate and namespace Python bindings; Rust bindings are built by cargo."""

import re
from pathlib import Path

import grpc_tools
from grpc_tools import protoc

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "plugins/python/common/src/edge_material_sdk/generated"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    files = sorted((ROOT / "proto").rglob("*.proto"))
    result = protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{ROOT / 'proto'}",
            f"-I{Path(grpc_tools.__file__).parent / '_proto'}",
            f"--python_out={OUT}",
            f"--pyi_out={OUT}",
            f"--grpc_python_out={OUT}",
            *map(str, files),
        ]
    )
    if result:
        raise SystemExit(result)
    for path in (p for p in OUT.rglob("*") if p.suffix in {".py", ".pyi"}):
        code = re.sub(
            r"^from (common|material|runtime|gateway)(\.\S+) import ",
            r"from edge_material_sdk.generated.\1\2 import ",
            path.read_text(),
            flags=re.MULTILINE,
        )
        code = re.sub(
            r"(BuildTopDescriptorsAndMessages\(DESCRIPTOR, ')(common|material|runtime|gateway)(\.)",
            r"\1edge_material_sdk.generated.\2\3",
            code,
        )
        path.write_text(code)
    for directory in [
        OUT,
        *(p for p in OUT.rglob("*") if p.is_dir() and "__pycache__" not in p.parts),
    ]:
        (directory / "__init__.py").touch()
    print(f"Generated {len(files)} protobuf contracts")


if __name__ == "__main__":
    main()
