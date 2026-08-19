"""Background tasks must survive uncaught exceptions: logged + restarted."""

import asyncio

from gladiator.config import Settings
from gladiator.runtime import Runtime


async def test_supervisor_logs_and_restarts(tmp_path):
    rt = Runtime(Settings(recall_api_key="x", gemini_api_key="x", runs_dir=tmp_path))
    calls = {"n": 0}
    recovered = asyncio.Event()

    async def flaky() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        recovered.set()
        await asyncio.sleep(3600)

    task = rt._supervise(flaky, "flaky")
    try:
        await asyncio.wait_for(recovered.wait(), timeout=5)  # 1s restart backoff
        assert calls["n"] == 2
        errors = [e for e in rt.bus.ring if e["type"] == "runtime.task_error"]
        assert errors and errors[0]["data"]["task"] == "flaky"
        assert errors[0]["data"]["kind"] == "RuntimeError"
    finally:
        task.cancel()
        rt.bus.close()


async def test_supervisor_lets_normal_completion_end_the_task(tmp_path):
    rt = Runtime(Settings(recall_api_key="x", gemini_api_key="x", runs_dir=tmp_path))

    async def finishes() -> None:
        return  # e.g. reanchor loop ending after the meeting

    task = rt._supervise(finishes, "finishes")
    await asyncio.wait_for(task, timeout=2)  # no restart loop
    assert not any(e["type"] == "runtime.task_error" for e in rt.bus.ring)
    rt.bus.close()
