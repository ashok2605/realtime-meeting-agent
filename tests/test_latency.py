"""LatencyTracker waterfall assembly and pacing behavior of the Pacer."""

import asyncio

from gladiator.audio.bridge import OUT_BYTES_PER_MS, AudioBridge, Pacer
from gladiator.core.events import EventBus
from gladiator.core.latency import LatencyTracker
from gladiator.core.turn_gate import FloorGate, SpeechState


def test_waterfall_segments_add_up():
    t = {"now": 100.0}
    bus = EventBus()
    tracker = LatencyTracker(bus, clock=lambda: t["now"])
    tracker.clock_sync.update(offset_ms=0.0, rtt_ms=10.0)

    tracker.mark_speech_end(100.0)
    t["now"] = 100.9
    tracker.mark_first_audio(epoch=3)          # 900ms detect+think
    t["now"] = 100.95
    tracker.mark_first_send(epoch=3, gate_hold_ms=30.0)   # 50ms serve
    t["now"] = 101.0
    # page play arrives via wall-clock conversion; offset 0 makes it ~now
    import time
    tracker.mark_page_play(epoch=3, page_ts_ms=time.time() * 1000)

    turns = [e for e in bus.ring if e["type"] == "latency.turn"]
    assert len(turns) == 1
    d = turns[0]["data"]
    assert d["detect_think"] == 900.0
    assert d["serve"] == 50.0
    assert d["gate_hold_ms"] == 30.0
    assert d["epoch"] == 3
    stats = tracker.stats()
    assert stats["turns"] == 1 and stats["detect_think"]["p50"] == 900.0


async def test_pacer_keeps_bounded_head_start():
    """The pacer must not dump a whole response at once: at most prime_ms ahead."""
    bus = EventBus()
    latency = LatencyTracker(bus)
    speech = SpeechState(bot_name="Gladiator")
    gate = FloorGate(speech, bus, grace_ms=0, stale_drop_ms=10_000, max_hold_ms=10_000)
    bridge = AudioBridge(bus, latency, chunk_ms=40)

    sent: list[tuple[float, int]] = []
    loop = asyncio.get_running_loop()

    async def send(epoch: int, seq: int, pcm: bytes) -> bool:
        sent.append((loop.time(), len(pcm)))
        return True

    pacer = Pacer(bridge, gate, bus, latency, chunk_ms=40, prime_ms=160, send=send)
    task = asyncio.create_task(pacer.run())
    try:
        bridge.on_model_audio(b"\x00\x01" * (24000 * 2))  # 2s of audio, arrives at once
        await asyncio.sleep(0.5)
        sent_ms = sum(n for _, n in sent) / OUT_BYTES_PER_MS
        # after 500ms wall time we may have sent at most ~500 + prime + one chunk
        assert sent_ms <= 500 + 160 + 80, f"pacer dumped {sent_ms}ms in 500ms wall"
        assert sent_ms >= 400, "pacer must keep up with realtime"
    finally:
        task.cancel()


async def test_pacer_discards_stale_epoch_chunks():
    bus = EventBus()
    latency = LatencyTracker(bus)
    speech = SpeechState(bot_name="Gladiator")
    gate = FloorGate(speech, bus, grace_ms=0, stale_drop_ms=10_000, max_hold_ms=10_000)
    bridge = AudioBridge(bus, latency, chunk_ms=40)
    sent: list[int] = []

    async def send(epoch: int, seq: int, pcm: bytes) -> bool:
        sent.append(epoch)
        await asyncio.sleep(0.01)
        return True

    pacer = Pacer(bridge, gate, bus, latency, chunk_ms=40, prime_ms=40, send=send)
    task = asyncio.create_task(pacer.run())
    try:
        bridge.on_model_audio(b"\x00\x01" * 24000)  # 1s
        await asyncio.sleep(0.12)
        bridge.on_interrupted()                      # flush mid-play
        bridge.on_model_audio(b"\x00\x01" * 4800)   # new turn, 200ms
        bridge.on_generation_complete()
        await asyncio.sleep(0.4)
        epochs = set(sent)
        assert len(epochs) == 2
        old, new = min(epochs), max(epochs)
        # nothing from the old epoch may be sent after the new epoch started
        first_new = sent.index(new)
        assert old not in sent[first_new:], "stale chunks leaked after flush"
    finally:
        task.cancel()


def test_anchor_consumed_once_and_clearable():
    """One utterance anchors at most one turn; nudges clear the anchor."""
    t = {"now": 100.0}
    bus = EventBus()
    tracker = LatencyTracker(bus, clock=lambda: t["now"])

    tracker.mark_speech_end(100.0)
    t["now"] = 130.0                    # a genuinely slow (30s) speech-caused turn
    tracker.mark_first_audio(epoch=1)
    assert tracker._turns[1].speech_end_mono == 100.0, "slow turns keep their anchor"

    t["now"] = 131.0                    # second turn, no new speech
    tracker.mark_first_audio(epoch=2)
    assert tracker._turns[2].speech_end_mono is None, "anchor is consume-once"

    tracker.mark_speech_end(131.5)
    tracker.clear_anchor()              # a nudge went out before the model spoke
    t["now"] = 133.0
    tracker.mark_first_audio(epoch=3)
    assert tracker._turns[3].speech_end_mono is None, "nudge turns carry no anchor"
