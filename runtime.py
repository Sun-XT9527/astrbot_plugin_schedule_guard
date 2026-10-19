"""Adapters for Proactive Chat v1.2.6 and LivingMemory 2.7.0-beta.2."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import time
import uuid
from datetime import datetime

from .engine import (
    BEIJING,
    SYSTEM_PROMPT,
    Decision,
    bootstrap_candidate,
    history_sources,
    parse_decision,
)

EXTRA_KEY = "schedule_guard.check"
PROACTIVE = "astrbot_plugin_proactive_chat"
MEMORY = "astrbot_plugin_livingmemory"


class GuardService:
    def __init__(self, context, config, store, logger):
        self.context, self.config, self.store, self.logger = (
            context,
            config,
            store,
            logger,
        )
        self.running = True
        self.audit_tasks = set()

    async def shutdown(self):
        self.running = False
        tasks = list(self.audit_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def audit_next_run(self, record):
        owner = self.instance(PROACTIVE)
        if not getattr(owner, "scheduler", None):
            return
        task = asyncio.create_task(self.record_next_run(record))
        self.audit_tasks.add(task)
        task.add_done_callback(self.audit_tasks.discard)

    async def record_next_run(self, record, delay=0.5):
        # Observe the original scheduler after its current call finishes. Never alter it.
        try:
            for _ in range(60):
                await asyncio.sleep(delay)
                if not self.running:
                    return
                owner = self.instance(PROACTIVE)
                scheduler = getattr(owner, "scheduler", None)
                job = scheduler.get_job(record["session_id"]) if scheduler else None
                when = getattr(job, "next_run_time", None)
                if when and when > datetime.now(BEIJING):
                    value = when.astimezone(BEIJING).isoformat()
                    await asyncio.to_thread(self.store.next_run, record["event_id"], value)
                    self.logger.info(
                        f"[日程判断] 下一轮 {record['event_id']} next_run_time={value}"
                    )
                    return
            self.logger.warning(f"[日程判断] 尚未观察到下一轮任务 {record['event_id']}")
        except Exception as exc:
            self.logger.warning(f"[日程判断] 下一轮任务记录失败: {type(exc).__name__}")

    def instance(self, name):
        for metadata in self.context.get_all_stars():
            if metadata.name == name and metadata.activated:
                return metadata.star_cls
        return None

    def sessions(self):
        values = self.config.get("session_list", []) or []
        return [
            value
            for value in values
            if isinstance(value, str)
            and any(token in value.split(":")[1:2] for token in ("FriendMessage", "PrivateMessage"))
        ]

    def choose_session(self, value=None):
        if value and value in self.sessions():
            return value
        if value:
            raise ValueError("该会话不在日程判断的私聊列表中")
        if not self.sessions():
            raise ValueError("请先配置需要检查的私聊会话")
        return self.sessions()[0]

    def is_proactive(self, event):
        owner = getattr(event, "_proactive_plugin", None)
        message_id = str(getattr(getattr(event, "message_obj", None), "message_id", ""))
        return (
            owner is not None
            and owner is self.instance(PROACTIVE)
            and message_id.startswith("proactive:")
            and event.unified_msg_origin in self.sessions()
        )

    def make_event(self, session_id):
        owner = self.instance(PROACTIVE)
        if owner is None:
            raise ValueError("主动消息插件当前未启用")
        return owner._build_proactive_event(self.choose_session(session_id))

    async def history(self, session_id):
        manager = self.context.conversation_manager
        cid = await manager.get_curr_conversation_id(session_id)
        if not cid:
            return []
        conversation = await manager.get_conversation(session_id, cid)
        values = conversation.history if conversation else []
        values = json.loads(values) if isinstance(values, str) else values
        return values if isinstance(values, list) else []

    async def memory_bridge(self, event):
        plugin = self.instance(MEMORY)
        initializer = getattr(plugin, "initializer", None)
        engine = getattr(initializer, "memory_engine", None)
        if plugin is None or not getattr(initializer, "is_initialized", False) or engine is None:
            raise ValueError("长期记忆尚未就绪")
        package = plugin.__class__.__module__.rsplit(".", 1)[0]
        scopes = importlib.import_module(f"{package}.core.memory_scope")
        utils = importlib.import_module(f"{package}.core.utils")
        conf = plugin.config_manager
        if not scopes.is_event_memory_allowed(conf, event):
            raise ValueError("当前会话不允许访问长期记忆")
        scope = scopes.resolve_memory_scope(conf, event) or event.unified_msg_origin
        persona = (
            await utils.get_persona_id(self.context, event)
            if conf.filtering_settings.get("use_persona_filtering", True)
            else None
        )
        return engine, scope, persona

    async def recall(self, event, now):
        sources, errors = [], []
        try:
            timeout = max(1, min(30, int(self.config.get("memory_timeout_seconds", 10))))
            engine, scope, persona = await asyncio.wait_for(self.memory_bridge(event), timeout)
            weekday = "一二三四五六日"[now.weekday()]
            queries = [
                "用户 日程 课表 没空时间表",
                f"{now:%Y-%m-%d} 周{weekday} 临时安排 取消 改期 请假",
            ]
            results = await asyncio.gather(
                *[
                    asyncio.wait_for(
                        engine.search_memories(
                            query=query, k=5, session_id=scope, persona_id=persona
                        ),
                        timeout,
                    )
                    for query in queries
                ],
                return_exceptions=True,
            )
            seen = set()
            for result in results:
                if isinstance(result, BaseException):
                    errors.append(type(result).__name__)
                    continue
                for memory in result:
                    key = str(memory.doc_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    sources.append(
                        {
                            "id": f"M{key}",
                            "role": "memory",
                            "content": memory.content,
                            "created_at": memory.metadata.get("create_time"),
                            "memory_id": memory.doc_id,
                            "origin": {
                                key: memory.metadata[key]
                                for key in ("source", "memory_type", "topics")
                                if key in memory.metadata
                            },
                        }
                    )
        except Exception as exc:
            errors.append(type(exc).__name__)
        return sources, errors

    async def evaluate(self, event, history, now):
        now = now.astimezone(BEIJING)
        context_sources = history_sources(history)
        memories, errors = await self.recall(event, now)
        sources = context_sources + memories
        provider_id = ""
        try:
            provider_id = str(
                self.config.get("provider_id", "")
                or await self.context.get_current_chat_provider_id(event.unified_msg_origin)
            )
            timeout = max(1, min(120, int(self.config.get("judge_timeout_seconds", 30))))
            if errors:
                # A missed recall could contain a cancellation overriding the timetable.
                decision = Decision(
                    reason="日程记忆检索未完成，按设置放行",
                    error="memory_search_failed",
                )
            elif not sources:
                decision = Decision(
                    reason="聊天和记忆中均没有可用资料，按设置放行", error="no_sources"
                )
            else:
                prompt = json.dumps(
                    {
                        "now": now.isoformat(),
                        "weekday": "周" + "一二三四五六日"[now.weekday()],
                        "context": context_sources,
                        "memories": memories,
                        "memory_search_errors": errors,
                    },
                    ensure_ascii=False,
                    default=str,
                )
                # This SDK call invokes the provider directly, without event hooks/tools/history writes.
                response = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider_id,
                        system_prompt=SYSTEM_PROMPT,
                        prompt=prompt,
                        tools=None,
                        contexts=[],
                        max_tokens=1500,
                    ),
                    timeout,
                )
                decision = parse_decision(response.completion_text or "", sources, now)
        except Exception as exc:
            decision = Decision(reason="判断服务暂不可用，按设置放行", error=type(exc).__name__)
        return decision, {
            "memory_count": len(memories),
            "memory_errors": errors,
            "context_count": len(context_sources),
            "provider_id": provider_id,
        }

    async def check(self, event, history, now=None, simulation=False):
        now = now or datetime.now(BEIJING)
        started = time.monotonic()
        decision, info = await self.evaluate(event, history, now)
        event_id = (
            "simulation:" + uuid.uuid4().hex if simulation else str(event.message_obj.message_id)
        )
        record = {
            "event_id": event_id,
            "session_id": event.unified_msg_origin,
            "checked_at": now.astimezone(BEIJING).isoformat(),
            "mode": "simulation" if simulation else "live",
            **decision.to_dict(),
            **info,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "delivery": "simulation"
            if simulation
            else "skipped_busy"
            if decision.status == "busy"
            else "allowed_unconfirmed",
        }
        previous = event.get_extra(EXTRA_KEY) if not simulation else None
        record["triggered_at"] = (
            previous["record"]["triggered_at"] if previous else record["checked_at"]
        )
        try:
            await asyncio.to_thread(self.store.save, record)
        except Exception as exc:
            self.logger.error(f"[日程判断] 检查记录保存失败: {type(exc).__name__}")
        self.logger.info(
            f"[日程判断] {'模拟' if simulation else '已触发'} {event.unified_msg_origin} time={record['triggered_at']} result={decision.status} delivery={record['delivery']} reason={decision.reason} evidence={json.dumps(decision.evidence, ensure_ascii=False)}"
        )
        return record

    async def on_request(self, event, req):
        if (
            not self.running
            or not self.config.get("enabled", False)
            or not self.is_proactive(event)
        ):
            return
        existing = event.get_extra(EXTRA_KEY)
        if existing:
            if existing["record"]["status"] == "busy":
                event.stop_event()
            return
        history = list(getattr(req, "contexts", None) or [])
        record = await self.check(event, history)
        if not self.running:
            return
        event.set_extra(EXTRA_KEY, {"record": record, "history": history})
        if record["status"] == "busy":
            event.stop_event()
            self.audit_next_run(record)

    async def on_response(self, event, response):
        state = event.get_extra(EXTRA_KEY)
        if (
            not self.running
            or not self.config.get("enabled", False)
            or not state
            or not self.is_proactive(event)
        ):
            return
        previous = state["record"]
        now = datetime.now(BEIJING)
        boundary = previous.get("next_boundary")
        checked_at = datetime.fromisoformat(previous["checked_at"])
        # Most rounds make one judgment call. A known boundary/date change or unusually
        # slow generation requires fresh evidence and a second call before any TTS.
        if (
            (boundary and now >= datetime.fromisoformat(boundary))
            or now.date() != checked_at.date()
            or (now - checked_at).total_seconds() > 300
        ):
            history = await self.history(event.unified_msg_origin)
            record = await self.check(event, history or state["history"], now)
            state["record"] = record
            if self.running and record["status"] == "busy":
                event.stop_event()
                self.audit_next_run(record)

    async def after_sent(self, event):
        state = event.get_extra(EXTRA_KEY)
        if not self.running or not state or not self.is_proactive(event):
            return
        if getattr(event, "_has_send_oper", False) is True:
            delivery = "sent_partial" if getattr(event, "proactive_send_failed", False) else "sent"
            sent_at = datetime.now(BEIJING).isoformat()
        else:
            delivery, sent_at = "not_sent", None
        await asyncio.to_thread(self.store.delivery, state["record"]["event_id"], delivery, sent_at)
        self.logger.info(f"[日程判断] 发送确认 {event.unified_msg_origin} delivery={delivery}")
        self.audit_next_run(state["record"])

    async def bootstrap(self, session_id):
        event = self.make_event(session_id)
        content = bootstrap_candidate(await self.history(session_id))
        if not content:
            raise ValueError("没有找到带具体时间的完整文字日程，请在聊天中提供文字日程")
        digest = hashlib.sha256(content.encode()).hexdigest()
        engine, scope, persona = await self.memory_bridge(event)
        results = await engine.search_memories(
            query=content[:200], k=5, session_id=scope, persona_id=persona
        )
        existing = next(
            (
                item
                for item in results
                if item.content.strip() == content
                or item.metadata.get("schedule_guard_source_hash") == digest
            ),
            None,
        )
        memory_id = (
            existing.doc_id
            if existing
            else await engine.add_memory(
                content=content,
                session_id=scope,
                persona_id=persona,
                importance=0.9,
                metadata={
                    "topics": ["日程", "课表", "没空时间表"],
                    "schedule_guard_source_hash": digest,
                    "source": "user_conversation_schedule",
                    "memory_type": "event",
                },
                source_messages=[{"role": "user", "content": content}],
            )
        )
        verified = await engine.search_memories(
            query="大三上 没空时间表 日程 课表",
            k=5,
            session_id=scope,
            persona_id=persona,
        )
        return {
            "memory_id": memory_id,
            "created": existing is None,
            "verified": any(str(item.doc_id) == str(memory_id) for item in verified),
            "characters": len(content),
        }

    def next_tasks(self):
        owner = self.instance(PROACTIVE)
        result = []
        for session_id in self.sessions():
            job = owner.scheduler.get_job(session_id) if owner and owner.scheduler else None
            result.append(
                {
                    "session_id": session_id,
                    "next_run_time": job.next_run_time.isoformat()
                    if job and job.next_run_time
                    else None,
                }
            )
        return result
