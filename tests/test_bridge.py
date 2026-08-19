"""TurnAudioBuffer chunking/epochs, EnergyVAD, inbound backpressure."""

import asyncio

from gladiator.audio.bridge import (
    OUT_BYTES_PER_MS,
    AudioBridge,
    EnergyVAD,
    TurnAudioBuffer,
)
from gladiator.core.events import EventBus
from gladiator.core.latency import LatencyTracker


async def test_buffer_rechunks_arbitrary_part_sizes():
    buf = TurnAudioBuffer(chunk_ms=40)
    chunk_bytes = 40 * OUT_BYTES_PER_MS
    buf.append(b"a" * (chunk_bytes + 100))      # one chunk + tail
    buf.append(b"b" * (chunk_bytes - 100))      # completes second chunk exactly
    epoch, c1 = await buf.get()
    _, c2 = await buf.get()
    assert len(c1) == len(c2) == chunk_bytes
    assert buf.buffered_ms == 0

    buf.append(b"c" * 10)
    buf.finish_turn()                            # partial tail flushes at turn end
    _, c3 = await buf.get()
    assert len(c3) == 10


async def test_flush_advances_epoch_and_drops_everything():
    buf = TurnAudioBuffer(chunk_ms=40)
    buf.append(b"x" * (40 * OUT_BYTES_PER_MS * 5))
    e0 = buf.epoch
    dropped = buf.flush()
    assert dropped == 200.0
    assert buf.epoch == e0 + 1
    assert buf.buffered_ms == 0


def test_energy_vad_marks_speech_end_after_hangover():
    t = {"now": 0.0}
    ends: list[float] = []
    vad = EnergyVAD(threshold=500, hangover_ms=300,
                    on_speech_end=ends.append, clock=lambda: t["now"])
    loud = (2000).to_bytes(2, "little", signed=True) * 320
    quiet = b"\x00\x00" * 320

    vad.feed(loud)
    assert vad.in_voice
    t["now"] = 0.1
    vad.feed(quiet)          # pause shorter than hangover: still "in voice"
    assert vad.in_voice and not ends
    t["now"] = 0.2
    vad.feed(loud)           # resumes; no speech-end fired
    t["now"] = 0.6
    vad.feed(quiet)
    assert not vad.in_voice
    assert ends == [0.2], "speech end anchored at the last voiced frame"


async def test_inbound_queue_drops_oldest_never_blocks():
    bridge = AudioBridge(EventBus(), LatencyTracker(EventBus()), chunk_ms=40)
    for i in range(200):  # far beyond maxsize, no sender running
        bridge.feed_frame(i.to_bytes(2, "little") * 160)
    assert bridge._inbound.qsize() == 64
    newest = await drain_last(bridge._inbound)
    assert newest == (199).to_bytes(2, "little") * 160, "newest audio survives"


async def drain_last(q: asyncio.Queue) -> bytes:
    item = None
    while not q.empty():
        item = q.get_nowait()
    return item


async def test_muted_bridge_forwards_nothing():
    bridge = AudioBridge(EventBus(), LatencyTracker(EventBus()), chunk_ms=40)
    bridge.muted = True
    bridge.feed_frame(b"\x00\x01" * 160)
    assert bridge._inbound.qsize() == 0


async def test_force_drop_discard_is_sticky_across_refires():
    """Review finding: the hard-stop watchdog can re-fire within its 280ms
    agent_playing window; the second force_drop used to CLEAR the discard flag,
    letting the killed generation's still-streaming tail play as a new turn."""
    bridge = AudioBridge(EventBus(), LatencyTracker(EventBus()), chunk_ms=40)
    bridge.on_model_audio(b"\x00\x01" * 24000)   # generation streaming
    bridge.force_drop("hard_stop")
    assert bridge._discarding
    t_first = bridge.last_interrupt_mono
    assert t_first > 0, "a force drop counts as a handled overlap"

    bridge.force_drop("hard_stop")               # watchdog re-fires
    assert bridge._discarding, "discard must be sticky until the turn boundary"

    bridge.on_model_audio(b"\x00\x01" * 24000)   # tail of the killed generation
    assert bridge.buffer.buffered_ms == 0, "tail must be discarded, not replayed"

    bridge.on_turn_complete()                     # boundary ends discard mode
    bridge.on_model_audio(b"\x00\x01" * 4800)
    assert bridge.buffer.buffered_ms > 0, "next generation plays normally"
