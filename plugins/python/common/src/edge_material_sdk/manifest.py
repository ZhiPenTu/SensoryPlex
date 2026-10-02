"""声明式插件契约；读取元数据不会导入插件业务代码。"""

import copy
import hashlib
import json
import re
from pathlib import Path

import yaml
from google.protobuf.json_format import MessageToDict
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from packaging.specifiers import SpecifierSet
from packaging.version import Version

SDK_VERSION = "0.1.4"
MAX_SCHEMA_BYTES = 65536
MAX_JSON_BYTES = 1 << 20
MEDIA_TYPES = {"media.video_frame", "media.audio_segment"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read_local(root, relative, limit=MAX_SCHEMA_BYTES):
    path = Path(root) / relative
    if path.is_symlink() or not path.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("manifest_path_escape")
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("manifest_file_size_rejected")
    return path.read_bytes()


def validate_schema(schema):
    """仅允许文档内引用，拒绝远程检索和递归配置默认值。"""
    if len(canonical(schema)) > MAX_SCHEMA_BYTES:
        raise ValueError("schema_size_rejected")
    todo = [(schema, 0)]
    while todo:
        value, depth = todo.pop()
        if depth > 32:
            raise ValueError("schema_depth_rejected")
        if isinstance(value, dict):
            for key in ("$ref", "$dynamicRef"):
                if key in value and not str(value[key]).startswith("#"):
                    raise ValueError("remote_schema_reference_forbidden")
            todo.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            todo.extend((item, depth + 1) for item in value)
    # 引用图必须无环，防止一个小 schema 导致无限递归或默认值展开。
    visiting, visited = set(), set()

    def visit(value):
        if not isinstance(value, (dict, list)):
            return
        identity = id(value)
        if identity in visiting:
            raise ValueError("schema_reference_cycle_forbidden")
        if identity in visited:
            return
        visiting.add(identity)
        if isinstance(value, dict):
            for key in ("$ref", "$dynamicRef"):
                if key not in value:
                    continue
                reference = value[key]
                if not isinstance(reference, str) or not reference.startswith("#/"):
                    raise ValueError("schema_reference_invalid")
                target = schema
                try:
                    for part in reference[2:].split("/"):
                        target = target[part.replace("~1", "/").replace("~0", "~")]
                except (KeyError, TypeError):
                    raise ValueError("schema_reference_invalid") from None
                visit(target)
            for item in value.values():
                visit(item)
        else:
            for item in value:
                visit(item)
        visiting.remove(identity)
        visited.add(identity)

    visit(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ValueError("schema_invalid") from None


def normalize_config(schema, config):
    """先补缺省值再校验；显式 null 不替换为默认值，整数恢复不损失精度。"""
    validate_schema(schema)

    def walk(value, rule, depth=0):
        if depth > 32:
            raise ValueError("config_depth_rejected")
        if "$ref" in rule:
            target = schema
            for part in rule["$ref"][2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            return walk(value, target, depth + 1)
        if isinstance(value, dict):
            value = copy.deepcopy(value)
            for key, child in rule.get("properties", {}).items():
                if key not in value and "default" in child:
                    value[key] = copy.deepcopy(child["default"])
                if key in value:
                    value[key] = walk(value[key], child, depth + 1)
        elif isinstance(value, list):
            value = [walk(item, rule.get("items", {}), depth + 1) for item in value]
        elif (
            rule.get("type") in ("integer", "number")
            and type(value) is float
            and value.is_integer()
        ):
            if abs(value) > (1 << 53):
                raise ValueError("config_integer_precision_rejected")
            value = int(value)
        return value

    result = walk(config, schema)
    if len(canonical(result)) > MAX_SCHEMA_BYTES:
        raise ValueError("config_size_rejected")
    if not Draft202012Validator(schema).is_valid(result):
        raise ValueError("plugin_config_invalid")
    return result


def load_manifest(root):
    root = Path(root)
    manifest = yaml.safe_load(read_local(root, "plugin.yaml"))
    if not isinstance(manifest, dict) or manifest.get("kind") != "ProcessorPlugin":
        raise ValueError("manifest_kind_invalid")
    version = manifest.get("apiVersion")
    if version not in {"edge.material.plugin/v1", "edge.material.plugin/v2"}:
        raise ValueError("manifest_version_unsupported")
    meta, spec = manifest["metadata"], manifest["spec"]
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{2,119}", meta["name"]):
        raise ValueError("plugin_id_invalid")
    Version(str(meta["version"]))
    schema = json.loads(read_local(root, spec["config"]["schema"]))
    validate_schema(schema)
    if schema.get("type") != "object":
        raise ValueError("configuration_must_be_object")
    if version.endswith("/v1"):
        return {"manifest": manifest, "config_schema": schema, "schemas": {}, "v2": False}
    if Version(SDK_VERSION) not in SpecifierSet(spec["sdk"]["runtime"]):
        raise ValueError("sdk_version_incompatible")
    entry = spec["entrypoint"]
    if entry.get("transport") != "grpc" or not re.fullmatch(
        r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)+", entry["module"]
    ):
        raise ValueError("entrypoint_invalid")
    if spec.get("artifacts", {}).get("form") != "local_native":
        raise ValueError("unsupported_form")
    resources = spec["resources"]
    for key, maximum in (("memoryBytes", 64 << 30), ("cpuMillicores", 64000)):
        if type(resources.get(key)) is not int or not 1 <= resources[key] <= maximum:
            raise ValueError("resource_limit_invalid")
    for key, maximum in (
        ("maxConcurrency", 32),
        ("maxBatchSize", 128),
        ("defaultDeadlineMs", 300000),
    ):
        if type(resources.get(key)) is not int or not 1 <= resources[key] <= maximum:
            raise ValueError("resource_limit_invalid")
    modes = spec["executionModes"]
    if not modes or not set(modes) <= {"sync", "async_enrichment"}:
        raise ValueError("execution_mode_invalid")
    if spec.get("ordering") not in {"ordered", "unordered"}:
        raise ValueError("ordering_invalid")
    if spec.get("modelApplicability") not in {"model_based", "not_applicable"}:
        raise ValueError("model_applicability_required")
    schemas = {}
    for side in ("inputs", "outputs"):
        declarations = spec[side]
        if not 1 <= len(declarations) <= 32:
            raise ValueError("contract_count_rejected")
        if len({item["modality"] for item in declarations}) != len(declarations):
            raise ValueError("duplicate_contract_modality")
        for item in declarations:
            modality = item["modality"]
            if side == "inputs" and modality in MEDIA_TYPES:
                continue
            if not re.fullmatch(r"[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+){2,}", modality):
                raise ValueError("custom_modality_namespace_required")
            contract = item["schema"]
            if not re.fullmatch(r"[a-zA-Z0-9_.:-]{3,160}", contract["id"]):
                raise ValueError("schema_identity_invalid")
            Version(str(contract["version"]))
            raw = read_local(root, contract["path"])
            actual = digest(raw)
            if contract.get("digest") and contract["digest"] != actual:
                raise ValueError("schema_digest_mismatch")
            contract["digest"] = actual
            document = json.loads(raw)
            validate_schema(document)
            schemas[actual] = document
            for pointer in item.get("textFields", []):
                if (
                    not isinstance(pointer, str)
                    or not pointer.startswith("/")
                    or len(pointer) > 256
                ):
                    raise ValueError("text_field_pointer_invalid")
    return {"manifest": manifest, "config_schema": schema, "schemas": schemas, "v2": True}


def contracts(manifest, side):
    return [
        {
            "modality": item["modality"],
            "schema_id": item["schema"]["id"],
            "schema_version": str(item["schema"]["version"]),
            "schema_digest": item["schema"]["digest"],
        }
        for item in manifest["spec"][side]
        if "schema" in item
    ]


def validate_payload(observation, declarations, schemas):
    declaration = next(
        (item for item in declarations if item["modality"] == observation.modality), None
    )
    if not declaration:
        raise ValueError("payload_modality_not_declared")
    contract = declaration["schema"]
    if (observation.schema_id, observation.schema_version, observation.schema_digest) != (
        contract["id"],
        str(contract["version"]),
        contract["digest"],
    ):
        raise ValueError("payload_schema_identity_mismatch")
    value = MessageToDict(observation.payload)
    if len(canonical(value)) > MAX_JSON_BYTES:
        raise ValueError("payload_size_rejected")
    if not Draft202012Validator(schemas[contract["digest"]]).is_valid(value):
        raise ValueError("payload_schema_invalid")


def declared_text(payload, pointers):
    """受限 JSON Pointer 文本提取，只有声明的字符串可进入现有文本索引。"""
    result = []
    for pointer in pointers[:32]:
        value = payload
        for part in pointer.split("/")[1:]:
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                value = None
                break
        if isinstance(value, str) and value.strip():
            result.append(value.strip())
    text = "\n".join(result)
    if len(text.encode()) > 65536:
        raise ValueError("index_text_size_rejected")
    return text


def artifact_digest(root):
    """固定相对路径和字节计算可执行内容身份，拒绝链接，不执行模块。"""
    root = Path(root)
    files = [root / "pyproject.toml", *sorted((root / "src").rglob("*"))]
    hashed = hashlib.sha256()
    for path in files:
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("artifact_symlink_forbidden")
        if path.is_file():
            if path.stat().st_size > 16 << 20:
                raise ValueError("artifact_file_size_rejected")
            hashed.update(path.relative_to(root).as_posix().encode() + b"\0")
            hashed.update(hashlib.sha256(path.read_bytes()).digest())
    return "sha256:" + hashed.hexdigest()
