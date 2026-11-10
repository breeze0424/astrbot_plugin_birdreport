"""生成 AstrBot 插件配置 schema（`_conf_schema.json`）。

城市下拉选项由 `data/regions.json` 生成；`/刷新城市` 也会用本模块重新生成 schema。
"""

from __future__ import annotations

import json
from pathlib import Path

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"


def build_conf_schema(city_names: list[str]) -> dict:
    """返回 AstrBot `_conf_schema.json` 的内容。"""
    return {
        "provider_id": {
            "description": "生成简短导语的主模型。留空则直接发送分地点鸟讯。",
            "type": "string",
            "_special": "select_provider",
            "default": "",
        },
        "subscriptions": {
            "description": "鸟讯订阅：在指定时间，向指定 QQ 群 / QQ（私聊）发送指定城市过去 24 小时的鸟讯。",
            "type": "template_list",
            "templates": {
                "subscription": {
                    "name": "订阅",
                    "hint": "一条定时鸟讯推送（时间按北京时间，窗口按整点对齐、往前 24 小时）",
                    "display_item": "city",
                    "items": {
                        "city": {
                            "description": "城市（birdreport 支持的地级市 / 直辖市 / 特别行政区）",
                            "type": "string",
                            "options": sorted(city_names),
                            "default": "",
                        },
                        "time": {
                            "description": "发送时间 HH:MM（北京时间，24 小时制）",
                            "type": "string",
                            "default": "08:00",
                            "hint": "例如 08:30。8:00~8:59 发送的都是「前日 7:00 至当日 7:00」的鸟讯。",
                        },
                        "platform": {
                            "description": "平台适配器",
                            "type": "string",
                            "options": ["aiocqhttp"],
                            "default": "aiocqhttp",
                        },
                        "target_type": {
                            "description": "目标类型",
                            "type": "string",
                            "options": ["group", "private"],
                            "default": "group",
                        },
                        "target_id": {
                            "description": "目标 ID：群聊填群号，私聊填 QQ 号",
                            "type": "string",
                            "default": "",
                        },
                        "polish": {
                            "description": "是否由主 AI 生成简短导语（地点与鸟种正文不改写）",
                            "type": "bool",
                            "default": True,
                        },
                        "enabled": {
                            "description": "是否启用该订阅",
                            "type": "bool",
                            "default": True,
                        },
                    },
                }
            },
        },
    }


def write_schema(city_names: list[str], path: Path = _SCHEMA_PATH) -> None:
    path.write_text(
        json.dumps(build_conf_schema(city_names), ensure_ascii=False, indent=2), encoding="utf-8"
    )
