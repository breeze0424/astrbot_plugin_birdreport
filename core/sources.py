"""Choose the primary report source and fall back only when it fails."""

from __future__ import annotations

from . import query
from .ebird import get_ebird_news
from .errors import BirdReportError, InvalidFilterError, ProtocolChangedError


async def get_with_fallback(client, province: str, city: str, city_name: str,
                            api_key: str, hours: int = 24) -> dict:
    try:
        return await query.get_bird_news(client, province, city, hours=hours)
    except InvalidFilterError:
        raise
    except (BirdReportError, KeyError, TypeError, ValueError) as exc:
        primary_error = exc if isinstance(exc, BirdReportError) else ProtocolChangedError("观鸟记录中心响应结构异常")
    if not api_key.strip():
        raise primary_error
    try:
        return await get_ebird_news(api_key.strip(), city_name, hours=hours)
    except BirdReportError as exc:
        raise BirdReportError(
            f"观鸟记录中心不可用（{primary_error}）；eBird 备用源也不可用（{exc}）"
        ) from exc
