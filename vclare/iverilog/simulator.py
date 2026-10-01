"""Pipeline adapter for the original ``evaluate_tb_diff.py`` simulation path.

The command lines, timeouts and grouping below mirror
``vclare/iverilog/evaluate_tb_diff.py`` exactly:

* compile: ``iverilog -g2012 -o <vvp> <tb> <dut>`` with ``shell=True``,
  working directory set to the task folder, timeout 60s
* run: ``vvp <vvp>`` with ``shell=True``, timeout 10s
* ``extract_check_lines`` keeps the lines containing ``[check]``; if none are
  present it keeps every non-empty line
* candidates are grouped by the exact extracted string

The pipeline needs one behavioral signature per candidate. The blob returned by
``extract_check_lines`` is therefore stored under the single key ``run``, which
makes ``SimRepair.cluster`` group candidates exactly the way
``evaluate_tb_diff.py`` does.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Sequence


def run_cmd(cmd: str, cwd: str, timeout: int) -> tuple:
    """Identical to ``evaluate_tb_diff.run_cmd``."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "ERROR: timeout after {}s".format(timeout)


def extract_check_lines(sim_output: str) -> str:
    """Identical to ``evaluate_tb_diff.extract_check_lines``."""
    lines = [line.strip() for line in sim_output.splitlines() if line.strip()]
    checks = [line for line in lines if "[check]" in line]
    if checks:
        return "\n".join(checks)
    return "\n".join(lines)


def _parse_check_detail(blob: str) -> Dict[str, str]:
    """Optional per-case view of ``[check]`` lines, for display only.

    This never feeds clustering; the behavioral signature is always the raw
    blob so grouping stays identical to ``evaluate_tb_diff.py``.
    """
    detail: Dict[str, str] = {}
    for line in blob.splitlines():
        if "[check]" not in line:
            continue
        payload = line.split("[check]", 1)[1].strip()
        case_match = re.search(r"case\s*[=:]\s*([A-Za-z0-9_.\-]+)", payload)
        if case_match:
            detail[case_match.group(1)] = payload
        else:
            detail[payload] = payload
    return detail


class IverilogSimulator:
    """Simulate candidates with the original iverilog/vvp command lines."""

    def __init__(
        self,
        iverilog: str = "iverilog",
        vvp: str = "vvp",
        compile_timeout: int = 60,
        run_timeout: int = 10,
    ) -> None:
        self.iverilog = iverilog
        self.vvp = vvp
        self.compile_timeout = compile_timeout
        self.run_timeout = run_timeout

    def simulate(
        self,
        testbench: str,
        candidates: Sequence[str],
        workdir: str,
        timeout: int = None,  # noqa: A002 - kept for the Simulator protocol
    ) -> List[Dict[str, Any]]:
        work = Path(workdir)
        work.mkdir(parents=True, exist_ok=True)
        tb_path = work / "testbench.sv"
        tb_path.write_text(testbench or "", encoding="utf-8")

        results: List[Dict[str, Any]] = []
        for index, source in enumerate(candidates, start=1):
            candidate_id = "c{}".format(index)
            dut_path = work / "case{}.sv".format(index)
            dut_path.write_text(source or "", encoding="utf-8")
            vvp_path = work / "sim_{}.vvp".format(index)

            cmd_compile = "{} -g2012 -o {} {} {}".format(
                self.iverilog, vvp_path, tb_path, dut_path
            )
            code, out = run_cmd(cmd_compile, cwd=str(work), timeout=self.compile_timeout)
            if code != 0:
                blob = "COMPILE_ERROR: {}".format(out.strip())
                results.append(
                    {
                        "candidate_id": candidate_id,
                        "compiled": False,
                        "outputs": {"run": blob},
                        "blob": blob,
                        "detail": {},
                        "error": out,
                    }
                )
                continue

            cmd_run = "{} {}".format(self.vvp, vvp_path)
            _, out = run_cmd(cmd_run, cwd=str(work), timeout=self.run_timeout)
            blob = extract_check_lines(out)
            results.append(
                {
                    "candidate_id": candidate_id,
                    "compiled": True,
                    "outputs": {"run": blob},
                    "blob": blob,
                    "detail": _parse_check_detail(blob),
                    "error": "",
                }
            )
        return results
