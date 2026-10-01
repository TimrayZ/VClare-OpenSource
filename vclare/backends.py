"""Pluggable backends for the multi-stage VClare pipeline.

Two kinds of backend are defined:

``LLMBackend``
    The LLM work that is *not* a confirmation point: inconsistency mining,
    targeted specification repair, candidate generation and testbench
    generation. There is deliberately no ``arbitrate`` method. The two VClare
    confirmation points are answered by a human through result JSON, never by an
    LLM response.

    The prompts shipped in this release live in :mod:`vclare.prompts` and cover
    ``mine_inconsistency``, ``generate_candidates`` and ``generate_testbench``.
    The prompt for ``repair_spec`` is intentionally not included; that method
    raises by default and accepts an externally supplied prompt.

``Simulator``
    Behavioral simulation of the generated candidates. ``IcarusSimulator``
    shells out to iverilog/vvp; ``PrecomputedSimulator`` replays stored outputs
    so the pipeline can be exercised without an EDA toolchain.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Protocol, Sequence

from .iverilog import IverilogSimulator
from .llm import LLMClient, system_user
from .prompts import (
    MINING_SYSTEM_PROMPT,
    MINING_USER_PROMPT,
    RTL_4_SHOT_EXAMPLES,
    TESTCASE_GENERATION_PROMPT,
    TESTCASE_OUTPUT_CONTRACT,
    TESTCASE_SYSTEM_PROMPT,
    VERILOG_EXTRA_ORDER_PROMPT,
    VERILOG_GENERATION_PROMPT,
    VERILOG_IF_PROMPT,
    VERILOG_SYSTEM_PROMPT,
)
from .spec_repair import InconsistencyPair, _extract_md, parse_inconsistency_pairs

PROMPT_NOT_INCLUDED = (
    "The prompt for stage '{stage}' is intentionally not part of this release. "
    "Supply it explicitly when constructing the backend."
)


class LLMBackend(Protocol):
    """Interface for the non-arbitration LLM stages."""

    def mine_inconsistency(
        self, spec: str, max_pairs: int = 3
    ) -> List[Dict[str, Any]]:
        ...

    def repair_spec(
        self, spec: str, pair: InconsistencyPair, decision_value: str
    ) -> str:
        ...

    def generate_candidates(
        self,
        spec: str,
        count: int,
        module_name: str = "TopModule",
        module_interface: str = "",
    ) -> List[str]:
        ...

    def generate_testbench(
        self,
        spec: str,
        candidates: Sequence[str],
        module_name: str = "TopModule",
        module_interface: str = "",
    ) -> str:
        ...


class OpenAIBackend:
    """LLM backend for any OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        llm: LLMClient,
        mining_prompt: str = MINING_USER_PROMPT,
        mining_system_prompt: str = MINING_SYSTEM_PROMPT,
        repair_prompt: str = None,
        repair_system_prompt: str = "",
    ) -> None:
        """Create a backend.

        ``mining_prompt`` defaults to the prompt shipped in
        :mod:`vclare.prompts`. ``repair_prompt`` is not shipped with this
        release; when it is omitted, the repair stage raises ``RuntimeError``
        and the rest of the pipeline is unaffected.
        """
        self.llm = llm
        self.calls: List[Dict[str, Any]] = []
        self.mining_prompt = mining_prompt
        self.mining_system_prompt = mining_system_prompt
        self.repair_prompt = repair_prompt
        self.repair_system_prompt = repair_system_prompt

    def _call(self, stage: str, system: str, user: str, temperature: float) -> str:
        text = self.llm.chat(system_user(system, user), temperature=temperature)
        self.calls.append({"stage": stage, "prompt": user, "response": text})
        return text

    def mine_inconsistency(
        self, spec: str, max_pairs: int = 3
    ) -> List[Dict[str, Any]]:
        if not self.mining_prompt:
            raise RuntimeError(
                PROMPT_NOT_INCLUDED.format(stage="stage1_mine_inconsistency")
            )
        raw = self._call(
            "mine_inconsistency",
            self.mining_system_prompt,
            self.mining_prompt.format(spec=spec),
            0.0,
        )
        return parse_inconsistency_pairs(raw, max_pairs)

    def repair_spec(
        self, spec: str, pair: InconsistencyPair, decision_value: str
    ) -> str:
        if decision_value == "irrelevant":
            return spec
        if not self.repair_prompt:
            raise RuntimeError(
                PROMPT_NOT_INCLUDED.format(stage="stage3_repair_spec")
            )
        if decision_value == "source1":
            correct, wrong, cs, ws = pair.a1, pair.a2, "1", "2"
        elif decision_value == "source2":
            correct, wrong, cs, ws = pair.a2, pair.a1, "2", "1"
        else:
            raise ValueError("unknown decision {!r}".format(decision_value))

        raw = self._call(
            "repair_spec",
            self.repair_system_prompt,
            self.repair_prompt.format(
                spec=spec,
                correct=correct,
                wrong=wrong,
                correct_source=cs,
                wrong_source=ws,
            ),
            0.0,
        )
        return _extract_md(raw)

    def generate_candidates(
        self,
        spec: str,
        count: int,
        module_name: str = "TopModule",
        module_interface: str = "",
    ) -> List[str]:
        prompt = (
            VERILOG_GENERATION_PROMPT.format(
                examples_prompt=RTL_4_SHOT_EXAMPLES,
                input_spec=spec,
            )
            + "\n"
            + VERILOG_EXTRA_ORDER_PROMPT
            + VERILOG_IF_PROMPT.format(module_interface=module_interface or "")
            + "\n"
        )
        return [
            self._call(
                "generate_candidates", VERILOG_SYSTEM_PROMPT, prompt, 0.8
            )
            for _ in range(count)
        ]

    def generate_testbench(
        self,
        spec: str,
        candidates: Sequence[str],
        module_name: str = "TopModule",
        module_interface: str = "",
    ) -> str:
        del candidates
        prompt = TESTCASE_GENERATION_PROMPT.format(
            spec=spec,
            header=module_interface or module_name,
        )
        # Mirrors PipelineFull.stagex1_direct_testcases_generation, which
        # appends this contract after formatting the base prompt.
        prompt += TESTCASE_OUTPUT_CONTRACT
        raw = self._call(
            "generate_testbench", TESTCASE_SYSTEM_PROMPT, prompt, 0.0
        )
        fenced = re.search(r"```(?:verilog|sv|systemverilog)?\s*\n(.*?)```", raw, re.DOTALL)
        return fenced.group(1).strip() if fenced else raw.strip()


class OfflineBackend:
    """Replay precomputed LLM outputs so the pipeline runs without an API key.

    ``artifacts`` is the parsed content of ``examples/demo_artifacts.json``.
    """

    def __init__(self, artifacts: Dict[str, Any]) -> None:
        self.artifacts = artifacts

    def mine_inconsistency(
        self, spec: str, max_pairs: int = 3
    ) -> List[Dict[str, Any]]:
        return list(self.artifacts.get("mined_pairs", []))[:max_pairs]

    def repair_spec(
        self, spec: str, pair: InconsistencyPair, decision_value: str
    ) -> str:
        repaired = self.artifacts.get("repaired_spec", {})
        if isinstance(repaired, dict):
            return repaired.get(decision_value, spec)
        return spec

    def generate_candidates(
        self,
        spec: str,
        count: int,
        module_name: str = "TopModule",
        module_interface: str = "",
    ) -> List[str]:
        return list(self.artifacts.get("candidates", []))[:count]

    def generate_testbench(
        self,
        spec: str,
        candidates: Sequence[str],
        module_name: str = "TopModule",
        module_interface: str = "",
    ) -> str:
        return str(self.artifacts.get("testbench", ""))


class DisabledBackend:
    """Explicit backend for runs that only exercise the interface.

    Every LLM stage raises, which makes the missing wiring visible instead of
    silently producing empty artifacts.
    """

    def _fail(self, stage: str):
        raise RuntimeError(
            "LLM backend is disabled: stage '{}' needs a configured backend "
            "(OpenAIBackend or OfflineBackend).".format(stage)
        )

    def mine_inconsistency(self, spec: str, max_pairs: int = 3):
        self._fail("mine_inconsistency")

    def repair_spec(self, spec: str, pair: InconsistencyPair, decision_value: str):
        self._fail("repair_spec")

    def generate_candidates(
        self,
        spec: str,
        count: int,
        module_name: str = "TopModule",
        module_interface: str = "",
    ):
        self._fail("generate_candidates")

    def generate_testbench(
        self,
        spec: str,
        candidates: Sequence[str],
        module_name: str = "TopModule",
        module_interface: str = "",
    ):
        self._fail("generate_testbench")


class Simulator(Protocol):
    """Interface for behavioral simulation of the candidate pool."""

    def simulate(
        self,
        testbench: str,
        candidates: Sequence[str],
        workdir: str,
        timeout: int = 60,
    ) -> List[Dict[str, Any]]:
        ...


class IcarusSimulator(IverilogSimulator):
    """Backwards-compatible alias for the original iverilog simulation path.

    The implementation lives in :mod:`vclare.iverilog.simulator` and mirrors the
    research harness command lines in ``evaluate_tb_diff.py``.
    """


class GoldenTBJudge:
    """Thin wrapper over ``vclare.iverilog.score.judge_task``.

    This is the golden-testbench pass/fail path used by the original
    ``stage2e`` of the research harness. It is only invoked when a run supplies
    a golden testbench, which the open-source release does not ship.
    """

    def judge(
        self,
        task_name: str,
        tb_code: str,
        cases: Dict[str, str],
        base_path: str,
        timeout: int = 20,
        truth_labels: Sequence[str] = None,
    ):
        from .iverilog import score

        return score.judge_task(
            task_name,
            tb_code,
            cases,
            base_path,
            timeout=timeout,
            truth_labels=truth_labels,
        )


class PrecomputedSimulator:
    """Replay stored simulation traces for offline runs."""

    def __init__(self, traces: Sequence[Dict[str, Any]]) -> None:
        self.traces = list(traces)

    def simulate(
        self,
        testbench: str,
        candidates: Sequence[str],
        workdir: str,
        timeout: int = 60,
    ) -> List[Dict[str, Any]]:
        del testbench, workdir, timeout
        results = list(self.traces[: len(candidates)])
        while len(results) < len(candidates):
            results.append(
                {
                    "candidate_id": "c{}".format(len(results) + 1),
                    "compiled": False,
                    "outputs": {},
                    "error": "missing precomputed trace",
                }
            )
        return results


def load_artifacts(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
