import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SCHEMA = json.loads((Path(__file__).parents[2] / "docs/contracts/plugin.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)


@pytest.fixture
def manifest():
    return {
        "apiVersion": "edge.material.plugin/v1",
        "kind": "ProcessorPlugin",
        "metadata": {
            "name": "org.sensoryplex.contract",
            "version": "0.1.0",
            "vendor": "test",
            "license": "UNLICENSED",
            "description": "contract fixture only",
        },
        "spec": {
            "sdk": {"runtime": ">=0.1.0 <0.2.0", "protocol": "v1"},
            "entrypoint": {"transport": "grpc", "command": ["test-only"], "port": 50051},
            "capabilities": {
                "consumes": ["media.video_frame"],
                "produces": ["observation.ocr"],
                "supports": {"cancellation": True, "retry": "idempotent"},
            },
            "resources": {
                "cpu": "1",
                "memory": "1Gi",
                "maxConcurrency": 1,
                "maxBatchSize": 1,
                "defaultDeadlineMs": 1000,
            },
            "security": {
                "network": "none",
                "dataEgress": "local_only",
                "filesystem": {"readOnly": True, "writablePaths": ["/tmp"]},
            },
            "artifacts": {
                "image": "example.invalid/test:0.1.0",
                "digest": "sha256:" + "a" * 64,
                "sbom": "test.spdx.json",
                "signature": "test-only",
            },
        },
    }


def test_valid_manifest(manifest):
    Draft202012Validator.check_schema(SCHEMA)
    VALIDATOR.validate(manifest)


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("artifacts", "digest", "sha256:REPLACE_WITH_REAL_DIGEST"),
        ("artifacts", "image", "test:latest"),
        ("resources", "maxConcurrency", 0),
        ("resources", "cpu", "0"),
        ("security", "dataEgress", "cloud"),
        ("security", "network", "allowlist"),
    ],
)
def test_rejects_unsafe_or_incomplete_manifest(manifest, section, field, value):
    mutated = copy.deepcopy(manifest)
    mutated["spec"][section][field] = value
    assert list(VALIDATOR.iter_errors(mutated))
