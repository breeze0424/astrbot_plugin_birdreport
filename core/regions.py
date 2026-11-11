"""省市列表：从 birdreport 的 adcode 接口抓取、过滤并缓存。

规则（与用户需求对齐）：
- 保留「地级市或同级别城市」（地级市 / 自治州 / 地区 / 盟）。
- 直辖市（北京/天津/上海/重庆）与特别行政区（香港/澳门）视为「城市」层级。
- 排除区、县级市、县、林区、新疆生产建设兵团市。

判定依据：国家行政区划代码中，地级行政区划的 `city_code` 以 "00" 结尾；
省直辖县级市/县/兵团市不以 "00" 结尾（新疆兵团市个别以 00 结尾，用 659 前缀排除）。
直辖市与特别行政区的城市名等于省名，查询时 `city` 置空。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .client import BirdReportClient
from .errors import InvalidFilterError

# 直辖市 + 特别行政区：城市名 == 省名，查询时 city 置空
_DIRECT_MUNICIPALITIES = {"北京市", "天津市", "上海市", "重庆市", "香港", "澳门"}

def _is_keepable_city(city_code: str) -> bool:
    """地级行政区划代码以 00 结尾；排除新疆兵团市(659xxx，属县级市)。"""
    return city_code.endswith("00") and not city_code.startswith("659")


async def fetch_regions(client: BirdReportClient) -> list[dict]:
    """抓取全量「城市」层级，返回 [{name, province, city}]（name 用于下拉展示）。"""
    provinces = (await client.post("front/system/adcode/province", {})).get("data") or []
    regions: list[dict] = []
    for p in provinces:
        province_name = str(p["province_name"])
        province_code = str(p["province_code"])
        resp = await client.post("front/system/adcode/city", {"province_code": province_code})
        cities = resp.get("data") or []
        for c in cities:
            city_name = str(c["city_name"])
            city_code = str(c["city_code"])
            if not _is_keepable_city(city_code):
                continue
            # 直辖市/特区：城市名等于省名 → 查询时 city 置空
            if city_name in _DIRECT_MUNICIPALITIES or city_name == province_name:
                regions.append({"name": city_name, "province": province_name, "city": ""})
            else:
                regions.append({"name": city_name, "province": province_name, "city": city_name})
    return regions


_STEM_SUFFIXES = ("特别行政区", "自治区", "自治州", "自治县", "地区", "林区", "盟", "市")


def _stem(name: str) -> str:
    n = (name or "").strip()
    for s in _STEM_SUFFIXES:
        if n.endswith(s) and n != s:
            n = n[: -len(s)]
            break
    return n


def resolve_city(regions: list[dict], name: str) -> tuple[str, str]:
    """根据展示名解析为 (province, city)。

    先精确匹配，再按「去掉行政后缀」的模糊匹配（如 `厦门` 匹配 `厦门市`）。
    未知城市抛 InvalidFilterError。
    """
    name = (name or "").strip()
    for r in regions:
        if r["name"] == name:
            return r["province"], r["city"]
    target = _stem(name)
    if target:
        matches = [r for r in regions if _stem(r["name"]) == target]
        if len(matches) == 1:
            return matches[0]["province"], matches[0]["city"]
        if len(matches) > 1:
            raise InvalidFilterError(f"城市名「{name}」有歧义，请写全称："
                                     + "、".join(r["name"] for r in matches))
    raise InvalidFilterError(f"未知城市：{name}（请从下拉列表选择 birdreport 支持的城市）")


def save_regions(path: Path, regions: list[dict]) -> None:
    payload = {
        "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "count": len(regions),
        "cities": regions,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_regions(path: Path) -> list[dict]:
    """读取缓存的城市列表。文件缺失时抛 FileNotFoundError，由调用方回退到抓取。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    cities = payload.get("cities")
    if not isinstance(cities, list):
        raise InvalidFilterError("regions.json 结构异常")
    return cities


async def build_regions(client: BirdReportClient, path: Path) -> list[dict]:
    """抓取 + 过滤 + 落盘，返回城市列表。"""
    regions = await fetch_regions(client)
    save_regions(path, regions)
    return regions
