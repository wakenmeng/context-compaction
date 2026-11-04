#!/usr/bin/env python3
"""Run the official harness with gold patches to prove the environments work

Every selected instance must resolve. A failure here is an environment problem
(emulation, image, timeout), not a model problem, so it is counted toward the
M4 environment failure rate

Re-running with the same --set resumes, finished instances are skipped
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from context_compaction import pins
from fetch_dataset import verify
from sample_instances import load_records

REPORT_DIR = pins.ROOT / "results" / "gold"


def selected_ids(which, instances_path=pins.INSTANCES_PATH):
    config = json.loads(Path(instances_path).read_text())
    if which == "all":
        return config["smoke"]["ids"] + config["formal"]["ids"]
    return list(config[which]["ids"])


def pull_images(images):
    for image in sorted(set(images)):
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
            continue
        print(f"pulling {image}", flush=True)
        # the harness pulls without a platform, which fails on arm64 hosts
        subprocess.run(["docker", "pull", "--platform", "linux/amd64", image], check=True)


def summarize(report_path, ids):
    report = json.loads(Path(report_path).read_text())
    resolved = set(report.get("resolved_ids", []))
    bad = sorted(set(ids) - resolved)
    return {"requested": len(ids), "resolved": len(resolved & set(ids)), "not_resolved": bad}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", dest="which", choices=["smoke", "formal", "all"], default="smoke")
    parser.add_argument("--instance", action="append", help="explicit instance id, repeatable, overrides --set")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=1800, help="seconds per instance")
    parser.add_argument("--run-id", help="harness run id; use a new one to rerun finished instances")
    args = parser.parse_args(argv)

    problem = verify()
    if problem:
        sys.exit(problem)
    ids = args.instance or selected_ids(args.which)
    run_id = args.run_id or "gold-" + ("custom" if args.instance else args.which)

    records = {r["instance_id"]: r for r in load_records(pins.DATASET_PATH)}
    unknown = [i for i in ids if i not in records]
    if unknown:
        sys.exit(f"unknown instance ids: {unknown}")
    pull_images(records[i]["image"] for i in ids)

    started = time.time()
    cmd = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", str(pins.DATASET_DIR),
        "--split", "test",
        "--predictions_path", "gold",
        "--run_id", run_id,
        "--max_workers", str(args.workers),
        "--timeout", str(args.timeout),
        "--report_dir", str(REPORT_DIR),
        "--instance_ids", *ids,
    ]
    code = subprocess.run(cmd, cwd=pins.ROOT).returncode
    reports = sorted(REPORT_DIR.glob(f"*.{run_id}.json"))
    if code or not reports:
        sys.exit(f"harness exited {code}, report {'missing' if not reports else reports[-1]}")

    # keep per-instance test output next to the report so `pull` brings it home
    harness_logs = pins.ROOT / "logs" / "run_evaluation" / run_id
    if harness_logs.exists():
        shutil.copytree(harness_logs, REPORT_DIR / "logs" / run_id, dirs_exist_ok=True)

    summary = summarize(reports[-1], ids)
    summary["minutes"] = round((time.time() - started) / 60, 1)
    (REPORT_DIR / f"{run_id}.summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    if summary["not_resolved"]:
        sys.exit(f"{len(summary['not_resolved'])} of {len(ids)} gold patches did not resolve")


if __name__ == "__main__":
    main()
