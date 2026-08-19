"""Tool declarations and dispatch for the Live session.

Contract (user-mandated): tool handlers only mutate local state or schedule
background work — they must return to the receive loop immediately. Anything
slow (Recall REST calls, grounded search) runs as an asyncio task; for
web_search the tool response itself is deferred and sent when ready.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from google.genai import types

from ..core.discovery import PHASES, DiscoveryTracker
from ..core.events import EventBus


def build_declarations(tracker: DiscoveryTracker, search_as_function: bool) -> list[dict]:
    qids = list(tracker.questions)
    decls = [
        {
            "name": "record_answer",
            "description": (
                "Record a participant's answer to a discovery question on the live dashboard. "
                "Invocation condition: a participant just provided information answering any "
                "preset question, fully or partially, including revisions to earlier answers."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "question_id": {"type": "STRING", "enum": qids},
                    "answer_summary": {"type": "STRING",
                                       "description": "Faithful 1-2 sentence summary of the answer."},
                    "verbatim_quote": {"type": "STRING",
                                       "description": "Short direct quote, if a crisp one exists."},
                    "confidence": {"type": "STRING", "enum": ["low", "medium", "high"],
                                   "description": "How clearly the answer addressed the question."},
                    "complete": {"type": "BOOLEAN",
                                 "description": "false if the answer was partial and needs follow-up."},
                },
                "required": ["question_id", "answer_summary"],
            },
        },
        {
            "name": "mark_question_asked",
            "description": ("Mark a preset question as asked. Invocation condition: you just "
                            "asked one of the preset discovery questions."),
            "parameters": {
                "type": "OBJECT",
                "properties": {"question_id": {"type": "STRING", "enum": qids}},
                "required": ["question_id"],
            },
        },
        {
            "name": "mark_question_skipped",
            "description": ("Mark a question skipped. Invocation condition: a participant "
                            "declined to answer or asked to move on."),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "question_id": {"type": "STRING", "enum": qids},
                    "reason": {"type": "STRING"},
                },
                "required": ["question_id"],
            },
        },
        {
            "name": "set_meeting_phase",
            "description": "Update the meeting phase shown on the dashboard.",
            "parameters": {
                "type": "OBJECT",
                "properties": {"phase": {"type": "STRING", "enum": list(PHASES)}},
                "required": ["phase"],
            },
        },
        {
            "name": "leave_meeting",
            "description": ("Make the bot leave the meeting. Invocation condition: a participant "
                            "clearly asked you to leave, or the wrap-up is complete and you have "
                            "said goodbye."),
            "parameters": {
                "type": "OBJECT",
                "properties": {"reason": {"type": "STRING"}},
                "required": ["reason"],
            },
        },
    ]
    if search_as_function:
        decls.append({
            "name": "web_search",
            "description": ("Search the web and return a grounded answer with sources. "
                            "Invocation condition: a participant asks for facts you don't know."),
            "parameters": {
                "type": "OBJECT",
                "properties": {"query": {"type": "STRING"}},
                "required": ["query"],
            },
        })
    return decls


class ToolDispatcher:
    def __init__(self, tracker: DiscoveryTracker, bus: EventBus, *,
                 request_leave: Callable[[str], None],
                 web_search: Callable[[str], Awaitable[dict[str, Any]]] | None = None) -> None:
        self._tracker = tracker
        self._bus = bus
        self._request_leave = request_leave  # schedules the Recall REST call, returns instantly
        self._web_search = web_search
        self._cancelled_ids: set[str] = set()

    def handle(self, fc: types.FunctionCall) -> dict[str, Any] | None:
        """Returns the immediate response dict, or None if the response will be
        sent later by a background task (deferred tools like web_search)."""
        args = dict(fc.args or {})
        self._bus.emit("tool.call", id=fc.id, name=fc.name, args=args)
        try:
            result = self._dispatch(fc.name or "", args, fc.id or "")
        except Exception as exc:  # a tool bug must never kill the receive loop
            result = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
        if result is not None:
            self._bus.emit("tool.result", id=fc.id, name=fc.name, result=result)
        return result

    def _dispatch(self, name: str, args: dict[str, Any], call_id: str) -> dict[str, Any] | None:
        if name == "record_answer":
            return self._tracker.record_answer(
                question_id=args.get("question_id", ""),
                answer_summary=args.get("answer_summary", ""),
                verbatim_quote=args.get("verbatim_quote", ""),
                confidence=args.get("confidence", "medium"),
                complete=bool(args.get("complete", True)),
            )
        if name == "mark_question_asked":
            return self._tracker.mark_asked(args.get("question_id", ""))
        if name == "mark_question_skipped":
            return self._tracker.mark_skipped(args.get("question_id", ""),
                                              args.get("reason", ""))
        if name == "set_meeting_phase":
            return self._tracker.set_phase(args.get("phase", ""))
        if name == "leave_meeting":
            # Respond to Gemini immediately; the REST call runs in the background
            # so the model can finish saying goodbye while the bot exits.
            self._request_leave(args.get("reason", "requested"))
            return {"status": "ok", "detail": "leaving the meeting now"}
        if name == "web_search" and self._web_search is not None:
            return None  # deferred: session layer awaits the coroutine in a task
        return {"status": "error", "detail": f"unknown tool '{name}'"}

    async def run_deferred(self, fc: types.FunctionCall) -> dict[str, Any]:
        args = dict(fc.args or {})
        if fc.name == "web_search" and self._web_search is not None:
            result = await self._web_search(args.get("query", ""))
        else:
            result = {"status": "error", "detail": f"no deferred handler for '{fc.name}'"}
        self._bus.emit("tool.result", id=fc.id, name=fc.name, result=result, deferred=True)
        return result

    def cancel(self, ids: list[str]) -> None:
        self._cancelled_ids.update(ids)
        self._bus.emit("tool.cancelled", ids=ids)

    def is_cancelled(self, call_id: str | None) -> bool:
        return call_id in self._cancelled_ids
