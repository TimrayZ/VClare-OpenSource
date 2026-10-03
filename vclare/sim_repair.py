"""Sim-Level Repair: behavioral clustering, MBR ranking and arbitration.

Candidates are generated from the (possibly defective) specification and
simulated on an automatically generated testbench. Candidates that produce the
same outputs on every test case form a behavioral cluster. Clusters are ranked
by the Minimum Bayes Risk consistency score used by VRank:

    R(c) = n - sum_over_candidates l(c, c')

where ``l`` is 1 when the two candidates disagree on any test case (or when
either fails to simulate), and 0 otherwise.

When more than one viable cluster remains, VClare finds the first test case on
which the two top clusters diverge and asks a human engineer which behavior is
intended. This is the second of the two confirmation points. The LLM
arbitration prompt used during data collection is not part of this release.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .arbiter import (
    BEHAVIOR_CHOICE,
    ArbitrationOption,
    ArbitrationQuestion,
    Decision,
    HumanArbiter,
)

FAILED = "<failed>"


@dataclass
class Candidate:
    """One generated Verilog implementation and its simulation trace."""

    candidate_id: str
    source: str
    outputs: Dict[str, str] = field(default_factory=dict)
    compiled: bool = True
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """True when the candidate failed to compile or produced no output."""
        if not self.compiled:
            return True
        if not self.outputs:
            return True
        return all(not str(value).strip() for value in self.outputs.values())

    def signature(self, test_cases: Sequence[str]) -> Tuple[str, ...]:
        if not self.compiled:
            return (FAILED,)
        return tuple(str(self.outputs.get(case, FAILED)) for case in test_cases)

    def output_for(self, test_case: str) -> str:
        if self.failed:
            return FAILED
        return str(self.outputs.get(test_case, FAILED))


@dataclass
class Cluster:
    """A group of behaviorally equivalent candidates."""

    cluster_id: int
    members: List[Candidate]
    tests: Sequence[str]
    score: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def representative(self) -> Candidate:
        """Prefer a candidate that compiled, otherwise the first member."""
        for candidate in self.members:
            if candidate.compiled:
                return candidate
        return self.members[0]

    def outputs(self) -> Dict[str, str]:
        representative = self.representative
        return {
            case: representative.output_for(case)
            for case in self.tests
        }

    def to_dict(self) -> Dict[str, Any]:
        representative = self.representative
        return {
            "cluster_id": self.cluster_id,
            "size": self.size,
            "score": self.score,
            "compiled": representative.compiled,
            "representative": representative.candidate_id,
            "members": [c.candidate_id for c in self.members],
            "source": representative.source,
            "outputs": self.outputs(),
        }


def _disagree(left: Candidate, right: Candidate, tests: Sequence[str]) -> int:
    if left is right:
        return 0
    if left.failed or right.failed:
        return 1
    return int(left.signature(tests) != right.signature(tests))


class SimRepair:
    """Cluster candidates, rank them, and arbitrate on divergence."""

    def __init__(self, test_cases: Sequence[str]) -> None:
        self.test_cases = list(test_cases)

    # --------------------------------------------------------------- clustering
    def cluster(self, candidates: Sequence[Candidate]) -> List[Cluster]:
        """Group candidates by behavioral equivalence and rank by MBR score."""
        if not candidates:
            return []

        size = len(candidates)

        def score_for(member: Candidate) -> int:
            return size - sum(
                _disagree(member, other, self.test_cases) for other in candidates
            )

        clusters: List[Cluster] = []

        # Candidates that fail to compile or produce no output are each placed
        # in a singleton cluster (paper Sec. 3.2.1).
        for candidate in candidates:
            if candidate.failed:
                clusters.append(
                    Cluster(
                        cluster_id=len(clusters),
                        members=[candidate],
                        tests=self.test_cases,
                        score=score_for(candidate),
                        meta={"failed": True},
                    )
                )

        # Candidates that simulated successfully are grouped by behavioral
        # equivalence: identical outputs on all test cases.
        buckets: Dict[Tuple[str, ...], List[Candidate]] = {}
        for candidate in candidates:
            if candidate.failed:
                continue
            buckets.setdefault(candidate.signature(self.test_cases), []).append(
                candidate
            )

        for members in buckets.values():
            # Score the cluster by its best member, matching MBR/VRank ranking.
            member_scores = [score_for(member) for member in members]
            clusters.append(
                Cluster(
                    cluster_id=len(clusters),
                    members=members,
                    tests=self.test_cases,
                    score=max(member_scores),
                    meta={"member_scores": member_scores},
                )
            )

        clusters.sort(key=lambda c: (c.score, c.size), reverse=True)
        return clusters

    # ------------------------------------------------------------- divergence
    def first_divergence(
        self,
        cluster_a: Cluster,
        cluster_b: Cluster,
    ) -> Optional[Tuple[str, str, str]]:
        """Return ``(test_case, output_a, output_b)`` for the first split."""
        outputs_a = cluster_a.outputs()
        outputs_b = cluster_b.outputs()
        for case in self.test_cases:
            if outputs_a.get(case) != outputs_b.get(case):
                return case, outputs_a.get(case, FAILED), outputs_b.get(case, FAILED)
        return None

    # ------------------------------------------------------------ arbitration
    def build_question(
        self,
        task_id: str,
        clusters: Sequence[Cluster],
        defect_type: str = "",
    ) -> Optional[ArbitrationQuestion]:
        """Build the second VClare confirmation question, if a split exists."""
        viable = [c for c in clusters if not c.representative.failed]
        if len(viable) < 2:
            return None

        top, second = viable[0], viable[1]
        divergence = self.first_divergence(top, second)
        if divergence is None:
            return None
        test_case, output_top, output_second = divergence

        options = [
            ArbitrationOption(
                value="cluster_1",
                label="Cluster 1 behavior is correct",
                detail="{} -> {}".format(test_case, output_top),
                meta={"cluster_id": top.cluster_id, "output": output_top},
            ),
            ArbitrationOption(
                value="cluster_2",
                label="Cluster 2 behavior is correct",
                detail="{} -> {}".format(test_case, output_second),
                meta={"cluster_id": second.cluster_id, "output": output_second},
            ),
            ArbitrationOption(
                value="abstain",
                label="No confirmation available",
                detail="Fall back to the standard MBR-ranked selection.",
            ),
        ]

        return ArbitrationQuestion(
            kind=BEHAVIOR_CHOICE,
            task_id=task_id,
            defect_type=defect_type,
            prompt=(
                "The two highest-ranked behavioral clusters disagree on "
                "test case '{}'. Which expected output is intended?".format(test_case)
            ),
            options=options,
            context={
                "test_case": test_case,
                "clusters": [c.to_dict() for c in clusters],
                "ranking": [c.cluster_id for c in clusters],
                "distinguishing": {
                    "test_case": test_case,
                    "cluster_1": output_top,
                    "cluster_2": output_second,
                },
            },
        )

    def select(
        self,
        clusters: Sequence[Cluster],
        decision: Optional[Decision] = None,
    ) -> Cluster:
        """Return the selected cluster, honouring a human confirmation if given."""
        if not clusters:
            raise ValueError("no clusters to select from")
        if decision is None or decision.value == "abstain":
            return clusters[0]

        if decision.value == "cluster_1":
            target = decision.context.get("distinguishing", {})
            wanted = target.get("cluster_1")
            for cluster in clusters:
                if wanted is not None and cluster.outputs().get(
                    target.get("test_case", ""), None
                ) == wanted:
                    return cluster
            return clusters[0]

        if decision.value == "cluster_2":
            target = decision.context.get("distinguishing", {})
            wanted = target.get("cluster_2")
            for cluster in clusters:
                if wanted is not None and cluster.outputs().get(
                    target.get("test_case", ""), None
                ) == wanted:
                    return cluster
            return clusters[1] if len(clusters) > 1 else clusters[0]

        raise ValueError("unknown arbitration value {!r}".format(decision.value))

    # ---------------------------------------------------------------- pipeline
    def run(
        self,
        task_id: str,
        candidates: Sequence[Candidate],
        arbiter: HumanArbiter,
        defect_type: str = "",
        interactive: bool = True,
    ) -> Dict[str, Any]:
        """Cluster, optionally arbitrate, and return a serialisable trace."""
        clusters = self.cluster(candidates)
        question = self.build_question(task_id, clusters, defect_type=defect_type)
        decision = arbiter.ask(question, interactive=interactive) if question else None
        selected = self.select(clusters, decision)

        return {
            "task_id": task_id,
            "test_cases": list(self.test_cases),
            "clusters": [c.to_dict() for c in clusters],
            "question": question.to_dict() if question else None,
            "decision": decision.to_dict() if decision else None,
            "selected_cluster": selected.cluster_id,
            "selected_candidate": selected.representative.candidate_id,
            "selected_source": selected.representative.source,
        }


def majority_output(
    candidates: Iterable[Candidate],
    test_case: str,
) -> Tuple[str, int]:
    """Return the most common output for ``test_case`` and its vote count."""
    counter = Counter(candidate.output_for(test_case) for candidate in candidates)
    if not counter:
        return FAILED, 0
    return counter.most_common(1)[0]
