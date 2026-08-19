"""Per-turn latency instrumentation.

A "turn" is one agent response, keyed by the outbound audio epoch. The
waterfall decomposes the user-perceived wait (human stops talking -> agent
audible in the meeting) into:

    detect+think : human speech end        -> first model audio byte received
                   (Gemini's VAD silence window + model generation)
    serve        : first model audio byte  -> first chunk sent to the page
                   (floor-gate hold + pacer scheduling; usually ~0)
    transit+play : first chunk sent        -> page schedules it on its AudioContext
                   (tunnel network + page jitter buffer)

Page timestamps arrive in the page's clock; ClockSync estimates the offset via
NTP-style pings so all marks land on the server's monotonic timeline.
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .events import EventBus

SEGMENTS = ("detect_think", "serve", "transit_play", "e2e")


class ClockSync:
    """Rolling estimate of (page clock - server clock) in ms, from ping samples."""

    def __init__(self, window: int = 9) -> None:
        self._offsets: deque[float] = deque(maxlen=window)
        self._rtts: deque[float] = deque(maxlen=window)

    def update(self, offset_ms: float, rtt_ms: float) -> None:
        self._offsets.append(offset_ms)
        self._rtts.append(rtt_ms)

    @property
    def offset_ms(self) -> float | None:
        return statistics.median(self._offsets) if self._offsets else None

    @property
    def rtt_ms(self) -> float | None:
        return statistics.median(self._rtts) if self._rtts else None

    def page_ms_to_server_mono(self, page_ts_ms: float) -> float | None:
        """Convert a page performance-now-epoch timestamp to server monotonic seconds."""
        if self.offset_ms is None:
            return None
        # Page sends timestamps as (its wall ms). offset = page_wall - server_wall.
        server_wall_ms = page_ts_ms - self.offset_ms
        # Convert wall to our monotonic using the current wall<->mono anchor.
        return time.monotonic() - (time.time() * 1000 - server_wall_ms) / 1000


@dataclass
class Turn:
    epoch: int
    speech_end_mono: float | None = None
    first_audio_mono: float | None = None
    first_send_mono: float | None = None
    page_play_mono: float | None = None
    gate_hold_ms: float = 0.0
    interrupted: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


class LatencyTracker:
    def __init__(self, bus: EventBus, clock=time.monotonic, window: int = 50) -> None:
        self._bus = bus
        self._clock = clock
        self.clock_sync = ClockSync()
        self._turns: dict[int, Turn] = {}
        self._last_speech_end: float | None = None
        self._history: dict[str, deque[float]] = {s: deque(maxlen=window) for s in SEGMENTS}
        self._completed = 0

    # --- marks -----------------------------------------------------------
    def mark_speech_end(self, mono: float | None = None) -> None:
        self._last_speech_end = mono if mono is not None else self._clock()

    def clear_anchor(self) -> None:
        """A text nudge is causing the next turn — it must not inherit speech timing."""
        self._last_speech_end = None

    def mark_first_audio(self, epoch: int) -> None:
        turn = self._turns.setdefault(epoch, Turn(epoch=epoch))
        if turn.first_audio_mono is None:
            turn.first_audio_mono = self._clock()
            # consume-once: one utterance anchors at most one turn
            turn.speech_end_mono = self._last_speech_end
            self._last_speech_end = None

    def mark_first_send(self, epoch: int, gate_hold_ms: float = 0.0) -> None:
        turn = self._turns.setdefault(epoch, Turn(epoch=epoch))
        if turn.first_send_mono is None:
            turn.first_send_mono = self._clock()
            turn.gate_hold_ms = gate_hold_ms

    def mark_page_play(self, epoch: int, page_ts_ms: float) -> None:
        turn = self._turns.get(epoch)
        if turn is None or turn.page_play_mono is not None:
            return
        mono = self.clock_sync.page_ms_to_server_mono(page_ts_ms)
        turn.page_play_mono = mono if mono is not None else self._clock()
        self._finalize(turn)

    def mark_interrupted(self, epoch: int) -> None:
        turn = self._turns.get(epoch)
        if turn is not None:
            turn.interrupted = True

    # --- assembly --------------------------------------------------------
    def _finalize(self, turn: Turn) -> None:
        def ms(a: float | None, b: float | None) -> float | None:
            return round((b - a) * 1000, 1) if a is not None and b is not None else None

        waterfall = {
            "epoch": turn.epoch,
            "detect_think": ms(turn.speech_end_mono, turn.first_audio_mono),
            "serve": ms(turn.first_audio_mono, turn.first_send_mono),
            "transit_play": ms(turn.first_send_mono, turn.page_play_mono),
            "e2e": ms(turn.speech_end_mono, turn.page_play_mono),
            "gate_hold_ms": round(turn.gate_hold_ms, 1),
            "interrupted": turn.interrupted,
            "clock_rtt_ms": self.clock_sync.rtt_ms,
            **turn.extras,
        }
        for seg in SEGMENTS:
            if waterfall.get(seg) is not None:
                self._history[seg].append(waterfall[seg])
        self._completed += 1
        self._bus.emit("latency.turn", **waterfall)
        self._turns.pop(turn.epoch, None)

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"turns": self._completed}
        for seg, values in self._history.items():
            if values:
                ordered = sorted(values)
                out[seg] = {
                    "p50": round(ordered[len(ordered) // 2], 1),
                    "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
                    "last": round(values[-1], 1),
                }
        return out
