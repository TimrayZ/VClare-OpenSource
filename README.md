# VClare

This Repo is an opensourced implementation of VClare framework. The related paper 
is recently accepted by Neurips workshop on AI for Chip Design.

VClare is a repair framework for Verilog generation from imperfect hardware
specifications. **Contradictions**, **incompleteness** and **vagueness** are
common in hardware specifications and substantially reduce the correctness of
LLM-generated RTL. VClare recovers the intended design through two complementary
paths:

1. **Spec-Level Repair**: semantic inconsistency mining at the specification
   text level, followed by a single human confirmation and a minimal targeted
   edit produced by the LLM.
2. **Sim-Level Repair**: the specification is left unchanged. Instead, multiple
   implementations are sampled from the specification, an automated testbench is
   generated and simulated, the implementations are clustered by behavioral
   equivalence, and the clusters are ranked by an MBR consistency score. When
   several viable behavioral clusters remain, the framework requests a human
   confirmation on the test case that best distinguishes the first and second
   ranked clusters.

> This repository contains framework code only, so that readers can follow the
> framework stably in a serial order. It does not contain the original or
> defect-injected specifications (the released benchmark datasets can be plugged
> in manually), and it does not contain LLM-generated candidate implementations.
> The artifact that runs out of the box is a **synthetic demo** used to validate
> the flow and the human interface. Reproducing the experiments requires
> parallelizing this framework, and we recommend supplying human feedback as a
> batch of JSON files after the LLM stages; otherwise the experiment process is
> extremely long.

---

## 1. The VClare Method

### 1.1 Spec-Level Repair

1. **Semantic inconsistency mining**: the LLM reads the original specification
   `S_orig` and returns up to `m` inconsistency pairs `(a1, a2)`. If a vagueness
   or incompleteness issue has no second quotable statement, the model sets
   `a2 = a1` and marks it as a standalone issue.
2. **Human confirmation**: the pairs are presented to an engineer. The engineer
   does not rewrite the specification and does not write test cases; only a
   minimal signal is required:
   - `source1`: `a1` reflects the intended behavior, `a2` should be modified;
   - `source2`: `a2` reflects the intended behavior, `a1` should be modified;
   - `irrelevant`: the pair is not a genuine defect and should be discarded.
3. **Targeted repair**: for each confirmed pair, the LLM modifies only the
   statement judged to be incorrect and leaves all other content unchanged,
   producing the repaired specification `S_repair`.

Code: `vclare/spec_repair.py`.

### 1.2 Sim-Level Repair

1. **Candidate generation**: sample `N` Verilog implementations
   `{c_1, ..., c_N}` from the specification (`S_repair` or the original
   `S_orig`) at temperature > 0.
2. **Automated testbench**: the LLM generates a testbench `T` containing
   multiple test cases.
3. **Simulation and clustering**: each candidate is simulated on `T` with
   Icarus Verilog. Candidates that fail to compile or produce no output become
   singleton clusters; the remaining candidates are grouped into behavioral
   equivalence clusters `{C_1, ..., C_k}` when their outputs match on every test
   case.
4. **MBR consistency ranking**: candidates are scored by the formula in
   Section 1.4, clusters are ranked by their best score, and `C_1` is the
   top-ranked cluster.
5. **Optional human confirmation**: when `k > 1`, the framework finds the first
   test case `t_d` on which `C_1` and `C_2` produce different outputs and
   presents this distinguishing point to the engineer:
   - `cluster_1`: adopt the behavior of `C_1` on this test case;
   - `cluster_2`: adopt the behavior of `C_2` on this test case;
   - `abstain`: no human signal is available, so the framework degrades to
     standard VRank selection (that is, it takes `C_1`).

Code: `vclare/sim_repair.py`.

### 1.3 Complementarity of the Two Paradigms

- Spec-Level Repair relies on the LLM's **long-context localization ability**.
  It works best when the specification is short, the defect is localized, and an
  explicit contradiction is present. As the specification grows, localization
  becomes unreliable and can even produce spurious edits that make the
  specification worse.
- Sim-Level Repair does not require the LLM to locate the defect. It relies on
  **execution-level behavioral consensus**, which is more robust for long,
  multi-module specifications.
- The two paradigms can be used independently or sequentially: Spec-Level Repair
  first, followed by behavioral validation of the generated implementations by
  Sim-Level Repair.

### 1.4 MBR Consistency Score

For a candidate `c`:

```
R(c) = n - sum_{c' in C} l(c, c')
```

where `l(c, c') = 1` if `c` and `c'` differ on any test case (or if either fails
simulation), and 0 otherwise. A cluster is scored by its best member, matching
the cluster ranking used by VRank.

---

## 2. Experimental Methodology

This section describes the setup of the automated experiments in the paper.
**The data and results are not part of this repository.**

### 2.1 Dataset Construction

Two benchmarks are constructed by injecting three types of semantic defects into
correct specifications:

| Dataset | Source | Scale | Characteristics |
| --- | --- | --- | --- |
| VerilogEval-Defect | VerilogEval-human | 156 single-module tasks x 3 defect types | Short specifications, suitable for fine-grained analysis |
| ComplexVDB-Defect | ComplexVDB / ReflectBench | 53 multi-module tasks x 3 defect types, covering 8 design domains | Long specifications with substantial structural redundancy, closer to real engineering documents |

The three injected defect types:

- **Contradiction**: conflicting statements in different sections, for example
  requiring both synchronous and asynchronous reset, or both active-high and
  active-low polarity.
- **Incompleteness**: missing edge cases or constraints, for example overflow
  handling, reset behavior, or invalid-input handling.
- **Vagueness**: missing precise behavioral descriptions, using wording with
  multiple interpretations.

For every task, the original **unmodified** specification is kept as a
reference. The VerilogEval golden testbench is **used only in the final
evaluation stage** and does not participate in the repair process.

### 2.2 Models and Implementation

| Item | Configuration |
| --- | --- |
| Backbone LLM | `deepseek-v4-flash` (DS) and `gpt-5.4-nano` (GPT) |
| Temperature | default |
| Reasoning effort | DS = low, GPT = medium |
| Simulator | Icarus Verilog (iverilog) v13.0 |
| Repetitions | 5 independent runs per configuration, to account for LLM non-determinism |
| Machine | 2 x Xeon Gold 6126, 280 GB RAM |

### 2.3 Evaluation Metric

The primary metric is **pass@k** with `n = 10`:

```
pass@k = E_problems[ 1 - C(n - c, k) / C(n, k) ]
```

where `n` is the number of sampled candidates and `c` is the number of
candidates that pass the golden testbench. After clustering, pass@k reports
whether the selected top-k candidates contain a correct implementation.

### 2.4 Baselines and Repair Configurations

**Baselines**

- **No Repair (Original)**: generate Verilog directly from the defective
  specification without any repair.
- **Blind Fix**: tell the LLM only that the specification may be defective and
  let it repair the specification zero-shot before generating code, without
  structured mining or behavioral validation.
- **VRank**: the original VRank pipeline, which performs execution-based
  behavioral clustering and ranking without test-case arbitration.
- **SpecFix-no-oracle**: an adaptation of the software-domain SpecFix to
  Verilog. It first generates multiple implementations, selects the dominant
  behavioral cluster, revises the specification from that cluster, and
  regenerates code from the revised specification.

**VClare Configurations**

- **Spec-Level Repair**: mining + confirmation + targeted repair, followed by
  generating a single implementation from the repaired specification.
- **Sim-Level Repair**: sampling candidates directly from the defective
  original specification, followed by clustering and optional arbitration.
- **Hybrid Repair (NA)**: Spec-Level Repair first, then Sim-Level Repair, with
  no arbitration in Sim-Level Repair.
- **Hybrid Repair**: the full sequential configuration, Spec-Level Repair plus
  Sim-Level Repair, with test-case arbitration enabled.

### 2.5 Experimental Procedure

1. Inject one defect type into each task to obtain a defective specification
   `S_orig`.
2. Spec-Level: mine inconsistency pairs -> human confirmation -> targeted repair,
   producing `S_repair`.
3. Sim-Level: sample 10 candidates from `S_orig` or `S_repair` -> generate a
   testbench -> simulate -> cluster -> MBR ranking -> optional arbitration ->
   select an implementation.
4. Evaluate the selected implementation with the golden testbench and compute
   pass@k.
5. Repeat each configuration 5 times and average.

### 2.6 Main Observations

- Spec-Level Repair yields the largest gains on **contradiction** defects, but
  its localization ability degrades as specifications grow longer and it can
  produce harmful edits.
- Sim-Level Repair improves consistently on **all defect types** and is more
  robust for long, multi-module specifications.
- Hybrid performs best on single-module tasks; on multi-module tasks, using
  Sim-Level Repair directly is often more reliable than applying Spec-Level
  Repair first.
- Because vague and incomplete defects lack a ground-truth statement to compare
  against, automated repair tends to introduce spurious edits. These scenarios
  benefit more from lightweight human confirmation.

---

## 3. Repository Layout

```
VClare-OpenSource/
├── README.md                      English documentation
├── README-zh.md                   Chinese documentation
├── LICENSE
├── requirements.txt               The framework itself has no dependencies
├── .env.example
├── vclare/
│   ├── arbiter.py                 Data structures and decision log for the two human confirmation points
│   ├── spec_repair.py             Spec-Level Repair
│   ├── sim_repair.py              Sim-Level Repair (clustering + MBR + divergence detection)
│   ├── pipeline.py                Multi-stage pipeline + result-JSON arbitration bridge
│   ├── backends.py                Pluggable backends for the LLM stages and the simulator
│   ├── prompts.py                 LLM prompts included in this release (mining / RTL generation / testcase generation)
│   ├── llm.py                     Minimal OpenAI-compatible client (standard library only)
│   └── iverilog/
│       ├── score.py               Golden testbench judgement
│       ├── evaluate_tb_diff.py    Behavioral difference evaluation
│       └── simulator.py           Pipeline adapter with identical commands, timeouts and grouping
├── run_pipeline.py                Command-line entry point for the multi-stage pipeline
├── webui/
│   ├── index.html                 Human arbitration console
│   ├── styles.css
│   ├── app.js
│   └── server.py                  Local server + JSON API
├── examples/
│   ├── demo_cases.json            Synthetic demo data
│   ├── demo_artifacts.json        Precomputed artifacts for the offline pipeline
│   └── run_demo.py                Offline end-to-end demo
└── tests/
    └── test_vclare.py             Unit tests + demo end-to-end test
```

---

## 4. Quick Start

The framework itself requires no third-party Python packages. Python 3.9+ is
required.

```bash
# 1. Run the offline demo
python examples/run_demo.py

# 2. Run the tests
python -m unittest discover -s tests -v

# 3. Start the human arbitration console
python webui/server.py --port 8770 --open

# 4. Multi-stage pipeline: confirmations are auto-filled from demo_artifacts.json
python run_pipeline.py --demo --mode hybrid --autofill

# 5. Attach the console to a pipeline run directory and read/write its result JSON
python webui/server.py --port 8771 --state-dir saves/experiments/run_001
```

Console URL: `http://127.0.0.1:8770/`

The demo decision log is written to `examples/demo_decisions.json`, and decisions
made in the console are written to `webui/decisions.json`. Both are ignored by
`.gitignore`.

---

## 5. Human Confirmation Console

The console places the two confirmation points required by VClare in a single
workbench.

![Spec-Level confirmation](docs/spec-level.png)

![Sim-Level confirmation](docs/sim-level.png)

**Confirmation point 1: Spec-Level.** A single inconsistency pair is shown and
the engineer only has to state which statement reflects the intended behavior:

- `Source 1 is correct`
- `Source 2 is correct`
- `Irrelevant, discard this pair`

**Confirmation point 2: Sim-Level.** The outputs of `C_1` and `C_2` are compared
across all test cases, the first diverging test case is highlighted, and the
engineer chooses which behavior to adopt:

- `Cluster 1 behavior is correct`
- `Cluster 2 behavior is correct`
- `No confirmation available` (degrades to standard MBR selection)

The console also shows the MBR score and member count of each cluster, the
Verilog source of the representative candidate, and the decision audit log on
the right.

### Programmatic Interface

```python
from vclare.arbiter import HumanArbiter
from vclare.sim_repair import Candidate, SimRepair
from vclare.spec_repair import InconsistencyPair, SpecRepair

arbiter = HumanArbiter(log_path="decisions.json")

# Confirmation point 1
pair = InconsistencyPair(a1="...", a2="...", defect_type="contradictory", index=1)
question = SpecRepair.build_question("task_id", pair, total_pairs=3)
decision = arbiter.ask(question, interactive=False)  # or interactive=True for the terminal

# Confirmation point 2
repair = SimRepair(test_cases=["t1", "t2"])
clusters = repair.cluster(candidates)
question = repair.build_question("task_id", clusters)
decision = arbiter.ask(question, interactive=True)
selected = repair.select(clusters, decision)
```

With `interactive=False`, if `auto_answers` contains no matching answer, the
first option is selected and marked `auto_fallback`, so unattended CI runs do
not block.

---

## 6. Multi-Stage Pipeline and Result JSON

`run_pipeline.py` retains the full research-level stage decomposition. It does
not need to run to completion in one go: every stage writes a result JSON, and
the two confirmation points are handed over to the human console through JSON
files.

![Pipeline-attached console](docs/pipeline-console.png)

### Stage Decomposition

| cycle | stage | type | description |
| --- | --- | --- | --- |
| 1 | `stage0_load_input` | local | Load specification and configuration |
| 2 | `stage1_mine_inconsistency` | LLM | Mine inconsistency pairs |
| 3 | `stage2_arbitrate_pairs` | **human** | Confirm Source 1 / Source 2 / irrelevant |
| 4 | `stage3_repair_spec` | LLM | Targeted specification repair |
| 5 | `stage4_generate_candidates` | LLM | Sample N candidate implementations |
| 6 | `stage4e_evaluate_golden_tb` | EDA (optional) | Run candidates against the golden testbench; skipped by default |
| 7 | `stage5_generate_testbench` | LLM | Generate the automated testbench |
| 8 | `stage6_simulate_and_cluster` | EDA | iverilog simulation + behavioral clustering |
| 9 | `stage7_rank_mbr` | local | MBR consistency ranking |
| 10 | `stage8_arbitrate_divergence` | **human** | Confirm behavior on the first diverging test case |
| 11 | `stage9_select_and_report` | local | Select the implementation and summarize results |

`--mode spec` runs only the Spec-Level stages, `--mode sim` runs only the
Sim-Level stages, and `--mode hybrid` runs the full flow.

### Result JSON Contract

The run directory (`--out`) contains:

```
saves/experiments/run_001/
├── state.json                    Recoverable intermediate state
├── arbitration_pending.json      pipeline -> console: pending questions
├── arbitration_decisions.json    console -> pipeline: human answers
├── cycles/
│   ├── cycle_01_stage0_load_input.json
│   ├── ...
│   └── cycle_11_stage9_select_and_report.json
└── run_result.json               Final result and full context
```

`arbitration_pending.json`:

```json
{
  "version": 1,
  "experiment_id": "run_001",
  "stage": "stage2_arbitrate_pairs",
  "questions": [
    {
      "question_id": "run_001::stage2::1",
      "kind": "inconsistency_pair",
      "task_id": "my_task",
      "prompt": "...",
      "options": [{"value": "source1", "label": "Source 1 is correct"}],
      "context": {"index": 1, "a1": "...", "a2": "..."}
    }
  ]
}
```

`arbitration_decisions.json`:

```json
{
  "version": 1,
  "decisions": [
    {
      "question_id": "run_001::stage2::1",
      "kind": "inconsistency_pair",
      "value": "source1",
      "source": "human",
      "context": {"index": 1}
    }
  ]
}
```

Question IDs are derived from `experiment_id + stage + index` and are stable, so
the pipeline can be stopped and restarted at any time and will read the same
answers.

### Running the Pipeline

```bash
# 1) Run the full flow offline; confirmations are auto-filled from demo_artifacts.json
python run_pipeline.py --demo --mode hybrid --autofill

# 2) Human in the loop: run until the first confirmation point and stop
python run_pipeline.py --demo --mode hybrid --out saves/experiments/run_001
#    Attach the console to this run directory
python webui/server.py --port 8771 --state-dir saves/experiments/run_001
#    After answering in the console, re-run the same command to resume
python run_pipeline.py --demo --mode hybrid --out saves/experiments/run_001

# 3) Interface only, without running the LLM stages
python run_pipeline.py --spec-file spec.txt --mode hybrid --backend disabled
```

With `--policy fallback`, the pipeline uses a deterministic fallback when no
human answer is available: an inconsistency pair falls back to `irrelevant`
(the specification is not modified) and a behavioral divergence falls back to
`abstain` (standard MBR selection). This is the degradation path when no human
input is available.

### Replacing the Backends

- `vclare/backends.py::OpenAIBackend` covers all LLM stages;
- `DisabledBackend` is used to validate the interface only;
- `IcarusSimulator` runs real iverilog, while `PrecomputedSimulator` replays
  existing simulation results.

---

## 7. Runtime Requirements and Simulation Integration

### Runtime Requirements

- Python 3.9+
- The framework, demo, web console and tests use only the standard library
- Real simulation requires a local installation of Icarus Verilog v13.0

### LLM Stages and Prompts

The LLM stages (inconsistency mining, targeted repair, candidate generation and
testbench generation) are defined by the `LLMBackend` interface in
`vclare/backends.py`. The release ships `OfflineBackend`, which replays
precomputed artifacts, and `DisabledBackend`, which only validates the
interface.

The LLM prompts included in this release are collected in `vclare/prompts.py`:

| Prompt | Stage | Purpose |
| --- | --- | --- |
| `MINING_SYSTEM_PROMPT`, `MINING_USER_PROMPT` | `stage1_mine_inconsistency` | Semantic inconsistency mining |
| `REPAIR_SYSTEM_PROMPT`, `REPAIR_USER_PROMPT` | `stage3_repair_spec` | Targeted repair of a confirmed inconsistency pair |
| `VERILOG_SYSTEM_PROMPT`, `VERILOG_GENERATION_PROMPT`, `VERILOG_EXTRA_ORDER_PROMPT`, `VERILOG_IF_PROMPT`, `RTL_4_SHOT_EXAMPLES` | `stage4_generate_candidates` | Verilog RTL generation, including four in-context examples |
| `TESTCASE_GENERATION_PROMPT`, `TESTCASE_SYSTEM_PROMPT` | `stage5_generate_testbench` | Testcase generation |

The blind-fix prompt used by the Blind Fix baseline (repairing a specification
without any mined inconsistency) is not part of the VClare framework and is
therefore not included in this repository.

The Verilog generation prompts are adapted from VerilogCoder, and the
attribution is kept in `vclare/prompts.py`.

### Icarus Verilog Integration

`vclare/iverilog/` provides the Icarus Verilog integration:

| File | Purpose |
| --- | --- |
| `vclare/iverilog/score.py` | Golden testbench judgement: compile and run, then decide pass/fail from success markers |
| `vclare/iverilog/evaluate_tb_diff.py` | Behavioral difference evaluation: extract outputs from `[check]` lines and group by the whole extracted output |
| `vclare/iverilog/simulator.py` | Pipeline adapter that reuses the same commands, timeouts and grouping semantics |

The simulation path follows these conventions:

- Compile command: `iverilog -g2012 -o <vvp> <tb> <dut>` with `shell=True`, run
  from the task directory;
- Run command: `vvp <vvp>` with `shell=True`;
- Timeouts: 60s for compilation, 10s for simulation;
- Output extraction: `extract_check_lines` keeps only lines containing `[check]`;
  if there are none, it keeps every non-empty line;
- Grouping: candidates are grouped by the exact **whole extracted string**, not
  by parsed fields.

Relevant command-line flags:

```bash
--simulator iverilog    # use the Icarus Verilog simulation path
--simulator precomputed # replay existing simulation results
--golden-tb <path>      # optional, enables the golden testbench stage
```

---

## 8. License

The framework code is released under the MIT License; see `LICENSE`. The
repository does not include data files from VerilogEval, ComplexVDB or
ReflectBench; please follow their respective licenses and citation requirements
when using those datasets.

---

## 9. Citation

If this framework is useful for your research, please cite:

> Zhuorui Zhao, Bing Li, Yu Li, Zheyu Yan, and Ulf Schlichtmann.
> "VClare: Resolving Imperfect Specifications in LLM-Based Verilog Generation."
> NeurIPS Workshop on AI for Chip Design, 2026.

```bibtex
@inproceedings{zhao2026vclare,
  title     = {VClare: Resolving Imperfect Specifications in LLM-Based Verilog Generation},
  author    = {Zhuorui Zhao and Bing Li and Yu Li and Zheyu Yan and Ulf Schlichtmann},
  booktitle = {NeurIPS Workshop on AI for Chip Design},
  year      = {2026},
  note      = {Accepted}
}
```
