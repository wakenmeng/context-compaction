#!/usr/bin/env python3
"""Run one experiment group on SWE-bench Verified tasks from the frozen sample

Each task writes results/<group>/<instance_id>/: events.jsonl, states/,
traj.json, usage.json, patch.diff and run.json. Predictions for the official
harness go to results/<group>/preds.json. Finished tasks are skipped, so an
interrupted run can be restarted with the same command
"""
import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from context_compaction import credentials, pins
from context_compaction.agent import CompactingAgent, dumps, git_commit
from fetch_dataset import verify
from gold_eval import selected_ids
from sample_instances import load_records

GROUPS = {"c": CompactingAgent}
DONE = {"Submitted", "LimitsExceeded", "TimeExceeded", "ContextOverflow", "RepeatedFormatError"}
# first try plus the plan's two reruns for infrastructure failures
MAX_ATTEMPTS = 3
_preds_lock = threading.Lock()


def recursive_merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        out[k] = recursive_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def load_config(path):
    from minisweagent.config import builtin_config_dir

    overrides = yaml.safe_load(Path(path).read_text())
    base_name = overrides.pop("base", None)
    base = {}
    if base_name:
        base = yaml.safe_load((builtin_config_dir / "benchmarks" / f"{base_name}.yaml").read_text())
    config = recursive_merge(base, overrides)
    suffix = config["agent"].pop("instance_template_suffix", "")
    config["agent"]["instance_template"] += suffix
    return config


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def changed_since(commit, paths, root=pins.ROOT):
    """Whether any of `paths` differs from `commit`, in commits or in the working tree"""
    result = subprocess.run(["git", "-C", str(root), "diff", "--quiet", commit, "--", *paths])
    if result.returncode not in (0, 1):
        raise SystemExit(f"cannot compare against frozen commit {commit[:12]}")
    return result.returncode == 1


def check_frozen(group, config, root=pins.ROOT, changed=changed_since):
    """The formal set only runs on the configuration frozen after the smoke runs"""
    path = Path(root) / "configs" / f"frozen_{group}.json"
    if not path.exists():
        raise SystemExit(f"no {path.name}: freeze the configuration after the smoke runs first")
    frozen = json.loads(path.read_text())
    if config_hash(config) != frozen["config_sha256"]:
        raise SystemExit("config differs from the frozen one")
    if changed(frozen["code_commit"], frozen["frozen_paths"]):
        raise SystemExit(f"code under {frozen['frozen_paths']} changed since frozen commit {frozen['code_commit'][:12]}")
    return frozen


def claim_out_dir(out_dir, config):
    """Results from different configs must never mix in one directory"""
    out_dir.mkdir(parents=True, exist_ok=True)
    sha = config_hash(config)
    saved = out_dir / "config.json"
    if saved.exists() and config_hash(json.loads(saved.read_text())) != sha:
        raise SystemExit(f"{out_dir} holds results for another config; pass a new --out")
    saved.write_text(dumps(config))
    return sha


def finished(run_dir, config_sha):
    path = run_dir / "run.json"
    if not path.exists():
        return False
    record = json.loads(path.read_text())
    return record.get("exit_status") in DONE and record.get("config_sha256") == config_sha


def write_preds(out_dir, config_sha, model_name):
    """Rebuild preds.json from finished task dirs, so it can never drift from them; atomic"""
    with _preds_lock:
        preds = {}
        for run_json in sorted(out_dir.glob("*/run.json")):
            if finished(run_json.parent, config_sha):
                iid = run_json.parent.name
                patch = (run_json.parent / "patch.diff").read_text() if (run_json.parent / "patch.diff").exists() else ""
                preds[iid] = {"instance_id": iid, "model_name_or_path": model_name, "model_patch": patch}
        tmp = out_dir / "preds.json.tmp"
        tmp.write_text(json.dumps(preds, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, out_dir / "preds.json")
        return preds


def prepare_run_dir(out_dir, iid, max_attempts=MAX_ATTEMPTS):
    """Archive any unfinished earlier attempt (its spend stays on record) and return
    (run_dir, attempt number), or None once the attempts are used up"""
    run_dir = out_dir / iid
    archive = out_dir / "attempts"
    earlier = sorted(archive.glob(f"{iid}.*")) if archive.exists() else []
    if run_dir.exists():
        archive.mkdir(exist_ok=True)
        target = archive / f"{iid}.{len(earlier) + 1}"
        shutil.move(str(run_dir), str(target))
        earlier.append(target)
    if len(earlier) >= max_attempts:
        return None
    run_dir.mkdir(parents=True)
    return run_dir, len(earlier) + 1


def run_instance(group, instance, config, out_dir):
    from minisweagent.models import get_model
    from minisweagent.run.benchmarks.swebench import get_sb_environment

    iid = instance["instance_id"]
    prepared = prepare_run_dir(out_dir, iid)
    if prepared is None:
        return iid, f"gave up after {MAX_ATTEMPTS} attempts"
    run_dir, attempt = prepared
    started = time.time()
    sha = config_hash(config)
    record = {
        "group": group, "instance_id": iid, "config_sha256": sha, "attempt": attempt,
        "code_commit": git_commit(pins.ROOT), "model": config["model"]["model_name"], "started": started,
    }
    agent, env, info = None, None, {}
    try:
        env = get_sb_environment(copy.deepcopy(config), {**instance, "image_name": f"docker.io/{instance['image']}"})
        model = get_model(config=copy.deepcopy(config["model"]))
        agent = GROUPS[group](
            model, env, run_dir=run_dir, output_path=run_dir / "traj.json",
            **config["agent"], **config.get("compaction", {}),
        )
        info = agent.run(instance["problem_statement"])
        record["exit_status"] = info.get("exit_status")
    except Exception as e:
        record.update(exit_status=type(e).__name__, error=str(e), traceback=traceback.format_exc())
    finally:
        patch = info.get("submission") or ""
        (run_dir / "patch.diff").write_text(patch)
        if agent is not None and hasattr(agent, "log"):
            summary = agent.summary()
            (run_dir / "usage.json").write_text(dumps(summary))
            record["summary"] = summary
        record["finished"] = time.time()
        (run_dir / "run.json").write_text(dumps(record))
        write_preds(out_dir, sha, config["model"]["model_name"])
        if env is not None and hasattr(env, "cleanup"):
            env.cleanup()
    return iid, record["exit_status"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group", choices=sorted(GROUPS), required=True)
    parser.add_argument("--set", dest="which", choices=["smoke", "formal", "all"])
    parser.add_argument("--instance", action="append", help="explicit instance id, repeatable")
    parser.add_argument("--config", help="default: configs/group_<group>.yaml")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", help="default: results/<group>")
    args = parser.parse_args(argv)
    if not (args.which or args.instance):
        parser.error("give --set or --instance")

    problem = verify()
    if problem:
        raise SystemExit(problem)
    if credentials.load()["OPENAI_API_KEY"] is None:
        raise SystemExit("OPENAI_API_KEY missing: set it or CC_CREDENTIALS_DIR (see .env.example)")

    config = load_config(args.config or pins.ROOT / "configs" / f"group_{args.group}.yaml")
    if args.which in ("formal", "all"):
        frozen = check_frozen(args.group, config)
        print(f"frozen configuration verified: code {frozen['code_commit'][:12]}, config {frozen['config_sha256'][:12]}")
    out_dir = Path(args.out or pins.ROOT / "results" / args.group)
    sha = claim_out_dir(out_dir, config)

    ids = args.instance or selected_ids(args.which)
    # env-unsolvable tasks still run: the plan reports results with and without them
    records = {r["instance_id"]: r for r in load_records(pins.DATASET_PATH)}
    write_preds(out_dir, sha, config["model"]["model_name"])
    todo = [i for i in ids if not finished(out_dir / i, sha)]
    print(f"{len(ids)} selected, {len(ids) - len(todo)} already finished, {len(todo)} to run", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_instance, args.group, records[i], config, out_dir) for i in todo]
        for n, future in enumerate(as_completed(futures), 1):
            iid, status = future.result()
            print(f"[{n}/{len(todo)}] {iid}: {status}", flush=True)


if __name__ == "__main__":
    main()
