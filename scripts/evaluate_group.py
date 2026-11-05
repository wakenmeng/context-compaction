#!/usr/bin/env python3
"""Evaluate a group's predictions with the official SWE-bench harness

Reads results/<group>/preds.json, runs the harness on the pinned dataset
snapshot, and writes results/<group>/<instance_id>/eval.json plus a summary
in results/<group>/eval_summary.<set>.json. Empty patches count as unresolved
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from context_compaction import pins
from context_compaction.agent import dumps
from fetch_dataset import verify
from gold_eval import pull_images, selected_ids
from sample_instances import load_records


def default_run_id(group, which, preds):
    """The harness skips instances it already has a report for under the same run id,
    so the default id is tied to the exact predictions being scored"""
    digest = hashlib.sha256(json.dumps(preds, sort_keys=True).encode()).hexdigest()[:12]
    return f"group-{group}-{which}-{digest}"


def select_preds(preds, which, instances_path=pins.INSTANCES_PATH):
    """Only the frozen set being reported: smoke and formal never mix in one score"""
    wanted = set(selected_ids(which, instances_path))
    return {i: p for i, p in preds.items() if i in wanted}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group", required=True)
    parser.add_argument("--set", dest="which", choices=["smoke", "formal", "all"], required=True)
    parser.add_argument("--results", help="default: results/<group>")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=1800, help="seconds per instance")
    parser.add_argument("--run-id", help="default: group-<group>")
    args = parser.parse_args(argv)

    problem = verify()
    if problem:
        sys.exit(problem)
    out_dir = Path(args.results or pins.ROOT / "results" / args.group)
    preds_path = out_dir / "preds.json"
    preds = select_preds(json.loads(preds_path.read_text()), args.which)
    if not preds:
        sys.exit(f"no {args.which} predictions in {preds_path}")
    run_id = args.run_id or default_run_id(args.group, args.which, preds)
    ids = sorted(preds)

    records = {r["instance_id"]: r for r in load_records(pins.DATASET_PATH)}
    pull_images(records[i]["image"] for i in ids)
    report_dir = out_dir / "eval"
    report_dir.mkdir(parents=True, exist_ok=True)
    # the harness wants a list or jsonl; keep the json map as the source of truth
    preds_list = report_dir / "preds.jsonl"
    preds_list.write_text("".join(json.dumps(preds[i]) + "\n" for i in ids))
    cmd = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", str(pins.DATASET_DIR), "--split", "test",
        "--predictions_path", str(preds_list), "--run_id", run_id,
        "--max_workers", str(args.workers), "--timeout", str(args.timeout),
        "--report_dir", str(report_dir), "--instance_ids", *ids,
    ]
    code = subprocess.run(cmd, cwd=pins.ROOT).returncode
    reports = sorted(report_dir.glob(f"*.{run_id}.json"))
    if code or not reports:
        sys.exit(f"harness exited {code}, report {'missing' if not reports else reports[-1]}")
    report = json.loads(reports[-1].read_text())
    resolved = set(report.get("resolved_ids", []))
    empty = set(report.get("empty_patch_ids", []))
    errors = set(report.get("error_ids", []))

    harness_logs = pins.ROOT / "logs" / "run_evaluation" / run_id
    for iid in ids:
        entry = {"instance_id": iid, "run_id": run_id, "resolved": iid in resolved, "empty_patch": iid in empty,
                 "harness_error": iid in errors}
        logs = next(harness_logs.glob(f"*/{iid}"), None) if harness_logs.exists() else None
        target = out_dir / iid / "eval_logs"
        shutil.rmtree(target, ignore_errors=True)  # never leave logs from an older scoring behind
        if logs and (logs / "report.json").exists():
            entry["report"] = json.loads((logs / "report.json").read_text()).get(iid)
            shutil.copytree(logs, target)
        if (out_dir / iid).exists():
            (out_dir / iid / "eval.json").write_text(dumps(entry))

    summary = {"run_id": run_id, "set": args.which, "evaluated": len(ids), "resolved": len(resolved & set(ids)),
               "empty_patch": len(empty), "harness_errors": len(errors), "resolved_ids": sorted(resolved & set(ids))}
    (out_dir / f"eval_summary.{args.which}.json").write_text(dumps(summary))
    print(json.dumps({k: v for k, v in summary.items() if k != "resolved_ids"}, indent=2))


if __name__ == "__main__":
    main()
