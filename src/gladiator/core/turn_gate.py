"""Turn-taking floor control: the agent must never talk over a human.

Signals: a live EnergyVAD on the meeting audio (primary) and Recall's
speech_on/off events (attribution; gating fallback when the VAD is stale or
absent). Why this split — and its live-tested failure modes — is DESIGN.md §4.

The Pacer consults FloorGate per audio chunk: OPEN after grace_ms of silence;
waits while a human speaks; DROP once held past stale_drop_ms (an out-of-date
answer must not play when the human stops). Stuck speech_on entries are pruned
after max_hold_ms.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .events import EventBus

OPEN = "open"
DROP = "drop"


class VoiceSignal(Protocol):
    """Duck type for EnergyVAD (kept abstract to avoid an import cycle)."""

    in_voice: bool

    def last_quiet_mono(self) -> float | None: ...

    def is_stale(self, max_age_s: float) -> bool: ...


@dataclass
class Speaker:
    id: str
    name: str
    since_mono: float


class SpeechState:
    def __init__(self, bot_name: str, clock: Callable[[], float] = time.monotonic) -> None:
        self._bot_name = bot_name.strip().lower()
        self._clock = clock
        self.speaking: dict[str, Speaker] = {}
        self.last_speech_off_mono: float = -1e9
        self.changed = asyncio.Event()

    def _is_self(self, name: str | None) -> bool:
        # The bot is a meeting participant too; never gate on our own voice.
        return bool(name) and name.strip().lower() == self._bot_name

    def on_speech_on(self, participant_id: str, name: str | None) -> None:
        if self._is_self(name):
            return
        self.speaking[participant_id] = Speaker(
            id=participant_id, name=name or "unknown", since_mono=self._clock())
        self._wake()

    def on_speech_off(self, participant_id: str, name: str | None) -> None:
        if self._is_self(name):
            return
        if participant_id in self.speaking:
            del self.speaking[participant_id]
        self.last_speech_off_mono = self._clock()
        self._wake()

    def on_participant_leave(self, participant_id: str) -> None:
        # A speaker who leaves mid-speech will never send speech_off.
        if participant_id in self.speaking:
            del self.speaking[participant_id]
            self.last_speech_off_mono = self._clock()
            self._wake()

    def prune_stuck(self, max_hold_s: float) -> list[str]:
        """Drop speakers 'speaking' for longer than max_hold_s (lost speech_off)."""
        now = self._clock()
        stuck = [pid for pid, s in self.speaking.items() if now - s.since_mono > max_hold_s]
        for pid in stuck:
            del self.speaking[pid]
        if stuck:
            self.last_speech_off_mono = now
            self._wake()
        return stuck

    def any_human_speaking(self) -> bool:
        return bool(self.speaking)

    def _wake(self) -> None:
        self.changed.set()

    def snapshot(self) -> dict:
        return {"speaking": [s.name for s in self.speaking.values()]}


class FloorGate:
    def __init__(self, speech: SpeechState, bus: EventBus, *,
                 grace_ms: int, stale_drop_ms: int, max_hold_ms: int,
                 vad: VoiceSignal | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._speech = speech
        self._vad = vad
        self._bus = bus
        self._grace_s = grace_ms / 1000
        self._stale_drop_s = stale_drop_ms / 1000
        self._max_hold_s = max_hold_ms / 1000
        self._clock = clock
        self.last_hold_ms: float = 0.0

    # no frames for this long -> VAD untrustworthy, events gate instead
    VAD_STALE_AFTER_S = 1.5

    def _live_vad(self) -> VoiceSignal | None:
        if self._vad is not None and not self._vad.is_stale(self.VAD_STALE_AFTER_S):
            return self._vad
        return None

    def _vad_speaking(self) -> bool:
        vad = self._live_vad()
        return vad is not None and vad.in_voice

    def _silence_duration(self) -> float:
        vad = self._live_vad()
        if vad is not None:
            quiet = vad.last_quiet_mono()
            return 0.0 if quiet is None else self._clock() - quiet
        return self._clock() - self._speech.last_speech_off_mono

    def is_open(self) -> bool:
        """A live VAD is authoritative (events are attribution-only then);
        when the VAD is stale or absent, events gate. Rationale: DESIGN.md §4."""
        vad = self._live_vad()
        if vad is not None:
            return not vad.in_voice and self._silence_duration() >= self._grace_s
        return (not self._speech.any_human_speaking()
                and self._silence_duration() >= self._grace_s)

    async def wait_for_floor(self) -> str:
        """Block until the agent may speak. Returns OPEN, or DROP if the wait
        exceeded stale_drop (the buffered response should be discarded)."""
        start = self._clock()
        held_logged = False
        while True:
            stuck = self._speech.prune_stuck(self._max_hold_s)
            if stuck:
                self._bus.emit("gate.stuck_speaker_pruned", participants=stuck)

            if self.is_open():
                self.last_hold_ms = (self._clock() - start) * 1000
                if held_logged:
                    self._bus.emit("gate.release", held_ms=round(self.last_hold_ms, 1))
                return OPEN

            held = self._clock() - start
            if held >= self._stale_drop_s:
                self.last_hold_ms = held * 1000
                self._bus.emit("gate.stale_drop", held_ms=round(self.last_hold_ms, 1),
                               speaking=self._speech.snapshot()["speaking"],
                               vad_voice=self._vad_speaking())
                return DROP

            if not held_logged:
                self._bus.emit("gate.hold", speaking=self._speech.snapshot()["speaking"],
                               vad_voice=self._vad_speaking())
                held_logged = True

            # Wake on speech-state change, or poll to re-check grace/stale deadlines.
            self._speech.changed.clear()
            timeout = min(0.05, max(0.005, self._stale_drop_s - held))
            try:
                await asyncio.wait_for(self._speech.changed.wait(), timeout=timeout)
            except TimeoutError:
                pass
