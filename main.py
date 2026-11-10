"""观鸟鸟讯 AstrBot 插件。

功能：
- 定时（每天指定 HH:MM，北京时间）向指定 QQ 群 / QQ（私聊）主动推送指定城市过去 24 小时的鸟讯；
- 支持由 AstrBot 主 AI 生成简短导语，地点正文保持固定格式；
- 提供 `/鸟讯 <城市>` 立即查询、`/刷新城市` 刷新省市列表；
- 注册 AI 工具 `query_bird_news`，供主 AI 在对话中即时查询。

订阅在 AstrBot 的插件配置面板（`_conf_schema.json`）中填写，城市用下拉列表选择。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

from .core import query as query_mod
from .core import regions as regions_mod
from .core import schema as schema_mod
from .core.client import BirdReportClient
from .core.errors import (
    BirdReportError,
    EmptyResult,
    InvalidFilterError,
    VerificationRequiredError,
)

TZ = ZoneInfo("Asia/Shanghai")

_REGIONS_PATH = Path(__file__).resolve().parent / "data" / "regions.json"

_POLISH_SYSTEM_PROMPT = (
    "为观鸟群消息写一句自然的开场白，不超过40个汉字。"
    "不要加入具体鸟种、地点、数量等未经提供的事实；只返回这一句话，不要换行。"
)


def _friendly_error(exc: BirdReportError) -> str:
    """把异常转成对用户友好的中文提示。"""
    if isinstance(exc, VerificationRequiredError):
        return "触发了网站的反滥用验证码，本次查询被跳过。请稍后重试，或先在网页端完成一次验证。"
    if isinstance(exc, EmptyResult):
        return "该时段暂无公开观鸟报告。"
    if isinstance(exc, InvalidFilterError):
        return str(exc)
    return f"查询失败：{exc}"


@register("astrbot_plugin_birdreport", "如风", "定时推送指定城市过去 24 小时鸟讯", "1.0.3")
class BirdReportPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._regions: list[dict] = []
        self._scheduler: Optional[AsyncIOScheduler] = None

    async def initialize(self):
        self._load_regions()
        self._build_scheduler()

    async def terminate(self):
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    # ---- 内部工具 ----

    def _load_regions(self):
        try:
            self._regions = regions_mod.load_regions(_REGIONS_PATH)
        except (FileNotFoundError, InvalidFilterError, ValueError) as exc:
            logger.warning(f"regions.json 不可用，稍后可用 /刷新城市 生成：{exc}")
            self._regions = []

    def _build_scheduler(self):
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
        self._scheduler = AsyncIOScheduler(timezone=TZ)
        subs = self.config.get("subscriptions") or []
        for i, sub in enumerate(subs):
            if not isinstance(sub, dict) or not sub.get("enabled", True):
                continue
            if not str(sub.get("target_id", "")).strip() or not str(sub.get("city", "")).strip():
                continue
            try:
                hour, minute = self._parse_time(str(sub.get("time", "08:00")))
            except ValueError as exc:
                logger.warning(f"订阅 {i} 时间格式错误：{exc}")
                continue
            self._scheduler.add_job(
                self._run_subscription,
                CronTrigger(hour=hour, minute=minute, timezone=TZ),
                args=[sub],
                id=f"birdreport_{i}",
                replace_existing=True,
            )
        self._scheduler.start()
        logger.info(f"鸟讯插件已启动，共 {len(self._scheduler.get_jobs())} 条订阅任务")

    @staticmethod
    def _parse_time(text: str) -> tuple[int, int]:
        parts = (text or "").strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"应为 HH:MM，实际为 {text!r}")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"时间超出范围：{text!r}")
        return hour, minute

    async def _run_subscription(self, sub: dict):
        city_name = str(sub.get("city", "")).strip()
        try:
            province, city = regions_mod.resolve_city(self._regions, city_name)
            result = await self._fetch_result(province, city, hours=24)
            intro = await self._polish_intro(city_name) if sub.get("polish", True) else ""
            messages = query_mod.format_messages(city_name, result, intro=intro)
        except BirdReportError as exc:
            messages = [f"[鸟讯] {city_name}：{_friendly_error(exc)}"]
        except Exception as exc:
            logger.error(f"订阅任务查询失败（{city_name}）：{exc}")
            messages = [f"[鸟讯] {city_name}：查询出错，请查看插件日志。"]
        for message in messages:
            await self._send_to(sub, message)

    async def _fetch_result(self, province: str, city: str, hours: int = 24) -> dict:
        client = BirdReportClient(timeout=25)
        try:
            return await query_mod.get_bird_news(client, province, city, hours=hours)
        finally:
            await client.close()

    async def _polish_intro(self, city_label: str) -> str:
        provider_id = (self.config.get("provider_id") or "").strip()
        if not provider_id:
            return ""
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=f"给{city_label}的过去24小时观鸟消息写一句简短开场白。",
                system_prompt=_POLISH_SYSTEM_PROMPT,
            )
            text = " ".join((resp.completion_text or "").split()).strip()
            return text if 0 < len(text) <= 80 else ""
        except Exception as exc:
            logger.error(f"AI 导语生成失败：{exc}")
            return ""

    async def _send_to(self, sub: dict, text: str):
        platform_cfg = str(sub.get("platform") or "aiocqhttp").strip()
        target_type = str(sub.get("target_type") or "group").strip()
        target_id = str(sub.get("target_id") or "").strip()
        if not target_id:
            logger.warning("订阅缺少目标 ID，跳过发送")
            return

        # 用户填的可能是「适配器类型名（aiocqhttp）」或「实例 id（default）」，
        # 而 send_message 需要的是实例 id，这里统一解析。
        platform_id = self._resolve_platform_id(platform_cfg)

        message_type = "GroupMessage" if target_type == "group" else "FriendMessage"
        umo = f"{platform_id}:{message_type}:{target_id}"
        try:
            ok = await self.context.send_message(umo, MessageChain().message(text))
            if not ok:
                logger.warning(f"未找到平台实例发送订阅消息：{umo}")
        except Exception as exc:
            logger.error(f"发送订阅消息失败（{umo}）：{exc}")

    def _resolve_platform_id(self, platform_cfg: str) -> str:
        """把配置里的平台值解析为平台实例 id（类型名 → 实例 id）。"""
        try:
            for platform in self.context.platform_manager.platform_insts:
                meta = platform.meta()
                if platform_cfg in (meta.id, meta.name):
                    return meta.id
        except Exception:
            pass
        return platform_cfg

    # ---- 指令 ----

    @filter.command("鸟讯", alias={"/鸟讯"})
    async def bird_news_cmd(
        self, event: AstrMessageEvent, city: Optional[str] = None
    ) -> MessageEventResult:
        """查询指定城市过去 24 小时的鸟讯，例如 /鸟讯 厦门"""
        city_name = (city or "").strip()
        if not city_name:
            yield event.plain_result("用法：/鸟讯 <城市>，例如 /鸟讯 厦门")
            return
        try:
            province, c = regions_mod.resolve_city(self._regions, city_name)
        except InvalidFilterError as exc:
            yield event.plain_result(str(exc))
            return
        try:
            result = await self._fetch_result(province, c, hours=24)
        except BirdReportError as exc:
            yield event.plain_result(_friendly_error(exc))
            return
        intro = await self._polish_intro(city_name)
        for message in query_mod.format_messages(city_name, result, intro=intro):
            yield event.plain_result(message)

    @filter.command("刷新城市")
    async def refresh_cities(self, event: AstrMessageEvent) -> MessageEventResult:
        """重新从 birdreport 拉取省市列表并更新配置面板下拉（管理员用）。"""
        client = BirdReportClient(timeout=25)
        try:
            regions = await regions_mod.build_regions(client)
        except Exception as exc:
            yield event.plain_result(f"刷新失败：{exc}")
            return
        finally:
            await client.close()
        schema_mod.write_schema([r["name"] for r in regions])
        self._regions = regions
        yield event.plain_result(
            f"已刷新 {len(regions)} 个城市。请「重载插件」以更新配置面板的城市下拉。"
        )

    # ---- AI 工具 ----

    @filter.llm_tool(name="query_bird_news")
    async def query_bird_news(
        self, event: AstrMessageEvent, city: str
    ) -> str:
        """查询指定城市过去 24 小时的观鸟鸟讯，返回结构化摘要供你归纳成自然语言回复。

        Args:
            city(string): 城市名，例如 厦门市、北京市、香港
        """
        city_name = (city or "").strip()
        try:
            province, c = regions_mod.resolve_city(self._regions, city_name)
            result = await self._fetch_result(province, c, hours=24)
        except BirdReportError as exc:
            return _friendly_error(exc)
        return query_mod.summarize_compact(city_name, result)
