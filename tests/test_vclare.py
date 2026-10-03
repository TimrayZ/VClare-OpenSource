"""Unit and end-to-end tests for the VClare reference implementation.

Run with::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vclare.arbiter import (  # noqa: E402
    BEHAVIOR_CHOICE,
    INCONSISTENCY_PAIR,
    ArbitrationOption,
    ArbitrationQuestion,
    Decision,
    HumanArbiter,
)
from vclare.sim_repair import Candidate, SimRepair  # noqa: E402
from vclare.spec_repair import InconsistencyPair, SpecRepair  # noqa: E402
from vclare.backends import OfflineBackend, PrecomputedSimulator, load_artifacts  # noqa: E402
from vclare.backends import OpenAIBackend  # noqa: E402
from vclare.iverilog import IverilogSimulator  # noqa: E402
from vclare.pipeline import (  # noqa: E402
    AwaitingArbitration,
    PIPELINE_VERSION,
    PipelineConfig,
    VClarePipeline,
    decision_from_payload,
)

DEMO_ARTIFACTS = PROJECT_ROOT / "examples" / "demo_artifacts.json"


def candidate(cid: str, outputs: dict, compiled: bool = True) -> Candidate:
    return Candidate(
        candidate_id=cid,
        source="// {}".format(cid),
        outputs=outputs,
        compiled=compiled,
    )


class TestSimRepair(unittest.TestCase):
    def setUp(self) -> None:
        self.tests = ["t1", "t2", "t3", "t4", "t5", "t6", "t7"]
        self.candidates = [
            candidate("a1", {"t1": "0", "t2": "0", "t3": "A"}),
            candidate("a2", {"t1": "0", "t2": "0", "t3": "A"}),
            candidate("a3", {"t1": "0", "t2": "0", "t3": "A"}),
            candidate("b1", {"t1": "0", "t2": "0", "t3": "B"}),
            candidate("b2", {"t1": "0", "t2": "0", "t3": "B"}),
            candidate("bad", {}, compiled=False),
        ]
        self.repair = SimRepair(self.tests)

    def test_clustering_groups_equal_outputs(self):
        clusters = self.repair.cluster(self.candidates)
        sizes = sorted(cluster.size for cluster in clusters)
        self.assertEqual(sizes, [1, 2, 3])

    def test_failed_candidates_each_form_a_singleton_cluster(self):
        bad1 = candidate("bad1", {}, compiled=False)
        bad2 = candidate("bad2", {}, compiled=False)
        # compiled but produced no output -> also a failure
        empty = candidate("empty", {}, compiled=True)
        good = candidate("good", {"t1": "0", "t2": "0", "t3": "A"})

        clusters = self.repair.cluster([good, bad1, bad2, empty])
        self.assertEqual(len(clusters), 4, "each failure must be its own cluster")
        failed = [c for c in clusters if c.representative.failed]
        self.assertEqual(len(failed), 3)
        self.assertTrue(all(c.size == 1 for c in failed))

    def test_mbr_ranking_orders_by_consistency(self):
        clusters = self.repair.cluster(self.candidates)
        scores = [cluster.score for cluster in clusters]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(clusters[0].size, 3)
        self.assertEqual(clusters[1].size, 2)

    def test_first_divergence_finds_earliest_case(self):
        clusters = self.repair.cluster(self.candidates)
        divergence = self.repair.first_divergence(clusters[0], clusters[1])
        self.assertIsNotNone(divergence)
        self.assertEqual(divergence[0], "t3")
        self.assertEqual(divergence[1], "A")
        self.assertEqual(divergence[2], "B")

    def test_behavior_question_is_built(self):
        clusters = self.repair.cluster(self.candidates)
        question = self.repair.build_question("task", clusters)
        self.assertIsNotNone(question)
        self.assertEqual(question.kind, BEHAVIOR_CHOICE)
        self.assertEqual(
            question.option_values(), ["cluster_1", "cluster_2", "abstain"]
        )

    def test_arbitration_can_override_mbr(self):
        clusters = self.repair.cluster(self.candidates)
        question = self.repair.build_question("task", clusters)
        arbiter = HumanArbiter()
        decision = arbiter.ask(question, interactive=False)
        # Non-interactive runs fall back to the first option: the top cluster.
        self.assertEqual(self.repair.select(clusters, decision).size, 3)

        second = [o for o in question.options if o.value == "cluster_2"][0]
        chosen = arbiter.resolve(question, "cluster_2")
        selected = self.repair.select(clusters, chosen)
        self.assertEqual(selected.outputs()["t3"], second.meta["output"])

    def test_single_viable_cluster_has_no_question(self):
        same = [
            candidate("a", {"t1": "0", "t2": "0", "t3": "A"}),
            candidate("b", {"t1": "0", "t2": "0", "t3": "A"}),
        ]
        clusters = self.repair.cluster(same)
        self.assertIsNone(self.repair.build_question("task", clusters))


class TestSpecRepair(unittest.TestCase):
    def make_pair(self) -> InconsistencyPair:
        return InconsistencyPair(
            a1="reset is active-high and synchronous.",
            a2="reset is active-low and asynchronous.",
            defect_type="contradictory",
            rationale="polarity and timing conflict",
            index=1,
        )

    def test_question_offers_source1_source2_and_discard(self):
        question = SpecRepair.build_question("task", self.make_pair(), 3)
        self.assertEqual(question.kind, INCONSISTENCY_PAIR)
        self.assertEqual(
            question.option_values(), ["source1", "source2", "irrelevant"]
        )
        self.assertIn("intended behavior", question.prompt)

    def test_standalone_pair_is_detected(self):
        pair = InconsistencyPair(a1="same", a2="same")
        self.assertTrue(pair.is_standalone)


class TestHumanArbiter(unittest.TestCase):
    def make_question(self, qid: str = "q1") -> ArbitrationQuestion:
        return ArbitrationQuestion(
            question_id=qid,
            kind=INCONSISTENCY_PAIR,
            task_id="task",
            prompt="pick one",
            options=[
                ArbitrationOption("source1", "Source 1 is correct"),
                ArbitrationOption("source2", "Source 2 is correct"),
                ArbitrationOption("irrelevant", "Irrelevant"),
            ],
        )

    def test_rejects_unknown_value(self):
        arbiter = HumanArbiter()
        question = self.make_question()
        with self.assertRaises(ValueError):
            arbiter.resolve(question, "bogus")

    def test_records_and_summarises(self):
        arbiter = HumanArbiter()
        question = self.make_question()
        arbiter.resolve(question, "source1")
        self.assertEqual(arbiter.pending_count, 0)
        self.assertEqual(arbiter.summary(), {INCONSISTENCY_PAIR: 1})

    def test_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "decisions.json")
            arbiter = HumanArbiter(log_path=path)
            arbiter.resolve(self.make_question(), "source2")
            self.assertTrue(os.path.exists(path))

            restored = HumanArbiter(log_path=path)
            restored.load()
            self.assertEqual(len(restored.decisions), 1)
            self.assertEqual(restored.decisions[0].value, "source2")

    def test_non_interactive_uses_first_option(self):
        arbiter = HumanArbiter()
        decision = arbiter.ask(self.make_question(), interactive=False)
        self.assertEqual(decision.value, "source1")
        self.assertEqual(decision.source, "auto_fallback")


class TestDemo(unittest.TestCase):
    def test_offline_demo_passes(self):
        demo = PROJECT_ROOT / "examples" / "run_demo.py"
        result = subprocess.run(
            [sys.executable, str(demo)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            "demo failed:\nSTDOUT:\n{}\nSTDERR:\n{}".format(
                result.stdout, result.stderr
            ),
        )
        self.assertIn("VERIFICATION PASSED", result.stdout)

    def test_webui_questions_match_the_pipeline(self):
        sys.path.insert(0, str(PROJECT_ROOT / "webui"))
        import server  # type: ignore

        questions = server.build_demo_questions()
        kinds = sorted(question["kind"] for question in questions)
        self.assertEqual(kinds, [BEHAVIOR_CHOICE, INCONSISTENCY_PAIR])


class TestPipeline(unittest.TestCase):
    def make_pipeline(self, tmp: str, mode: str = "hybrid", policy: str = "defer"):
        artifacts = load_artifacts(str(DEMO_ARTIFACTS))
        config = PipelineConfig(
            task_id="demo_counter_contradiction",
            spec="demo spec",
            output_dir=tmp,
            defect_type="contradictory",
            mode=mode,
            arbitration_policy=policy,
        )
        pipeline = VClarePipeline(
            config,
            OfflineBackend(artifacts),
            PrecomputedSimulator(artifacts["simulation_results"]),
            experiment_id="test_run",
        )
        return pipeline, artifacts

    def test_defer_then_resume_through_result_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, artifacts = self.make_pipeline(tmp)

            run = pipeline.run()
            self.assertEqual(run.status, "awaiting_arbitration")
            pending = json.loads(Path(run.pending_path).read_text(encoding="utf-8"))
            self.assertEqual(pending["stage"], "stage2_arbitrate_pairs")
            self.assertEqual(len(pending["questions"]), 1)

            # A human (or the web console) writes the answer into result JSON.
            question = pending["questions"][0]
            pipeline.store.record(
                decision_from_payload(
                    {
                        "question_id": question["question_id"],
                        "kind": question["kind"],
                        "task_id": question["task_id"],
                        "value": "source1",
                        "label": "Source 1 is correct",
                        "options": [o["value"] for o in question["options"]],
                        "context": question["context"],
                        "source": "human",
                    }
                )
            )

            run = pipeline.run()
            self.assertEqual(run.status, "awaiting_arbitration")
            pending = json.loads(Path(run.pending_path).read_text(encoding="utf-8"))
            self.assertEqual(pending["stage"], "stage8_arbitrate_divergence")
            question = pending["questions"][0]
            pipeline.store.record(
                decision_from_payload(
                    {
                        "question_id": question["question_id"],
                        "kind": question["kind"],
                        "task_id": question["task_id"],
                        "value": "cluster_1",
                        "label": "Cluster 1 behavior is correct",
                        "options": [o["value"] for o in question["options"]],
                        "context": question["context"],
                        "source": "human",
                    }
                )
            )

            run = pipeline.run()
            self.assertEqual(run.status, "completed")
            result = json.loads(Path(run.result_path).read_text(encoding="utf-8"))
            self.assertEqual(
                result["context"]["result"]["selected_candidate"], "c1"
            )
            self.assertEqual(result["version"], PIPELINE_VERSION)

    def test_fallback_policy_never_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, _ = self.make_pipeline(tmp, policy="fallback")
            run = pipeline.run()
            self.assertEqual(run.status, "completed")

    def test_unknown_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, _ = self.make_pipeline(tmp, mode="bogus")
            with self.assertRaises(ValueError):
                pipeline.run()

    def test_stage_cycle_snapshots_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, _ = self.make_pipeline(tmp, policy="fallback")
            pipeline.run()
            cycles = sorted((Path(tmp) / "cycles").glob("cycle_*.json"))
            self.assertGreaterEqual(len(cycles), 10)

    def test_cli_autofill_runs_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "run")
            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "run_pipeline.py"),
                    "--demo",
                    "--mode",
                    "hybrid",
                    "--autofill",
                    "--out",
                    out,
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.returncode,
                0,
                "pipeline failed:\n{}\n{}".format(result.stdout, result.stderr),
            )
            payload = json.loads(
                (Path(out) / "run_result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                payload["context"]["result"]["selected_candidate"], "c1"
            )

    def test_stage4e_skips_without_golden_tb(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, _ = self.make_pipeline(tmp, policy="fallback")
            pipeline.run()
            files = list((Path(tmp) / "cycles").glob("*stage4e*.json"))
            self.assertEqual(len(files), 1)
            snapshot = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(snapshot["data"]["skipped"], "no golden testbench provided")

    def test_extra_testcase_stage_appends_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, _ = self.make_pipeline(tmp, policy="fallback")
            run = pipeline.run()
            self.assertEqual(run.status, "completed")
            context = json.loads(
                Path(run.result_path).read_text(encoding="utf-8")
            )["context"]
            self.assertIn("raw_testcases2", context["extra_testcases"])
            self.assertIn("raw_testcases2", context["testbench"])
            files = list((Path(tmp) / "cycles").glob("*stage5e*.json"))
            self.assertEqual(len(files), 1)


class TestIverilogAdapter(unittest.TestCase):
    """Fidelity checks for the original iverilog path.

    These tests mock ``run_cmd``. No iverilog or vvp process is started.
    """

    def test_extract_check_lines_matches_original_semantics(self):
        from vclare.iverilog import extract_check_lines

        self.assertEqual(
            extract_check_lines("noise\n[check] case=1 out=1\nmore\n"),
            "[check] case=1 out=1",
        )
        # Without any [check] line the original keeps every non-empty line.
        self.assertEqual(extract_check_lines("a\n\nb\n"), "a\nb")

    def test_command_lines_and_timeouts_match_the_original(self):
        captured = []

        def fake_run_cmd(cmd, cwd, timeout):
            captured.append((cmd, timeout))
            return 0, "[check] case=1 out=1"

        with tempfile.TemporaryDirectory() as tmp:
            simulator = IverilogSimulator()
            with patch("vclare.iverilog.simulator.run_cmd", side_effect=fake_run_cmd):
                simulator.simulate("module tb; endmodule", ["module dut; endmodule"], tmp)

        self.assertEqual(len(captured), 2)
        compile_cmd, compile_timeout = captured[0]
        run_cmd, run_timeout = captured[1]
        self.assertIn("-g2012", compile_cmd)
        self.assertEqual(compile_timeout, 60)
        self.assertEqual(run_cmd.strip().split()[0], "vvp")
        self.assertEqual(run_timeout, 10)

    def test_grouping_is_by_exact_extracted_blob(self):
        outputs = [
            "[check] case=1 out=1",
            "[check] case=1 out=1",
            "[check] case=1 out=0",
        ]
        state = {"runs": 0}

        def fake_run_cmd(cmd, cwd, timeout):
            if "-o" in cmd:
                return 0, ""
            index = state["runs"]
            state["runs"] += 1
            return 0, outputs[index]

        with tempfile.TemporaryDirectory() as tmp:
            simulator = IverilogSimulator()
            with patch("vclare.iverilog.simulator.run_cmd", side_effect=fake_run_cmd):
                traces = simulator.simulate("tb", ["a", "b", "c"], tmp)

        self.assertEqual([t["outputs"]["run"] for t in traces], outputs)
        candidates = [
            Candidate(
                candidate_id=trace["candidate_id"],
                source="",
                outputs=trace["outputs"],
                compiled=trace["compiled"],
            )
            for trace in traces
        ]
        clusters = SimRepair(["run"]).cluster(candidates)
        self.assertEqual(sorted(cluster.size for cluster in clusters), [1, 2])


class _FakeLLM:
    def __init__(self):
        self.messages = None

    def chat(self, messages, temperature=0.0):
        self.messages = messages
        return "```verilog\nmodule TopModule(); endmodule\n```"


class _FakeMiningLLM(_FakeLLM):
    def chat(self, messages, temperature=0.0):
        self.messages = messages
        return (
            "Let me reason about the specification first.\n\n"
            "```json\n"
            '[{"source1": "x", "source2": "y"}]\n'
            "```"
        )


class _FakeRepairLLM(_FakeLLM):
    def chat(self, messages, temperature=0.0):
        self.messages = messages
        return "The conflicting clause is removed.\n```md\nfixed spec\n```"


class TestReleasedPrompts(unittest.TestCase):
    """The release ships the Verilog generation and testcase prompts."""

    def test_verilog_generation_prompts_are_present(self):
        from vclare import prompts

        self.assertIn(
            "Complete the following Verilog code", prompts.VERILOG_SYSTEM_PROMPT
        )
        self.assertIn(
            "Please write a module in Verilog RTL language",
            prompts.VERILOG_GENERATION_PROMPT,
        )
        self.assertIn("Other requirements:", prompts.VERILOG_EXTRA_ORDER_PROMPT)
        self.assertIn(
            "Here are some examples of RTL Verilog code",
            prompts.RTL_4_SHOT_EXAMPLES,
        )

    def test_testcase_prompt_is_present(self):
        from vclare import prompts

        self.assertIn(
            "I will give you a circuit specification and the header of the",
            prompts.TESTCASE_GENERATION_PROMPT,
        )
        self.assertIn(
            "Generate comprehensive test cases", prompts.TESTCASE_SYSTEM_PROMPT
        )

    def test_mining_prompt_is_shipped(self):
        from vclare import prompts

        self.assertIn(
            "Identify exactly 3 potential self-inconsistencies",
            prompts.MINING_USER_PROMPT,
        )
        self.assertIn(
            "Your job is to identify self-inconsistencies",
            prompts.MINING_SYSTEM_PROMPT,
        )
        self.assertIn('"source1"', prompts.MINING_SYSTEM_PROMPT)

    def test_mining_uses_released_prompt(self):
        fake = _FakeMiningLLM()
        backend = OpenAIBackend(fake)
        pairs = backend.mine_inconsistency("my spec", 3)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["a1"], "x")
        self.assertEqual(pairs[0]["a2"], "y")
        user = fake.messages[1]["content"]
        self.assertIn("Identify exactly 3 potential self-inconsistencies", user)
        self.assertIn("my spec", user)

    def test_repair_prompt_is_shipped(self):
        from vclare import prompts

        self.assertIn("known inconsistency", prompts.REPAIR_USER_PROMPT)
        self.assertIn("minimal", prompts.REPAIR_SYSTEM_PROMPT)

    def test_repair_uses_released_prompt(self):
        fake = _FakeRepairLLM()
        repair = SpecRepair(fake)
        decision = Decision(
            question_id="q1",
            kind=INCONSISTENCY_PAIR,
            task_id="task",
            value="source1",
            label="Source 1 is correct",
        )
        result = repair.repair(
            "spec body",
            InconsistencyPair(a1="A", a2="B", index=1),
            decision,
        )
        self.assertEqual(result, "fixed spec")
        user = fake.messages[1]["content"]
        self.assertIn("Resolution: 'source 1' is correct", user)
        self.assertIn('Correct statement  : "A"', user)
        self.assertIn('Incorrect statement: "B"', user)
        self.assertIn("spec body", user)

    def test_candidate_generation_uses_released_templates(self):
        fake = _FakeLLM()
        backend = OpenAIBackend(fake)
        backend.generate_candidates(
            "my spec", 1, "TopModule", "module TopModule();"
        )
        user = fake.messages[1]["content"]
        self.assertIn("Please write a module in Verilog RTL language", user)
        self.assertIn("Other requirements:", user)
        self.assertIn("<input_spec>", user)
        self.assertIn("my spec", user)
        self.assertIn("module TopModule();", user)

    def test_testbench_generation_uses_released_template(self):
        fake = _FakeLLM()
        backend = OpenAIBackend(fake)
        backend.generate_testbench(
            "my spec", [], "TopModule", "module TopModule();"
        )
        user = fake.messages[1]["content"]
        self.assertIn(
            "I will give you a circuit specification and the header of the", user
        )
        self.assertIn("my spec", user)
        self.assertIn("module TopModule();", user)
        self.assertIn("Strict output contract for integration", user)
        self.assertIn("[check]", user)

    def test_extra_testcase_prompt_is_shipped(self):
        from vclare import prompts

        self.assertIn(
            "Insert 2-4 additional testcases", prompts.EXTRA_TESTCASE_PROMPT
        )
        self.assertIn(
            "Provide only additional testcase blocks",
            prompts.EXTRA_TESTCASE_SYSTEM_PROMPT,
        )

    def test_extra_testcase_generation_uses_released_template(self):
        fake = _FakeLLM()
        backend = OpenAIBackend(fake)
        backend.generate_extra_testcases("my spec", "current tb", start_index=2)
        user = fake.messages[1]["content"]
        self.assertIn("Insert 2-4 additional testcases", user)
        self.assertIn("starting from raw_testcases2", user)
        self.assertIn("my spec", user)
        self.assertIn("current tb", user)


if __name__ == "__main__":
    unittest.main()
