"""FloorGate state machine: the 'agent never interrupts a human' invariants."""

import asyncio

from gladiator.core.events import EventBus
from gladiator.core.turn_gate import DROP, OPEN, FloorGate, SpeechState


def make_gate(grace_ms=30, stale_drop_ms=200, max_hold_ms=500):
    speech = SpeechState(bot_name="Gladiator")
    gate = FloorGate(speech, EventBus(), grace_ms=grace_ms,
                     stale_drop_ms=stale_drop_ms, max_hold_ms=max_hold_ms)
    return speech, gate


async def test_open_when_silent_past_grace():
    speech, gate = make_gate()
    speech.on_speech_on("1", "Priya")
    speech.on_speech_off("1", "Priya")
    await asyncio.sleep(0.04)  # > grace
    assert await gate.wait_for_floor() == OPEN


async def test_holds_while_human_speaks_then_opens():
    speech, gate = make_gate()
    speech.on_speech_on("1", "Priya")

    async def release():
        await asyncio.sleep(0.05)
        speech.on_speech_off("1", "Priya")

    asyncio.get_running_loop().create_task(release())
    result = await gate.wait_for_floor()
    assert result == OPEN
    # gate waited for speech_off + grace
    assert gate.last_hold_ms >= 50


async def test_grace_period_blocks_immediately_after_speech_off():
    speech, gate = make_gate(grace_ms=80, stale_drop_ms=1000)
    speech.on_speech_on("1", "Priya")
    speech.on_speech_off("1", "Priya")
    assert not gate.is_open()  # inside grace window
    assert await gate.wait_for_floor() == OPEN
    assert gate.last_hold_ms >= 70  # ~grace

    # long-quiet room: instantly open
    assert await gate.wait_for_floor() == OPEN


async def test_stale_drop_when_human_monologues():
    """User-mandated: a response buffered behind a long human turn must be
    DROPped, never played stale when they finally stop."""
    speech, gate = make_gate(stale_drop_ms=120)
    speech.on_speech_on("1", "Priya")  # never stops talking
    result = await gate.wait_for_floor()
    assert result == DROP
    assert gate.last_hold_ms >= 120


async def test_bot_own_speech_events_ignored():
    speech, gate = make_gate()
    speech.on_speech_on("99", "Gladiator")  # the bot hears itself via Recall
    assert not speech.any_human_speaking()
    await asyncio.sleep(0.04)
    assert await gate.wait_for_floor() == OPEN


async def test_stuck_speech_on_pruned_after_max_hold():
    """A lost speech_off can't mute the agent forever."""
    speech, gate = make_gate(grace_ms=10, stale_drop_ms=100_000, max_hold_ms=150)
    speech.on_speech_on("1", "Priya")  # speech_off never arrives
    result = await asyncio.wait_for(gate.wait_for_floor(), timeout=2)
    assert result == OPEN
    assert not speech.any_human_speaking()


async def test_participant_leaving_mid_speech_releases_floor():
    speech, gate = make_gate(grace_ms=20, stale_drop_ms=100_000)
    speech.on_speech_on("1", "Priya")

    async def leave():
        await asyncio.sleep(0.03)
        speech.on_participant_leave("1")

    asyncio.get_running_loop().create_task(leave())
    assert await asyncio.wait_for(gate.wait_for_floor(), timeout=2) == OPEN


async def test_two_speakers_both_must_stop():
    speech, gate = make_gate(grace_ms=20, stale_drop_ms=100_000)
    speech.on_speech_on("1", "Priya")
    speech.on_speech_on("2", "Marco")
    speech.on_speech_off("1", "Priya")
    assert speech.any_human_speaking()

    async def marco_stops():
        await asyncio.sleep(0.03)
        speech.on_speech_off("2", "Marco")

    asyncio.get_running_loop().create_task(marco_stops())
    assert await asyncio.wait_for(gate.wait_for_floor(), timeout=2) == OPEN
