"""Human arbitration for the two VClare confirmation points.

VClare needs exactly two lightweight confirmation signals from a human
engineer:

1. ``inconsistency_pair``: given two conflicting specification statements
   mined by the LLM, the engineer states which statement reflects the intended
   behavior, or marks the pair as irrelevant.
2. ``behavior_choice``: given two behavioral clusters that disagree on one
   distinguishing test case, the engineer states which behavior is intended,
   or abstains.

The automated research harness used an LLM to answer these two questions during
data collection. In this open-source release the two LLM arbitration prompts are
removed. The decisions are collected from a human instead, either through the
terminal or through the local web interface in ``webui/``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


INCONSISTENCY_PAIR = "inconsistency_pair"
BEHAVIOR_CHOICE = "behavior_choice"


@dataclass
class ArbitrationOption:
    """One selectable answer of an arbitration question."""

    value: str
    label: str
    detail: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ArbitrationQuestion:
    """A single confirmation request shown to the engineer."""

    kind: str
    task_id: str
    prompt: str
    options: List[ArbitrationOption]
    question_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    defect_type: str = ""
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["options"] = [o.to_dict() for o in self.options]
        return data

    def option_values(self) -> List[str]:
        return [o.value for o in self.options]


@dataclass
class Decision:
    """A recorded answer to an arbitration question."""

    question_id: str
    kind: str
    task_id: str
    value: str
    label: str
    options: List[str] = field(default_factory=list)
    defect_type: str = ""
    source: str = "human"
    notes: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class HumanArbiter:
    """Collect and persist the two VClare confirmation signals.

    Args:
        log_path: optional JSON file where every decision is appended.
        auto_answers: optional mapping ``question_id -> value`` used for
            non-interactive runs (CI, offline demos, replay of a human study).
    """

    def __init__(
        self,
        log_path: Optional[str] = None,
        auto_answers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.log_path = log_path
        self.auto_answers = dict(auto_answers or {})
        self.decisions: List[Decision] = []
        self._pending: Dict[str, ArbitrationQuestion] = {}

    # ------------------------------------------------------------------ helpers
    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def pending(self) -> List[ArbitrationQuestion]:
        return list(self._pending.values())

    def decision_for(self, question_id: str) -> Optional[Decision]:
        for decision in self.decisions:
            if decision.question_id == question_id:
                return decision
        return None

    # ------------------------------------------------------------- question API
    def register(self, question: ArbitrationQuestion) -> ArbitrationQuestion:
        """Add a question to the pending set and return it."""
        self._pending[question.question_id] = question
        return question

    def resolve(
        self,
        question: ArbitrationQuestion,
        value: str,
        source: str = "human",
        notes: str = "",
    ) -> Decision:
        """Record an answer for ``question`` after validating it."""
        if value not in question.option_values():
            raise ValueError(
                "value {!r} is not one of {}".format(value, question.option_values())
            )

        chosen = next(o for o in question.options if o.value == value)
        decision = Decision(
            question_id=question.question_id,
            kind=question.kind,
            task_id=question.task_id,
            value=value,
            label=chosen.label,
            options=question.option_values(),
            defect_type=question.defect_type,
            source=source,
            notes=notes,
            context=dict(question.context),
        )
        self.decisions.append(decision)
        self._pending.pop(question.question_id, None)
        if self.log_path:
            self.save(self.log_path)
        return decision

    def ask(
        self,
        question: ArbitrationQuestion,
        interactive: bool = True,
    ) -> Decision:
        """Answer ``question`` via auto-answers, the terminal, or a fallback.

        When ``interactive`` is False and no auto answer exists, the first
        option is selected and flagged as an automatic fallback so the run never
        blocks in unattended environments.
        """
        self.register(question)

        if question.question_id in self.auto_answers:
            return self.resolve(
                question,
                self.auto_answers[question.question_id],
                source="auto",
                notes="provided by auto_answers",
            )

        if not interactive:
            return self.resolve(
                question,
                question.options[0].value,
                source="auto_fallback",
                notes="non-interactive run, first option used",
            )

        return self._ask_cli(question)

    # --------------------------------------------------------------- persistence
    def save(self, path: Optional[str] = None) -> str:
        target = path or self.log_path
        if not target:
            raise ValueError("no log_path configured")
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "version": 1,
                    "updated_at": time.time(),
                    "decisions": [d.to_dict() for d in self.decisions],
                },
                handle,
                indent=2,
                ensure_ascii=False,
            )
        return target

    def load(self, path: Optional[str] = None) -> List[Decision]:
        target = path or self.log_path
        if not target or not os.path.exists(target):
            return self.decisions
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        entries = payload.get("decisions", payload) if isinstance(payload, dict) else payload
        self.decisions = [Decision(**entry) for entry in entries]
        return self.decisions

    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for decision in self.decisions:
            counts[decision.kind] = counts.get(decision.kind, 0) + 1
        return counts

    # --------------------------------------------------------------------- CLI
    def _ask_cli(self, question: ArbitrationQuestion) -> Decision:
        print()
        print("=" * 72)
        print("[VClare arbitration] {} :: {}".format(question.kind, question.task_id))
        print("=" * 72)
        print(question.prompt)
        print("-" * 72)
        for index, option in enumerate(question.options, start=1):
            line = "{}. {} [{}]".format(index, option.label, option.value)
            print(line)
            if option.detail:
                for detail_line in option.detail.splitlines():
                    print("     " + detail_line)
        print("-" * 72)

        while True:
            raw = input("Select 1-{} (q to quit): ".format(len(question.options))).strip()
            if raw.lower() == "q":
                raise KeyboardInterrupt("arbitration aborted by user")
            try:
                index = int(raw) - 1
            except ValueError:
                print("Enter a number between 1 and {}.".format(len(question.options)))
                continue
            if 0 <= index < len(question.options):
                return self.resolve(question, question.options[index].value)
            print("Enter a number between 1 and {}.".format(len(question.options)))


def question_from_dict(payload: Dict[str, Any]) -> ArbitrationQuestion:
    """Rebuild an :class:`ArbitrationQuestion` from JSON data."""
    return ArbitrationQuestion(
        question_id=payload.get("question_id") or uuid.uuid4().hex[:12],
        kind=payload["kind"],
        task_id=payload["task_id"],
        prompt=payload["prompt"],
        options=[ArbitrationOption(**option) for option in payload["options"]],
        defect_type=payload.get("defect_type", ""),
        context=payload.get("context", {}),
    )
