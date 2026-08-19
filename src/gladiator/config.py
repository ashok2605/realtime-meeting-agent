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
    public_base_url: str = ""  # tunnel https base, no trailing slash

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 8000

    # --- identity / model ---
    bot_name: str = "Gladiator"
    voice_name: str = "Kore"
    # pinned: 2.5 native-audio has an open truncation bug (python-genai#2117)
    gemini_model: str = "gemini-3.1-flash-live-preview"

    # --- audio path ---
    audio_source: str = "page"        # "page" (~20ms frames) | "recall_ws" (200ms fallback)
    outbound_chunk_ms: int = 40       # pacer cadence toward the page
    page_buffer_target_ms: int = 160  # page-side jitter buffer the pacer keeps primed

    # --- Gemini VAD (automatic activity detection) ---
    vad_silence_duration_ms: int = 700  # docs recommend 500-800; high = never split humans
    vad_prefix_padding_ms: int = 300    # 0 clips speech onset
    hybrid_vad: bool = False  # audio_stream_end on Recall speech_off; latency knob, ships OFF

    # --- floor gate (agent must never interrupt a human; DESIGN.md §4) ---
    vad_gate: bool = True           # gate on meeting-audio VAD (page path only)
    gate_grace_ms: int = 300        # silence required before agent may start speaking
    gate_stale_drop_ms: int = 2500  # gate held longer than this -> drop buffered answer
    gate_max_hold_ms: int = 20000   # stuck speech_on safety valve
    gate_hard_stop_ms: int = 400    # sustained human speech over agent audio -> force flush

    # --- context management (sizing rationale: DESIGN.md §6) ---
    compression_trigger_tokens: int = 100_000
    compression_target_tokens: int = 64_000
    reanchor_interval_s: int = 480  # periodic discovery-state note; 0 disables

    # --- tools ---
    search_mode: str = "native"  # "native" google_search | "function" fallback (DESIGN.md §7)

    # --- bookkeeping ---
    runs_dir: Path = Path("runs")
    # auth token for everything the tunnel exposes (page, websockets, dashboard)
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
