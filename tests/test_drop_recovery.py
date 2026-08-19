"""Regression for the 'bot seems mute at meeting start' finding.

People are usually talking right when they admit the bot, so (1) the greeting
must wait for a quiet floor instead of being generated straight into a
stale-drop, and (2) when a response IS force-dropped, the model must be told
participants never heard it — otherwise its world model and the humans'
diverge (it believes it asked a question nobody heard)."""

import asyncio
import time

from gladiator.config import Settings
from gladiator.runtime import Runtime


class StubSession:
    async def send_realtime_input(self, **kwargs):
        pass

    async def send_tool_response(self, **kwargs):
        pass


def make_runtime(tmp_path) -> Runtime:
    rt = Runtime(Settings(recall_api_key="x", gemini_api_key="x", runs_dir=tmp_path))
    rt.session._session = StubSession()
    rt._admitted = True
    rt.page_capture_ok = True
    return rt


def room_talking(rt: Runtime) -> None:
    rt.bridge.vad.in_voice = True
    rt.bridge.vad.voice_since = time.monotonic()
    rt.bridge.vad.last_voice_mono = time.monotonic()


def room_quiet(rt: Runtime, since_s: float = 1.0) -> None:
    rt.bridge.vad.in_voice = False
    rt.bridge.vad.voice_since = None
    rt.bridge.vad.last_voice_mono = time.monotonic() - since_s


def saw(rt: Runtime, event_type: str) -> bool:
    return any(e["type"] == event_type for e in rt.bus.ring)


async def test_greeting_waits_for_quiet_floor(tmp_path):
    rt = make_runtime(tmp_path)
    room_talking(rt)
    await rt._maybe_greet()
    await asyncio.sleep(0.3)
    assert not saw(rt, "gemini.greeting_sent"), "must not greet into a talking room"

    room_quiet(rt)
    await asyncio.sleep(0.4)
    assert saw(rt, "gemini.greeting_sent"), "greet once the floor is quiet"
    assert saw(rt, "runtime.greeting_waited_for_floor")


async def test_greeting_immediate_when_room_is_quiet(tmp_path):
    rt = make_runtime(tmp_path)
    room_quiet(rt)
    await rt._maybe_greet()
    await asyncio.sleep(0.3)
    assert saw(rt, "gemini.greeting_sent")
    assert not saw(rt, "runtime.greeting_waited_for_floor"), "no wait event when instant"


async def test_dropped_response_note_after_floor_frees(tmp_path):
    rt = make_runtime(tmp_path)
    room_talking(rt)
    rt.bridge.on_model_audio(b"\x00\x01" * 24000)
    rt.bridge.force_drop("gate_stale")     # response the humans never heard
    await asyncio.sleep(0.3)
    assert not saw(rt, "runtime.dropped_response_note"), "not while they're talking"

    room_quiet(rt)
    await asyncio.sleep(0.5)
    assert saw(rt, "runtime.dropped_response_note")
    nudges = [e for e in rt.bus.ring if e["type"] == "gemini.nudge"]
    assert any("could NOT hear" in e["data"]["text"] for e in nudges)


async def test_no_note_if_model_already_moved_on(tmp_path):
    rt = make_runtime(tmp_path)
    room_talking(rt)
    rt.bridge.on_model_audio(b"\x00\x01" * 24000)
    rt.bridge.force_drop("gate_stale")
    rt.bridge.on_turn_complete()                  # dropped generation's boundary
    rt.bridge.on_model_audio(b"\x00\x01" * 4800)  # a NEWER generation started
    room_quiet(rt)
    await asyncio.sleep(0.5)
    assert not saw(rt, "runtime.dropped_response_note"), \
        "superseded drops need no restatement"
