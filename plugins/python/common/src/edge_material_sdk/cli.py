"""sensoryplex-plugin：独立仓库初始化、校验、构建、签名和验证。"""

import argparse
import json
import tarfile
from pathlib import Path

import yaml

from .manifest import SDK_VERSION, artifact_digest, load_manifest
from .release import build, keygen, public_identity, sign, verify_bundle, verify_signature


def create(root, plugin_id):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    (root / "src/custom_plugin").mkdir(parents=True)
    (root / "src/custom_plugin/__init__.py").write_text("")
    (
        root / "src/custom_plugin/plugin.py"
    ).write_text('''"""独立算法插件；替换 process 后使用真实输入进行验收。"""
from edge_material_sdk.processor import ProcessorPlugin
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as pb

class Plugin(ProcessorPlugin):
    def describe(self):
        return pb.PluginDescription()

    async def process(self, request, cancel_token):
        cancel_token.raise_if_cancelled()
        return pb.ProcessResponse(
            outcome=pb.PROCESS_OUTCOME_NO_OBSERVATIONS,
            outcome_reason="algorithm_has_no_detection",
        )

def create_plugin(config, registration, artifact):
    limits = registration["manifest"]["spec"]["resources"]
    return Plugin(max_concurrency=limits["maxConcurrency"], max_batch_size=limits["maxBatchSize"])
''')
    (root / "pyproject.toml").write_text("""[project]
name = "custom-plugin"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = ["edge-material-sdk==0.1.4"]
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[tool.hatch.build.targets.wheel]
packages = ["src/custom_plugin"]
""")
    (root / "config.schema.json").write_text(
        json.dumps({"type": "object", "properties": {}, "additionalProperties": False})
    )
    (root / "result.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            }
        )
    )
    manifest = {
        "apiVersion": "edge.material.plugin/v2",
        "kind": "ProcessorPlugin",
        "metadata": {"name": plugin_id, "version": "0.1.0"},
        "spec": {
            "sdk": {"runtime": ">=0.1.1,<0.2", "protocol": "v1"},
            "entrypoint": {
                "transport": "grpc",
                "module": "custom_plugin.plugin",
                "factory": "create_plugin",
            },
            "artifacts": {"form": "local_native"},
            "config": {"schema": "config.schema.json"},
            "modelApplicability": "not_applicable",
            "executionModes": ["sync", "async_enrichment"],
            "ordering": "ordered",
            "resources": {
                "maxConcurrency": 1,
                "maxBatchSize": 8,
                "defaultDeadlineMs": 30000,
                "memoryBytes": 268435456,
                "cpuMillicores": 1000,
            },
            "inputs": [{"modality": "media.video_frame"}],
            "outputs": [
                {
                    "modality": plugin_id + ".result",
                    "schema": {
                        "id": plugin_id + ".result",
                        "version": "1.0.0",
                        "path": "result.schema.json",
                    },
                    "textFields": ["/text"],
                }
            ],
        },
    }
    (root / "plugin.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    (root / "README.md").write_text(
        "# "
        + plugin_id
        + "\n\nInstall the SDK wheel, resolve your own uv.lock, implement process, "
        + "then validate/build/sign.\n"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sensoryplex-plugin")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("create")
    init.add_argument("project")
    init.add_argument("--id", required=True)
    for command in ("validate", "compat"):
        sub.add_parser(command).add_argument("project")
    builder = sub.add_parser("build")
    builder.add_argument("project")
    builder.add_argument("--out", required=True)
    builder.add_argument("--sdk-wheel", required=True)
    keys = sub.add_parser("keygen")
    keys.add_argument("--private-key", required=True)
    keys.add_argument("--public-key", required=True)
    signer = sub.add_parser("sign")
    signer.add_argument("release")
    signer.add_argument("--private-key", required=True)
    verifier = sub.add_parser("verify")
    verifier.add_argument("release")
    verifier.add_argument("--public-key", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            create(args.project, args.id)
            result = {"created": args.id}
        elif args.command in {"validate", "compat"}:
            registration = load_manifest(args.project)
            result = {
                "valid": True,
                "sdk": SDK_VERSION,
                "plugin_id": registration["manifest"]["metadata"]["name"],
                "artifact_digest": artifact_digest(args.project),
            }
        elif args.command == "build":
            result = build(args.project, args.out, args.sdk_wheel)
        elif args.command == "keygen":
            keygen(args.private_key, args.public_key)
            result = {"signer_id": public_identity(Path(args.public_key).read_bytes())}
        else:
            folder = Path(args.release)
            descriptor = json.loads((folder / "release.json").read_bytes())
            verify_bundle(folder / "bundle.tar.gz", descriptor)
            if args.command == "sign":
                (folder / "release.sig").write_text(sign(descriptor, args.private_key))
                result = {"signed": descriptor["release_id"]}
            else:
                verify_signature(
                    descriptor,
                    (folder / "release.sig").read_text(),
                    Path(args.public_key).read_bytes(),
                )
                result = {"verified": descriptor["release_id"]}
        print(json.dumps(result))
        return 0
    except (ValueError, KeyError, TypeError, OSError, tarfile.TarError, yaml.YAMLError) as error:
        print(
            json.dumps(
                {"error": str(error) if isinstance(error, ValueError) else "plugin_project_invalid"}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
