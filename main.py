"""AstrBot entrypoint; all decisions are internal and never sent to QQ."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from quart import request
from starlette.responses import HTMLResponse

from .engine import BEIJING
from .runtime import GuardService
from .storage import AuditStore

PLUGIN = "astrbot_plugin_schedule_guard"
PREFIX = f"/{PLUGIN}/page/"


class ScheduleGuardPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.guard = GuardService(
            context,
            config,
            AuditStore(StarTools.get_data_dir(PLUGIN) / "checks.sqlite3"),
            logger,
        )
        self.bootstrap_lock = asyncio.Lock()
        for route, handler, method in (
            ("view", self.view, "GET"),
            ("stats", self.stats, "GET"),
            ("records", self.records, "GET"),
            ("check", self.simulate, "POST"),
            ("bootstrap", self.bootstrap, "POST"),
        ):
            context.register_web_api(PREFIX + route, handler, [method], "主动消息日程判断")

    async def initialize(self):
        logger.info(
            f"[日程判断] 已加载，拦截={'启用' if self.config.get('enabled', False) else '关闭'}，会话数={len(self.guard.sessions())}"
        )

    async def terminate(self):
        await self.guard.shutdown()
        self.context.registered_web_apis[:] = [
            item for item in self.context.registered_web_apis if not item[0].startswith(PREFIX)
        ]

    @filter.on_llm_request(priority=10000)
    async def before_proactive(self, event: AstrMessageEvent, req: ProviderRequest):
        await self.guard.on_request(event, req)

    @filter.on_llm_response(priority=10000)
    async def recheck_boundary(self, event: AstrMessageEvent, response: LLMResponse):
        await self.guard.on_response(event, response)

    @filter.after_message_sent(priority=10000)
    async def confirm_sent(self, event: AstrMessageEvent):
        await self.guard.after_sent(event)

    async def stats(self):
        return {
            "status": "ok",
            "data": {
                "enabled": bool(self.config.get("enabled", False)),
                "sessions": self.guard.next_tasks(),
                "counts": await asyncio.to_thread(self.guard.store.stats),
                "memory_available": self.guard.instance("astrbot_plugin_livingmemory") is not None,
                "provider_id": self.config.get("provider_id", "") or "当前会话模型",
            },
        }

    async def view(self):
        # Served by AstrBot's authenticated extension route; no new listener or token.
        root = Path(__file__).parent / "pages" / "dashboard"
        html = (root / "index.html").read_text(encoding="utf-8")
        html = html.replace(
            '<link rel="stylesheet" href="style.css">',
            "<style>" + (root / "style.css").read_text(encoding="utf-8") + "</style>",
        )
        html = html.replace(
            '<script defer src="app.js"></script>',
            "<script>" + (root / "app.js").read_text(encoding="utf-8") + "</script>",
        )
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    async def records(self):
        return {
            "status": "ok",
            "data": {"records": await asyncio.to_thread(self.guard.store.list)},
        }

    async def simulate(self):
        try:
            body = await request.get_json() or {}
            session_id = self.guard.choose_session(body.get("session_id"))
            now = datetime.fromisoformat(body["at"]) if body.get("at") else datetime.now(BEIJING)
            if now.tzinfo is None:
                now = now.replace(tzinfo=BEIJING)
            event = self.guard.make_event(session_id)
            history = await self.guard.history(session_id)
            record = await self.guard.check(event, history, now, simulation=True)
            return {"status": "ok", "data": record}
        except Exception as exc:
            logger.warning(f"[日程判断] 模拟检查失败: {type(exc).__name__}")
            return {
                "status": "error",
                "message": str(exc)[:200]
                if isinstance(exc, ValueError)
                else "模拟检查失败，请查看日志",
            }

    async def bootstrap(self):
        try:
            body = await request.get_json() or {}
            session_id = self.guard.choose_session(body.get("session_id"))
            async with self.bootstrap_lock:
                result = await asyncio.wait_for(self.guard.bootstrap(session_id), 90)
            return {"status": "ok", "data": result}
        except Exception as exc:
            logger.warning(f"[日程判断] 初始日程记忆写入/验证失败: {type(exc).__name__}")
            return {
                "status": "error",
                "message": str(exc)[:200]
                if isinstance(exc, ValueError)
                else "记忆保存或验证失败，请查看日志",
            }
