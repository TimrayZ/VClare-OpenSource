"""VClare reference implementation.

VClare recovers design intent from defective hardware specifications using two
complementary paradigms:

* Spec-Level Repair: LLM-driven inconsistency mining followed by targeted
  specification editing.
* Sim-Level Repair: simulation-based behavioral clustering with optional
  human arbitration on a distinguishing test case.

This open-source package ships only the framework code. Datasets, generated
candidates, golden testbenches and experiment results are intentionally not
included.
"""

from .arbiter import (
    HumanArbiter,
    ArbitrationQuestion,
    ArbitrationOption,
    Decision,
)
from .spec_repair import InconsistencyPair, SpecRepair
from .sim_repair import Candidate, Cluster, SimRepair
from .pipeline import (
    AwaitingArbitration,
    PipelineConfig,
    PipelineRun,
    STAGES,
    VClarePipeline,
)

__all__ = [
    "HumanArbiter",
    "ArbitrationQuestion",
    "ArbitrationOption",
    "Decision",
    "InconsistencyPair",
    "SpecRepair",
    "Candidate",
    "Cluster",
    "SimRepair",
    "VClarePipeline",
    "PipelineConfig",
    "PipelineRun",
    "AwaitingArbitration",
    "STAGES",
]

__version__ = "1.0.0"
