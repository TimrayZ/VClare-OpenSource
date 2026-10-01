"""Icarus Verilog integration for the VClare pipeline.

This package keeps the two simulation code paths used by the original research
harness, without rewriting them:

* ``score.py`` - the golden-testbench pass/fail judge. Copied verbatim from the
  research harness (``compitition_dir/score.py``); use ``score.judge_task``.
* ``evaluate_tb_diff.py`` - the behavioral-difference evaluator. Copied verbatim
  from the research harness; it extracts ``[check]`` lines and groups candidates
  by exact output. The pipeline adapter in ``simulator.py`` reimplements the
  same command lines, timeouts and grouping so it can be imported without the
  optional matplotlib dependency of the full evaluation script.

Both files require Icarus Verilog (iverilog/vvp) on PATH at runtime. ``score.py``
invokes ``bash -lc`` and therefore expects a POSIX shell, matching the original
harness.
"""

from .simulator import (  # noqa: F401
    IverilogSimulator,
    extract_check_lines,
    run_cmd,
)
from . import score  # noqa: F401

__all__ = [
    "IverilogSimulator",
    "extract_check_lines",
    "run_cmd",
    "score",
]
