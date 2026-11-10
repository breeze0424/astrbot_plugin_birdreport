"""通用查询核心：按地点提取、压缩并分段呈现公开鸟讯。"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .client import BirdReportClient, decrypt_data, detail_url, report_url, PAGE_SIZE
from .errors import BirdReportError, EmptyResult, PartialResultError

TZ = ZoneInfo("Asia/Shanghai")

# 逐篇读详情的保护参数：避免大城市报告多时触发网站反滥用验证码。
DETAIL_SLEEP = 1.0        # 每篇详情之间的间隔（秒）
MAX_DETAIL = 30           # 单个窗口最多展开多少篇详情；超出部分只保留列表级信息
MAX_PLACES = 10           # 一次推送最多展示的不同地点数
SPECIES_PER_PLACE = 5
MESSAGE_LIMIT = 700


def compute_window(now: datetime | None = None, hours: int = 24) -> tuple[datetime, datetime]:
    """按整点对齐的滚动窗口。

    - `window_end = floor(now, 1小时) - 1小时`
    - `window_start = window_end - hours`

    例：T=8:00/8:30/8:59 → 前日 7:00 至 当日 7:00；T=9:00 → 前日 8:00 至 当日 8:00。
    """
    now = (now or datetime.now(TZ)).astimezone(TZ)
    window_end = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    window_start = window_end - timedelta(hours=hours)
    return window_start, window_end


def base_filters(province: str, city: str, start_time: str, end_time: str) -> dict:
    return {
        "taxonid": "", "startTime": start_time, "endTime": end_time,
        "province": province, "city": city, "district": "",
        "pointname": "", "username": "", "serial_id": "", "ctime": "",
        "version": "CH4", "state": "2", "mode": 0, "outside_type": 0,
        "sortBy": "serial_id", "orderBy": "asc",
    }


async def search_reports(
    client: BirdReportClient,
    province: str,
    city: str,
    start_time: str,
    end_time: str,
    page_sleep: float = 1.0,
) -> list[dict]:
    """分页拉取指定省/市/日期范围的公开报告列表（保留接口原始字段）。"""
    filters = base_filters(province, city, start_time, end_time)
    results: list[dict] = []
    total: int | None = None
    page = 1
    while total is None or len(results) < total:
        response = await client.post(
            "front/record/activity/search", {"page": page, "limit": PAGE_SIZE, **filters}
        )
        new_total = int(response["count"])
        if total is None:
            total = new_total
        elif total != new_total:
            raise PartialResultError("查询期间报告总数发生变化；请重试以避免漏读")
        rows = decrypt_data(response["data"]) if new_total else []
        expected = min(PAGE_SIZE, total - len(results))
        if not isinstance(rows, list) or len(rows) != expected:
            raise PartialResultError(f"报告列表第 {page} 页应有 {expected} 行，实际 {len(rows)} 行")
        for row in rows:
            if int(row["state"]) != 2:
                continue
            results.append(row)
        page += 1
        if page > 1 and page_sleep > 0:
            await asyncio.sleep(page_sleep)
    return results


async def get_report_detail(client: BirdReportClient, row: dict) -> dict:
    """读取单篇报告详情 + 鸟种，返回规范化字段。

    只发 2 次请求（详情 + 鸟种），鸟种总数直接采用列表中的 `taxoncount`，
    以降低触发网站验证码的概率。
    """
    report_id = str(row["reportId"])
    number = str(row["serial_id"])

    response = await client.post("front/activity/get", {"reportId": report_id})
    if not response.get("success") or int(response.get("errorCode", 0)) != 2:
        raise BirdReportError(f"报告 {number} 的详情不可公开读取", "business")
    data = decrypt_data(response["data"])
    if str(data.get("serialId")) != number:
        raise PartialResultError(f"详情编号与列表不符：{number}")

    taxon_response = await client.post(
        "front/activity/taxon", {"page": 1, "limit": 1500, "reportId": report_id}
    )
    birds = decrypt_data(taxon_response["data"]) if int(taxon_response.get("count", 0)) else []
    if not isinstance(birds, list):
        raise PartialResultError(f"报告 {number} 的鸟种数据格式异常")
    if len(birds) != int(taxon_response.get("count", 0)):
        raise PartialResultError(f"报告 {number} 的鸟种列表未完整返回")

    species = [
        {
            "name": str(bird["taxon_name"]),
            "count": str(bird.get("taxon_count", "")),
            "taxon_id": str(bird.get("taxon_id", "")),
        }
        for bird in birds
    ]
    return {
        "number": number,
        "report_id": report_id,
        "start": str(data["startTime"]),
        "end": str(data["endTime"]),
        "user": str(data["username"]),
        "place": str(data["address"]),
        "note": str(data.get("note") or ""),
        "listed_species_count": int(row.get("taxoncount", 0)),
        "species_count": int(row.get("taxoncount", 0)),
        "records_count": len(birds),
        "species": species,
        "url": detail_url(report_id),
        "details_fetched": True,
        "group_place": _place_key(str(row["address"])),
    }


def _list_only_report(row: dict) -> dict:
    """报告数超出上限时，仅用列表字段构造的轻量报告（无鸟种明细）。"""
    report_id = str(row["reportId"])
    return {
        "number": str(row["serial_id"]),
        "report_id": report_id,
        "start": str(row["start_time"]),
        "end": str(row["end_time"]),
        "user": str(row["username"]),
        "place": str(row["address"]),
        "note": "",
        "listed_species_count": int(row.get("taxoncount", 0)),
        "species_count": int(row.get("taxoncount", 0)),
        "records_count": 0,
        "species": [],
        "url": detail_url(report_id),
        "details_fetched": False,
        "group_place": _place_key(str(row["address"])),
    }


def _place_key(address: str) -> str:
    """只折叠空白，不猜测两个不同地址是否为同一地点。"""
    return " ".join(address.split())


def _in_window(start_time: str, window_start: datetime, window_end: datetime) -> bool:
    try:
        dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        return False
    return window_start <= dt < window_end


async def get_bird_news(
    client: BirdReportClient,
    province: str,
    city: str,
    now: datetime | None = None,
    hours: int = 24,
    max_detail: int = MAX_DETAIL,
    detail_sleep: float = DETAIL_SLEEP,
) -> dict:
    """查询某城市过去 `hours` 小时（整点对齐）的鸟讯。

    返回 dict：{window_start, window_end, province, city, reports, truncated}。
    报告数超过 `max_detail` 时，仅最近若干篇展开鸟种明细，其余仅保留列表信息。
    """
    window_start, window_end = compute_window(now, hours)
    start_date = window_start.strftime("%Y-%m-%d")
    end_date = window_end.strftime("%Y-%m-%d")

    rows = await search_reports(client, province, city, start_date, end_date)
    windowed = [r for r in rows if _in_window(str(r.get("start_time", "")), window_start, window_end)]
    windowed.sort(key=lambda r: str(r.get("start_time", "")))

    # 编号从大到小仅作为提交先后的近似顺序，接口未提供明确的发布时间。
    windowed.sort(key=lambda r: str(r["serial_id"]), reverse=True)
    all_places = list(dict.fromkeys(_place_key(str(r["address"])) for r in windowed))
    selected_places = all_places[:MAX_PLACES]
    selected = [r for r in windowed if _place_key(str(r["address"])) in selected_places]
    # 先保证每处至少读取一篇，再将剩余额度给较新的报告。
    first_by_place: dict[str, dict] = {}
    for row in selected:
        first_by_place.setdefault(_place_key(str(row["address"])), row)
    priority = list(first_by_place.values())
    chosen_ids = {str(r["reportId"]) for r in priority}
    priority.extend(r for r in selected if str(r["reportId"]) not in chosen_ids)
    detail_rows = priority[:max(0, max_detail)]
    detail_ids = {str(r["reportId"]) for r in detail_rows}
    list_only_rows = [r for r in selected if str(r["reportId"]) not in detail_ids]
    reports: list[dict] = [_list_only_report(r) for r in list_only_rows]
    for i, row in enumerate(detail_rows):
        if i > 0 and detail_sleep > 0:
            await asyncio.sleep(detail_sleep)
        reports.append(await get_report_detail(client, row))
    reports.sort(key=lambda r: str(r["number"]), reverse=True)

    return {
        "window_start": window_start.strftime("%Y-%m-%d %H:%M"),
        "window_end": window_end.strftime("%Y-%m-%d %H:%M"),
        "province": province,
        "city": city,
        "reports": reports,
        "truncated": bool(list_only_rows or len(all_places) > MAX_PLACES),
        "total": len(windowed),
        "place_total": len(all_places),
        "selected_places": selected_places,
    }


def _place_lines(result: dict) -> list[str]:
    """每处一行；鸟种只统计已展开的报告，绝不推断未读详情。"""
    reports = result["reports"]
    places = result.get("selected_places") or list(dict.fromkeys(
        r.get("group_place") or _place_key(r["place"]) for r in reports
    ))
    groups: dict[str, list[dict]] = defaultdict(list)
    for report in reports:
        groups[report.get("group_place") or _place_key(report["place"])].append(report)

    per_place: dict[str, dict[str, dict]] = {}
    species_places: dict[str, set[str]] = defaultdict(set)
    for place in places:
        species: dict[str, dict] = {}
        for report in groups[place]:
            if not report.get("details_fetched"):
                continue
            note = str(report.get("note") or "")
            for bird in report["species"]:
                name = str(bird["name"])
                key = str(bird.get("taxon_id") or name)
                if key not in species:
                    species[key] = {"name": name, "order": len(species), "note_pos": None}
                pos = note.find(name)
                if pos >= 0:
                    old = species[key]["note_pos"]
                    species[key]["note_pos"] = pos if old is None else min(old, pos)
                species_places[key].add(place)
        per_place[place] = species

    lines = []
    for place in places:
        group = groups[place]
        read = sum(bool(r.get("details_fetched")) for r in group)
        label = place if len(place) <= 80 else place[:79] + "…"
        prefix = f"{label}（{len(group)}篇）"
        if not read:
            lines.append(prefix + "：鸟种明细未获取。")
            continue
        species = per_place[place]
        ranked = sorted(species.items(), key=lambda kv: (
            0 if kv[1]["note_pos"] is not None else 1 if len(species_places[kv[0]]) == 1 else 2,
            kv[1]["note_pos"] if kv[1]["note_pos"] is not None else kv[1]["order"],
            kv[1]["order"],
        ))
        selected = [item["name"] for _, item in ranked[:SPECIES_PER_PLACE]]
        other = len(species) - len(selected)
        line = prefix + "：" + "、".join(selected)
        if other:
            line += f"；另有{other}种"
        if read < len(group):
            line += f"（已读{read}/{len(group)}篇详情，鸟种数仅含已读报告）"
        lines.append(line + "。")
    return lines


def format_messages(
    city_label: str, result: dict, generated_at: str = "", intro: str = "",
    max_chars: int = MESSAGE_LIMIT,
) -> list[str]:
    """按地点分段，单条不超过 max_chars；AI 只可提供短导语。"""
    total = result.get("total", len(result["reports"]))
    place_total = result.get("place_total", len(result.get("selected_places", [])))
    shown = len(result.get("selected_places", []))
    header = (f"{city_label}鸟讯｜{result['window_start']}—{result['window_end']}\n"
              f"中国观鸟记录中心公开报告{total}篇；展示{shown}/{place_total}处地点。")
    if not result["reports"]:
        return [header + "\n该时段暂无公开报告。"]
    intro = " ".join(intro.split())[:80]
    lines = _place_lines(result)
    chunks: list[list[str]] = []
    current: list[str] = []
    # 留足分段序号及首次导语的空间。
    allowance = max_chars - len(header) - len(intro) - 16
    for line in lines:
        if current and len("\n".join(current + [line])) > allowance:
            chunks.append(current)
            current = []
        current.append(line)
    if current:
        chunks.append(current)
    messages = []
    for index, chunk in enumerate(chunks, 1):
        title = header + (f"（{index}/{len(chunks)}）" if len(chunks) > 1 else "")
        lead = ("\n" + intro) if index == 1 and intro else ""
        message = title + lead + "\n" + "\n".join(chunk)
        if len(message) > max_chars:
            raise ValueError("单个地点过长，无法在消息长度限制内完整呈现")
        messages.append(message)
    return messages


def summarize(city_label: str, result: dict, generated_at: str) -> str:
    """兼容旧调用；返回按地点组织的纯文本。"""
    return "\n\n".join(format_messages(city_label, result, generated_at))


def summarize_compact(city_label: str, result: dict) -> str:
    """AI 工具的地点摘要，保留地点与鸟种的对应关系。"""
    return "\n\n".join(format_messages(city_label, result))


def build_source_url(province: str, city: str, window_start: datetime, window_end: datetime) -> str:
    """生成对应的网页查询链接（仅用于展示/溯源）。"""
    return report_url(base_filters(
        province, city,
        window_start.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d"),
    ))
