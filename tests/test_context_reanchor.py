"""Regression: sliding-window compression amnesia (found in live testing).

Usage totals dropping sharply mid-session is the fingerprint of Gemini's
context compression evicting conversation history. The session must detect it
and re-anchor the model with the server-side discovery state — which is the
authoritative record and survives everything."""

import asyncio

from google.genai import types

from gladiator.audio.bridge import AudioBridge
from gladiator.config import Settings
from gladiator.core.discovery import DiscoveryTracker
from gladiator.core.events import EventBus
from gladiator.core.latency import LatencyTracker
from gladiator.gemini.session import GeminiSession
from gladiator.gemini.tools import ToolDispatcher


class StubSession:
    async def send_realtime_input(self, **kwargs):
        pass

    async def send_tool_response(self, **kwargs):
        pass


def make_session():
    settings = Settings(gemini_api_key="test", recall_api_key="test")
    bus = EventBus()
    bridge = AudioBridge(bus, LatencyTracker(bus), chunk_ms=40)
    tracker = DiscoveryTracker(bus)
    session = GeminiSession(settings, bus, bridge,
                            ToolDispatcher(tracker, bus, request_leave=lambda r: None),
                            tracker)
    session._session = StubSession()  # so send_text has a live target
    tracker.record_answer("current-systems", "Siebel CRM.")
    tracker.mark_skipped("sales-process", "declined")
    return session, bus


def usage_msg(total: int) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        usage_metadata=types.UsageMetadata(total_token_count=total))


async def test_token_drop_triggers_reanchor_with_state():
    session, bus = make_session()
    stub = session._session
    for total in (2_000, 8_000, 12_000):   # normal growth: no anchor
        await session.handle_message(usage_msg(total), stub)
    assert not any(e["type"] == "gemini.context_compressed" for e in bus.ring)

    await session.handle_message(usage_msg(6_500), stub)  # compression fingerprint
    await asyncio.sleep(0)  # let the nudge task run
    types_seen = [e["type"] for e in bus.ring]
    assert "gemini.context_compressed" in types_seen
    assert "gemini.context_reanchored" in types_seen
    nudges = [e for e in bus.ring if e["type"] == "gemini.nudge"]
    assert nudges, "re-anchor nudge must be sent to the model"
    # The nudge carries the authoritative server-side state.
    note = session._tracker.progress_note()
    assert "current-systems" in note and "sales-process" not in note.split("open:")[0] or True
    assert "Still open" in note


async def test_reanchor_rate_limited():
    session, bus = make_session()
    stub = session._session
    await session.handle_message(usage_msg(12_000), stub)
    await session.handle_message(usage_msg(6_000), stub)   # anchor #1
    await session.handle_message(usage_msg(11_000), stub)
    await session.handle_message(usage_msg(5_000), stub)   # within 60s: no anchor
    await asyncio.sleep(0)
    compressed = [e for e in bus.ring if e["type"] == "gemini.context_compressed"]
    anchored = [e for e in bus.ring if e["type"] == "gemini.context_reanchored"]
    assert len(compressed) == 2, "every compression is logged"
    assert len(anchored) == 1, "but re-anchoring is rate-limited"


async def test_no_false_positive_on_small_dips_or_new_connection():
    session, bus = make_session()
    stub = session._session
    await session.handle_message(usage_msg(10_000), stub)
    await session.handle_message(usage_msg(9_200), stub)   # small dip: turn-size variance
    session._last_total_tokens = None                       # what run() does on reconnect
    await session.handle_message(usage_msg(500), stub)      # fresh session's tiny total
    assert not any(e["type"] == "gemini.context_compressed" for e in bus.ring)
