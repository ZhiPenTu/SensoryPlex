"""Manifest v2 的标准 gRPC 生命周期宿主；业务模块只在 Start 后装载。"""

import argparse
import asyncio
import hashlib
import importlib
import sys
import time
from pathlib import Path

import grpc
from google.protobuf.json_format import MessageToDict

from .buffer_reader import is_loopback_endpoint
from .endpoint import remove_endpoint_file, write_endpoint_file
from .generated.common.v1 import common_pb2 as common
from .generated.material.v1 import material_pb2 as material
from .generated.runtime.v1 import runtime_pb2 as pb
from .generated.runtime.v1 import runtime_pb2_grpc as rpc
from .manifest import (
    artifact_digest,
    canonical,
    contracts,
    load_manifest,
    normalize_config,
    validate_payload,
)
from .processor import CancelToken


def make_observation(
    request, source, payload, *, declaration, plugin_id, plugin_version, artifact, config
):
    """确定性算法事实助手；身份由执行幂等键、来源和结果字节生成。"""
    source_item_id = (
        source.describe_source_item()
        if hasattr(source, "describe_source_item")
        else source.source_item_id
    )
    content_hash = source.digest if hasattr(source, "digest") else source.content_hash
    identity = hashlib.sha256(
        request.context.idempotency_key.encode() + canonical(payload) + source_item_id.encode()
    ).hexdigest()[:32]
    return material.Observation(
        observation_id="obs_" + identity,
        stream_id=request.context.stream_id,
        source_id=request.context.source_id,
        source_item_id=source_item_id,
        time_range=source.time_range,
        modality=declaration["modality"],
        payload=payload,
        content_hash=content_hash,
        timing_source="runtime",
        quality_state="final",
        created_at_unix_ms=int(time.time() * 1000),
        confidence_unavailable_reason="deterministic_algorithm_has_no_model_confidence",
        schema_id=declaration["schema"]["id"],
        schema_version=str(declaration["schema"]["version"]),
        schema_digest=declaration["schema"]["digest"],
        provenance=material.Provenance(
            plugin=plugin_id,
            plugin_version=plugin_version,
            artifact_digest=artifact,
            config_hash="sha256:" + hashlib.sha256(canonical(config)).hexdigest(),
            processor_release_id=request.processor_release_id,
            model_applicability=material.MODEL_APPLICABILITY_NOT_APPLICABLE,
            execution_backend="python_cpu",
        ),
    )


class Servicer(rpc.ProcessorPluginServiceServicer):
    def __init__(self, root):
        self.root = root
        self.registration = load_manifest(root)
        self.manifest = self.registration["manifest"]
        self.spec = self.manifest["spec"]
        self.artifact = artifact_digest(root)
        self.state, self.plugin = "created", None
        self.tokens = {}

    async def Describe(self, request, context):
        meta = self.manifest["metadata"]
        return pb.PluginDescription(
            name=meta["name"],
            version=str(meta["version"]),
            protocol="v1",
            consumes=[item["modality"] for item in self.spec["inputs"]],
            produces=[item["modality"] for item in self.spec["outputs"]],
            memory_kinds=["cpu_shared_memory"]
            if any(item["modality"].startswith("media.") for item in self.spec["inputs"])
            else [],
            artifact_digest=self.artifact,
            input_contracts=contracts(self.manifest, "inputs"),
            output_contracts=contracts(self.manifest, "outputs"),
            execution_modes=self.spec["executionModes"],
            max_concurrency=self.spec["resources"]["maxConcurrency"],
            max_batch_size=self.spec["resources"]["maxBatchSize"],
            ordering=self.spec["ordering"],
        )

    async def ValidateConfig(self, request, context):
        try:
            normalize_config(self.registration["config_schema"], MessageToDict(request.config))
            return pb.ValidationResult(valid=True)
        except ValueError as error:
            return pb.ValidationResult(valid=False, field_errors=[str(error)])

    async def Start(self, request, context):
        if self.state == "ready":
            return pb.LifecycleResponse(state="ready")
        try:
            config = normalize_config(
                self.registration["config_schema"], MessageToDict(request.config)
            )
            module = importlib.import_module(self.spec["entrypoint"]["module"])
            factory = getattr(module, self.spec["entrypoint"].get("factory", "create_plugin"))
            self.plugin = factory(config, self.registration, self.artifact)
            self.config_hash = "sha256:" + hashlib.sha256(canonical(config)).hexdigest()
            if (
                self.plugin.max_concurrency != self.spec["resources"]["maxConcurrency"]
                or self.plugin.max_batch_size != self.spec["resources"]["maxBatchSize"]
            ):
                raise ValueError("plugin_resource_declaration_mismatch")
            self.state = "ready"
            return pb.LifecycleResponse(state="ready")
        except Exception:
            self.state = "failed"
            return pb.LifecycleResponse(
                state="failed",
                error=common.ProcessingError(
                    code=common.INVALID_INPUT, reason_code="plugin_start_failed"
                ),
            )

    async def Health(self, request, context):
        return pb.HealthResponse(state=self.state)

    async def Process(self, request, context):
        if self.state != "ready":
            return pb.ProcessResponse(
                error=common.ProcessingError(
                    code=common.RESOURCE_EXHAUSTED, reason_code="plugin_not_ready", retryable=True
                )
            )
        if (
            request.context.request_id in self.tokens
            or len(self.tokens) >= self.spec["resources"]["maxConcurrency"]
        ):
            return pb.ProcessResponse(
                error=common.ProcessingError(
                    code=common.RESOURCE_EXHAUSTED, reason_code="concurrency_limit", retryable=True
                )
            )
        token = CancelToken()
        self.tokens[request.context.request_id] = token
        try:
            for item in request.inputs:
                if item.HasField("observation"):
                    validate_payload(
                        item.observation, self.spec["inputs"], self.registration["schemas"]
                    )
                elif item.HasField("buffer"):
                    buffer = item.buffer
                    if not is_loopback_endpoint(buffer.locator.handoff_endpoint):
                        raise ValueError("data_locality_violation")
                    if not 0 < buffer.locator.length <= 64 << 20:
                        raise ValueError("input_byte_limit_exceeded")
                    if buffer.format.width > 16384 or buffer.format.height > 16384:
                        raise ValueError("input_dimension_limit_exceeded")
                    modality = {
                        "video_frame": "media.video_frame",
                        "audio_segment": "media.audio_segment",
                    }.get(item.buffer.kind, item.buffer.kind)
                    if modality not in {value["modality"] for value in self.spec["inputs"]}:
                        raise ValueError("input_modality_not_declared")
            response = await self.plugin.invoke(request, token)
            if len(response.observations) > 1024 or response.ByteSize() > 4 << 20:
                raise ValueError("output_limit_exceeded")
            sources = []
            for item in request.inputs:
                if item.HasField("observation"):
                    source = item.observation
                    sources.append((source.source_item_id, source.content_hash, source.time_range))
                elif item.HasField("buffer"):
                    source = item.buffer
                    sources.append(
                        (
                            f"{source.buffer_id}@{source.content_hash.removeprefix('sha256:')[:16]}",
                            source.content_hash,
                            source.time_range,
                        )
                    )
            for observation in response.observations:
                validate_payload(observation, self.spec["outputs"], self.registration["schemas"])
                p = observation.provenance
                if (p.plugin, p.plugin_version, p.artifact_digest, p.processor_release_id) != (
                    self.manifest["metadata"]["name"],
                    str(self.manifest["metadata"]["version"]),
                    self.artifact,
                    request.processor_release_id,
                ):
                    raise ValueError("output_processor_identity_mismatch")
                if p.config_hash != self.config_hash:
                    raise ValueError("output_configuration_mismatch")
                applicability = (
                    material.MODEL_APPLICABILITY_NOT_APPLICABLE
                    if self.spec["modelApplicability"] == "not_applicable"
                    else material.MODEL_APPLICABILITY_MODEL_BASED
                )
                if p.model_applicability != applicability:
                    raise ValueError("output_model_applicability_mismatch")
                if not any(
                    observation.source_item_id == identity
                    and observation.content_hash == content
                    and span.start_ms <= observation.time_range.start_ms
                    and observation.time_range.end_ms <= span.end_ms
                    for identity, content, span in sources
                ):
                    raise ValueError("output_source_lineage_mismatch")
            return response
        except ValueError as error:
            return pb.ProcessResponse(
                error=common.ProcessingError(code=common.INVALID_INPUT, reason_code=str(error))
            )
        finally:
            self.tokens.pop(request.context.request_id, None)

    async def Cancel(self, request, context):
        if token := self.tokens.get(request.request_id):
            token.cancel()
        return pb.LifecycleResponse(state=self.state)

    async def Drain(self, request, context):
        self.state = "draining"
        deadline = time.monotonic() + min(request.grace_period_ms, 300000) / 1000
        while self.tokens and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return pb.LifecycleResponse(state="draining")

    async def Stop(self, request, context):
        self.state = "stopped"
        for token in self.tokens.values():
            token.cancel()
        return pb.LifecycleResponse(state="stopped")


async def serve(args):
    roots = [Path(item).parent for item in sys.path if item and Path(item).name == "src"]
    root = next((item for item in roots if (item / "plugin.yaml").is_file()), None)
    if root is None:
        raise ValueError("plugin_payload_manifest_missing")
    servicer = Servicer(root)
    if args.expect_digest != servicer.artifact:
        raise ValueError("plugin_artifact_digest_mismatch")
    server = grpc.aio.server(
        maximum_concurrent_rpcs=servicer.spec["resources"]["maxConcurrency"] + 8,
        options=[
            ("grpc.max_receive_message_length", 4 << 20),
            ("grpc.max_send_message_length", 4 << 20),
        ],
    )
    rpc.add_ProcessorPluginServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port(f"127.0.0.1:{args.port}")
    if not port:
        raise ValueError("plugin_loopback_bind_failed")
    await server.start()
    write_endpoint_file(
        args.endpoint_file,
        f"127.0.0.1:{port}",
        plugin_id=servicer.manifest["metadata"]["name"],
        plugin_version=str(servicer.manifest["metadata"]["version"]),
        artifact_digest=servicer.artifact,
    )
    try:
        await server.wait_for_termination()
    finally:
        await server.stop(1)
        remove_endpoint_file(args.endpoint_file)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--expect-digest", required=True)
    parser.add_argument("--endpoint-file", required=True)
    try:
        asyncio.run(serve(parser.parse_args()))
    except (KeyboardInterrupt, ValueError):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
