"""Central configuration. Everything tunable lives here; secrets come from .env."""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- secrets / endpoints ---
    recall_api_key: str = ""
    recall_region: str = "us-west-2"
    gemini_api_key: str = ""
    # Public https base URL of the tunnel (no trailing slash). Tunnel-agnostic:
    # anything that forwards https/wss to this server works (ngrok, cloudflared, ...).
    public_base_url: str = ""

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 8000

    # --- identity / model ---
    bot_name: str = "Gladiator"
    voice_name: str = "Kore"
    # Pinned: 2.5 native-audio line has an open truncation bug that worsens with
    # tool calls + context compression (googleapis/python-genai#2117).
    gemini_model: str = "gemini-3.1-flash-live-preview"

    # --- audio path ---
    # "page": capture meeting audio inside the Output Media page (~20ms frames).
    # "recall_ws": fallback via Recall's audio_mixed_raw websocket (200ms chunks).
    audio_source: str = "page"
    inbound_frame_ms: int = 20
    outbound_chunk_ms: int = 40      # pacer cadence toward the page
    page_buffer_target_ms: int = 160  # page-side jitter buffer the pacer keeps primed

    # --- Gemini VAD (automatic activity detection) ---
    vad_silence_duration_ms: int = 700   # docs recommend 500-800; high = never split humans
    vad_prefix_padding_ms: int = 300     # 0 clips speech onset
    # Hybrid VAD: on Recall speech_off, send audio_stream_end to skip the server's
    # silence wait. Ships OFF; implemented as a measurable latency knob, not tuned.
    hybrid_vad: bool = False

    # --- floor gate (agent must never interrupt a human) ---
    # Gate on the EnergyVAD of the live meeting audio (primary signal; Recall's
    # speech events barely fire in small meetings). Disable only if a room's
    # speakers leak agent audio back into the meeting mic.
    vad_gate: bool = True
    gate_grace_ms: int = 300        # silence required before agent may start speaking
    gate_stale_drop_ms: int = 2500  # gate held longer than this -> drop buffered answer
    gate_max_hold_ms: int = 20000   # stuck speech_on safety valve
    gate_hard_stop_ms: int = 400    # sustained human speech over agent audio -> force flush

    # --- tools ---
    # "native": google_search inside the Live session (verified working on this key).
    # "function": own web_search declaration backed by a grounded generateContent call.
    search_mode: str = "native"

    # --- bookkeeping ---
    runs_dir: Path = Path("runs")
    # Auth token for everything the tunnel exposes (page, websockets, dashboard).
    session_token: str = Field(default_factory=lambda: secrets.token_urlsafe(16))

    @property
    def recall_base(self) -> str:
        return f"https://{self.recall_region}.recall.ai/api/v1"

    @property
    def public_ws_base(self) -> str:
        return self.public_base_url.replace("https://", "wss://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
