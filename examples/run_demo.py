"""Offline end-to-end demo for the VClare framework.

The demo uses only synthetic data from ``demo_cases.json``. It runs both
confirmation points of VClare without any LLM key and without iverilog:

* the LLM mining step is replaced by the pre-mined pairs shipped with the
  synthetic demo;
* the LLM targeted-edit step is replaced by a deterministic local edit;
* clustering, MBR ranking and divergence detection use the real framework code.

Usage::

    python examples/run_demo.py                 # auto-answer, verify, exit 0
    python examples/run_demo.py --interactive   # answer in the terminal
    python examples/run_demo.py --json out.json # also dump the full trace
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vclare.arbiter import HumanArbiter  # noqa: E402
from vclare.sim_repair import Candidate, SimRepair  # noqa: E402
from vclare.spec_repair import InconsistencyPair, SpecRepair  # noqa: E402

DEMO_FILE = Path(__file__).resolve().parent / "demo_cases.json"


def load_demo() -> Dict[str, Any]:
    with open(DEMO_FILE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def offline_repair(spec: str, pair: InconsistencyPair, value: str) -> str:
    """Deterministic stand-in for the LLM targeted-edit step.

    The real ``SpecRepair.repair`` calls an LLM. For the offline demo the wrong
    statement is annotated instead, so the control flow stays identical.
    """
    if value == "irrelevant":
        return spec
    wrong = pair.a2 if value == "source1" else pair.a1
    if wrong and wrong in spec:
        return spec.replace(
            wrong,
            wrong + "  [CONFIRMED DEFECT: to be rewritten by targeted repair]",
        )
    return spec


def build_candidates(payload: Dict[str, Any]) -> List[Candidate]:
    return [
        Candidate(
            candidate_id=item["candidate_id"],
            source=item["source"],
            outputs=item.get("outputs", {}),
            compiled=bool(item.get("compiled", True)),
        )
        for item in payload.get("candidates", [])
    ]


def run_spec_level(
    case: Dict[str, Any],
    arbiter: HumanArbiter,
    interactive: bool,
) -> Dict[str, Any]:
    pairs = [
        InconsistencyPair(
            a1=item["a1"],
            a2=item["a2"],
            defect_type=item.get("defect_type", ""),
            rationale=item.get("rationale", ""),
            index=int(item.get("index", 1)),
        )
        for item in case.get("pairs", [])
    ]

    if not interactive:
        arbiter.auto_answers[case["_question_id"]] = case["expected_decision"]

    repaired = case["spec"]
    trace: List[Dict[str, Any]] = []
    for pair in pairs:
        if pair.is_standalone:
            trace.append({"pair": pair.to_dict(), "skipped": "standalone"})
            continue
        question = SpecRepair.build_question(case["task_id"], pair, len(pairs))
        # keep the deterministic id so --interactive does not need a key press
        # for the demo, but still allow a real interactive answer
        question.question_id = case["_question_id"]
        decision = arbiter.ask(question, interactive=interactive)
        repaired = offline_repair(repaired, pair, decision.value)
        trace.append({"pair": pair.to_dict(), "decision": decision.to_dict()})
        if decision.value != "irrelevant":
            break

    return {
        "task_id": case["task_id"],
        "question_id": case["_question_id"],
        "original_spec": case["spec"],
        "repaired_spec": repaired,
        "trace": trace,
        "expected_decision": case["expected_decision"],
    }


def run_sim_level(
    case: Dict[str, Any],
    arbiter: HumanArbiter,
    interactive: bool,
) -> Dict[str, Any]:
    repair = SimRepair(case["test_cases"])
    candidates = build_candidates(case)
    clusters = repair.cluster(candidates)

    question = repair.build_question(
        case["task_id"], clusters, defect_type=case.get("defect_type", "")
    )
    if question is None:
        raise AssertionError("demo must produce a sim-level confirmation question")

    question.question_id = case["_question_id"]
    if not interactive:
        arbiter.auto_answers[case["_question_id"]] = case["expected_decision"]
    decision = arbiter.ask(question, interactive=interactive)
    selected = repair.select(clusters, decision)

    return {
        "task_id": case["task_id"],
        "question_id": case["_question_id"],
        "test_cases": case["test_cases"],
        "clusters": [cluster.to_dict() for cluster in clusters],
        "ranking": [cluster.cluster_id for cluster in clusters],
        "distinguishing": question.context.get("distinguishing"),
        "decision": decision.to_dict(),
        "selected_cluster": selected.cluster_id,
        "selected_candidate": selected.representative.candidate_id,
        "selected_source": selected.representative.source,
        "expected_decision": case["expected_decision"],
    }


def verify(spec_result: Dict[str, Any], sim_result: Dict[str, Any]) -> List[str]:
    problems: List[str] = []

    spec_trace = [item for item in spec_result["trace"] if item.get("decision")]
    if not spec_trace:
        problems.append("spec-level: no arbitrated pair")
    elif spec_trace[0]["decision"]["value"] != spec_result["expected_decision"]:
        problems.append(
            "spec-level: decision {} != expected {}".format(
                spec_trace[0]["decision"]["value"], spec_result["expected_decision"]
            )
        )

    clusters = sim_result["clusters"]
    if len(clusters) != 3:
        problems.append("sim-level: expected 3 clusters, found {}".format(len(clusters)))
    if clusters and clusters[0]["size"] != 4:
        problems.append("sim-level: top cluster should have 4 members")
    distinguishing = sim_result.get("distinguishing") or {}
    if distinguishing.get("test_case") != "t3":
        problems.append("sim-level: distinguishing test case should be t3")
    if distinguishing.get("cluster_1") == distinguishing.get("cluster_2"):
        problems.append("sim-level: clusters must disagree on the distinguishing case")
    if sim_result["decision"]["value"] != sim_result["expected_decision"]:
        problems.append("sim-level: arbitration did not return the expected decision")
    if sim_result["selected_candidate"] != "c1":
        problems.append(
            "sim-level: selected candidate {} != c1".format(
                sim_result["selected_candidate"]
            )
        )

    return problems


def print_summary(spec_result: Dict[str, Any], sim_result: Dict[str, Any]) -> None:
    print()
    print("=" * 72)
    print("VClare offline demo")
    print("=" * 72)

    decision = [
        item for item in spec_result["trace"] if item.get("decision")
    ][0]["decision"]
    print("Spec-Level Repair")
    print("  task            : {}".format(spec_result["task_id"]))
    print("  confirmation    : {} ({})".format(decision["label"], decision["value"]))
    print("  repaired spec   : {} chars".format(len(spec_result["repaired_spec"])))

    print()
    print("Sim-Level Repair")
    print("  task            : {}".format(sim_result["task_id"]))
    print(
        "  clusters        : {}".format(
            ", ".join(
                "C{}={} (MBR {})".format(c["cluster_id"], c["size"], c["score"])
                for c in sim_result["clusters"]
            )
        )
    )
    distinguishing = sim_result["distinguishing"]
    print(
        "  divergence      : {} -> C1={} / C2={}".format(
            distinguishing["test_case"],
            distinguishing["cluster_1"],
            distinguishing["cluster_2"],
        )
    )
    print("  confirmation    : {}".format(sim_result["decision"]["label"]))
    print(
        "  selected        : {} ({})".format(
            sim_result["selected_candidate"], sim_result["selected_cluster"]
        )
    )
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(description="VClare offline demo")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="answer the two confirmation questions in the terminal",
    )
    parser.add_argument("--json", help="write the full trace to this JSON file")
    parser.add_argument(
        "--log",
        default=str(Path(__file__).resolve().parent / "demo_decisions.json"),
        help="where to store the recorded decisions",
    )
    args = parser.parse_args()

    demo = load_demo()
    spec_cases = demo.get("spec_level", [])
    sim_cases = demo.get("sim_level", [])
    if not spec_cases or not sim_cases:
        print("demo data is incomplete", file=sys.stderr)
        return 2

    spec_case = dict(spec_cases[0])
    sim_case = dict(sim_cases[0])
    spec_case["_question_id"] = "demo::spec::1"
    sim_case["_question_id"] = "demo::sim::1"

    if os.path.exists(args.log):
        os.remove(args.log)
    arbiter = HumanArbiter(log_path=args.log)

    spec_result = run_spec_level(spec_case, arbiter, args.interactive)
    sim_result = run_sim_level(sim_case, arbiter, args.interactive)

    print_summary(spec_result, sim_result)

    problems = verify(spec_result, sim_result)
    if problems:
        print()
        print("VERIFICATION FAILED")
        for problem in problems:
            print("  - {}".format(problem))
        return 1

    print()
    print("VERIFICATION PASSED: both VClare confirmation points behaved as expected.")
    print("Decision log: {}".format(args.log))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "spec_level": spec_result,
                    "sim_level": sim_result,
                    "decisions": [d.to_dict() for d in arbiter.decisions],
                },
                handle,
                indent=2,
                ensure_ascii=False,
            )
        print("Full trace: {}".format(args.json))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
