"""Context maintenance: deterministic periodic re-anchoring.

History (kept for the walkthrough): a usage-drop eviction *detector* was built
and then deleted after live data showed usage_metadata.total_token_count is
per-turn accounting noise (interrupted turns report partial usage; totals
swing 4.2k -> 8.9k -> 4.9k in a minute). Final design: no detection at all —
an oversized compression window makes eviction impossible in meeting-length
sessions, and the runtime re-sends the authoritative discovery state on a
timer, only at quiet moments, never during wrap-up or after the meeting."""

import asyncio
import time

from gladiator.config import Settings
from gladiator.runtime import Runtime


class StubSession:
    async def send_realtime_input(self, **kwargs):
        pass

    async def send_tool_response(self, **kwargs):
        pass


def make_runtime(tmp_path, **overrides) -> Runtime:
    settings = Settings(recall_api_key="x", gemini_api_key="x", runs_dir=tmp_path,
                        reanchor_interval_s=1, **overrides)
    rt = Runtime(settings)
    rt.session._session = StubSession()
    rt._admitted = True
    rt.session._greeted = True
    return rt


def room_quiet(rt: Runtime) -> None:
    now = time.monotonic()
    rt.bridge.vad.last_feed_mono = now  # VAD live (feed flowing), hearing silence
    rt.bridge.vad.in_voice = False
    rt.bridge.vad.last_voice_mono = now - 5


def room_talking(rt: Runtime) -> None:
    now = time.monotonic()
    # far-future feed mark = "frames keep flowing" (a single poke would go
    # stale mid-test; production updates this 50x/sec)
    rt.bridge.vad.last_feed_mono = now + 3600
    rt.bridge.vad.in_voice = True
    rt.bridge.vad.voice_since = now
    rt.bridge.vad.last_voice_mono = now


async def run_reanchor_for(rt: Runtime, seconds: float) -> None:
    task = asyncio.create_task(rt._periodic_reanchor())
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        rt.bus.close()


def saw(rt: Runtime, event_type: str) -> bool:
    return any(e["type"] == event_type for e in rt.bus.ring)


async def test_periodic_reanchor_sends_state_at_quiet_moment(tmp_path):
    rt = make_runtime(tmp_path)
    rt.tracker.record_answer("current-systems", "Siebel CRM.")
    room_quiet(rt)
    await run_reanchor_for(rt, 1.6)
    assert saw(rt, "runtime.periodic_reanchor")
    nudges = [e for e in rt.bus.ring if e["type"] == "gemini.nudge"]
    assert any("authoritative discovery state" in e["data"]["text"] for e in nudges)


async def test_reanchor_waits_out_a_talking_room(tmp_path):
    rt = make_runtime(tmp_path)
    room_talking(rt)  # and they keep talking
    await run_reanchor_for(rt, 1.6)
    assert not saw(rt, "runtime.periodic_reanchor")


async def test_reanchor_never_interrupts_open_generation(tmp_path):
    """Review finding: a text nudge is a user turn — landing it while the model
    is generating cancels the pending answer. Quiet floor is not enough."""
    rt = make_runtime(tmp_path)
    room_quiet(rt)
    rt.bridge.on_model_audio(b"\x00\x01" * 4800)  # generation in flight
    await run_reanchor_for(rt, 1.6)
    assert not saw(rt, "runtime.periodic_reanchor")


async def test_reanchor_skips_wrapup_and_stops_after_meeting(tmp_path):
    rt = make_runtime(tmp_path)
    room_quiet(rt)
    rt.tracker.set_phase("wrapup")
    await run_reanchor_for(rt, 1.4)
    assert not saw(rt, "runtime.periodic_reanchor"), "no re-anchoring during goodbye"

    rt2 = make_runtime(tmp_path)
    room_quiet(rt2)
    rt2.bot_id = "bot-1"
    rt2.bot_status = "done"  # meeting over
    task = asyncio.create_task(rt2._periodic_reanchor())
    await asyncio.sleep(1.4)
    assert task.done(), "task must end for good once the meeting is over"
    assert not saw(rt2, "runtime.periodic_reanchor")
    rt2.bus.close()


async def test_reanchor_event_only_when_actually_delivered(tmp_path):
    """Review finding: the event fired before send_text, which silently no-ops
    mid-reconnect — the log claimed anchors that never reached the model."""
    rt = make_runtime(tmp_path)
    room_quiet(rt)
    rt.session._session = None  # Gemini mid-reconnect
    await run_reanchor_for(rt, 1.6)
    assert not saw(rt, "runtime.periodic_reanchor")
    assert saw(rt, "gemini.send_dropped"), "the failed delivery is still observable"


async def test_not_before_greeting(tmp_path):
    rt = make_runtime(tmp_path)
    rt.session._greeted = False  # admitted but never greeted
    room_quiet(rt)
    await run_reanchor_for(rt, 1.4)
    assert not saw(rt, "runtime.periodic_reanchor")
