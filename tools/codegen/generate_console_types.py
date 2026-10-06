"""从生成的 Proto descriptor 生成浏览器使用的 JSON 类型，避免重复定义契约。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

from edge_material_sdk.generated.gateway.v1 import console_pb2, gateway_pb2  # noqa: E402
from edge_material_sdk.generated.node.v1 import node_pb2  # noqa: E402
from google.protobuf.descriptor import FieldDescriptor as F  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "apps/console/src/api/contracts.ts"


def generate():
    files = {}

    def collect(file):
        if file.name.startswith("google/") or file.name in files:
            return
        for dependency in file.dependencies:
            collect(dependency)
        files[file.name] = file

    collect(console_pb2.DESCRIPTOR)
    collect(gateway_pb2.DESCRIPTOR)
    collect(node_pb2.DESCRIPTOR)
    lines = [
        "// Generated from proto/ by tools/generate_console_types.py. Do not edit.",
        "export type JsonValue = string | number | boolean | null | JsonValue[] | JsonObject;",
        "export interface JsonObject { [key: string]: JsonValue; }",
    ]
    for file in files.values():
        for enum in file.enum_types_by_name.values():
            lines.append(
                f"export type {enum.name} = " + " | ".join(f'"{v.name}"' for v in enum.values) + ";"
            )
        for message in file.message_types_by_name.values():
            lines.append(f"export interface {message.name} {{")
            for field in message.fields:
                is_map = False
                if field.type == F.TYPE_MESSAGE:
                    if field.message_type.GetOptions().map_entry:
                        is_map = True
                        kind = "Record<string, string>"
                    elif field.message_type.full_name == "google.protobuf.Struct":
                        kind = "JsonObject"
                    else:
                        kind = field.message_type.name
                elif field.type == F.TYPE_ENUM:
                    kind = field.enum_type.name
                elif field.type == F.TYPE_BOOL:
                    kind = "boolean"
                elif field.type in {
                    F.TYPE_STRING,
                    F.TYPE_BYTES,
                    F.TYPE_INT64,
                    F.TYPE_UINT64,
                    F.TYPE_SINT64,
                    F.TYPE_FIXED64,
                    F.TYPE_SFIXED64,
                }:
                    kind = "string"
                else:
                    kind = "number"
                if field.label == F.LABEL_REPEATED and not is_map:
                    kind += "[]"
                optional = "?" if field.has_presence else ""
                lines.append(f"  {field.name}{optional}: {kind};")
            lines.append("}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n")
    print(f"Generated {OUT.relative_to(ROOT)} from {len(files)} Proto descriptors")


if __name__ == "__main__":
    generate()
