"""Spec-Level Repair: semantic inconsistency mining and targeted editing.

The pipeline follows three steps:

1. ``mine`` asks the LLM to surface up to ``max_pairs`` inconsistency pairs
   ``(a1, a2)`` from a specification.
2. Arbitration: a human engineer confirms which statement reflects the intended
   behavior, or marks the pair as irrelevant. This is the first of the two
   confirmation points of VClare. The open-source release collects the answer
   through :class:`~vclare.arbiter.HumanArbiter`; the LLM arbitration prompt
   used during data collection is not shipped.
3. ``repair`` asks the LLM for a constrained edit that rewrites only the
   statement the engineer marked as incorrect.

Both prompts used by this module (``mine`` and the targeted ``repair``) are
shipped in :mod:`vclare.prompts`. The blind-fix prompt is not part of VClare and
is therefore not included.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .arbiter import (
    INCONSISTENCY_PAIR,
    ArbitrationOption,
    ArbitrationQuestion,
    Decision,
    HumanArbiter,
)
from .llm import LLMClient, system_user
from .prompts import (
    MINING_SYSTEM_PROMPT,
    MINING_USER_PROMPT,
    REPAIR_SYSTEM_PROMPT,
    REPAIR_USER_PROMPT,
)


PROMPT_NOT_INCLUDED = (
    "SpecRepair.{stage} needs a prompt: pass {arg} explicitly to SpecRepair(...)."
)


@dataclass
class InconsistencyPair:
    """A suspected defect expressed as two conflicting statements."""

    a1: str
    a2: str
    defect_type: str = ""
    rationale: str = ""
    index: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_standalone(self) -> bool:
        return self.a1.strip() == self.a2.strip()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "a1": self.a1,
            "a2": self.a2,
            "defect_type": self.defect_type,
            "rationale": self.rationale,
            "standalone": self.is_standalone,
        }


def parse_inconsistency_pairs(text: str, max_pairs: int = 3) -> List[Dict[str, str]]:
    """Extract ``source1``/``source2`` pairs from a miner response.

    The miner reasons first and then emits a tagged ```json block, so the LAST
    fenced block is the one that carries the answer. The returned dictionaries
    use the internal ``a1``/``a2`` keys.
    """
    matches = list(re.finditer(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL))
    candidate = matches[-1].group(1).strip() if matches else text.strip()
    try:
        parsed = json.loads(candidate)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []

    pairs: List[Dict[str, str]] = []
    for item in parsed[:max_pairs]:
        if isinstance(item, dict) and "source1" in item and "source2" in item:
            pairs.append(
                {
                    "a1": str(item["source1"]).strip(),
                    "a2": str(item["source2"]).strip(),
                }
            )
    return pairs


def _extract_md(text: str) -> str:
    fenced = re.search(r"```(?:md|markdown)?\s*\n(.*?)```", text, re.DOTALL)
    return fenced.group(1).strip() if fenced else text.strip()


class SpecRepair:
    """Run Spec-Level Repair for a single specification."""

    def __init__(
        self,
        llm: LLMClient = None,
        max_pairs: int = 3,
        mining_prompt: str = MINING_USER_PROMPT,
        mining_system_prompt: str = MINING_SYSTEM_PROMPT,
        repair_prompt: str = REPAIR_USER_PROMPT,
        repair_system_prompt: str = REPAIR_SYSTEM_PROMPT,
    ) -> None:
        """Create a Spec-Level Repair runner.

        ``mining_prompt`` and ``repair_prompt`` default to the prompts shipped
        in :mod:`vclare.prompts`.
        """
        self.llm = llm
        self.max_pairs = max_pairs
        self.mining_prompt = mining_prompt
        self.mining_system_prompt = mining_system_prompt
        self.repair_prompt = repair_prompt
        self.repair_system_prompt = repair_system_prompt

    # ------------------------------------------------------------------- mining
    def mine(self, spec: str) -> List[InconsistencyPair]:
        """Ask the LLM for inconsistency pairs, mirroring the paper's step."""
        if not self.mining_prompt:
            raise RuntimeError(
                PROMPT_NOT_INCLUDED.format(stage="mine", arg="mining_prompt")
            )
        raw = self.llm.chat(
            system_user(
                self.mining_system_prompt,
                self.mining_prompt.format(spec=spec),
            ),
            temperature=0.0,
        )

        pairs: List[InconsistencyPair] = []
        for index, item in enumerate(
            parse_inconsistency_pairs(raw, self.max_pairs), start=1
        ):
            pairs.append(
                InconsistencyPair(
                    a1=item["a1"],
                    a2=item["a2"],
                    index=index,
                )
            )
        return [p for p in pairs if p.a1 or p.a2]

    # -------------------------------------------------------------- arbitration
    @staticmethod
    def build_question(
        task_id: str,
        pair: InconsistencyPair,
        total_pairs: int,
    ) -> ArbitrationQuestion:
        """Build the first VClare confirmation question.

        The engineer is *not* asked to rewrite the specification. The only
        signal requested is which of the two statements should be trusted.
        """
        source1 = ArbitrationOption(
            value="source1",
            label="Source 1 is correct",
            detail=pair.a1,
        )
        source2 = ArbitrationOption(
            value="source2",
            label="Source 2 is correct",
            detail=pair.a2,
        )
        irrelevant = ArbitrationOption(
            value="irrelevant",
            label="Irrelevant, discard this pair",
            detail="The pair is not a genuine defect, or carries no correction signal.",
        )
        return ArbitrationQuestion(
            kind=INCONSISTENCY_PAIR,
            task_id=task_id,
            defect_type=pair.defect_type,
            prompt=(
                "Inconsistency pair {index} of {total}: which statement reflects "
                "the intended behavior?".format(index=pair.index, total=total_pairs)
            ),
            options=[source1, source2, irrelevant],
            context={
                "pair": pair.to_dict(),
                "a1": pair.a1,
                "a2": pair.a2,
                "rationale": pair.rationale,
                "index": pair.index,
                "total_pairs": total_pairs,
            },
        )

    # ------------------------------------------------------------------- repair
    def repair(
        self,
        spec: str,
        pair: InconsistencyPair,
        decision: Decision,
    ) -> str:
        """Apply a constrained edit for a confirmed pair."""
        if decision.value == "irrelevant":
            return spec
        if not self.repair_prompt:
            raise RuntimeError(
                PROMPT_NOT_INCLUDED.format(stage="repair", arg="repair_prompt")
            )
        if decision.value == "source1":
            answer, believed, rejected = "source 1", pair.a1, pair.a2
        elif decision.value == "source2":
            answer, believed, rejected = "source 2", pair.a2, pair.a1
        else:
            raise ValueError("unknown arbitration value {!r}".format(decision.value))

        raw = self.llm.chat(
            system_user(
                self.repair_system_prompt,
                self.repair_prompt.format(
                    index=pair.index,
                    src1=pair.a1,
                    src2=pair.a2,
                    ans=answer,
                    believed=believed,
                    rejected=rejected,
                    defective_spec=spec,
                ),
            ),
            temperature=0.0,
        )
        return _extract_md(raw)

    # ----------------------------------------------------------------- pipeline
    def run(
        self,
        task_id: str,
        spec: str,
        arbiter: HumanArbiter,
        interactive: bool = True,
    ) -> Dict[str, Any]:
        """Mine, arbitrate and repair, returning a serialisable trace."""
        pairs = self.mine(spec)
        repaired = spec
        trace: List[Dict[str, Any]] = []

        for pair in pairs:
            if pair.is_standalone:
                # A standalone issue has no second statement to compare, so it
                # cannot be arbitrated as source1/source2 and is left untouched.
                trace.append(
                    {
                        "pair": pair.to_dict(),
                        "decision": None,
                        "skipped": "standalone issue without a comparable statement",
                    }
                )
                continue

            question = self.build_question(task_id, pair, len(pairs))
            decision = arbiter.ask(question, interactive=interactive)
            repaired = self.repair(repaired, pair, decision)
            trace.append({"pair": pair.to_dict(), "decision": decision.to_dict()})

            # The paper stops reading pairs once a genuine defect is confirmed.
            if decision.value != "irrelevant":
                break

        return {
            "task_id": task_id,
            "original_spec": spec,
            "mined_pairs": [p.to_dict() for p in pairs],
            "repaired_spec": repaired,
            "trace": trace,
        }
