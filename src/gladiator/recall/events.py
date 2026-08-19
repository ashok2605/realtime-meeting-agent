"""Routes Recall realtime-websocket events into SpeechState / bridge / bus."""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from ..audio.bridge import AudioBridge
from ..core.events import EventBus
from ..core.turn_gate import SpeechState


class RecallEventRouter:
    def __init__(self, speech: SpeechState, bus: EventBus, bridge: AudioBridge,
                 on_human_speech_off: Callable[[], None] | None = None,
                 on_roster_change: Callable[[str, str], None] | None = None) -> None:
        self._speech = speech
        self._bus = bus
        self._bridge = bridge
        self._on_human_speech_off = on_human_speech_off  # hybrid-VAD hook
        self._on_roster_change = on_roster_change  # ("join"|"leave", name)
        self.roster: dict[str, str] = {}  # participant id -> name

    def handle(self, raw: dict[str, Any]) -> None:
        event = raw.get("event", "")
        payload = (raw.get("data") or {}).get("data") or {}
        participant = payload.get("participant") or {}
        pid = str(participant.get("id", ""))
        name = participant.get("name")

        if event == "participant_events.speech_on":
            self._speech.on_speech_on(pid, name)
            self._bus.emit("recall.speech_on", participant=name or pid)
        elif event == "participant_events.speech_off":
            self._speech.on_speech_off(pid, name)
            self._bus.emit("recall.speech_off", participant=name or pid)
            if (self._on_human_speech_off is not None
                    and not self._speech.any_human_speaking()):
                self._on_human_speech_off()
        elif event == "participant_events.join":
            self.roster[pid] = name or pid
            self._bus.emit("recall.participant_join", participant=name or pid)
            if self._on_roster_change is not None:
                self._on_roster_change("join", name or pid)
        elif event == "participant_events.leave":
            self.roster.pop(pid, None)
            self._speech.on_participant_leave(pid)
            self._bus.emit("recall.participant_leave", participant=name or pid)
            if self._on_roster_change is not None:
                self._on_roster_change("leave", name or pid)
        elif event == "audio_mixed_raw.data":
            # Fallback input path only (audio_source="recall_ws").
            buffer_b64 = payload.get("buffer")
            if buffer_b64:
                self._bridge.feed_frame(base64.b64decode(buffer_b64))
        else:
            self._bus.emit("recall.event", event=event)
