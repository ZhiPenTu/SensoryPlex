"""生成并命名空间化 Python 绑定；Rust 绑定由 cargo 构建。"""

import re
from pathlib import Path

import grpc_tools
from grpc_tools import protoc

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "plugins/python/common/src/edge_material_sdk/generated"
# 命名空间化的顶层包名：新增 proto 包时必须同步这里，否则生成代码里的
# `from <pkg>.<ver> import ...` 不会被改写成 SDK 的命名空间，导入直接失败。
PACKAGES = "common|material|media|runtime|gateway|index"


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
            rf"^from ({PACKAGES})(\.\S+) import ",
            r"from edge_material_sdk.generated.\1\2 import ",
            path.read_text(),
            flags=re.MULTILINE,
        )
        code = re.sub(
            rf"(BuildTopDescriptorsAndMessages\(DESCRIPTOR, ')({PACKAGES})(\.)",
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
