from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

# Load by location so clone/archive directory names do not affect test discovery.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if "astrbot_plugin_schedule_guard" not in sys.modules:
    package_spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_schedule_guard",
        PACKAGE_ROOT / "__init__.py",
        submodule_search_locations=[str(PACKAGE_ROOT)],
    )
    package_module = importlib.util.module_from_spec(package_spec)
    sys.modules["astrbot_plugin_schedule_guard"] = package_module
    package_spec.loader.exec_module(package_module)
from astrbot_plugin_schedule_guard.engine import (
    BEIJING,
    bootstrap_candidate,
    history_sources,
    parse_decision,
)
from astrbot_plugin_schedule_guard.runtime import EXTRA_KEY, GuardService
from astrbot_plugin_schedule_guard.storage import AuditStore

UMO = "test:FriendMessage:alice"
# Synthetic fixture, unrelated to any real user's timetable.
TABLE = "# 测试日程 · 没空时间表\n每周一 13:50–16:30 上课。2026-01-01 至 2026-12-31，02-16除外。\n每周一18:20–20:45测试活动，03-02提前至19:50结束。\n2026-07-01、2026-07-02全天占用。"
HISTORY = [{"role": "user", "content": TABLE}]
LOG = logging.getLogger("schedule_guard_tests")


def output(status="busy", source="H0000", quote="每周一 13:50–16:30 上课", boundary=None):
    return json.dumps(
        {
            "status": status,
            "reason": "测试日程判断",
            "evidence": [{"source": source, "quote": quote}],
            "next_boundary": boundary,
        },
        ensure_ascii=False,
    )


class FakeEvent:
    def __init__(self, owner=None, message_id="proactive:one"):
        self._proactive_plugin = owner
        self.message_obj = NS(message_id=message_id)
        self.unified_msg_origin = UMO
        self.extra = {}
        self.stopped = False
        self._has_send_oper = False
        self.proactive_send_failed = False

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value

    def stop_event(self):
        self.stopped = True


class FakeContext:
    def __init__(self, owner, outputs=None):
        self.owner, self.outputs, self.calls = owner, list(outputs or [output()]), []

        async def cid(umo):
            return "conversation"

        async def conversation(umo, cid):
            return NS(history=HISTORY)

        self.conversation_manager = NS(get_curr_conversation_id=cid, get_conversation=conversation)

    def get_all_stars(self):
        return [
            NS(
                name="astrbot_plugin_proactive_chat",
                activated=True,
                star_cls=self.owner,
            )
        ]

    async def get_current_chat_provider_id(self, umo):
        return "current/model"

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return NS(completion_text=value)

    async def send_message(self, *args, **kwargs):
        raise AssertionError("Judgment and simulation must never call platform send")


class EngineTests(unittest.TestCase):
    def test_busy_requires_real_user_evidence(self):
        now = datetime(2026, 10, 19, 14, tzinfo=BEIJING)
        sources = history_sources(HISTORY)
        self.assertEqual(parse_decision(output(), sources, now).status, "busy")
        self.assertEqual(
            parse_decision(output(quote="不存在的证据"), sources, now).status, "unknown"
        )
        self.assertEqual(parse_decision(output(source="M999"), sources, now).status, "unknown")
        sources[0]["role"] = "assistant"
        self.assertEqual(parse_decision(output(), sources, now).status, "unknown")

    def test_bad_json_and_status_fail_open(self):
        now = datetime.now(BEIJING)
        for raw in ("上课中", "[]", "{}", '{"status":"yes"}', "null"):
            self.assertEqual(parse_decision(raw, history_sources(HISTORY), now).status, "unknown")

    def test_code_fence_and_valid_boundary(self):
        now = datetime(2026, 10, 19, 14, tzinfo=BEIJING)
        decision = parse_decision(
            "```json\n" + output(boundary="2026-10-19T16:30:00+08:00") + "\n```",
            history_sources(HISTORY),
            now,
        )
        self.assertEqual(decision.next_boundary, "2026-10-19T16:30:00+08:00")
        for boundary in (
            "2026-10-19T13:00:00+08:00",
            "2026-10-19T16:30:00",
            "not-a-date",
        ):
            self.assertIsNone(
                parse_decision(
                    output(boundary=boundary), history_sources(HISTORY), now
                ).next_boundary
            )

    def test_history_filters_generated_tasks_and_media(self):
        history = HISTORY + [
            {"role": "user", "content": "[系统任务：主动对话] 请发送消息"},
            {"role": "tool", "content": "ignore"},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "secret"}},
                    {"type": "text", "text": "今天请假"},
                ],
            },
        ]
        sources = history_sources(history)
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[-1]["content"], "今天请假")

    def test_bootstrap_keeps_full_timetable_not_assistant_guesses(self):
        history = HISTORY + [
            {"role": "assistant", "content": TABLE + "改动"},
            {"role": "user", "content": "我明天上课"},
        ]
        self.assertEqual(bootstrap_candidate(history), TABLE)


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AuditStore(Path(self.temp.name) / "checks.db")
        self.owner = object()
        self.context = FakeContext(self.owner)
        self.guard = GuardService(
            self.context, {"enabled": True, "session_list": [UMO]}, self.store, LOG
        )

        async def no_memory(event, now):
            return [], []

        self.guard.recall = no_memory
        self.event = FakeEvent(self.owner)
        self.req = NS(contexts=HISTORY)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_busy_stops_before_body_and_records(self):
        await self.guard.on_request(self.event, self.req)
        self.assertTrue(self.event.stopped)
        self.assertEqual(self.store.list()[0]["delivery"], "skipped_busy")
        call = self.context.calls[0]
        self.assertEqual(call["contexts"], [])
        self.assertIsNone(call["tools"])
        self.assertNotIn("人格", call["prompt"])
        await self.guard.on_request(self.event, self.req)
        self.assertEqual(len(self.context.calls), 1)
        self.assertEqual(len(self.store.list()), 1)

    async def test_free_unknown_timeout_and_bad_json_allow(self):
        for index, value in enumerate(
            (
                output("free"),
                '{"status":"unknown","reason":"没有日程"}',
                TimeoutError(),
                "bad json",
            )
        ):
            self.context.outputs = [value]
            event = FakeEvent(self.owner, f"proactive:{index}")
            await self.guard.on_request(event, self.req)
            self.assertFalse(event.stopped)
            self.assertEqual(
                event.get_extra(EXTRA_KEY)["record"]["delivery"], "allowed_unconfirmed"
            )

    async def test_only_configured_real_proactive_events(self):
        for event in (FakeEvent(object()), FakeEvent(self.owner, "real-user-message")):
            await self.guard.on_request(event, self.req)
            self.assertFalse(event.stopped)
        self.event.unified_msg_origin = "test:FriendMessage:other"
        await self.guard.on_request(self.event, self.req)
        self.guard.config["enabled"] = False
        await self.guard.on_request(FakeEvent(self.owner), self.req)
        self.assertEqual(len(self.context.calls), 0)
        self.assertEqual(self.store.list(), [])

    async def test_memory_only_and_scoped_two_queries(self):
        self.guard.recall = GuardService.recall.__get__(self.guard)
        calls = []

        async def search(**kwargs):
            calls.append(kwargs)
            return [NS(doc_id=7, content=TABLE, metadata={"create_time": 1})]

        async def bridge(event):
            return NS(search_memories=search), "private-scope", "persona-one"

        self.guard.memory_bridge = bridge
        self.context.outputs = [output(source="M7")]
        record = await self.guard.check(
            self.event, [], datetime(2026, 10, 19, 14, tzinfo=BEIJING), True
        )
        self.assertEqual(record["status"], "busy")
        self.assertEqual(record["memory_count"], 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(
            all(
                call["k"] == 5
                and call["session_id"] == "private-scope"
                and call["persona_id"] == "persona-one"
                for call in calls
            )
        )

    async def test_memory_failure_records_unknown_and_allows_despite_old_context(self):
        self.guard.recall = GuardService.recall.__get__(self.guard)

        async def broken(event):
            raise RuntimeError("unavailable")

        self.guard.memory_bridge = broken
        record = await self.guard.check(self.event, HISTORY, simulation=True)
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(record["memory_errors"], ["RuntimeError"])
        self.assertEqual(len(self.context.calls), 0)

    async def test_partial_memory_failure_allows_missing_cancellation(self):
        self.guard.recall = GuardService.recall.__get__(self.guard)

        async def search(**kwargs):
            if "取消" in kwargs["query"]:
                raise TimeoutError()
            return [
                NS(
                    doc_id=7,
                    content=TABLE,
                    metadata={"source": "user_conversation_schedule"},
                )
            ]

        async def memory_bridge(event):
            return NS(search_memories=search), "scope", "persona"

        self.guard.memory_bridge = memory_bridge
        record = await self.guard.check(self.event, HISTORY, simulation=True)
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(record["error"], "memory_search_failed")
        self.assertEqual(record["memory_count"], 1)
        self.assertEqual(len(self.context.calls), 0)

    async def test_no_context_no_memory_is_unknown_without_model(self):
        record = await self.guard.check(self.event, [], simulation=True)
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(len(self.context.calls), 0)

    async def test_recent_cancellation_preserved_after_old_memory(self):
        history = HISTORY + [{"role": "user", "content": "今天下午这节课取消了，我有空。"}]
        self.context.outputs = [output("free", source="H0001", quote="今天下午这节课取消了")]
        record = await self.guard.check(self.event, history, simulation=True)
        self.assertEqual(record["status"], "free")
        payload = json.loads(self.context.calls[0]["prompt"])
        self.assertEqual(payload["context"][-1]["content"], history[-1]["content"])

    async def test_simulation_does_not_stop_event_or_confirm_send(self):
        record = await self.guard.check(self.event, HISTORY, simulation=True)
        self.assertFalse(self.event.stopped)
        self.assertEqual(record["delivery"], "simulation")
        self.assertNotIn("live:sent", self.store.stats())

    async def test_after_hook_is_not_alone_proof_of_delivery(self):
        self.context.outputs = [output("free")]
        await self.guard.on_request(self.event, self.req)
        await self.guard.after_sent(self.event)
        self.assertEqual(self.store.list()[0]["delivery"], "not_sent")
        self.event._has_send_oper = True
        await self.guard.after_sent(self.event)
        self.assertEqual(self.store.list()[0]["delivery"], "sent")
        self.event.proactive_send_failed = True
        await self.guard.after_sent(self.event)
        self.assertEqual(self.store.list()[0]["delivery"], "sent_partial")

    async def test_crossed_boundary_rechecks_before_tts(self):
        self.context.outputs = [output("free"), output("busy")]
        await self.guard.on_request(self.event, self.req)
        state = self.event.get_extra(EXTRA_KEY)
        state["record"]["next_boundary"] = (
            datetime.now(BEIJING) - timedelta(seconds=1)
        ).isoformat()
        await self.guard.on_response(self.event, NS())
        self.assertTrue(self.event.stopped)
        self.assertEqual(len(self.context.calls), 2)
        self.assertEqual(self.store.list()[0]["delivery"], "skipped_busy")

    async def test_audit_survives_reopen_and_upserts_event(self):
        await self.guard.check(self.event, HISTORY)
        self.context.outputs.append(output())
        await self.guard.check(self.event, HISTORY)
        reopened = AuditStore(self.store.path)
        self.assertEqual(len(reopened.list()), 1)
        self.assertEqual(reopened.stats()["live:skipped_busy"], 1)

    async def test_next_round_is_observed_and_saved_without_modifying_scheduler(self):
        record = await self.guard.check(self.event, HISTORY)
        when = datetime.now(BEIJING) + timedelta(minutes=30)
        self.context.owner = NS(scheduler=NS(get_job=lambda session: NS(next_run_time=when)))
        await self.guard.record_next_run(record, delay=0)
        self.assertEqual(self.store.list()[0]["next_run_time"], when.isoformat())


def load_original_flow(path):
    saved = {
        name: sys.modules.get(name)
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.core",
            "astrbot.core.agent",
            "astrbot.core.agent.message",
        )
    }
    for name in saved:
        sys.modules[name] = types.ModuleType(name)
    sys.modules["astrbot.api"].logger = LOG
    for name in ("AssistantMessageSegment", "TextPart", "UserMessageSegment"):
        setattr(
            sys.modules["astrbot.core.agent.message"],
            name,
            lambda **kwargs: NS(**kwargs),
        )
    for name in (
        "original_guard_fixture",
        "original_guard_fixture.core",
        "original_guard_fixture.utils",
        "original_guard_fixture.utils.time_utils",
    ):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["original_guard_fixture.utils.time_utils"].is_quiet_time = lambda *args: False
    spec = importlib.util.spec_from_file_location("original_guard_fixture.core.chat_flow", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    return module.ProactiveCoreMixin


@unittest.skipUnless(
    os.environ.get("PROACTIVE_PLUGIN_ROOT"),
    "Set PROACTIVE_PLUGIN_ROOT for the installed v1.2.6 integration test",
)
class OriginalFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_round_has_no_body_tts_send_count_or_archive_but_reschedules(
        self,
    ):
        mixin = load_original_flow(
            Path(os.environ["PROACTIVE_PLUGIN_ROOT"]) / "core" / "chat_flow.py"
        )

        class Harness(mixin):
            def __init__(self):
                self.data_lock = asyncio.Lock()
                self.session_data = {UMO: {"unanswered_count": 2}}
                self.last_message_times = {}
                self.telemetry = None
                self.manual_trigger_sessions = set()
                self.web_admin_server = None
                self._terminating = False
                self.body = self.tts = self.sends = self.archives = self.schedules = 0

            def _normalize_session_id(self, s):
                return s

            async def _is_chat_allowed(self, s):
                return True, "allowed"

            def _get_session_config(self, s):
                return {
                    "enable": True,
                    "schedule_settings": {"max_unanswered_times": 4},
                }

            async def _prepare_llm_request(self, s):
                return {
                    "conv_id": "c",
                    "history": HISTORY,
                    "system_prompt": "persona",
                    "session_id": s,
                }

            def _build_proactive_event(self, s):
                return self.event

            async def _generate_llm_response(self, *args, **kwargs):
                await self.guard.on_request(kwargs["event"], NS(contexts=HISTORY))
                if kwargs["event"].stopped:
                    return None, ""
                self.body += 1
                return NS(), ""

            async def _schedule_next_chat_and_save(self, s):
                self.schedules += 1

            async def _send_proactive_message(self, *a, **k):
                self.tts += 1
                self.sends += 1
                return True

            async def _finalize_and_reschedule(self, *a, **k):
                self.archives += 1

            def _get_session_log_str(self, *a):
                return UMO

        owner = Harness()
        with tempfile.TemporaryDirectory() as directory:
            context = FakeContext(owner)
            owner.guard = GuardService(
                context,
                {"enabled": True, "session_list": [UMO]},
                AuditStore(Path(directory) / "checks.db"),
                LOG,
            )

            async def recall(*args):
                return [], []

            owner.guard.recall = recall
            owner.event = FakeEvent(owner)
            await owner.check_and_chat(UMO)
            self.assertEqual((owner.body, owner.tts, owner.sends, owner.archives), (0, 0, 0, 0))
            self.assertEqual(owner.schedules, 1)
            self.assertEqual(owner.session_data[UMO]["unanswered_count"], 2)
            self.assertEqual(owner.guard.store.list()[0]["delivery"], "skipped_busy")


if __name__ == "__main__":
    unittest.main()
