"""Audio bridge: meeting audio -> Gemini, Gemini audio -> meeting.

Invariant (tested): the Gemini receive loop must NEVER block on audio
delivery, because the same loop carries `interrupted` and tool calls. So:

  receive loop --(sync append)--> TurnAudioBuffer --(Pacer task)--> page

The buffer is unbounded server-side and flushable in O(1) via an epoch bump.
Only the page keeps a small (~160 ms) jitter buffer, so a barge-in cutoff is
near-instant: on `interrupted` we bump the epoch (server flush) and tell the
page to drop its queued samples.

Epoch semantics: the epoch is a turn/generation counter. It advances when a
new model turn starts and whenever we forcibly flush (interruption, stale
drop, hard stop). Chunks are stamped with their epoch; anything from an old
epoch is discarded by both the Pacer and the page.
"""

from __future__ import annotations

import array
import asyncio
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable

from ..core.events import EventBus
from ..core.latency import LatencyTracker
from ..core.turn_gate import DROP, FloorGate

OUT_SAMPLE_RATE = 24_000
OUT_BYTES_PER_MS = OUT_SAMPLE_RATE * 2 // 1000  # 48 bytes per ms (mono s16le)


class TurnAudioBuffer:
    """Epoch-stamped chunk queue between the Gemini receive loop and the Pacer."""

    def __init__(self, chunk_ms: int) -> None:
        self.epoch = 0
        self._chunk_bytes = chunk_ms * OUT_BYTES_PER_MS
        self._chunks: deque[bytes] = deque()
        self._pending = bytearray()
        self._data = asyncio.Event()

    def append(self, data: bytes) -> None:
        """Called synchronously from the receive loop; never blocks."""
        self._pending.extend(data)
        while len(self._pending) >= self._chunk_bytes:
            self._chunks.append(bytes(self._pending[: self._chunk_bytes]))
            del self._pending[: self._chunk_bytes]
        if self._chunks:
            self._data.set()

    def finish_turn(self) -> None:
        """Push any partial trailing chunk when a generation completes."""
        if self._pending:
            self._chunks.append(bytes(self._pending))
            self._pending.clear()
            self._data.set()

    def flush(self) -> float:
        """Drop everything and advance the epoch. Returns dropped audio in ms."""
        dropped = len(self._pending) + sum(len(c) for c in self._chunks)
        self._chunks.clear()
        self._pending.clear()
        self.epoch += 1
        self._data.set()  # wake the pacer so it observes the new epoch
        return dropped / OUT_BYTES_PER_MS

    def new_turn(self) -> int:
        """A fresh model generation is starting; give it its own epoch."""
        self._pending.clear()
        self.epoch += 1
        return self.epoch

    @property
    def buffered_ms(self) -> float:
        return (len(self._pending) + sum(len(c) for c in self._chunks)) / OUT_BYTES_PER_MS

    async def get(self) -> tuple[int, bytes | None]:
        """Next chunk with its epoch. Returns (epoch, None) on flush wakeups."""
        while not self._chunks:
            self._data.clear()
            await self._data.wait()
        try:
            return self.epoch, self._chunks.popleft()
        except IndexError:
            return self.epoch, None


class EnergyVAD:
    """RMS voice detector on the inbound meeting audio.

    This is the PRIMARY floor-gate signal: the first live rehearsal showed
    Recall's speech_on/off events are nearly absent in a single-participant
    Meet (one speech_on, zero speech_off in 4 minutes), while the meeting
    audio itself is always truthful — and verified to exclude the bot's own
    voice (RMS was zero during every agent turn). Also anchors latency
    measurement at the true last-voiced frame. Hangover keeps normal pauses
    from counting as speech-end.
    """

    def __init__(self, threshold: int = 500, hangover_ms: int = 300,
                 on_speech_end: Callable[[float], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._threshold = threshold
        self.hangover_s = hangover_ms / 1000
        self.on_speech_end = on_speech_end  # reassignable: runtime layers hooks on it
        self._clock = clock
        self.in_voice = False
        self.last_voice_mono: float | None = None
        self.voice_since: float | None = None  # start of the current utterance
        self.last_feed_mono: float | None = None  # None until the first frame arrives
        self.last_rms = 0

    def is_stale(self, max_age_s: float) -> bool:
        """True when the VAD can't be trusted: it has never seen a frame, or the
        audio feed stalled (page ws drop, capture failure). A frozen VAD must not
        hold authority — frozen 'quiet' opens the floor over talking humans,
        frozen 'voice' mutes the bot forever (review findings)."""
        return (self.last_feed_mono is None
                or self._clock() - self.last_feed_mono > max_age_s)

    def last_quiet_mono(self) -> float | None:
        """When the room last became quiet (voice end + hangover); None if speaking."""
        if self.in_voice or self.last_voice_mono is None:
            return None if self.in_voice else -1e9
        return self.last_voice_mono + self.hangover_s

    @staticmethod
    def _rms(pcm: bytes) -> int:
        # audioop is deprecated (removed in 3.13); ~320 samples/frame is cheap in pure python.
        if not pcm:
            return 0
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        return int(math.sqrt(sum(s * s for s in samples) / len(samples))) if samples else 0

    def feed(self, pcm: bytes) -> None:
        now = self._clock()
        self.last_feed_mono = now
        self.last_rms = self._rms(pcm)
        if self.last_rms >= self._threshold:
            if not self.in_voice:
                self.voice_since = now
            self.in_voice = True
            self.last_voice_mono = now
        elif self.in_voice and self.last_voice_mono is not None \
                and now - self.last_voice_mono >= self.hangover_s:
            self.in_voice = False
            self.voice_since = None
            if self.on_speech_end is not None:
                self.on_speech_end(self.last_voice_mono)


class AudioBridge:
    """Owns both directions of the audio path plus flush/discard bookkeeping."""

    def __init__(self, bus: EventBus, latency: LatencyTracker, *, chunk_ms: int) -> None:
        self._bus = bus
        self._latency = latency
        self.buffer = TurnAudioBuffer(chunk_ms)
        self.vad = EnergyVAD(on_speech_end=latency.mark_speech_end)
        self.muted = False
        self._turn_open = False
        # After a stale drop / hard stop the server may keep streaming the rest
        # of that generation; discard it until the turn boundary.
        self._discarding = False
        self._discarded_ms = 0.0
        self._inbound: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        self._inbound_dropped = 0
        self._frames_in = 0
        self.last_interrupt_mono: float = -1e9  # hard-stop watchdog reads this
        # Wired by the runtime:
        self.send_audio_to_gemini: Callable[[bytes], Awaitable[None]] | None = None
        self.notify_page_flush: Callable[[int], Awaitable[None]] | None = None
        self.on_state: Callable[[str], None] = lambda state: None
        # Called on stale/hard drops: the humans never heard that response, and
        # the model doesn't know — the runtime tells it once the floor is free.
        self.on_forced_drop: Callable[[str], None] | None = None

    @property
    def turn_open(self) -> bool:
        return self._turn_open

    # ---- inbound: meeting -> Gemini --------------------------------------
    def feed_frame(self, pcm: bytes) -> None:
        """Called by the page/Recall websocket handler for each ~20ms frame."""
        self._frames_in += 1
        self.vad.feed(pcm)
        if self.muted:
            return
        try:
            self._inbound.put_nowait(pcm)
        except asyncio.QueueFull:
            # Sender is behind (Gemini reconnect etc.) — drop oldest, keep newest.
            try:
                self._inbound.get_nowait()
                self._inbound.put_nowait(pcm)
            except asyncio.QueueEmpty:
                pass
            self._inbound_dropped += 1

    async def inbound_sender(self) -> None:
        """Forwards mic frames to Gemini; isolated so ws handlers never await sends."""
        while True:
            pcm = await self._inbound.get()
            if self.send_audio_to_gemini is not None:
                try:
                    await self.send_audio_to_gemini(pcm)
                except Exception as exc:  # session down mid-send; reconnect loop handles it
                    self._bus.emit("bridge.send_error", error=str(exc)[:200])
                    await asyncio.sleep(0.1)

    async def stats_reporter(self, interval_s: float = 5.0) -> None:
        while True:
            await asyncio.sleep(interval_s)
            self._bus.emit(
                "bridge.stats",
                frames_in=self._frames_in,
                inbound_dropped=self._inbound_dropped,
                discarded_ms=round(self._discarded_ms, 1),
                buffered_ms=round(self.buffer.buffered_ms, 1),
                rms=self.vad.last_rms,
                muted=self.muted,
            )

    # ---- outbound: Gemini -> page (called from the receive loop, sync) ----
    def on_model_audio(self, data: bytes) -> None:
        if self._discarding:
            self._discarded_ms += len(data) / OUT_BYTES_PER_MS
            return
        if not self._turn_open:
            self._turn_open = True
            epoch = self.buffer.new_turn()
            self._latency.mark_first_audio(epoch)
            self._bus.emit("turn.start", epoch=epoch)
            self.on_state("speaking")
        self.buffer.append(data)

    def on_generation_complete(self) -> None:
        self.buffer.finish_turn()

    def on_turn_complete(self, reason: str | None = None) -> None:
        self.buffer.finish_turn()
        if self._turn_open:
            self._bus.emit("turn.complete", epoch=self.buffer.epoch, reason=reason)
        self._turn_open = False
        self._discarding = False

    def on_interrupted(self) -> None:
        """Barge-in: Gemini cancelled the generation; kill our copies of it."""
        self.last_interrupt_mono = time.monotonic()
        epoch_before = self.buffer.epoch
        self._latency.mark_interrupted(epoch_before)
        dropped = self.buffer.flush()
        self._turn_open = False
        self._discarding = False  # server already cancelled; next audio is a new turn
        self._bus.emit("turn.interrupted", epoch=epoch_before, dropped_ms=round(dropped, 1))
        self._schedule_page_flush()
        self.on_state("listening")

    def force_drop(self, reason: str) -> None:
        """Stale drop / hard stop: discard this response, including its unsent tail."""
        # Counts as "overlap handled" for the watchdog, and discard must be
        # STICKY: a re-fired drop with _turn_open already False must not clear
        # it, or the killed generation's tail plays back as a fresh turn.
        self.last_interrupt_mono = time.monotonic()
        epoch_before = self.buffer.epoch
        dropped = self.buffer.flush()
        self._discarding = self._discarding or self._turn_open
        self._turn_open = False
        self._bus.emit("turn.force_drop", reason=reason, epoch=epoch_before,
                       dropped_ms=round(dropped, 1))
        self._schedule_page_flush()
        self.on_state("listening")
        if self.on_forced_drop is not None:
            self.on_forced_drop(reason)

    def _schedule_page_flush(self) -> None:
        if self.notify_page_flush is not None:
            asyncio.get_running_loop().create_task(self.notify_page_flush(self.buffer.epoch))

    def clear_speech_anchor(self) -> None:
        """A text nudge (not user speech) is about to cause the next model turn:
        the latency tracker must not attribute it to stale speech."""
        self._latency.clear_anchor()


class Pacer:
    """Feeds the page at realtime rate, consulting the FloorGate per chunk.

    Keeps at most ~page_buffer_target_ms of audio ahead of realtime on the
    page (prime burst against jitter, small enough that barge-in is instant).
    """

    def __init__(self, bridge: AudioBridge, gate: FloorGate, bus: EventBus,
                 latency: LatencyTracker, *,
                 chunk_ms: int, prime_ms: int,
                 send: Callable[[int, int, bytes], Awaitable[bool]],
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._bridge = bridge
        self._gate = gate
        self._bus = bus
        self._latency = latency
        self._chunk_ms = chunk_ms
        self._prime_ms = prime_ms
        self._send = send
        self._clock = clock
        self._sleep = sleep
        self._seq = 0
        self._last_send_mono: float = -1e9
        self._epoch_started: int | None = None
        self._turn_t0: float = 0.0
        self._sent_ms: float = 0.0

    @property
    def agent_playing(self) -> bool:
        """True if we shipped audio recently enough that the page is likely playing it."""
        return (self._clock() - self._last_send_mono) * 1000 < self._prime_ms + 3 * self._chunk_ms

    async def run(self) -> None:
        buffer = self._bridge.buffer
        while True:
            epoch, chunk = await buffer.get()
            if chunk is None or epoch != buffer.epoch:
                continue  # flushed while queued

            decision = await self._gate.wait_for_floor()
            if epoch != buffer.epoch:
                continue  # flushed (e.g. interrupted) while we waited on the gate
            if decision == DROP:
                self._bridge.force_drop("gate_stale")
                continue

            if self._epoch_started != epoch:
                self._epoch_started = epoch
                self._turn_t0 = self._clock()
                self._sent_ms = 0.0
                self._latency.mark_first_send(epoch, gate_hold_ms=self._gate.last_hold_ms)

            self._seq += 1
            delivered = await self._send(epoch, self._seq, chunk)
            if not delivered:
                # No page connected: drain at realtime pace so state stays sane.
                self._bus.emit("pacer.no_page", epoch=epoch) if self._seq % 50 == 1 else None
            self._last_send_mono = self._clock()
            self._sent_ms += len(chunk) / OUT_BYTES_PER_MS

            # Realtime pacing with a bounded head start (the page's jitter buffer).
            ahead_ms = self._sent_ms - (self._clock() - self._turn_t0) * 1000
            if ahead_ms > self._prime_ms:
                await self._sleep((ahead_ms - self._prime_ms) / 1000)
