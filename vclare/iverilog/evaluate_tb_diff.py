#!/usr/bin/env python3
"""
evaluate_tb_diff.py

Usage:
  python evaluate_tb_diff.py \
    --input input_data/test_data.json \
    --tb-map data/output_tb.json \
    --tb-dir path/to/pipeline_outputs \
    --truth truth_table.csv \
    --workdir ./tmp_eval \
    --timeout 10

Notes:
  - Requires iverilog and vvp (Icarus Verilog) on PATH.
  - Expects up to 20 case files per task: case1..case20 in the input JSON.
  - For each task this script will evaluate three testbench variants:
      baseline, final, improved
    It will look for files under: <tb-dir>/<task>/testbench_baseline.sv (or _final/_improved).
    If a variant file is missing, the script will copy an available variant for that task
    and report which variants were missing and which file was used to substitute.
  - Combined cluster PNGs are saved to the current directory: baseline_clusters.png,
    final_clusters.png, improved_clusters.png
"""
import argparse
import json
import os
import shutil
import subprocess
import csv
import sys
from pathlib import Path
from collections import defaultdict
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# Try to import defaults from read_file.py if present
try:
    from read_file import INPUT_DATA_FILE as RF_INPUT, OUT_TB_FILE as RF_OUT_TB, truth_table_path as RF_TRUTH
except Exception:
    RF_INPUT = None
    RF_OUT_TB = None
    RF_TRUTH = None

MAX_CASES = 20

def normalize_label(x):
    if isinstance(x, str):
        return "pass" if x.strip().lower() == "pass" else "fail"
    return "pass" if bool(x) else "fail"

def run_cmd(cmd, cwd, timeout):
    try:
        proc = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        return -1, f"ERROR: timeout after {timeout}s"

def extract_check_lines(sim_output):
    lines = [ln.strip() for ln in sim_output.splitlines() if ln.strip()]
    chk = [ln for ln in lines if "[check]" in ln]
    if chk:
        return "\n".join(chk)
    return "\n".join(lines)

def load_truth_map(truth_csv_path):
    truth_map = {}
    if not truth_csv_path:
        return truth_map
    truth_csv_path = os.path.abspath(truth_csv_path)
    if not os.path.exists(truth_csv_path):
        print(f"[WARN] truth CSV not found: {truth_csv_path}")
        return truth_map
    with open(truth_csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            raw_name = row[0].strip()
            if not raw_name:
                continue
            lowname = raw_name.lower()
            if lowname in ("name", "task", "id", "header"):
                continue
            # collect up to MAX_CASES columns, pad with 'fail' if fewer provided
            labels = [cell.strip() for cell in row[1:1+MAX_CASES]]
            if len(labels) < MAX_CASES:
                missing = MAX_CASES - len(labels)
                print(f"[WARN] truth row for '{raw_name}' has {len(labels)} labels; padding {missing} with 'fail'")
                labels += ["fail"] * missing
            labels = [normalize_label(x) for x in labels]

            # create several key variants so lookup is tolerant of naming differences
            variants = set()
            variants.add(raw_name)
            variants.add(raw_name.strip("[]"))
            variants.add(raw_name.lower())
            try:
                stem = Path(raw_name).stem
                name_only = Path(raw_name).name
                variants.add(stem)
                variants.add(stem.lower())
                variants.add(name_only)
            except Exception:
                pass

            for k in variants:
                if k:
                    truth_map[k] = labels
    return truth_map

def find_truth_labels(truth_map, name):
    # Try a number of normalized variants for lookup
    if not name:
        return None
    candidates = [
        name,
        name.strip(),
        name.strip("[]"),
        name.lower(),
        Path(name).name,
        Path(name).stem,
        Path(name).stem.lower()
    ]
    for c in candidates:
        if c in truth_map:
            return truth_map[c]
    return None

def plot_clusters_on_ax(ax, name, groups, label_map):
    """Plot clusters on a given axis; red=pass, green=fail."""
    x_pos = 0
    # Display groups in the order they appear in groups dict
    for output, members in groups.items():
        for idx in sorted(members):
            color = 'red' if label_map.get(idx) == 'pass' else 'green'
            ax.add_patch(Rectangle((x_pos, 0), 1, 1, facecolor=color, edgecolor='black'))
            ax.text(x_pos + 0.5, 0.5, str(idx), ha='center', va='center', fontsize=8)
            x_pos += 1
        # separator bar
        if x_pos < MAX_CASES:
            ax.axvline(x_pos - 0.5, color='black', linewidth=2)
    ax.set_xlim(0, MAX_CASES)
    ax.set_ylim(0, 1)
    ax.set_aspect('equal')
    ax.axis('off')
    ax.set_title(f"Task {name} Clusters (Red=Pass, Green=Fail)")

def evaluate_tb_set(tasks, tb_map, truth_map, work_root, timeout, label, tb_dir=None, variant_maps_all=None):
    """
    Evaluate testbenches provided by tb_map (mapping name->tb_code string).
    Returns aggregated stats and saves a combined cluster plot to ./{label}_clusters.png
    """
    judged_tasks = 0
    success_count = 0
    overspecified_count = 0
    underspecified_count = 0
    failed_totally_count = 0
    undecided_count = 0

    # detailed tracking
    undecided_reasons = defaultdict(int)
    # reasons: no_cases, no_tb, no_truth, truth_len_mismatch, not_found_variant
    success_tasks = []
    overspecified_tasks = []
    underspecified_tasks = []
    failed_totally_tasks = []

    plot_data = []  # collect (name, groups, label_map) for plotting

    for task in tasks:
        name = task.get("name")
        print(f"\n[{label}] Task: {name}")
        task_dir = work_root / name
        task_dir.mkdir(parents=True, exist_ok=True)

        # Write case files (1..MAX_CASES)
        case_files = []
        for idx in range(1, MAX_CASES+1):
            key = f"case{idx}"
            if key not in task:
                continue
            src = task[key]
            case_path = task_dir / f"case{idx}.sv"
            with open(case_path, 'w', encoding='utf-8') as f:
                f.write(src)
            case_files.append((idx, case_path))

        if not case_files:
            print(f"[{label}] [WARN] No case files for task {name}, skipping.")
            undecided_count += 1
            undecided_reasons['no_cases'] += 1
            continue

        # Get testbench code for this task (try provided map, then tb_dir, then saves)
        tb_code = tb_map.get(name)
        substituted_runtime = None
        if not tb_code:
            # try tb_dir first (variant-specific and fallbacks)
            if tb_dir:
                base_dir = tb_dir / name
                vname = label.lower()
                cand = base_dir / f"testbench_{vname}.sv"
                if cand.exists():
                    tb_code = read_tb_file(cand)
                    substituted_runtime = f"tb_dir/{cand.name}"
                else:
                    # try common fallbacks
                    for altname in ("testbench.sv", "TopModule_tb.sv", "testbench_baseline.sv", "testbench_improved.sv"):
                        alt = base_dir / altname
                        if alt.exists():
                            tb_code = read_tb_file(alt)
                            substituted_runtime = f"tb_dir/{alt.name}"
                            break
            # try saves locations
            if not tb_code:
                script_dir = Path(__file__).resolve().parent
                save_candidates = [
                    script_dir / 'saves' / 'task' / name,
                    script_dir / 'saves' / name,
                    Path('saves') / 'task' / name,
                    Path('saves') / name,
                    script_dir.parent / 'saves' / 'task' / name,
                ]
                for sdir in save_candidates:
                    vname = label.lower()
                    cand = sdir / f"testbench_{vname}.sv"
                    if cand.exists():
                        tb_code = read_tb_file(cand)
                        substituted_runtime = f"saves/{cand.name}"
                        break
                    # try plain file and final fallback
                    for altname in ("testbench.sv", "TopModule_tb.sv", "testbench_baseline.sv", "testbench_improved.sv"):
                        alt = sdir / altname
                        if alt.exists():
                            tb_code = read_tb_file(alt)
                            substituted_runtime = f"saves/{alt.name}"
                            break
                    if tb_code:
                        break

        if not tb_code:
            # if other variants exist for this task, mark as not_found_variant
            other_exist = False
            if variant_maps_all:
                for vname, vmap in variant_maps_all.items():
                    if vmap and name in vmap:
                        other_exist = True
                        break
            if other_exist:
                print(f"[{label}] [WARN] Testbench for task {name} not found for variant {label}, but other variants exist.")
                undecided_reasons['not_found_variant'] += 1
            else:
                print(f"[{label}] [WARN] No testbench found for task {name} in provided tb_map or files. Skipping.")
                undecided_reasons['no_tb'] += 1
            undecided_count += 1
            continue
        else:
            if substituted_runtime:
                print(f"[{label}] [INFO] Using testbench from {substituted_runtime} for task {name} (was missing in map).")

        # Write testbench into working dir (single file reused for compilation per case)
        tb_path = task_dir / "testbench.sv"
        with open(tb_path, 'w', encoding='utf-8') as f:
            f.write(tb_code)

        # Simulate each case separately (compile tb + single case DUT)
        sim_outputs = {}
        for idx, dut_path in case_files:
            vvp_path = task_dir / f"sim_{idx}.vvp"
            cmd_compile = f'iverilog -g2012 -o {vvp_path} {tb_path} {dut_path}'
            print(f"[{label}] [{name}] compiling case{idx} ...")
            code, out = run_cmd(cmd_compile, cwd=str(task_dir), timeout=60)
            if code != 0:
                print(f"[{label}] [ERROR] compile case{idx} failed (ret={code}). Output:\n{out}")
                sim_outputs[idx] = f"COMPILE_ERROR: {out.strip()}"
                continue
            cmd_run = f'vvp {vvp_path}'
            print(f"[{label}] [{name}] running case{idx} ...")
            code, out = run_cmd(cmd_run, cwd=str(task_dir), timeout=timeout)
            if code == -1:
                print(f"[{label}] [ERROR] Simulation timeout for case{idx}")
            extracted = extract_check_lines(out)
            sim_outputs[idx] = extracted

        # Group by exact output
        groups = defaultdict(list)
        for idx, txt in sim_outputs.items():
            groups[txt].append(idx)

        print(f"[{label}] [{name}] Output groups (unique outputs: {len(groups)}):")
        for i, (txt, members) in enumerate(groups.items(), start=1):
            print(f"  Group {i}: cases = {members}")

        # Truth labels required to classify
        truth_labels = find_truth_labels(truth_map, name)
        if not truth_labels:
            print(f"[{label}] [{name}] No truth labels available; cannot judge (skipped).")
            undecided_count += 1
            undecided_reasons['no_truth'] += 1
            continue
        if len(truth_labels) != MAX_CASES:
            print(f"[{label}] [{name}] truth labels length != {MAX_CASES}, skipping.")
            undecided_count += 1
            undecided_reasons['truth_len_mismatch'] += 1
            continue

        label_map = {i+1: normalize_label(truth_labels[i]) for i in range(MAX_CASES)}

        # Collect for plotting
        plot_data.append((name, groups, label_map))

        # Analyze groups
        pure_pass_groups = []
        mixed_groups = []
        for output, members in groups.items():
            has_pass = any(label_map.get(idx, "fail") == "pass" for idx in members)
            has_fail = any(label_map.get(idx, "fail") == "fail" for idx in members)
            if has_pass and not has_fail:
                pure_pass_groups.append((output, members))
            elif has_pass and has_fail:
                mixed_groups.append((output, members))

        num_pure_pass = len(pure_pass_groups)
        has_mixed = len(mixed_groups) > 0

        overspecified = num_pure_pass > 1
        underspecified = has_mixed
        success = (num_pure_pass == 1) and (not has_mixed)
        failed_totally = overspecified and underspecified

        judged_tasks += 1
        if success:
            success_count += 1
            success_tasks.append(name)
            print(f"[{label}] [{name}] SUCCESS: pass cases in exactly one pure group.")
        elif failed_totally:
            failed_totally_count += 1
            failed_totally_tasks.append(name)
            print(f"[{label}] [{name}] FAILED TOTALLY: overspecified and underspecified.")
        elif overspecified:
            overspecified_count += 1
            overspecified_tasks.append(name)
            print(f"[{label}] [{name}] OVERSPECIFIED: pass cases in multiple pure groups.")
        elif underspecified:
            underspecified_count += 1
            underspecified_tasks.append(name)
            print(f"[{label}] [{name}] UNDERSPECIFIED: pass cases mixed with fail.")
        else:
            # e.g., no pass cases at all: treat as undecided/fail
            failed_totally_count += 1
            failed_totally_tasks.append(name)
            print(f"[{label}] [{name}] OTHER FAIL/NO PASS CASES.")

    # Save combined plot for this label
    if False:
        num_tasks = len(plot_data)
        fig, axes = plt.subplots(nrows=num_tasks, figsize=(20, 2 * num_tasks))
        if num_tasks == 1:
            axes = [axes]
        for ax, (name, groups, label_map) in zip(axes, plot_data):
            plot_clusters_on_ax(ax, name, groups, label_map)
        plot_path = f"./{label.lower()}_clusters.png"
        plt.tight_layout()
        plt.savefig(plot_path, bbox_inches='tight')
        plt.close()
        print(f"[{label}] Combined cluster plot saved to {plot_path}")
    if plot_data:
        return (judged_tasks, success_count, overspecified_count, underspecified_count,
            failed_totally_count, undecided_count, dict(undecided_reasons),
            success_tasks, overspecified_tasks, underspecified_tasks, failed_totally_tasks)

def read_tb_file(path: Path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception:
        return None


def build_maps_from_saves(truth_csv_path, tasks, script_dir: Path):
    """
    Fallback: build tb maps from saved per-task folders listed in the truth CSV.
    Looks for folders under several likely `saves/task/<task_id>` locations.
    Only includes tasks where all three variants (baseline/final/improved) exist.
    Returns: (final_map, baseline_map, improved_map, completed_task_names)
    """
    final_map = {}
    baseline_map = {}
    improved_map = {}
    completed = []

    if not truth_csv_path:
        return final_map, baseline_map, improved_map, completed

    # helper to find the folder for a given task id
    def find_task_dir(task_id: str):
        candidates = [
            script_dir / 'saves' / 'task' / task_id,
            script_dir / 'saves' / task_id,
            Path('saves') / 'task' / task_id,
            Path('saves') / task_id,
            script_dir.parent / 'saves' / 'task' / task_id,
        ]
        for c in candidates:
            if c.exists() and c.is_dir():
                return c
        return None

    try:
        with open(truth_csv_path, newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                if not row:
                    continue
                raw_name = row[0].strip()
                if not raw_name:
                    continue
                lowname = raw_name.lower()
                if lowname in ("name", "task", "id", "header"):
                    continue

                task_id = raw_name
                task_dir = find_task_dir(task_id)
                if not task_dir:
                    continue

                # look for the three variant files
                b = task_dir / 'testbench_baseline.sv'
                ffinal = task_dir / 'TopModule_tb.sv'
                imp = task_dir / 'testbench_improved.sv'
                if not (b.exists() and ffinal.exists() and imp.exists()):
                    # missing at least one variant -> skip
                    continue

                # read contents
                cb = read_tb_file(b)
                cf = read_tb_file(ffinal)
                ci = read_tb_file(imp)
                if cb is None or cf is None or ci is None:
                    continue

                # determine the canonical task name as found in tasks list (if any)
                canonical = None
                for t in tasks:
                    if t.get('name') == task_id:
                        canonical = task_id
                        break
                if canonical is None:
                    # fallback: use the task_id as name
                    canonical = task_id

                baseline_map[canonical] = cb
                final_map[canonical] = cf
                improved_map[canonical] = ci
                completed.append(canonical)
    except Exception:
        pass

    return final_map, baseline_map, improved_map, completed


def find_finished_tasks(tasks, tb_dir: Path):
    """Return a set of task names that have an existing folder with any testbench file.
    Checks `tb_dir` and several `saves` locations.
    """
    found = set()
    script_dir = Path(__file__).resolve().parent
    for t in tasks:
        name = t.get('name')
        if not name:
            continue
        # check tb_dir
        if tb_dir:
            base = tb_dir / name
            if base.exists() and base.is_dir():
                for fname in (f"testbench_baseline.sv", f"TopModule_tb.sv", f"testbench_improved.sv", "testbench.sv"):
                    if (base / fname).exists():
                        found.add(name)
                        break
                if name in found:
                    continue
        # check saves locations
        save_candidates = [
            script_dir / 'saves' / 'task' / name,
            script_dir / 'saves' / name,
            Path('saves') / 'task' / name,
            Path('saves') / name,
            script_dir.parent / 'saves' / 'task' / name,
        ]
        for s in save_candidates:
            if s.exists() and s.is_dir():
                for fname in (f"testbench_baseline.sv", f"TopModule_tb.sv", f"testbench_improved.sv", "testbench.sv"):
                    if (s / fname).exists():
                        found.add(name)
                        break
            if name in found:
                break
    return found

def build_variant_maps(tasks, tb_dir: Path, tb_map_json: dict, baseline_map_json: dict):
    """
    For each task, collect 'baseline', 'final', 'improved' tb contents.
    Prefer files under tb_dir/<task>/testbench_{variant}.sv.
    Fallback to tb_map_json for 'final' or baseline_map_json for 'baseline' if provided.
    If a variant is missing but another variant exists for the same task, copy that one to fill.
    This version ensures that when only the 'final' map is provided, 'baseline' and 'improved'
    are simply copies of 'final'.
    Returns: dict of variant -> map(name->tb_code), and list of substituted (name, missing_variant, used_variant)
    """
    variants = ['baseline', 'final', 'improved']
    maps = {v: {} for v in variants}
    substituted = []  # tuples (task_name, missing_variant, used_variant)

    for task in tasks:
        name = task.get("name")
        # First, populate from JSON maps if available
        if tb_map_json and name in tb_map_json:
            maps['final'][name] = tb_map_json[name]
        if baseline_map_json and name in baseline_map_json:
            maps['baseline'][name] = baseline_map_json[name]

        # Then try to read files from tb_dir if provided (override JSON)
        if tb_dir:
            base_dir = tb_dir / name
            for v in variants:
                cand = base_dir / f"testbench_{v}.sv"
                if cand.exists():
                    content = read_tb_file(cand)
                    if content is not None:
                        maps[v][name] = content
            # Also accept a plain "testbench.sv" or "TopModule_tb.sv" as fallback
            if name not in maps['final']:
                alt = base_dir / "TopModule_tb.sv"
                if alt.exists():
                    c = read_tb_file(alt)
                    if c is not None:
                        maps['final'][name] = c
            if name not in maps['baseline']:
                alt = base_dir / "testbench_baseline.sv"
                if alt.exists():
                    c = read_tb_file(alt)
                    if c is not None:
                        maps['baseline'][name] = c
            if name not in maps['improved']:
                alt = base_dir / "testbench_improved.sv"
                if alt.exists():
                    c = read_tb_file(alt)
                    if c is not None:
                        maps['improved'][name] = c

        # Now fill missing variants. Prefer reading a real file in tb_dir or saves
        # before copying content from another variant.
        for v in variants:
            if name in maps[v]:
                continue
            filled = False
            # try to read from tb_dir if available
            if tb_dir:
                base_dir = tb_dir / name
                cand = base_dir / f"testbench_{v}.sv"
                if cand.exists():
                    c = read_tb_file(cand)
                    if c is not None:
                        maps[v][name] = c
                        substituted.append((name, v, 'tb_dir_file'))
                        filled = True
                if not filled:
                    # also accept a plain testbench.sv as a fallback
                    alt = base_dir / "testbench.sv"
                    if alt.exists():
                        c = read_tb_file(alt)
                        if c is not None:
                            maps[v][name] = c
                            substituted.append((name, v, 'tb_dir_file'))
                            filled = True
            if filled:
                continue

            # try common saves locations (where saved per-task folders might live)
            script_dir = Path(__file__).resolve().parent
            save_candidates = [
                script_dir / 'saves' / 'task' / name,
                script_dir / 'saves' / name,
                Path('saves') / 'task' / name,
                Path('saves') / name,
                script_dir.parent / 'saves' / 'task' / name,
            ]
            for sdir in save_candidates:
                cand = sdir / f"testbench_{v}.sv"
                if cand.exists():
                    c = read_tb_file(cand)
                    if c is not None:
                        maps[v][name] = c
                        substituted.append((name, v, 'saves_file'))
                        filled = True
                        break
                # also try plain file
                cand2 = sdir / "testbench.sv"
                if cand2.exists():
                    c = read_tb_file(cand2)
                    if c is not None:
                        maps[v][name] = c
                        substituted.append((name, v, 'saves_file'))
                        filled = True
                        break
            if filled:
                continue

            # If still not filled, leave missing — do not copy from other variants.
            # if still missing, leave missing (will be skipped later)

    return maps, substituted

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", help="input JSON file with cases (default from read_file.INPUT_DATA_FILE if available)")
    p.add_argument("--tb-map", help="JSON file produced by main.py mapping name->tb (default from read_file.OUT_TB_FILE if available)")
    p.add_argument("--tb-dir", default="./saves/task", help="directory root where pipeline produced per-task folders (optional)")
    p.add_argument("--truth", help="CSV truth table path (name, label1..label20). Default from read_file.truth_table_path if available")
    p.add_argument("--workdir", default="./tmp_eval", help="working dir to create per-task directories")
    p.add_argument("--timeout", type=int, default=10, help="timeout in seconds for simulation runs (vvp)")
    p.add_argument("--keep", action="store_true", help="keep workdir instead of deleting")
    args = p.parse_args()

    input_path = args.input or RF_INPUT
    tb_map_path = args.tb_map or RF_OUT_TB
    truth_path = args.truth or RF_TRUTH
    tb_dir_path = args.tb_dir

    if not input_path:
        print("Error: input JSON path not specified and read_file.INPUT_DATA_FILE not available.")
        sys.exit(1)
    if not tb_map_path and not tb_dir_path:
        print("Warning: neither --tb-map nor --tb-dir provided; evaluation may be limited.")

    input_path = os.path.abspath(input_path)
    tb_map_path = os.path.abspath(tb_map_path) if tb_map_path else None
    tb_dir = Path(tb_dir_path).absolute() if tb_dir_path else None
    work_root = Path(args.workdir).absolute()
    timeout = args.timeout

    if not os.path.exists(input_path):
        print(f"Error: input JSON not found: {input_path}")
        sys.exit(1)
    # If a tb-map path was provided but the file is missing or empty, warn and ignore it
    if tb_map_path:
        if not os.path.exists(tb_map_path) or os.path.getsize(tb_map_path) == 0:
            print(f"[WARN] tb-map file specified but missing or empty: {tb_map_path}; ignoring.")
            tb_map_path = None
    if tb_dir and not tb_dir.exists():
        print(f"Warning: tb-dir not found: {tb_dir}; will rely on tb-map JSONs if available.")
        tb_dir = None

    with open(input_path, 'r', encoding='utf-8') as f:
        tasks = json.load(f)

    # Load JSON tb maps if provided
    tb_map_json = {}
    baseline_map_json = {}
    if tb_map_path:
        with open(tb_map_path, 'r', encoding='utf-8') as f:
            try:
                tb_list = json.load(f)
                if isinstance(tb_list, dict):
                    tb_map_json = tb_list
                else:
                    for item in tb_list:
                        name = item.get("name")
                        tb = item.get("tb", "")
                        if name:
                            tb_map_json[name] = tb
            except Exception as e:
                print(f"Error parsing tb-map JSON: {e}")
                tb_map_json = {}

    truth_map = load_truth_map(truth_path)

    # If no tb-map JSON provided, try building maps from saved per-task folders
    script_dir = Path(__file__).resolve().parent
    completed_task_names = []
    if not tb_map_json:
        built_final, built_baseline, built_improved, completed = build_maps_from_saves(truth_path, tasks, script_dir)
        if built_final:
            print(f"[INFO] Built tb maps from saves for {len(completed)} completed tasks.")
            tb_map_json = built_final
            # Only override baseline map if we found baseline data too
            if built_baseline:
                baseline_map_json = built_baseline
            # Note: built_improved may be used by build_variant_maps later when tb_dir is provided
            # Filter tasks to only include those with completed saves
            completed_task_names = completed
            if completed_task_names:
                tasks = [t for t in tasks if t.get('name') in set(completed_task_names)]
                # attempt to infer a max completed id (best-effort numeric parse)
                max_completed = None
                nums = []
                for n in completed_task_names:
                    s = ''.join(ch for ch in str(n) if ch.isdigit())
                    if s:
                        try:
                            nums.append(int(s))
                        except Exception:
                            pass
                if nums:
                    max_completed = max(nums)
                    print(f"[INFO] Max numeric task id completed: {max_completed}")

    # Prepare workdir
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    
    # Build variant maps and record substitutions
    variant_maps, substituted = build_variant_maps(tasks, tb_dir, tb_map_json, baseline_map_json)

    if substituted:
        print("\nSubstitutions made for missing variants (task, missing_variant, used_variant):")
        for t, missing_v, used_v in substituted:
            print(f"  {t}: {missing_v} <- copied from {used_v}")

    # Evaluate each variant
    results = {}
    for variant in ['baseline', 'final', 'improved']:
        print("\n" + "=" * 80)
        print(f"EVALUATING {variant.upper()}")
        print("=" * 80)
        (judged, succ, over, under, failed_tot, undec,
         undec_reasons, succ_tasks, over_tasks, under_tasks, failed_tasks) = evaluate_tb_set(
            tasks, variant_maps.get(variant, {}), truth_map, work_root, timeout, variant.upper(), tb_dir, variant_maps
        )
        results[variant] = {
            'judged': judged,
            'success': succ,
            'overspecified': over,
            'underspecified': under,
            'failed_totally': failed_tot,
            'undecided': undec,
            'undecided_reasons': undec_reasons,
            'success_tasks': succ_tasks,
            'overspecified_tasks': over_tasks,
            'underspecified_tasks': under_tasks,
            'failed_totally_tasks': failed_tasks,
        }

    # Print summaries
    # Determine finished tasks (folders with tb files) to use as denominator for rates
    finished_tasks = find_finished_tasks(tasks, tb_dir)
    if finished_tasks:
        print(f"\n[INFO] Finished tasks detected (folders with tb files): {len(finished_tasks)}")
    else:
        print("\n[INFO] No finished task folders detected; using judged counts for rate denominators.")

    # Write out task name lists for each variant (success, overspecified, underspecified, failed_totally)
    for variant in ['baseline', 'final', 'improved']:
        r = results[variant]
        base_prefix = f"{variant.lower()}"
        try:
            with open(f"{base_prefix}_success.txt", 'w', encoding='utf-8') as f:
                for t in r.get('success_tasks', []):
                    f.write(t + "\n")
            with open(f"{base_prefix}_overspecified.txt", 'w', encoding='utf-8') as f:
                for t in r.get('overspecified_tasks', []):
                    f.write(t + "\n")
            with open(f"{base_prefix}_underspecified.txt", 'w', encoding='utf-8') as f:
                for t in r.get('underspecified_tasks', []):
                    f.write(t + "\n")
            with open(f"{base_prefix}_failed_totally.txt", 'w', encoding='utf-8') as f:
                for t in r.get('failed_totally_tasks', []):
                    f.write(t + "\n")
        except Exception:
            pass
    print("\n" + "="*60)
    for variant in ['baseline', 'final', 'improved']:
        r = results[variant]
        print(f"SUMMARY - {variant.upper()}")
        print(f"  Tasks judged (with truth labels): {r['judged']}")
        print(f"  Successful: {r['success']}")
        print(f"  Overspecified: {r['overspecified']}")
        print(f"  Underspecified: {r['underspecified']}")
        print(f"  Failed totally: {r['failed_totally']}")
        print(f"  Undecided/skipped: {r['undecided']}")
        denom = len(finished_tasks) if finished_tasks else r['judged']
        rate = (r['success'] / denom * 100.0) if denom > 0 else 0.0
        print(f"  Success rate: {rate:.2f}% ({r['success']}/{denom})")
        print("-" * 40)

    if not args.keep:
        try:
            shutil.rmtree(work_root)
        except Exception:
            pass
    else:
        print(f"Workdir kept at: {work_root}")

if __name__ == "__main__":
    main()