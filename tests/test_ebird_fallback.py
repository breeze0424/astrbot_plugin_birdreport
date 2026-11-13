import asyncio
import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx

from core import query, sources
from core.ebird import city_center, get_ebird_news
from core.errors import BirdReportError, NetworkError


def test_ebird_snapshot_uses_city_center_window_and_chinese_names():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["X-eBirdApiToken"] == "private-test-key"
        if request.url.path.endswith("/geo/recent"):
            assert request.url.params["dist"] == "50"
            return httpx.Response(200, json=[
                {"speciesCode": "bawfly2", "comName": "Blue-and-white Flycatcher",
                 "locName": "厦门植物园", "obsDt": "2026-11-12 12:00", "subId": "S123"},
                {"speciesCode": "blabul1", "comName": "Black Bulbul",
                 "locName": "厦门植物园", "obsDt": "2026-11-13 08:00", "subId": "S124"},
            ])
        return httpx.Response(200, json=[{"speciesCode": "bawfly2", "comName": "白腹琉璃"}])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await get_ebird_news(
                "private-test-key", "厦门市",
                now=datetime(2026, 11, 13, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
                client=client,
            )

    result = asyncio.run(run())
    assert len(seen) == 2
    assert len(result["reports"]) == result["total"] == 1
    assert result["reports"][0]["species"][0]["name"] == "白腹琉璃"
    message = query.format_messages("厦门", result)[0]
    assert "eBird" in message and "50公里" in message and "白腹琉璃" in message
    assert "公开报告" not in message
    assert city_center("厦门市")[0] > 24


def test_fallback_only_when_primary_fails():
    primary = {"reports": [], "source": "birdreport"}

    async def run():
        with patch.object(sources.query, "get_bird_news", new_callable=AsyncMock) as first, \
             patch.object(sources, "get_ebird_news", new_callable=AsyncMock) as second:
            first.return_value = primary
            assert await sources.get_with_fallback(None, "福建省", "厦门市", "厦门市", "key") is primary
            second.assert_not_awaited()
            first.side_effect = NetworkError("primary down")
            second.return_value = {"source": "ebird"}
            assert (await sources.get_with_fallback(None, "福建省", "厦门市", "厦门市", "key"))["source"] == "ebird"
            second.assert_awaited_once_with("key", "厦门市", hours=24)
            try:
                await sources.get_with_fallback(None, "福建省", "厦门市", "厦门市", "")
            except NetworkError:
                pass
            else:
                raise AssertionError("blank key must preserve the primary error")

    asyncio.run(run())


def test_key_is_masked_and_not_bundled():
    with open("_conf_schema.json", encoding="utf-8") as file:
        schema = json.load(file)
    assert schema["ebird_api_key"]["secret"] is True
    assert schema["ebird_api_key"]["default"] == ""


class EBirdFallbackTests(unittest.TestCase):
    test_ebird_snapshot_uses_city_center_window_and_chinese_names = staticmethod(
        test_ebird_snapshot_uses_city_center_window_and_chinese_names
    )
    test_fallback_only_when_primary_fails = staticmethod(test_fallback_only_when_primary_fails)
    test_key_is_masked_and_not_bundled = staticmethod(test_key_is_masked_and_not_bundled)
