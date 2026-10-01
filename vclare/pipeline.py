"""Multi-stage VClare pipeline with result-JSON arbitration.

The pipeline keeps the full research structure: mining, specification repair,
candidate generation, testbench generation, simulation clustering, MBR ranking
and the two confirmation points.

The two confirmation points are *not* answered by an LLM. When the pipeline
reaches one, it publishes the question to ``arbitration_pending.json`` and stops
with status ``awaiting_arbitration``. A human answers through the web console or
by editing ``arbitration_decisions.json``; the pipeline is then resumed and
reads the decision from that result JSON.

Every stage also writes a ``cycles/cycle_XX_<stage>.json`` snapshot, so an
external harness can drive or inspect the pipeline purely through JSON.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .arbiter import (
    BEHAVIOR_CHOICE,
    INCONSISTENCY_PAIR,
    ArbitrationQuestion,
    Decision,
    HumanArbiter,
    question_from_dict,
)
from .backends import GoldenTBJudge, LLMBackend, Simulator
from .sim_repair import Candidate, SimRepair
from .spec_repair import InconsistencyPair, SpecRepair

PIPELINE_VERSION = 1

STAGES: Sequence[str] = (
    "stage0_load_input",
    "stage1_mine_inconsistency",
    "stage2_arbitrate_pairs",
    "stage3_repair_spec",
    "stage4_generate_candidates",
    "stage4e_evaluate_golden_tb",
    "stage5_generate_testbench",
    "stage6_simulate_and_cluster",
    "stage7_rank_mbr",
    "stage8_arbitrate_divergence",
    "stage9_select_and_report",
)

MODE_STAGES: Dict[str, Sequence[str]] = {
    "spec": (
        "stage0_load_input",
        "stage1_mine_inconsistency",
        "stage2_arbitrate_pairs",
        "stage3_repair_spec",
        "stage9_select_and_report",
    ),
    "sim": (
        "stage0_load_input",
        "stage4_generate_candidates",
        "stage4e_evaluate_golden_tb",
        "stage5_generate_testbench",
        "stage6_simulate_and_cluster",
        "stage7_rank_mbr",
        "stage8_arbitrate_divergence",
        "stage9_select_and_report",
    ),
    "hybrid": STAGES,
}

FALLBACK_VALUE = {
    INCONSISTENCY_PAIR: "irrelevant",
    BEHAVIOR_CHOICE: "abstain",
}


class AwaitingArbitration(RuntimeError):
    """Raised when the pipeline needs a human answer before it can continue."""

    def __init__(self, stage: str, pending_path: str, question_ids: Sequence[str]):
        super().__init__(
            "stage '{}' is waiting for {} confirmation(s); see {}".format(
                stage, len(question_ids), pending_path
            )
        )
        self.stage = stage
        self.pending_path = pending_path
        self.question_ids = list(question_ids)


@dataclass
class PipelineConfig:
    """Static configuration of one pipeline run."""

    task_id: str
    spec: str
    output_dir: str
    defect_type: str = ""
    module_name: str = "TopModule"
    num_candidates: int = 10
    max_pairs: int = 3
    mode: str = "hybrid"
    arbitration_policy: str = "defer"  # "defer" or "fallback"
    golden_tb_path: Optional[str] = None
    module_interface: str = ""

    def stages(self) -> Sequence[str]:
        if self.mode not in MODE_STAGES:
            raise ValueError(
                "unknown mode {!r}; expected one of {}".format(
                    self.mode, sorted(MODE_STAGES)
                )
            )
        return MODE_STAGES[self.mode]


# --------------------------------------------------------------------- JSON I/O
def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    # OneDrive and Windows indexing can briefly lock the destination file.
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    try:
        tmp.unlink()
    except OSError:
        pass


def _read_json(path: Path) -> Optional[Any]:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def decision_from_payload(payload: Dict[str, Any]) -> Decision:
    """Build a :class:`Decision` from a possibly partial result-JSON entry."""
    return Decision(
        question_id=payload["question_id"],
        kind=payload.get("kind", ""),
        task_id=payload.get("task_id", ""),
        value=payload["value"],
        label=payload.get("label", payload["value"]),
        options=list(payload.get("options", [])),
        defect_type=payload.get("defect_type", ""),
        source=payload.get("source", "result_json"),
        notes=payload.get("notes", ""),
        context=dict(payload.get("context", {})),
        timestamp=float(payload.get("timestamp") or time.time()),
    )


class ArbitrationStore:
    """File bridge between the pipeline and a human interface.

    Files written by the pipeline:

    * ``arbitration_pending.json`` - questions waiting for an answer.

    File written by the human interface / reviewer:

    * ``arbitration_decisions.json`` - answers keyed by ``question_id``.
    """

    PENDING = "arbitration_pending.json"
    DECISIONS = "arbitration_decisions.json"

    def __init__(self, run_dir: str) -> None:
        self.run_dir = Path(run_dir)
        self.pending_path = self.run_dir / self.PENDING
        self.decisions_path = self.run_dir / self.DECISIONS

    def publish(
        self,
        experiment_id: str,
        stage: str,
        questions: Sequence[ArbitrationQuestion],
    ) -> None:
        _write_json(
            self.pending_path,
            {
                "version": PIPELINE_VERSION,
                "experiment_id": experiment_id,
                "stage": stage,
                "generated_at": time.time(),
                "questions": [q.to_dict() for q in questions],
            },
        )

    def clear_pending(self) -> None:
        if self.pending_path.exists():
            self.pending_path.unlink()

    def load_decisions(self) -> Dict[str, Decision]:
        payload = _read_json(self.decisions_path) or {}
        entries = payload.get("decisions", payload) if isinstance(payload, dict) else payload
        decisions: Dict[str, Decision] = {}
        for entry in entries:
            if not isinstance(entry, dict) or "question_id" not in entry or "value" not in entry:
                continue
            decision = decision_from_payload(entry)
            decisions[decision.question_id] = decision
        return decisions

    def record(self, decision: Decision) -> None:
        payload = _read_json(self.decisions_path) or {
            "version": PIPELINE_VERSION,
            "decisions": [],
        }
        entries = payload.get("decisions", [])
        entries = [e for e in entries if e.get("question_id") != decision.question_id]
        entries.append(decision.to_dict())
        payload["decisions"] = entries
        payload["updated_at"] = time.time()
        _write_json(self.decisions_path, payload)

    def resolved(self, questions: Sequence[ArbitrationQuestion]) -> Dict[str, Decision]:
        decisions = self.load_decisions()
        return {
            question.question_id: decisions[question.question_id]
            for question in questions
            if question.question_id in decisions
        }


@dataclass
class PipelineRun:
    experiment_id: str
    run_dir: str
    mode: str
    status: str
    completed_stages: List[str] = field(default_factory=list)
    pending_path: str = ""
    result_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "run_dir": self.run_dir,
            "mode": self.mode,
            "status": self.status,
            "completed_stages": list(self.completed_stages),
            "pending_path": self.pending_path,
            "result_path": self.result_path,
        }


class VClarePipeline:
    """Multi-stage VClare pipeline driven by result JSON."""

    def __init__(
        self,
        config: PipelineConfig,
        backend: LLMBackend,
        simulator: Simulator,
        experiment_id: Optional[str] = None,
    ) -> None:
        self.config = config
        self.backend = backend
        self.simulator = simulator
        self.run_dir = Path(config.output_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.experiment_id = experiment_id or self.run_dir.name
        self.store = ArbitrationStore(str(self.run_dir))
        self.cycles_dir = self.run_dir / "cycles"
        self.cycles_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.run_dir / "state.json"
        self.context: Dict[str, Any] = self._load_state() or self._initial_context()

    # ------------------------------------------------------------------- state
    def _initial_context(self) -> Dict[str, Any]:
        return {
            "version": PIPELINE_VERSION,
            "experiment_id": self.experiment_id,
            "task_id": self.config.task_id,
            "mode": self.config.mode,
            "defect_type": self.config.defect_type,
            "spec_original": self.config.spec,
            "completed_stages": [],
            "llm_calls": 0,
            "stage_status": {},
        }

    def _load_state(self) -> Optional[Dict[str, Any]]:
        payload = _read_json(self.state_path)
        if isinstance(payload, dict) and payload.get("task_id") == self.config.task_id:
            payload.setdefault("completed_stages", [])
            payload.setdefault("stage_status", {})
            return payload
        return None

    def _save_state(self) -> None:
        _write_json(self.state_path, self.context)

    def _question_id(self, stage: str, key: Any) -> str:
        """Stable id so a human answer survives a pipeline restart."""
        return "{}::{}::{}".format(self.experiment_id, stage, key)

    def _snapshot(self, cycle: int, stage: str, status: str, data: Dict[str, Any]) -> None:
        payload = {
            "version": PIPELINE_VERSION,
            "experiment_id": self.experiment_id,
            "task_id": self.config.task_id,
            "mode": self.config.mode,
            "cycle": cycle,
            "stage": stage,
            "status": status,
            "recorded_at": time.time(),
            "data": data,
        }
        _write_json(self.cycles_dir / "cycle_{:02d}_{}.json".format(cycle, stage), payload)

    # ------------------------------------------------------------ confirmation
    def _collect(
        self,
        stage: str,
        questions: Sequence[ArbitrationQuestion],
    ) -> List[Decision]:
        """Return the human decisions for ``questions``, or defer the pipeline."""
        if not questions:
            return []

        decisions = self.store.resolved(questions)
        missing = [q for q in questions if q.question_id not in decisions]

        if missing and self.config.arbitration_policy == "fallback":
            arbiter = HumanArbiter()
            for question in missing:
                decision = arbiter.resolve(
                    question,
                    FALLBACK_VALUE.get(question.kind, question.options[0].value),
                    source="no_human_fallback",
                    notes="arbitration_policy=fallback",
                )
                self.store.record(decision)
                decisions[question.question_id] = decision
            missing = []

        if missing:
            self.store.publish(self.experiment_id, stage, questions)
            raise AwaitingArbitration(
                stage, str(self.store.pending_path), [q.question_id for q in missing]
            )

        self.store.clear_pending()
        return [decisions[q.question_id] for q in questions]

    # ------------------------------------------------------------------ stages
    def stage0_load_input(self) -> Dict[str, Any]:
        return {
            "task_id": self.config.task_id,
            "mode": self.config.mode,
            "spec_chars": len(self.config.spec),
            "defect_type": self.config.defect_type,
        }

    def stage1_mine_inconsistency(self) -> Dict[str, Any]:
        pairs = self.backend.mine_inconsistency(self.config.spec, self.config.max_pairs)
        mined = []
        for index, payload in enumerate(pairs, start=1):
            mined.append(
                InconsistencyPair(
                    a1=str(payload.get("a1", "")).strip(),
                    a2=str(payload.get("a2", "")).strip(),
                    defect_type=str(payload.get("defect_type", self.config.defect_type)).strip(),
                    rationale=str(payload.get("rationale", "")).strip(),
                    index=index,
                )
            )
        self.context["mined_pairs"] = [pair.to_dict() for pair in mined]
        return {"mined_pairs": [pair.to_dict() for pair in mined]}

    def _pairs(self) -> List[InconsistencyPair]:
        return [
            InconsistencyPair(
                a1=item.get("a1", ""),
                a2=item.get("a2", ""),
                defect_type=item.get("defect_type", ""),
                rationale=item.get("rationale", ""),
                index=int(item.get("index", index)),
            )
            for index, item in enumerate(self.context.get("mined_pairs", []), start=1)
        ]

    def stage2_arbitrate_pairs(self) -> Dict[str, Any]:
        pairs = [pair for pair in self._pairs() if not pair.is_standalone]
        total = len(self._pairs())
        questions = [
            SpecRepair.build_question(self.config.task_id, pair, total)
            for pair in pairs
        ]
        for index, question in enumerate(questions, start=1):
            question.question_id = self._question_id("stage2", index)
            question.context["spec"] = self.config.spec
        self.context["pair_questions"] = [q.to_dict() for q in questions]
        decisions = self._collect("stage2_arbitrate_pairs", questions)
        self.context["pair_decisions"] = [d.to_dict() for d in decisions]
        return {
            "questions": [q.to_dict() for q in questions],
            "decisions": [d.to_dict() for d in decisions],
        }

    def _pair_decision(self) -> Optional[Decision]:
        entries = self.context.get("pair_decisions", [])
        if not entries:
            return None
        # The paper stops at the first pair that is not marked irrelevant.
        for entry in entries:
            decision = decision_from_payload(entry)
            if decision.value != "irrelevant":
                return decision
        return decision_from_payload(entries[0])

    def stage3_repair_spec(self) -> Dict[str, Any]:
        decision = self._pair_decision()
        spec = self.config.spec
        if decision is None:
            self.context["repaired_spec"] = spec
            return {"repaired_spec": spec, "applied": False}

        pair_payload = self.context.get("mined_pairs", [{}])[
            max(int(decision.context.get("index", 1)) - 1, 0)
        ]
        pair = InconsistencyPair(
            a1=pair_payload.get("a1", ""),
            a2=pair_payload.get("a2", ""),
            defect_type=pair_payload.get("defect_type", ""),
            rationale=pair_payload.get("rationale", ""),
            index=int(pair_payload.get("index", 1)),
        )
        repaired = self.backend.repair_spec(spec, pair, decision.value)
        self.context["repaired_spec"] = repaired
        return {
            "applied": decision.value != "irrelevant",
            "decision": decision.to_dict(),
            "repaired_spec": repaired,
        }

    def _active_spec(self) -> str:
        if self.config.mode in ("spec", "hybrid"):
            return self.context.get("repaired_spec") or self.config.spec
        return self.config.spec

    def stage4_generate_candidates(self) -> Dict[str, Any]:
        candidates = self.backend.generate_candidates(
            self._active_spec(),
            self.config.num_candidates,
            self.config.module_name,
            self.config.module_interface,
        )
        self.context["candidates"] = list(candidates)
        return {"count": len(candidates), "candidates": list(candidates)}

    def stage4e_evaluate_golden_tb(self) -> Dict[str, Any]:
        """Optional golden-testbench evaluation (the original stage2e).

        Runs only when ``golden_tb_path`` is configured, because the
        open-source release ships no benchmark data. The judgement itself is
        delegated to the original ``score.py::judge_task``.
        """
        if not self.config.golden_tb_path:
            return {"skipped": "no golden testbench provided"}

        golden_path = Path(self.config.golden_tb_path)
        if not golden_path.exists():
            return {"skipped": "golden testbench not found: {}".format(golden_path)}

        tb_code = golden_path.read_text(encoding="utf-8")
        cases = {
            "case{}".format(index): source
            for index, source in enumerate(self.context.get("candidates", []), start=1)
        }
        results, metrics = GoldenTBJudge().judge(
            self.config.task_id,
            tb_code,
            cases,
            str(self.run_dir / "golden_tb"),
            timeout=20,
        )
        self.context["golden_tb_results"] = list(results)
        self.context["golden_tb_metrics"] = metrics
        return {
            "golden_tb_path": str(golden_path),
            "results": list(results),
            "metrics": metrics,
        }

    def stage5_generate_testbench(self) -> Dict[str, Any]:
        testbench = self.backend.generate_testbench(
            self._active_spec(),
            self.context.get("candidates", []),
            self.config.module_name,
            self.config.module_interface,
        )
        self.context["testbench"] = testbench
        return {"testbench_chars": len(testbench), "testbench": testbench}

    def stage6_simulate_and_cluster(self) -> Dict[str, Any]:
        candidates = [
            Candidate(candidate_id="c{}".format(i), source=src)
            for i, src in enumerate(self.context.get("candidates", []), start=1)
        ]
        traces = self.simulator.simulate(
            self.context.get("testbench", ""),
            [c.source for c in candidates],
            str(self.run_dir / "sim"),
        )

        test_cases: List[str] = []
        by_id = {trace.get("candidate_id"): trace for trace in traces}
        for trace in traces:
            for case in trace.get("outputs", {}):
                if case not in test_cases:
                    test_cases.append(case)

        candidates = []
        for index, source in enumerate(self.context.get("candidates", []), start=1):
            trace = by_id.get("c{}".format(index), {})
            candidates.append(
                Candidate(
                    candidate_id="c{}".format(index),
                    source=source,
                    outputs=trace.get("outputs", {}),
                    compiled=bool(trace.get("compiled", False)),
                )
            )

        repair = SimRepair(test_cases)
        clusters = repair.cluster(candidates)
        self.context["test_cases"] = test_cases
        self.context["simulation_results"] = traces
        self.context["clusters"] = [cluster.to_dict() for cluster in clusters]
        return {
            "test_cases": test_cases,
            "simulation_results": traces,
            "clusters": [cluster.to_dict() for cluster in clusters],
        }

    def stage7_rank_mbr(self) -> Dict[str, Any]:
        clusters = self.context.get("clusters", [])
        ranking = [
            {
                "rank": index,
                "cluster_id": cluster["cluster_id"],
                "score": cluster["score"],
                "size": cluster["size"],
            }
            for index, cluster in enumerate(clusters, start=1)
        ]
        self.context["ranking"] = ranking
        return {"ranking": ranking}

    def stage8_arbitrate_divergence(self) -> Dict[str, Any]:
        clusters = self._clusters()
        repair = SimRepair(self.context.get("test_cases", []))
        question = repair.build_question(
            self.config.task_id, clusters, defect_type=self.config.defect_type
        )
        if question is None:
            self.context["divergence_question"] = None
            self.context["divergence_decision"] = None
            return {"question": None, "decision": None}

        question.question_id = self._question_id("stage8", 1)
        question.context["spec"] = self._active_spec()
        question.context["test_cases"] = list(self.context.get("test_cases", []))
        self.context["divergence_question"] = question.to_dict()
        decisions = self._collect("stage8_arbitrate_divergence", [question])
        self.context["divergence_decision"] = decisions[0].to_dict()
        return {"question": question.to_dict(), "decision": decisions[0].to_dict()}

    def _clusters(self):
        # Rebuild Cluster objects from the serialised snapshot.
        from .sim_repair import Cluster

        clusters = []
        for payload in self.context.get("clusters", []):
            representative = Candidate(
                candidate_id=payload.get("representative", "c?"),
                source=payload.get("source", ""),
                outputs=payload.get("outputs", {}),
                compiled=bool(payload.get("compiled", True)),
            )
            member_ids = list(payload.get("members") or [representative.candidate_id])
            if representative.candidate_id not in member_ids:
                member_ids.insert(0, representative.candidate_id)
            members = []
            for member_id in member_ids:
                if member_id == representative.candidate_id:
                    members.append(representative)
                else:
                    # Behaviorally equivalent to the representative by
                    # construction, so replay the same outputs.
                    members.append(
                        Candidate(
                            candidate_id=member_id,
                            source="",
                            outputs=dict(representative.outputs),
                            compiled=representative.compiled,
                        )
                    )
            clusters.append(
                Cluster(
                    cluster_id=payload["cluster_id"],
                    members=members,
                    tests=self.context.get("test_cases", []),
                    score=payload.get("score", 0),
                )
            )
        return clusters

    def stage9_select_and_report(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {
            "task_id": self.config.task_id,
            "mode": self.config.mode,
            "repaired_spec": self.context.get("repaired_spec", ""),
            "selected_cluster": None,
            "selected_candidate": None,
        }

        if self.config.mode == "spec":
            self.context["result"] = report
            return report

        clusters = self._clusters()
        repair = SimRepair(self.context.get("test_cases", []))
        decision_payload = self.context.get("divergence_decision")
        decision = decision_from_payload(decision_payload) if decision_payload else None
        selected = repair.select(clusters, decision)

        report.update(
            {
                "selected_cluster": selected.cluster_id,
                "selected_candidate": selected.representative.candidate_id,
                "selected_source": selected.representative.source,
            }
        )
        self.context["result"] = report
        return report

    # -------------------------------------------------------------------- run
    def _stage_callables(self) -> Dict[str, Any]:
        return {
            name: getattr(self, name) for name in STAGES
        }

    def run(self, max_stage: Optional[int] = None) -> PipelineRun:
        """Run or resume the pipeline until it finishes or needs a human."""
        stages = self.config.stages()
        completed = set(self.context.get("completed_stages", []))
        callables = self._stage_callables()
        status = "completed"
        pending_path = ""

        for index, name in enumerate(stages, start=1):
            if max_stage is not None and index > max_stage:
                status = "stopped"
                break
            if name in completed:
                continue

            try:
                data = callables[name]()
            except AwaitingArbitration as pending:
                self.context["stage_status"][name] = "awaiting_arbitration"
                self._snapshot(index, name, "awaiting_arbitration", {"pending_path": pending.pending_path})
                self._save_state()
                return PipelineRun(
                    experiment_id=self.experiment_id,
                    run_dir=str(self.run_dir),
                    mode=self.config.mode,
                    status="awaiting_arbitration",
                    completed_stages=sorted(completed, key=STAGES.index),
                    pending_path=pending.pending_path,
                    result_path=str(self.run_dir / "run_result.json"),
                )

            completed.add(name)
            self.context["completed_stages"] = sorted(completed, key=STAGES.index)
            self.context["stage_status"][name] = "completed"
            self._snapshot(index, name, "completed", data)
            self._save_state()

        result_path = self.run_dir / "run_result.json"
        _write_json(
            result_path,
            {
                "version": PIPELINE_VERSION,
                "experiment_id": self.experiment_id,
                "status": status,
                "context": self.context,
            },
        )
        return PipelineRun(
            experiment_id=self.experiment_id,
            run_dir=str(self.run_dir),
            mode=self.config.mode,
            status=status,
            completed_stages=sorted(completed, key=STAGES.index),
            pending_path=pending_path,
            result_path=str(result_path),
        )
