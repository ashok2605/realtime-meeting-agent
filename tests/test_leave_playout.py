"""Regression: leave_meeting must not eject the bot mid-goodbye.

Found in the first live rehearsal — the fixed 3s grace was shorter than the
farewell, so Recall pulled the bot while it was still talking. The leave task
now waits for the goodbye audio to fully drain (bounded)."""

import asyncio
import time

from gladiator.config import Settings
from gladiator.runtime import Runtime


async def test_leave_waits_for_goodbye_playout(tmp_path):
    settings = Settings(recall_api_key="test", gemini_api_key="test", runs_dir=tmp_path)
    rt = Runtime(settings)
    rt.bot_id = "bot-1"
    left_at: list[float] = []

    async def fake_leave(bot_id: str) -> None:
        left_at.append(time.monotonic())
    rt.recall.leave = fake_leave

    # Goodbye audio is buffered (nothing draining it in this test).
    rt.bridge.on_model_audio(b"\x00\x01" * 24000)  # 1s
    rt._request_leave("test goodbye")

    await asyncio.sleep(0.8)
    assert not left_at, "must not leave while goodbye audio is still queued"

    rt.bridge.buffer.flush()  # simulate playout finished
    await asyncio.sleep(0.6)
    assert left_at, "must leave promptly once the goodbye has drained"


async def test_leave_never_hangs_without_goodbye(tmp_path):
    """If the model calls leave_meeting but produces no farewell audio at all,
    the bot still leaves after the start-wait bound (4s), not never."""
    settings = Settings(recall_api_key="test", gemini_api_key="test", runs_dir=tmp_path)
    rt = Runtime(settings)
    rt.bot_id = "bot-1"
    left = asyncio.Event()

    async def fake_leave(bot_id: str) -> None:
        left.set()
    rt.recall.leave = fake_leave

    rt._request_leave("no goodbye")
    await asyncio.wait_for(left.wait(), timeout=6)
