"""Discovery session state: the preset questions and their tool-call-driven answers.

Theme: Salesforce implementation discovery for an automotive company.
All mutations happen through tool calls from Gemini Live (record_answer etc.)
or dashboard controls; every change emits an event and keeps a revision
history so re-answers are visible, idempotent updates.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .events import EventBus

PHASES = ("intro", "discovery", "wrapup", "done")


@dataclass
class Question:
    id: str
    label: str
    text: str
    status: str = "pending"  # pending | asked | partial | answered | skipped
    answer_summary: str = ""
    verbatim_quote: str = ""
    confidence: str = ""
    updated_at: float | None = None
    history: list[dict[str, Any]] = field(default_factory=list)


QUESTIONS: list[Question] = [
    Question("current-systems", "Current systems",
             "What CRM and dealer-management systems are you using today, and what is Salesforce replacing?"),
    Question("users-roles", "Users & roles",
             "How many users will be on Salesforce at go-live, and which teams or roles are they?"),
    Question("sales-process", "Sales process",
             "Walk me through your lead-to-close sales process — retail and fleet, if they differ."),
    Question("integrations", "Integrations",
             "Which systems must Salesforce integrate with — DMS, ERP, marketing, telephony, anything else?"),
    Question("data-migration", "Data migration",
             "What data needs to migrate — objects, rough volumes, quality concerns — and who owns it today?"),
    Question("customization", "Customization",
             "Do you expect custom objects or workflows beyond standard Sales Cloud, for example vehicle inventory or test-drive scheduling?"),
    Question("compliance", "Compliance",
             "Are there data-residency or compliance constraints we should design for, like GDPR or regional hosting?"),
    Question("timeline", "Timeline & success",
             "What is the target go-live date, and how will you measure whether the implementation succeeded?"),
]


class DiscoveryTracker:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self.questions: dict[str, Question] = {}
        for q in QUESTIONS:
            # Fresh copies so tests / multiple sessions don't share state.
            self.questions[q.id] = Question(id=q.id, label=q.label, text=q.text)
        self.phase = "intro"

    # --- tool-call entry points (must stay fast: local mutation only) ------
    def record_answer(self, question_id: str, answer_summary: str,
                      verbatim_quote: str = "", confidence: str = "medium",
                      complete: bool = True) -> dict[str, Any]:
        q = self.questions.get(question_id)
        if q is None:
            self._bus.emit("discovery.unknown_question", question_id=question_id)
            return {"status": "error", "detail": f"unknown question_id '{question_id}'",
                    "valid_ids": list(self.questions)}
        revision = {
            "answer_summary": answer_summary,
            "verbatim_quote": verbatim_quote,
            "confidence": confidence,
            "complete": complete,
            "at": time.time(),
        }
        q.history.append(revision)
        q.answer_summary = answer_summary
        q.verbatim_quote = verbatim_quote
        q.confidence = confidence
        q.status = "answered" if complete else "partial"
        q.updated_at = revision["at"]
        self._bus.emit("discovery.answer", question_id=question_id, status=q.status,
                       answer_summary=answer_summary, verbatim_quote=verbatim_quote,
                       confidence=confidence, revision=len(q.history))
        return {"status": "ok", "recorded": question_id,
                "remaining": self.remaining_ids()}

    def mark_asked(self, question_id: str) -> dict[str, Any]:
        q = self.questions.get(question_id)
        if q is None:
            return {"status": "error", "detail": f"unknown question_id '{question_id}'"}
        if q.status == "pending":
            q.status = "asked"
            q.updated_at = time.time()
        self._bus.emit("discovery.asked", question_id=question_id)
        return {"status": "ok"}

    def mark_skipped(self, question_id: str, reason: str = "") -> dict[str, Any]:
        q = self.questions.get(question_id)
        if q is None:
            return {"status": "error", "detail": f"unknown question_id '{question_id}'"}
        if q.status in ("answered", "partial"):
            # Don't erase an answer we already have; skipping is only for unanswered.
            return {"status": "error", "detail": f"'{question_id}' already has an answer"}
        q.status = "skipped"
        q.updated_at = time.time()
        q.history.append({"skipped": True, "reason": reason, "at": q.updated_at})
        self._bus.emit("discovery.skipped", question_id=question_id, reason=reason)
        return {"status": "ok", "remaining": self.remaining_ids()}

    def set_phase(self, phase: str) -> dict[str, Any]:
        if phase not in PHASES:
            return {"status": "error", "detail": f"unknown phase '{phase}'", "valid": PHASES}
        self.phase = phase
        self._bus.emit("discovery.phase", phase=phase)
        return {"status": "ok"}

    # --- views -------------------------------------------------------------
    def remaining_ids(self) -> list[str]:
        return [q.id for q in self.questions.values()
                if q.status in ("pending", "asked", "partial")]

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "questions": [
                {
                    "id": q.id, "label": q.label, "text": q.text, "status": q.status,
                    "answer_summary": q.answer_summary, "verbatim_quote": q.verbatim_quote,
                    "confidence": q.confidence, "updated_at": q.updated_at,
                    "revisions": len(q.history),
                }
                for q in self.questions.values()
            ],
        }

    def progress_note(self) -> str:
        """Compact state summary used to re-seed context after a session reconnect."""
        answered = [f"{q.id}: {q.answer_summary}" for q in self.questions.values()
                    if q.status in ("answered", "partial")]
        remaining = self.remaining_ids()
        return (
            f"Phase: {self.phase}. Answered so far: {'; '.join(answered) or 'none'}. "
            f"Still open: {', '.join(remaining) or 'none'}."
        )
