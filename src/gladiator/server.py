"""FastAPI assembly: routes only, logic lives in Runtime.

Everything here is reachable through the public tunnel, so every page and
websocket (except /healthz and static assets) requires the per-run session
token. The Gemini API key never leaves the server process.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .runtime import Runtime

WEB_DIR = Path(__file__).parent / "web"


def build_app(rt: Runtime) -> FastAPI:
    app = FastAPI(title="gladiator", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    def page(name: str) -> str:
        return (WEB_DIR / name / "index.html").read_text(encoding="utf-8")

    @app.get("/healthz")
    async def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/agent")
    async def agent(token: str | None = Query(None)) -> HTMLResponse:
        if not rt.check_token(token):
            return HTMLResponse("forbidden", status_code=403)
        return HTMLResponse(page("agent"))

    @app.get("/dashboard")
    async def dashboard(token: str | None = Query(None)) -> HTMLResponse:
        if not rt.check_token(token):
            return HTMLResponse("forbidden", status_code=403)
        return HTMLResponse(page("dashboard"))

    @app.get("/api/snapshot")
    async def snapshot(token: str | None = Query(None)) -> JSONResponse:
        if not rt.check_token(token):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(rt.snapshot())

    async def _ws_authed(ws: WebSocket) -> bool:
        if rt.check_token(ws.query_params.get("token")):
            return True
        await ws.close(code=4401)
        return False

    @app.websocket("/ws/agent")
    async def ws_agent(ws: WebSocket) -> None:
        if await _ws_authed(ws):
            await rt.handle_page_ws(ws)

    # Recall requires a trailing slash before query params; accept both forms.
    @app.websocket("/ws/recall")
    @app.websocket("/ws/recall/")
    async def ws_recall(ws: WebSocket) -> None:
        if await _ws_authed(ws):
            await rt.handle_recall_ws(ws)

    @app.websocket("/ws/dashboard")
    async def ws_dashboard(ws: WebSocket) -> None:
        if await _ws_authed(ws):
            await rt.handle_dashboard_ws(ws)

    return app
