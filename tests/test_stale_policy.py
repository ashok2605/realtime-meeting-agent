"""Stale-response policy through the real Pacer: a response buffered
behind a human monologue is dropped, tail included, never played late."""

import asyncio

from gladiator.audio.bridge import AudioBridge, Pacer
from gladiator.core.events import EventBus
from gladiator.core.latency import LatencyTracker
from gladiator.core.turn_gate import FloorGate, SpeechState


class PageSink:
    def __init__(self):
        self.chunks: list[tuple[int, int, bytes]] = []
        self.flushes: list[int] = []

    async def send(self, epoch: int, seq: int, pcm: bytes) -> bool:
        self.chunks.append((epoch, seq, pcm))
        return True

    async def flush(self, epoch: int) -> None:
        self.flushes.append(epoch)


def make_stack(stale_drop_ms=150, grace_ms=20):
    bus = EventBus()
    latency = LatencyTracker(bus)
    speech = SpeechState(bot_name="Gladiator")
    gate = FloorGate(speech, bus, grace_ms=grace_ms,
                     stale_drop_ms=stale_drop_ms, max_hold_ms=10_000)
    bridge = AudioBridge(bus, latency, chunk_ms=40)
    sink = PageSink()
    bridge.notify_page_flush = sink.flush
    pacer = Pacer(bridge, gate, bus, latency, chunk_ms=40, prime_ms=160, send=sink.send)
    return bus, speech, bridge, pacer, sink


def model_audio(bridge: AudioBridge, seconds: float) -> None:
    bridge.on_model_audio(b"\x01\x02" * int(24000 * seconds))


async def test_stale_answer_dropped_not_played_after_monologue():
    bus, speech, bridge, pacer, sink = make_stack()
    task = asyncio.create_task(pacer.run())
    try:
        # Human starts a monologue; Gemini (already triggered) starts answering.
        speech.on_speech_on("1", "Priya")
        model_audio(bridge, 3.0)

        await asyncio.sleep(0.4)  # > stale_drop while Priya keeps talking
        assert sink.chunks == [], "nothing may play while a human speaks"
        assert bridge.buffer.buffered_ms == 0, "stale response must be dropped"
        assert any(e["type"] == "turn.force_drop" for e in bus.ring)

        # The rest of that generation still streams in: must be discarded too.
        model_audio(bridge, 2.0)
        assert bridge.buffer.buffered_ms == 0, "generation tail must be discarded"

        # Priya stops. The dropped answer must NOT come back from the dead.
        speech.on_speech_off("1", "Priya")
        await asyncio.sleep(0.2)
        assert sink.chunks == []

        # Turn boundary ends discard mode; the next generation plays normally.
        bridge.on_turn_complete()
        model_audio(bridge, 0.2)
        bridge.on_generation_complete()
        await asyncio.sleep(0.3)
        assert sink.chunks, "fresh generation after the drop must play"
    finally:
        task.cancel()


async def test_quick_hold_releases_without_drop():
    """A short human interjection just delays the agent, it doesn't kill the answer."""
    bus, speech, bridge, pacer, sink = make_stack(stale_drop_ms=1_000)
    task = asyncio.create_task(pacer.run())
    try:
        speech.on_speech_on("1", "Priya")
        model_audio(bridge, 0.2)
        bridge.on_generation_complete()
        await asyncio.sleep(0.1)
        assert sink.chunks == []
        speech.on_speech_off("1", "Priya")
        await asyncio.sleep(0.3)
        assert sink.chunks, "answer should play once the human stopped"
        assert all(e == sink.chunks[0][0] for e, _, _ in sink.chunks)
    finally:
        task.cancel()


async def test_interrupted_while_gate_held_flushes_before_release():
    """Gemini cancelling a stale generation (its own VAD saw the barge-in) must
    leave nothing to play when the human stops."""
    bus, speech, bridge, pacer, sink = make_stack(stale_drop_ms=5_000)
    task = asyncio.create_task(pacer.run())
    try:
        speech.on_speech_on("1", "Priya")
        model_audio(bridge, 3.0)
        await asyncio.sleep(0.05)
        bridge.on_interrupted()  # server-side cancellation arrives
        speech.on_speech_off("1", "Priya")
        await asyncio.sleep(0.2)
        assert sink.chunks == []
        assert sink.flushes, "page told to flush on interruption"
    finally:
        task.cancel()
