"""非法队列通知不能占住单在飞结果消费者。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sensoryplex_api.enrichment_service import fuse_delivery


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b'{"task_id":"unknown"}', b"", b"x" * 1025])
async def test_bad_notification_is_terminated(payload):
    message = SimpleNamespace(data=payload, term=AsyncMock(), ack=AsyncMock(), nak=AsyncMock())
    await fuse_delivery(message, "unused")
    message.term.assert_awaited_once()
    message.ack.assert_not_awaited()
    message.nak.assert_not_awaited()
