"""通用插件的拒绝边界；测试事实不能代替真实媒体验收。"""

import tarfile

import pytest
import yaml
from edge_material_sdk.cli import create
from edge_material_sdk.generated.material.v1 import material_pb2 as material
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime
from edge_material_sdk.manifest import (
    artifact_digest,
    declared_text,
    load_manifest,
    normalize_config,
    validate_payload,
    validate_schema,
)
from edge_material_sdk.processor import ProcessorPlugin
from edge_material_sdk.release import (
    file_digest,
    keygen,
    require_locked_sdk,
    sign,
    verify_bundle,
    verify_signature,
)
from edge_material_sdk.validation import validate_observation


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "independent"
    create(root, "com.example.brightness")
    return root


def test_metadata_does_not_import_business_code(project):
    (project / "src/custom_plugin/plugin.py").write_text('raise RuntimeError("must_not_import")')
    registration = load_manifest(project)
    assert registration["v2"]
    assert len(registration["schemas"]) == 1
    assert artifact_digest(project).startswith("sha256:")


def test_nested_config_defaults_null_and_integer():
    schema = {
        "type": "object",
        "properties": {
            "nested": {
                "type": "object",
                "default": {},
                "properties": {"threshold": {"type": "integer", "default": 80}},
            },
            "values": {"type": "array", "items": {"type": "integer"}},
            "optional": {"type": ["string", "null"], "default": "default"},
        },
        "additionalProperties": False,
    }
    result = normalize_config(schema, {"values": [1.0, 2.0], "optional": None})
    assert result == {"nested": {"threshold": 80}, "values": [1, 2], "optional": None}
    assert type(result["values"][0]) is int
    with pytest.raises(ValueError, match="plugin_config_invalid"):
        normalize_config(schema, {"values": [1.1]})


@pytest.mark.parametrize(
    "reference",
    ["https://example.invalid/schema", "file:///etc/passwd", "other.json", "//host/schema"],
)
def test_schema_network_and_local_file_references_rejected(reference):
    with pytest.raises(ValueError, match="remote_schema_reference_forbidden"):
        validate_schema({"$ref": reference})


def test_symlink_and_schema_tampering_rejected(project, tmp_path):
    manifest = yaml.safe_load((project / "plugin.yaml").read_text())
    manifest["spec"]["outputs"][0]["schema"]["digest"] = "sha256:" + "a" * 64
    (project / "plugin.yaml").write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError, match="schema_digest_mismatch"):
        load_manifest(project)
    (project / "src/escape").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="artifact_symlink_forbidden"):
        artifact_digest(project)


def test_custom_payload_identity_and_values_are_both_checked(project, observation):
    registration = load_manifest(project)
    declaration = registration["manifest"]["spec"]["outputs"][0]
    observation.modality = declaration["modality"]
    observation.schema_id = declaration["schema"]["id"]
    observation.schema_version = declaration["schema"]["version"]
    observation.schema_digest = declaration["schema"]["digest"]
    validate_payload(observation, [declaration], registration["schemas"])
    observation.payload.update({"text": 1})
    with pytest.raises(ValueError, match="payload_schema_invalid"):
        validate_payload(observation, [declaration], registration["schemas"])
    observation.schema_digest = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="payload_schema_identity_mismatch"):
        validate_payload(observation, [declaration], registration["schemas"])


def test_non_model_does_not_accept_fake_model_identity(observation):
    p = observation.provenance
    p.model_applicability = material.MODEL_APPLICABILITY_NOT_APPLICABLE
    p.processor_release_id = "rel_independent"
    with pytest.raises(ValueError, match="non_model_provenance_invalid"):
        validate_observation(observation)
    for field in ("model_id", "model_version", "model_release_id", "model_artifact_digest"):
        p.ClearField(field)
    validate_observation(observation)


async def test_explicit_no_detection_is_success_but_ambiguous_empty_fails(observation):
    from tests.contracts.test_sdk import make_request

    class Empty(ProcessorPlugin):
        def describe(self):
            return runtime.PluginDescription()

        async def process(self, request, token):
            return runtime.ProcessResponse(
                outcome=runtime.PROCESS_OUTCOME_NO_OBSERVATIONS, outcome_reason="below_threshold"
            )

    plugin = Empty()
    response = await plugin.invoke(make_request(observation))
    assert not response.HasField("error") and not response.observations

    async def ambiguous(request, token):
        return runtime.ProcessResponse(outcome=runtime.PROCESS_OUTCOME_NO_OBSERVATIONS)

    plugin.process = ambiguous
    assert (
        await plugin.invoke(make_request(observation))
    ).error.reason_code == "ambiguous_plugin_result"


def test_declared_text_does_not_index_undeclared_values():
    assert (
        declared_text(
            {"text": "accepted", "secret": "hidden", "events": ["first"]}, ["/text", "/events/0"]
        )
        == "accepted\nfirst"
    )
    assert declared_text({"secret": "hidden"}, []) == ""


def test_ed25519_descriptor_tampering_and_wrong_key(tmp_path):
    private, public = tmp_path / "private.pem", tmp_path / "public.pem"
    keygen(private, public)
    descriptor = {"release_id": "rel_test", "bundle_digest": "sha256:" + "a" * 64}
    signature = sign(descriptor, private)
    verify_signature(descriptor, signature, public.read_bytes())
    descriptor["bundle_digest"] = "sha256:" + "b" * 64
    with pytest.raises(ValueError, match="release_signature_invalid"):
        verify_signature(descriptor, signature, public.read_bytes())
    assert private.stat().st_mode & 0o777 == 0o600


def test_signed_digest_does_not_make_unsafe_tar_acceptable(tmp_path):
    bundle = tmp_path / "bad.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        item = tarfile.TarInfo("../escape")
        archive.addfile(item)
    with pytest.raises(ValueError, match="unsafe_member"):
        verify_bundle(
            bundle, {"bundle_digest": file_digest(bundle), "bundle_bytes": bundle.stat().st_size}
        )


def test_sdk_wheel_must_match_project_lock_bytes(tmp_path):
    wheel = tmp_path / "edge_material_sdk-0.1.4-py3-none-any.whl"
    wheel.write_bytes(b"contract fixture only")
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname="edge-material-sdk"\nversion="0.1.4"\n'
        'wheels=[{hash="' + file_digest(wheel) + '"}]\n'
    )
    require_locked_sdk(tmp_path, wheel, ">=0.1.1,<0.2")
    wheel.write_bytes(b"different bytes")
    with pytest.raises(ValueError, match="sdk_wheel_lock_mismatch"):
        require_locked_sdk(tmp_path, wheel, ">=0.1.1,<0.2")
