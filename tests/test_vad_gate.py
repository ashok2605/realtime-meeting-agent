"""Floor gate signal policy: live VAD is authoritative; Recall events
gate only when the VAD is stale or absent (DESIGN.md §4)."""

import asyncio
import time

from gladiator.audio.bridge import EnergyVAD
from gladiator.config import Settings
from gladiator.core.events import EventBus
from gladiator.core.turn_gate import DROP, OPEN, FloorGate, SpeechState
from gladiator.runtime import Runtime

LOUD = (3000).to_bytes(2, "little", signed=True) * 320
QUIET = b"\x00\x00" * 320


def make_gate(vad: EnergyVAD, grace_ms=30, stale_drop_ms=200, clock=None):
    # One clock everywhere: mixing a fake gate clock with SpeechState's real
    # clock produces garbage silence durations (a reviewed pitfall).
    import time as _time
    clock = clock or _time.monotonic
    speech = SpeechState(bot_name="Gladiator", clock=clock)
    gate = FloorGate(speech, EventBus(), grace_ms=grace_ms,
                     stale_drop_ms=stale_drop_ms, max_hold_ms=10_000,
                     vad=vad, clock=clock)
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


async def test_vad_is_authoritative_over_phantom_speech_events():
    """A phantom speech_on (no speech_off) must not block a quiet room."""
    t = {"now": 100.0}
    vad = EnergyVAD(hangover_ms=50, clock=lambda: t["now"])
    speech, gate = make_gate(vad)
    gate._clock = lambda: t["now"]
    vad.feed(LOUD)
    t["now"] = 101.0
    vad.feed(QUIET)  # room genuinely quiet for ~1s
    speech.on_speech_on("1", "Madamanchi Ashok Chowdary")  # phantom, no speech_off
    assert gate.is_open(), "phantom event must not block a quiet room"
    assert await gate.wait_for_floor() == OPEN

    # real voice still blocks, of course
    vad.feed(LOUD)
    assert not gate.is_open()


async def test_events_still_gate_when_vad_disabled():
    speech = SpeechState(bot_name="Gladiator")
    gate = FloorGate(speech, EventBus(), grace_ms=0,
                     stale_drop_ms=100_000, max_hold_ms=10_000, vad=None)
    speech.on_speech_on("1", "Priya")
    assert not gate.is_open()
    speech.on_speech_off("1", "Priya")
    await asyncio.sleep(0.01)
    assert gate.is_open()


async def test_hard_stop_vad_is_final_while_live(tmp_path):
    """A live VAD's word is final for the watchdog: speech events alone must
    never kill agent audio (live regression: 18/23 kills fired during
    between-sentence pauses with a provably quiet mic). Events back-stop only
    when the VAD is stale."""
    settings = Settings(recall_api_key="x", gemini_api_key="x", runs_dir=tmp_path,
                        gate_hard_stop_ms=50, gate_max_hold_ms=1_000)
    rt = Runtime(settings)
    now = time.monotonic()
    rt.pacer._last_send_mono = now                      # agent actively playing

    # live VAD hearing voice fires
    rt.bridge.vad.last_feed_mono = now + 3600           # feed flowing
    rt.bridge.vad.in_voice = True
    rt.bridge.vad.voice_since = now - 0.2
    assert rt._hard_stop_due() is not None

    rt.bridge.last_interrupt_mono = now                 # Gemini already handled it
    assert rt._hard_stop_due() is None

    # live VAD quiet + fresh speech event = between-sentence pause: NO kill
    rt.bridge.last_interrupt_mono = -1e9
    rt.bridge.vad.in_voice = False
    rt.bridge.vad.voice_since = None
    rt.speech.on_speech_on("1", "Rajesh Raja")
    rt.speech.speaking["1"].since_mono = now - 2.0
    assert rt._hard_stop_due() is None, "events must not kill while the VAD is live"

    # VAD stale (feed died): events become the backstop again
    rt.bridge.vad.last_feed_mono = now - 10
    rt.speech.speaking["1"].since_mono = now - 0.2      # fresh
    assert rt._hard_stop_due() is not None

    # ...but an ancient stuck phantom is pruned, not acted on
    rt.speech.speaking["1"].since_mono = now - 5        # > max_hold (1s)
    assert rt._hard_stop_due() is None
    assert not rt.speech.any_human_speaking(), "phantom must be pruned"
    rt.bus.close()


async def test_stale_vad_falls_back_to_events_gating():
    """A stale (stalled-feed) VAD surrenders gate authority back to events."""
    t = {"now": 100.0}
    vad = EnergyVAD(hangover_ms=50, clock=lambda: t["now"])
    speech, gate = make_gate(vad, clock=lambda: t["now"])

    # feed alive and quiet -> VAD authority, phantom events ignored
    vad.feed(QUIET)
    t["now"] = 101.0
    speech.on_speech_on("1", "Priya")
    assert gate.is_open()

    # feed stalls (> VAD_STALE_AFTER_S with no frames) -> events gate again
    t["now"] = 103.0
    assert not gate.is_open(), "stale VAD must not keep the floor open"
    speech.on_speech_off("1", "Priya")
    t["now"] = 104.0
    assert gate.is_open()

    # frozen-in-voice stall must not mute the bot forever either
    vad.feed(LOUD)          # mid-word...
    assert not gate.is_open()
    t["now"] = 106.0        # ...feed dies for 2s: VAD stale, events say quiet
    assert gate.is_open()


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
