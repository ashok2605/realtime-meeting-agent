"""Regression: sliding-window compression amnesia detection (two live findings).

Finding 1 (runs/20260819-233914): compression evicting history shows up as
usage totals dropping and STAYING low; the model must be re-anchored with the
server-side discovery state.
Finding 2 (runs/20260820-001538): interrupted turns report partial-turn usage
— a single low reading that bounces back. Firing a re-anchor on those is
actively harmful (a text nudge makes the model speak, which caused unprompted
re-confirmation of answered questions). Detection requires two consecutive
depressed readings."""

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


async def feed(session, totals):
    for total in totals:
        await session.handle_message(usage_msg(total), session._session)


async def test_persistent_drop_triggers_reanchor_with_state():
    session, bus = make_session()
    # normal growth, then eviction: totals drop and stay low (finding 1 pattern)
    await feed(session, [2_000, 8_000, 12_000, 6_700, 7_200])
    await asyncio.sleep(0)  # let the nudge task run
    types_seen = [e["type"] for e in bus.ring]
    assert "gemini.context_compressed" in types_seen
    assert "gemini.context_reanchored" in types_seen
    nudges = [e for e in bus.ring if e["type"] == "gemini.nudge"]
    assert nudges, "re-anchor nudge must be sent to the model"
    assert "Still open" in session._tracker.progress_note()


async def test_single_dip_from_interrupted_turn_is_ignored():
    session, bus = make_session()
    # finding 2 pattern: dip on an interrupted turn, right back above trend
    await feed(session, [12_000, 18_794, 10_050, 21_567, 11_428, 23_229, 24_257])
    await asyncio.sleep(0)
    assert not any(e["type"] == "gemini.context_compressed" for e in bus.ring), \
        "oscillating totals are interrupted-turn artifacts, not eviction"
    assert not any(e["type"] == "gemini.nudge" for e in bus.ring)


async def test_reanchor_rate_limited():
    session, bus = make_session()
    await feed(session, [12_000, 6_000, 6_500])          # eviction #1 -> anchor
    await feed(session, [11_000, 5_000, 5_500])          # eviction #2 within 60s
    await asyncio.sleep(0)
    compressed = [e for e in bus.ring if e["type"] == "gemini.context_compressed"]
    anchored = [e for e in bus.ring if e["type"] == "gemini.context_reanchored"]
    assert len(compressed) == 2, "every confirmed eviction is logged"
    assert len(anchored) == 1, "but re-anchoring is rate-limited"


async def test_reconnect_reset_prevents_false_positive():
    session, bus = make_session()
    await feed(session, [10_000, 9_200])       # small dip: turn-size variance
    session._max_total_tokens = None           # what run() does on reconnect
    session._drop_streak = 0
    await feed(session, [500, 900])            # fresh session's tiny totals
    assert not any(e["type"] == "gemini.context_compressed" for e in bus.ring)
