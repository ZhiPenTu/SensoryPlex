"""vLLM 远程视觉多模态插件契约测试：输入规范化、模型自动探测、并发控制与错误映射。"""

from __future__ import annotations

import io
import json
import pathlib
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
import yaml
from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).parents[2]
PLUGIN_DIR = ROOT / "plugins/python/processors/vlm-vllm"
SCHEMA_PATH = ROOT / "docs/contracts/plugin.schema.json"

if str(PLUGIN_DIR / "src") not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR / "src"))

from edge_material_plugin_vlm_vllm import (  # noqa: E402
    PLUGIN_NAME,
    PLUGIN_VERSION,
    VisionVllmPlugin,
    VllmConfig,
    normalize_base_url,
    validate_config,
)
from edge_material_plugin_vlm_vllm.artifact import package_digest, sbom  # noqa: E402
from edge_material_plugin_vlm_vllm.png import encode_rgba  # noqa: E402
from edge_material_sdk import PluginError  # noqa: E402
from edge_material_sdk.generated.common.v1 import common_pb2 as common  # noqa: E402
from edge_material_sdk.validation import validate_observation  # noqa: E402


def test_manifest_conforms_to_platform_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    validator.validate(manifest)

    assert manifest["metadata"]["name"] == PLUGIN_NAME
    assert manifest["metadata"]["version"] == PLUGIN_VERSION
    assert manifest["spec"]["capabilities"]["consumes"] == ["media.video_frame"]
    assert manifest["spec"]["capabilities"]["produces"] == ["observation.vision.scene_description"]
    assert manifest["spec"]["resources"]["maxConcurrency"] == 64
    assert manifest["spec"]["security"]["allowedHosts"] == ["*"]


def test_normalize_base_url():
    assert normalize_base_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000/v1"
    assert normalize_base_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1"
    assert normalize_base_url("http://127.0.0.1:8000/v1/") == "http://127.0.0.1:8000/v1"
    assert (
        normalize_base_url("http://127.0.0.1:8000/v1/chat/completions")
        == "http://127.0.0.1:8000/v1"
    )
    assert (
        normalize_base_url("https://api.vllm.internal:8443/chat/completions")
        == "https://api.vllm.internal:8443/v1"
    )

    with pytest.raises(ValueError, match="base_url_must_start_with_http_or_https"):
        normalize_base_url("ftp://127.0.0.1:8000")


def test_validate_config_boundaries():
    assert validate_config(
        {
            "base_url": "http://127.0.0.1:8000",
            "data_plane_mode": "per_request",
        }
    ).valid
    assert validate_config(
        {
            "base_url": "http://127.0.0.1:8000/v1",
            "handoff_endpoint": "127.0.0.1:50051",
            "api_key": "sk-secret-token",
            "max_concurrency": 4,
            "model": "Qwen/Qwen2-VL-7B-Instruct",
            "prompt": "Custom prompt",
        }
    ).valid

    # 缺少 base_url
    assert not validate_config({}).valid
    # 并发数越界
    assert not validate_config(
        {
            "base_url": "http://127.0.0.1:8000",
            "data_plane_mode": "per_request",
            "max_concurrency": 0,
        }
    ).valid
    assert not validate_config(
        {
            "base_url": "http://127.0.0.1:8000",
            "data_plane_mode": "per_request",
            "max_concurrency": 65,
        }
    ).valid
    # 模式错误
    assert not validate_config(
        {"base_url": "http://127.0.0.1:8000", "data_plane_mode": "unknown"}
    ).valid
    # static 模式缺失 handoff_endpoint
    assert not validate_config(
        {"base_url": "http://127.0.0.1:8000", "data_plane_mode": "static"}
    ).valid
    assert validate_config(
        {
            "base_url": "http://127.0.0.1:8000",
            "data_plane_mode": "static",
            "handoff_endpoint": "127.0.0.1:50051",
        }
    ).valid


def test_config_hash_masks_api_key():
    c1 = VllmConfig(base_url="http://127.0.0.1:8000/v1", api_key="secret-1")
    c2 = VllmConfig(base_url="http://127.0.0.1:8000/v1", api_key="secret-1")
    c3 = VllmConfig(base_url="http://127.0.0.1:8000/v1", api_key="secret-2")

    assert c1.config_hash() == c2.config_hash()
    assert c1.config_hash() != c3.config_hash()
    # 确保明文 API Key 绝不在 effective dict 中流出
    assert "secret-1" not in json.dumps(c1.effective())


def _mock_http_response(payload: dict, status: int = 200):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    response.__exit__.return_value = None
    return response


def test_probe_model_auto_detect():
    digest = package_digest(PLUGIN_DIR)
    plugin = VisionVllmPlugin(artifact_digest=digest)

    mock_models_response = {
        "data": [
            {"id": "Qwen/Qwen2-VL-7B-Instruct", "created": 1720000000, "version": "vllm-0.6.0"}
        ]
    }

    with patch("urllib.request.urlopen", return_value=_mock_http_response(mock_models_response)):
        plugin.configure(
            {
                "base_url": "http://127.0.0.1:8000",
                "data_plane_mode": "per_request",
            }
        )

    assert plugin.started
    assert plugin.model is not None
    assert plugin.model.model_id == "Qwen/Qwen2-VL-7B-Instruct"
    assert plugin.model.backend == "vllm-remote-openai"


def test_probe_model_explicit_override():
    digest = package_digest(PLUGIN_DIR)
    plugin = VisionVllmPlugin(artifact_digest=digest)

    mock_models_response = {
        "data": [
            {"id": "Qwen/Qwen2-VL-7B-Instruct", "created": 1720000000},
            {"id": "OpenGVLab/InternVL2-8B", "created": 1720000100},
        ]
    }

    with patch("urllib.request.urlopen", return_value=_mock_http_response(mock_models_response)):
        plugin.configure(
            {
                "base_url": "http://127.0.0.1:8000",
                "model": "OpenGVLab/InternVL2-8B",
                "data_plane_mode": "per_request",
            }
        )

    assert plugin.model.model_id == "OpenGVLab/InternVL2-8B"

    # 测试找不到显式指定的模型时抛出明确异常
    with patch("urllib.request.urlopen", return_value=_mock_http_response(mock_models_response)):
        with pytest.raises(ValueError, match="model_not_available:non-existent-model"):
            plugin.configure(
                {
                    "base_url": "http://127.0.0.1:8000",
                    "model": "non-existent-model",
                    "data_plane_mode": "per_request",
                }
            )


@pytest.mark.asyncio
async def test_describe_png_and_telemetry():
    digest = package_digest(PLUGIN_DIR)
    plugin = VisionVllmPlugin(artifact_digest=digest)

    mock_models_response = {"data": [{"id": "Qwen/Qwen2-VL-7B-Instruct", "created": 1720000000}]}
    mock_chat_response = {
        "choices": [
            {"message": {"content": "A high-speed train traveling through a rural landscape."}}
        ],
        "usage": {"prompt_tokens": 150, "completion_tokens": 14, "total_tokens": 164},
    }

    def fake_urlopen(req, *args, **kwargs):
        if req.full_url.endswith("/models"):
            return _mock_http_response(mock_models_response)
        if req.full_url.endswith("/chat/completions"):
            # 校验 Header 中携带了 Authorization
            assert req.headers.get("Authorization") == "Bearer sk-test-key"
            # 校验请求体格式为 OpenAI Vision 多模态格式
            body = json.loads(req.data.decode("utf-8"))
            assert body["model"] == "Qwen/Qwen2-VL-7B-Instruct"
            assert body["messages"][0]["content"][0]["type"] == "image_url"
            assert body["messages"][0]["content"][0]["image_url"]["url"].startswith(
                "data:image/png;base64,"
            )
            return _mock_http_response(mock_chat_response)
        raise ValueError(f"unexpected url: {req.full_url}")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        plugin.configure(
            {
                "base_url": "http://127.0.0.1:8000",
                "api_key": "sk-test-key",
                "data_plane_mode": "per_request",
            }
        )

        dummy_png = encode_rgba(2, 2, b"\x00\x00\x00\xff" * 4)
        text, telemetry = await plugin._describe_png(dummy_png)

        assert text == "A high-speed train traveling through a rural landscape."
        assert telemetry["model"] == "Qwen/Qwen2-VL-7B-Instruct"
        assert telemetry["prompt_tokens"] == 150
        assert telemetry["completion_tokens"] == 14


@pytest.mark.asyncio
async def test_remote_rate_limit_error_mapping():
    digest = package_digest(PLUGIN_DIR)
    plugin = VisionVllmPlugin(artifact_digest=digest)

    mock_models_response = {"data": [{"id": "Qwen/Qwen2-VL-7B-Instruct", "created": 1720000000}]}

    def fake_urlopen(req, *args, **kwargs):
        if req.full_url.endswith("/models"):
            return _mock_http_response(mock_models_response)
        if req.full_url.endswith("/chat/completions"):
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"rate limit exceeded")
            )
        raise ValueError("unexpected url")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        plugin.configure(
            {
                "base_url": "http://127.0.0.1:8000",
                "data_plane_mode": "per_request",
            }
        )
        dummy_png = encode_rgba(2, 2, b"\x00\x00\x00\xff" * 4)
        with pytest.raises(PluginError) as exc_info:
            await plugin._describe_png(dummy_png)

        # 429 必须映射为 RESOURCE_EXHAUSTED 且 retryable=True
        assert exc_info.value.code == common.RESOURCE_EXHAUSTED
        assert exc_info.value.reason_code == "remote_api_rate_limited"
        assert exc_info.value.retryable is True


def test_describe_decoded_anchor_observation():
    digest = package_digest(PLUGIN_DIR)
    plugin = VisionVllmPlugin(artifact_digest=digest)

    mock_models_response = {"data": [{"id": "Qwen/Qwen2-VL-7B-Instruct", "created": 1720000000}]}
    mock_chat_response = {
        "choices": [{"message": {"content": "Scene observation test."}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
    }

    def fake_urlopen(req, *args, **kwargs):
        if req.full_url.endswith("/models"):
            return _mock_http_response(mock_models_response)
        if req.full_url.endswith("/chat/completions"):
            return _mock_http_response(mock_chat_response)
        raise ValueError("unexpected url")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        plugin.configure(
            {
                "base_url": "http://127.0.0.1:8000",
                "data_plane_mode": "per_request",
            }
        )
        dummy_png = encode_rgba(2, 2, b"\x00\x00\x00\xff" * 4)
        obs = plugin.describe_decoded_anchor(
            stream_id="stream_001",
            source_id="src_001",
            source_item_id="item_001",
            start_ms=1000,
            end_ms=2000,
            png=dummy_png,
            task_config_hash="sha256:" + "b" * 64,
        )

        validate_observation(obs)
        assert obs.modality == "vision.scene_description"
        assert obs.payload["text"] == "Scene observation test."
        assert obs.provenance.plugin == PLUGIN_NAME
        assert obs.provenance.model_id == "Qwen/Qwen2-VL-7B-Instruct"
        assert obs.provenance.execution_backend == "vllm-remote-openai"


def test_package_digest_and_sbom():
    digest = package_digest(PLUGIN_DIR)
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["spec"]["artifacts"]["digest"] == digest

    bom = sbom(PLUGIN_DIR)
    assert bom["bomFormat"] == "CycloneDX"
    assert len(bom["components"]) >= 3
