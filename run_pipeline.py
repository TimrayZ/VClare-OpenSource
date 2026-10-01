"""Command line entry point for the multi-stage VClare pipeline.

The pipeline communicates with the human interface through two result files in
the run directory:

* ``arbitration_pending.json``   questions waiting for a human answer
* ``arbitration_decisions.json`` answers written by the human interface

The two confirmation points are never answered from an LLM response.

Examples::

    # Offline demo, hybrid mode, answers auto-filled from demo_artifacts.json
    python run_pipeline.py --demo --mode hybrid --autofill

    # Same run, but stop at the first confirmation and wait for a human
    python run_pipeline.py --demo --mode hybrid
    # -> the web console can attach to the run directory:
    python webui/server.py --state-dir saves/experiments/run_001

    # Interface-only run against a real specification, no LLM configured
    python run_pipeline.py --spec-file spec.txt --mode hybrid --backend disabled
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vclare.backends import (  # noqa: E402
    DisabledBackend,
    IcarusSimulator,
    OfflineBackend,
    OpenAIBackend,
    PrecomputedSimulator,
    load_artifacts,
)
from vclare.llm import LLMClient  # noqa: E402
from vclare.pipeline import (  # noqa: E402
    AwaitingArbitration,
    PipelineConfig,
    VClarePipeline,
    decision_from_payload,
)

DEMO_ARTIFACTS = PROJECT_ROOT / "examples" / "demo_artifacts.json"
DEMO_SPEC = (
    "Implement a 4-bit synchronous up counter.\n\n"
    "- q increments by one on every rising edge of clk.\n"
    "- reset is active-high and synchronous: when reset is 1, q is cleared to 0 "
    "on the next rising edge of clk.\n"
    "- reset is active-low and asynchronous: when reset is 0, q is cleared to 0 "
    "immediately.\n"
    "- q saturates at 4'b1111 and does not wrap around.\n"
)


def build_backend(args: argparse.Namespace, artifacts: Dict[str, Any]):
    if args.backend == "offline":
        return OfflineBackend(artifacts)
    if args.backend == "disabled":
        return DisabledBackend()
    llm = LLMClient(model=args.model, provider=args.provider, base_url=args.base_url)
    return OpenAIBackend(llm)


def build_simulator(args: argparse.Namespace, artifacts: Dict[str, Any]):
    choice = args.simulator
    if choice == "auto":
        choice = "precomputed" if args.backend == "offline" else "iverilog"
    if choice == "precomputed":
        return PrecomputedSimulator(artifacts.get("simulation_results", []))
    return IcarusSimulator()


def autofill(pipeline: VClarePipeline, artifacts: Dict[str, Any]) -> List[str]:
    """Answer the pending questions from the demo artifact file.

    This stands in for a human reviewer: the answers come from a result JSON
    file, not from an LLM.
    """
    expected = artifacts.get("expected", {})
    pending = pipeline.store.pending_path
    if not pending.exists():
        return []
    payload = json.loads(pending.read_text(encoding="utf-8"))
    filled = []
    for question in payload.get("questions", []):
        if question["kind"] == "inconsistency_pair":
            value = expected.get("spec_decision", "irrelevant")
        else:
            value = expected.get("sim_decision", "abstain")
        option = next(
            (o for o in question["options"] if o["value"] == value),
            question["options"][0],
        )
        decision = decision_from_payload(
            {
                "question_id": question["question_id"],
                "kind": question["kind"],
                "task_id": question["task_id"],
                "value": option["value"],
                "label": option["label"],
                "options": [o["value"] for o in question["options"]],
                "defect_type": question.get("defect_type", ""),
                "source": "autofill_demo",
                "context": question.get("context", {}),
            }
        )
        pipeline.store.record(decision)
        filled.append("{}={}".format(question["question_id"], option["value"]))
    return filled


def main() -> int:
    parser = argparse.ArgumentParser(description="VClare multi-stage pipeline")
    parser.add_argument("--task-id", default="demo_counter_contradiction")
    parser.add_argument("--spec-file", help="specification text file")
    parser.add_argument("--demo", action="store_true", help="use the synthetic demo input")
    parser.add_argument("--defect-type", default="contradictory")
    parser.add_argument(
        "--module-interface",
        default="",
        help="optional module header text injected into the generation prompts",
    )
    parser.add_argument("--mode", choices=["spec", "sim", "hybrid"], default="hybrid")
    parser.add_argument("--out", default="saves/experiments/run_001")
    parser.add_argument(
        "--backend", choices=["offline", "openai", "disabled"], default="offline"
    )
    parser.add_argument(
        "--simulator",
        choices=["auto", "iverilog", "precomputed"],
        default="auto",
        help="iverilog uses the original evaluate_tb_diff command lines",
    )
    parser.add_argument(
        "--golden-tb",
        default=None,
        help="optional golden testbench; enables stage4e via score.py::judge_task",
    )
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--model", default="gpt-5.4-nano")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--num-candidates", type=int, default=10)
    parser.add_argument("--max-pairs", type=int, default=3)
    parser.add_argument("--max-stage", type=int, default=None)
    parser.add_argument(
        "--policy",
        choices=["defer", "fallback"],
        default="defer",
        help="what to do when no human answer exists yet",
    )
    parser.add_argument(
        "--autofill",
        action="store_true",
        help="offline demo only: answer confirmations from demo_artifacts.json",
    )
    args = parser.parse_args()

    artifacts = load_artifacts(str(DEMO_ARTIFACTS)) if args.backend == "offline" else {}

    if args.spec_file:
        spec = Path(args.spec_file).read_text(encoding="utf-8")
    elif args.demo:
        spec = DEMO_SPEC
    else:
        spec = DEMO_SPEC
        print("[pipeline] no --spec-file or --demo given, falling back to the demo spec")

    config = PipelineConfig(
        task_id=args.task_id,
        spec=spec,
        output_dir=args.out,
        defect_type=args.defect_type,
        num_candidates=args.num_candidates,
        max_pairs=args.max_pairs,
        mode=args.mode,
        arbitration_policy=args.policy,
        golden_tb_path=args.golden_tb,
        module_interface=args.module_interface,
    )
    pipeline = VClarePipeline(
        config,
        build_backend(args, artifacts),
        build_simulator(args, artifacts),
        experiment_id=Path(args.out).name,
    )

    waits = 0
    while True:
        run = pipeline.run(max_stage=args.max_stage)
        print(
            "[pipeline] status={} completed={}".format(
                run.status, ", ".join(run.completed_stages) or "(none)"
            )
        )
        if run.status != "awaiting_arbitration":
            break

        waits += 1
        if waits > 8:
            print(
                "[pipeline] still waiting after {} confirmations; stopping to avoid "
                "a resume loop.".format(waits)
            )
            return 1
        print("[pipeline] waiting for human confirmation: {}".format(run.pending_path))
        if not args.autofill:
            print(
                "[pipeline] answer through the web console, or write "
                "arbitration_decisions.json, then re-run to resume."
            )
            break

        filled = autofill(pipeline, artifacts)
        print("[pipeline] auto-filled demo decisions: {}".format(", ".join(filled)))

    if run.result_path and Path(run.result_path).exists():
        payload = json.loads(Path(run.result_path).read_text(encoding="utf-8"))
        result = payload.get("context", {}).get("result") or {}
        print("[pipeline] run_result.json: {}".format(run.result_path))
        print("[pipeline] selected: {}".format(result.get("selected_candidate") or "(spec only)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
