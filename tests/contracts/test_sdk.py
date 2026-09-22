import asyncio
import time

import pytest
from edge_material_sdk import CancelToken, ProcessorPlugin
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime
from edge_material_sdk.validation import validate_material, validate_observation


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan"), float("inf")])
def test_invalid_confidence(observation, confidence):
    observation.confidence = confidence
    with pytest.raises(ValueError, match="invalid_confidence"):
        validate_observation(observation)


def test_unknown_confidence_is_not_perfect(observation):
    observation.ClearField("confidence")
    with pytest.raises(ValueError, match="missing_confidence_reason"):
        validate_observation(observation)
    observation.confidence_unavailable_reason = "model_does_not_provide_confidence"
    validate_observation(observation)


def test_material_time_and_lineage(material):
    validate_material(material)
    material.observations[0].stream_id = "another_stream"
    with pytest.raises(ValueError, match="stream_mismatch"):
        validate_material(material)


class Echo(ProcessorPlugin):
    """测试替身，绝不会注册为模型插件。"""

    def describe(self):
        return runtime.PluginDescription(name="contract-test", version="0.1.0")

    async def process(self, request, cancel_token):
        return runtime.ProcessResponse(observations=[request.inputs[0].observation])


def make_request(observation):
    return runtime.ProcessRequest(
        context=common.RequestContext(
            request_id="request",
            trace_id="trace",
            pipeline_run_id="run",
            stream_id=observation.stream_id,
            source_id=observation.source_id,
            deadline_unix_ms=int(time.time() * 1000) + 1000,
            attempt=1,
            idempotency_key="stable",
            privacy_policy=common.PrivacyPolicy(data_egress="local_only"),
        ),
        inputs=[runtime.PluginInput(observation=observation)],
        processor_release_id="release",
    )


async def test_deadline_cancel_and_policy(observation):
    plugin = Echo()
    request = make_request(observation)
    assert (await plugin.invoke(request)).observations[0] == observation
    request.context.deadline_unix_ms = 1
    assert (await plugin.invoke(request)).error.code == common.DEADLINE_EXCEEDED
    token = CancelToken()
    token.cancel()
    with pytest.raises(asyncio.CancelledError):
        await plugin.invoke(request, token)
    request.context.privacy_policy.data_egress = "cloud"
    assert (await plugin.invoke(request)).error.code == common.DATA_POLICY_DENIED


async def test_timeout_releases_capacity(observation):
    class Slow(Echo):
        async def process(self, request, cancel_token):
            await asyncio.sleep(5)

    plugin = Slow()
    request = make_request(observation)
    request.context.deadline_unix_ms = int(time.time() * 1000) + 20
    assert (await plugin.invoke(request)).error.code == common.DEADLINE_EXCEEDED
    assert plugin._in_flight == 0


async def test_bounded_concurrency(observation):
    started, release = asyncio.Event(), asyncio.Event()

    class Waiting(Echo):
        async def process(self, request, token):
            started.set()
            await release.wait()
            return await super().process(request, token)

    plugin = Waiting()
    request = make_request(observation)
    task = asyncio.create_task(plugin.invoke(request))
    await started.wait()
    assert (await plugin.invoke(request)).error.code == common.RESOURCE_EXHAUSTED
    release.set()
    assert (await task).observations[0] == observation


async def test_empty_success_and_invalid_outputs_are_plugin_failures(observation):
    class Empty(Echo):
        async def process(self, request, token):
            return runtime.ProcessResponse()

    class Invalid(Echo):
        async def process(self, request, token):
            response = await super().process(request, token)
            response.observations[0].confidence = float("nan")
            return response

    for plugin in (Empty(), Invalid()):
        response = await plugin.invoke(make_request(observation))
        assert response.error.code == common.INTERNAL_PLUGIN_ERROR
