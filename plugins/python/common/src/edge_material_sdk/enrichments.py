"""通用补全消息的固定容量、路由、摘要和 WorkQueue 准入。"""

import hashlib

from .generated.orchestration.v1 import orchestration_pb2 as pb

STREAM = "sensoryplex-enrichments-v1"
TASK_PREFIX = "sensoryplex.tasks.enrichment.v1."
RESULT_SUBJECT = "sensoryplex.results.enrichment.v1"
ACK_WAIT_S = 90


def result_digest(result):
    copy = pb.EnrichmentResult()
    copy.CopyFrom(result)
    copy.ClearField("result_digest")
    return "sha256:" + hashlib.sha256(copy.SerializeToString(deterministic=True)).hexdigest()


async def require_stream(js, *, create=False):
    import nats.js.api as api
    from nats.js.errors import NotFoundError

    expected = api.StreamConfig(
        name=STREAM,
        subjects=[TASK_PREFIX + "*", RESULT_SUBJECT],
        storage=api.StorageType.FILE,
        retention=api.RetentionPolicy.WORK_QUEUE,
        max_msgs=100000,
        max_bytes=128 << 20,
        max_age=172800,
        duplicate_window=7200,
    )
    try:
        info = await js.stream_info(STREAM)
    except NotFoundError:
        if not create:
            raise ValueError("enrichment_stream_missing") from None
        return await js.add_stream(config=expected)
    for field in (
        "subjects",
        "storage",
        "retention",
        "max_msgs",
        "max_bytes",
        "max_age",
        "duplicate_window",
    ):
        if getattr(info.config, field) != getattr(expected, field):
            raise ValueError("enrichment_stream_contract_mismatch")
    return info


async def subscription(js, subject, durable):
    import nats.js.api as api
    from nats.js.errors import NotFoundError

    expected = api.ConsumerConfig(
        durable_name=durable,
        filter_subject=subject,
        ack_policy=api.AckPolicy.EXPLICIT,
        ack_wait=ACK_WAIT_S,
        max_deliver=32,
        max_ack_pending=1,
        max_waiting=1,
    )
    try:
        info = await js.consumer_info(STREAM, durable)
    except NotFoundError:
        await js.add_consumer(STREAM, config=expected)
    else:
        for field in (
            "filter_subject",
            "ack_policy",
            "ack_wait",
            "max_deliver",
            "max_ack_pending",
            "max_waiting",
        ):
            if getattr(info.config, field) != getattr(expected, field):
                raise ValueError("enrichment_consumer_contract_mismatch")
    return await js.pull_subscribe(subject, durable=durable, stream=STREAM)
