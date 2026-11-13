"""eBird API 2.0 fallback for recent observations near a city centre."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx

from .errors import InvalidFilterError, NetworkError, ProtocolChangedError
from .query import MAX_PLACES, _in_window, _place_key, compute_window

API_BASE = "https://api.ebird.org/v2"
CITY_CENTERS = Path(__file__).resolve().parent.parent / "data" / "city_centers.json"


def city_center(city_name: str) -> tuple[float, float]:
    """Return the bundled approximate centre in latitude, longitude order."""
    try:
        coordinates = json.loads(CITY_CENTERS.read_text(encoding="utf-8"))[city_name]
        lat, lng = map(float, coordinates)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError("invalid coordinates")
        return lat, lng
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        raise InvalidFilterError(f"eBird 缺少「{city_name}」的城市中心坐标") from exc


async def _get_json(client: httpx.AsyncClient, path: str, api_key: str, params: dict):
    try:
        response = await client.get(
            API_BASE + path,
            params=params,
            headers={"X-eBirdApiToken": api_key, "Accept": "application/json"},
        )
    except httpx.RequestError as exc:
        raise NetworkError("eBird 网络请求失败") from exc
    if response.status_code in (401, 403):
        raise NetworkError("eBird API Key 无效或无访问权限")
    if response.status_code >= 400:
        raise NetworkError(f"eBird 接口返回 HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise ProtocolChangedError("eBird 返回了非 JSON 数据") from exc


async def get_ebird_news(
    api_key: str,
    city_name: str,
    now: datetime | None = None,
    hours: int = 24,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Read eBird's recent snapshot within 50 km of the city centre.

    eBird returns a limited recent summary, not every checklist or observation.
    Filter it to the same half-open window used by the primary source.
    """
    if not api_key.strip():
        raise InvalidFilterError("请先在插件配置中填写自己的 eBird API Key")
    lat, lng = city_center(city_name)
    window_start, window_end = compute_window(now, hours)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5))
    try:
        observations = await _get_json(
            client,
            "/data/obs/geo/recent",
            api_key,
            {"lat": lat, "lng": lng, "dist": 50, "back": 2,
             "maxResults": 1000, "includeProvisional": "false"},
        )
        if not isinstance(observations, list):
            raise ProtocolChangedError("eBird 观测数据结构异常")
        windowed = []
        for observation in observations:
            if not isinstance(observation, dict):
                raise ProtocolChangedError("eBird 观测记录结构异常")
            when = str(observation.get("obsDt", ""))
            if _in_window(when, window_start, window_end):
                windowed.append(observation)

        # The observations endpoint returns English common names. Request the
        # Chinese taxonomy once for the returned species, keeping English names
        # when taxonomy lookup is temporarily unavailable.
        names: dict[str, str] = {}
        codes = sorted({str(obs.get("speciesCode", "")) for obs in windowed} - {""})
        for start in range(0, len(codes), 100):
            try:
                taxonomy = await _get_json(
                    client, "/ref/taxonomy/ebird", api_key,
                    {"species": ",".join(codes[start:start + 100]),
                     "locale": "zh_CN", "fmt": "json"},
                )
                if isinstance(taxonomy, list):
                    names.update({str(row["speciesCode"]): str(row["comName"])
                                  for row in taxonomy if isinstance(row, dict)
                                  and row.get("speciesCode") and row.get("comName")})
            except (NetworkError, ProtocolChangedError):
                break

        windowed.sort(key=lambda obs: str(obs.get("obsDt", "")), reverse=True)
        places = list(dict.fromkeys(
            _place_key(str(obs.get("locName") or "未知地点")) for obs in windowed
        ))
        selected_places = places[:MAX_PLACES]
        selected_set = set(selected_places)
        reports = []
        for obs in windowed:
            place = _place_key(str(obs.get("locName") or "未知地点"))
            if place not in selected_set:
                continue
            code = str(obs.get("speciesCode") or "")
            sub_id = str(obs.get("subId") or "")
            reports.append({
                "number": sub_id,
                "report_id": sub_id,
                "start": str(obs.get("obsDt") or ""),
                "end": str(obs.get("obsDt") or ""),
                "user": "",
                "place": place,
                "note": "",
                "listed_species_count": 1,
                "species_count": 1,
                "records_count": 1,
                "species": [{"name": names.get(code) or str(obs.get("comName") or code),
                             "count": str(obs.get("howMany") or ""), "taxon_id": code}],
                "url": f"https://ebird.org/checklist/{sub_id}" if sub_id else "",
                "details_fetched": True,
                "group_place": place,
            })
        return {
            "window_start": window_start.strftime("%Y-%m-%d %H:%M"),
            "window_end": window_end.strftime("%Y-%m-%d %H:%M"),
            "city": city_name,
            "source": "ebird",
            "reports": reports,
            "truncated": len(places) > MAX_PLACES,
            "total": len(windowed),
            "place_total": len(places),
            "selected_places": selected_places,
        }
    finally:
        if owns_client:
            await client.aclose()
