"""Recall.ai REST client (region-scoped)."""

from __future__ import annotations

from typing import Any

import httpx

from ..config import Settings
from ..core.events import EventBus


class RecallClient:
    def __init__(self, settings: Settings, bus: EventBus) -> None:
        self._settings = settings
        self._bus = bus
        self._http = httpx.AsyncClient(
            base_url=settings.recall_base,
            headers={"Authorization": f"Token {settings.recall_api_key}"},
            timeout=30.0,
        )

    async def create_bot(self, meeting_url: str) -> dict[str, Any]:
        s = self._settings
        token = s.session_token
        events = [
            "participant_events.join",
            "participant_events.leave",
            "participant_events.speech_on",
            "participant_events.speech_off",
        ]
        recording_config: dict[str, Any] = {}
        if s.audio_source == "recall_ws":
            # Fallback input path: mixed meeting audio over Recall's websocket
            # (16 kHz mono s16le, 200 ms chunks — a known latency floor).
            recording_config["audio_mixed_raw"] = {}
            events.append("audio_mixed_raw.data")
        recording_config["realtime_endpoints"] = [{
            "type": "websocket",
            # Recall requires a trailing slash before query params (400 otherwise).
            "url": f"{s.public_ws_base}/ws/recall/?token={token}",
            "events": events,
        }]
        payload: dict[str, Any] = {
            "meeting_url": meeting_url,
            "bot_name": s.bot_name,
            # Output Media needs CPU: the default 250-millicore bot produces
            # choppy audio (Recall FAQ). web_4_core matches their voice-agent demo.
            "variant": {"google_meet": "web_4_core"},
            "output_media": {
                "camera": {
                    "kind": "webpage",
                    "config": {"url": f"{s.public_base_url}/agent?token={token}"},
                }
            },
            "recording_config": recording_config,
            "automatic_leave": {
                "waiting_room_timeout": 600,           # gmeet max
                "everyone_left_timeout": {"timeout": 60},
            },
        }
        resp = await self._http.post("/bot/", json=payload)
        if resp.status_code >= 400:
            self._bus.emit("recall.create_bot_error", status=resp.status_code,
                           body=resp.text[:800])
            resp.raise_for_status()
        bot = resp.json()
        self._bus.emit("recall.bot_created", bot_id=bot.get("id"), meeting_url=meeting_url)
        return bot

    async def get_bot(self, bot_id: str) -> dict[str, Any]:
        resp = await self._http.get(f"/bot/{bot_id}/")
        resp.raise_for_status()
        return resp.json()

    async def leave(self, bot_id: str) -> None:
        resp = await self._http.post(f"/bot/{bot_id}/leave_call/")
        self._bus.emit("recall.leave_requested", bot_id=bot_id, status=resp.status_code)

    async def close(self) -> None:
        await self._http.aclose()
