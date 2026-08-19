"""Two-signal floor gate: EnergyVAD on the meeting audio is the primary
'a human is speaking' signal (regression from the first live rehearsal, where
Recall sent one speech_on and zero speech_off in a 4-minute single-participant
Meet, silently disabling the events-only gate)."""

import asyncio
import time

from gladiator.audio.bridge import EnergyVAD
from gladiator.config import Settings
from gladiator.core.events import EventBus
from gladiator.core.turn_gate import DROP, OPEN, FloorGate, SpeechState
from gladiator.runtime import Runtime

LOUD = (3000).to_bytes(2, "little", signed=True) * 320
QUIET = b"\x00\x00" * 320


def make_gate(vad: EnergyVAD, grace_ms=30, stale_drop_ms=200):
    speech = SpeechState(bot_name="Gladiator")
    gate = FloorGate(speech, EventBus(), grace_ms=grace_ms,
                     stale_drop_ms=stale_drop_ms, max_hold_ms=10_000, vad=vad)
    return speech, gate


async def test_vad_voice_holds_floor_without_any_recall_events():
    t = {"now": 0.0}
    vad = EnergyVAD(hangover_ms=50, clock=lambda: t["now"])
    _, gate = make_gate(vad, stale_drop_ms=150)
    vad.feed(LOUD)  # human audible; zero Recall events (the rehearsal scenario)
    assert not gate.is_open()
    assert await gate.wait_for_floor() == DROP  # keeps talking -> stale


async def test_vad_release_after_hangover_plus_grace():
    t = {"now": 100.0}
    vad = EnergyVAD(hangover_ms=100, clock=lambda: t["now"])
    _, gate = make_gate(vad, grace_ms=100)
    gate._clock = lambda: t["now"]
    vad.feed(LOUD)                       # voice at 100.0
    t["now"] = 100.05
    vad.feed(QUIET)                      # inside hangover: still counts as voice
    assert not gate.is_open()
    t["now"] = 100.15
    vad.feed(QUIET)                      # hangover elapsed -> quiet since 100.1
    assert not gate.is_open()            # grace (100ms after 100.1) not yet met
    t["now"] = 100.25                    # 150ms past the quiet point
    assert gate.is_open()
    assert await gate.wait_for_floor() == OPEN


async def test_either_signal_alone_blocks_the_floor():
    t = {"now": 0.0}
    vad = EnergyVAD(hangover_ms=50, clock=lambda: t["now"])
    speech, gate = make_gate(vad)
    # Recall event only (multi-participant case, quiet mic)
    speech.on_speech_on("1", "Priya")
    assert not gate.is_open()
    speech.on_speech_off("1", "Priya")
    t["now"] = 1.0
    await asyncio.sleep(0.05)  # real-clock grace for the events side
    assert gate.is_open()
    # VAD only (single-participant case, no events)
    vad.feed(LOUD)
    assert not gate.is_open()


async def test_hard_stop_due_fires_on_vad_only(tmp_path):
    settings = Settings(recall_api_key="x", gemini_api_key="x", runs_dir=tmp_path,
                        gate_hard_stop_ms=50)
    rt = Runtime(settings)
    now = time.monotonic()
    rt.pacer._last_send_mono = now                      # agent actively playing
    rt.bridge.vad.in_voice = True                       # human talking over it...
    rt.bridge.vad.voice_since = now - 0.2               # ...for 200ms
    assert rt._hard_stop_due() is not None

    rt.bridge.last_interrupt_mono = now                 # Gemini already handled it
    assert rt._hard_stop_due() is None

    rt.bridge.last_interrupt_mono = -1e9
    rt.bridge.vad.in_voice = False                      # nobody talking
    rt.bridge.vad.voice_since = None
    assert rt._hard_stop_due() is None
    rt.bus.close()


async def test_vad_gate_can_be_disabled(tmp_path):
    settings = Settings(recall_api_key="x", gemini_api_key="x", runs_dir=tmp_path,
                        vad_gate=False, gate_grace_ms=0)
    rt = Runtime(settings)
    rt.bridge.vad.in_voice = True  # echoing room: VAD must NOT gate
    rt.speech.last_speech_off_mono = time.monotonic() - 10
    assert rt.gate.is_open()
    # and the watchdog ignores VAD too
    rt.pacer._last_send_mono = time.monotonic()
    rt.bridge.vad.voice_since = time.monotonic() - 5
    assert rt._hard_stop_due() is None
