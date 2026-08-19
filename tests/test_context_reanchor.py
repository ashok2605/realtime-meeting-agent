"""Context maintenance regressions, shaped by three live findings:

1. Sliding-window eviction at small window sizes causes real mid-call amnesia
   (fixed by an oversized window — eviction can't occur in a meeting-length
   session).
2. usage_metadata.total_token_count is per-turn accounting, NOT a context-size
   proxy: interrupted turns report partial usage, and totals swing with turn
   size generally (4.2k -> 8.9k -> 4.9k in one live minute). So usage drops
   must NEVER trigger nudges (a false nudge makes the model speak unprompted);
   persistent dips are logged as informational `gemini.usage_dip` only.
3. The reliable mechanism is deterministic: a periodic re-anchor note with the
   authoritative server-side discovery state, delivered at a quiet moment."""

import asyncio
import time

from google.genai import types

from gladiator.audio.bridge import AudioBridge
from gladiator.config import Settings
from gladiator.core.discovery import DiscoveryTracker
from gladiator.core.events import EventBus
from gladiator.core.latency import LatencyTracker
from gladiator.gemini.session import GeminiSession
from gladiator.gemini.tools import ToolDispatcher
from gladiator.runtime import Runtime


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
    session._session = StubSession()
    return session, bus


def usage_msg(total: int) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        usage_metadata=types.UsageMetadata(total_token_count=total))


async def feed(session, totals):
    for total in totals:
        await session.handle_message(usage_msg(total), session._session)


async def test_usage_drops_never_nudge_only_log():
    session, bus = make_session()
    # verbatim pattern from runs/20260820-010704 that fooled the old detector
    await feed(session, [4_990, 2_795, 3_037, 3_569, 8_874, 4_878, 5_305])
    await asyncio.sleep(0)
    assert any(e["type"] == "gemini.usage_dip" for e in bus.ring), \
        "persistent dips are still logged for observability"
    assert not any(e["type"] == "gemini.nudge" for e in bus.ring), \
        "usage accounting noise must never make the model speak"


async def test_single_dip_not_even_logged():
    session, bus = make_session()
    # interrupted-turn artifact: one dip, right back above trend
    await feed(session, [12_000, 18_794, 10_050, 21_567])
    assert not any(e["type"] == "gemini.usage_dip" for e in bus.ring)


async def test_periodic_reanchor_sends_state_at_quiet_moment(tmp_path):
    settings = Settings(recall_api_key="x", gemini_api_key="x", runs_dir=tmp_path,
                        reanchor_interval_s=1)
    rt = Runtime(settings)
    rt.session._session = StubSession()
    rt._admitted = True
    rt.session._greeted = True
    rt.tracker.record_answer("current-systems", "Siebel CRM.")
    rt.bridge.vad.in_voice = False
    rt.bridge.vad.last_voice_mono = time.monotonic() - 5  # quiet room
    task = asyncio.create_task(rt._periodic_reanchor())
    try:
        await asyncio.sleep(1.6)
        assert any(e["type"] == "runtime.periodic_reanchor" for e in rt.bus.ring)
        nudges = [e for e in rt.bus.ring if e["type"] == "gemini.nudge"]
        assert any("authoritative discovery state" in e["data"]["text"] for e in nudges)
    finally:
        task.cancel()
        rt.bus.close()


async def test_periodic_reanchor_waits_out_a_talking_room(tmp_path):
    settings = Settings(recall_api_key="x", gemini_api_key="x", runs_dir=tmp_path,
                        reanchor_interval_s=1)
    rt = Runtime(settings)
    rt.session._session = StubSession()
    rt._admitted = True
    rt.session._greeted = True
    rt.bridge.vad.in_voice = True  # someone is talking, and keeps talking
    rt.bridge.vad.last_voice_mono = time.monotonic()
    task = asyncio.create_task(rt._periodic_reanchor())
    try:
        await asyncio.sleep(1.6)
        assert not any(e["type"] == "runtime.periodic_reanchor" for e in rt.bus.ring), \
            "the note must wait for a quiet moment"
    finally:
        task.cancel()
        rt.bus.close()


async def test_not_before_greeting(tmp_path):
    settings = Settings(recall_api_key="x", gemini_api_key="x", runs_dir=tmp_path,
                        reanchor_interval_s=1)
    rt = Runtime(settings)
    rt.session._session = StubSession()
    rt._admitted = True  # admitted but never greeted (e.g. page audio not up)
    task = asyncio.create_task(rt._periodic_reanchor())
    try:
        await asyncio.sleep(1.4)
        assert not any(e["type"] == "runtime.periodic_reanchor" for e in rt.bus.ring)
    finally:
        task.cancel()
        rt.bus.close()
