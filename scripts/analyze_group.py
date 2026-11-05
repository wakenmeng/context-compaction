#!/usr/bin/env python3
"""Recompute every reported number for a group run from its results directory

Reads results/<run>/<instance_id>/{run,usage,eval}.json and events.jsonl and
prints (or writes, with --json) the summary used in the report. Nothing here
calls a model or the harness, so the tables can be rebuilt by anyone

Repeated actions follow docs/plan.md ("H2 的计算"): an action is a shell command
(hist and ctx excluded); a repeat is a command that, after whitespace
normalization, equals one run before the compaction while no repo file changed
in between; the window is the 10 actions after each compaction, each action
counted once; tasks without a compaction are not exposed
"""
import argparse
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

WINDOW = 10


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (centre - half, centre + half)


def normalize(command):
    return re.sub(r"\s+", " ", command).strip()


def load_events(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def repeats(events):
    """(repeated actions, window actions) for one task, or None if it never compacted"""
    actions = [e for e in events if e["kind"] == "action"]
    compactions = [e["seq"] for e in events if e["kind"] == "compaction"]
    if not compactions:
        return None
    # repo version after each action: bumps whenever the action changed a file
    version, versions = 0, {}
    for a in actions:
        if a.get("changes"):
            version += 1
        versions[a["seq"]] = version
    counted, repeated = set(), 0
    for c in compactions:
        before = [a for a in actions if a["seq"] < c]
        window = [a for a in actions if a["seq"] > c][:WINDOW]
        seen = {}
        for a in before:
            seen.setdefault(normalize(a["payload"]["command"]), []).append(versions[a["seq"]])
        for a in window:
            if a["seq"] in counted:
                continue
            counted.add(a["seq"])
            # the repo state the command sees is the state after the previous action
            prior = [x for x in actions if x["seq"] < a["seq"]]
            state_now = versions[prior[-1]["seq"]] if prior else 0
            if state_now in seen.get(normalize(a["payload"]["command"]), []):
                repeated += 1
    return repeated, len(counted)


def overflow_cause(task_dir):
    """Why the last compaction reply before an overflow was rejected, replayed offline"""
    from context_compaction import patch as patching
    from context_compaction.compactor import parse_json
    from context_compaction.event_log import EventLog

    rows = [json.loads(line) for line in (task_dir / "compactions.jsonl").read_text().splitlines()] \
        if (task_dir / "compactions.jsonl").exists() else []
    if not rows:
        return "no compaction reply before the overflow"
    states = sorted((task_dir / "states").glob("v*.json"))
    base = json.loads(states[-1].read_text())["state"]
    log = EventLog(task_dir / "events.jsonl")
    try:
        proposal = parse_json(rows[-1]["reply"])
        if base.get("fallback_recap"):
            patching.rebuild(base, proposal, log)
        else:
            patching.apply_patch(base, proposal, log)
        return "accepted on replay"
    except patching.PatchRejected as e:
        return ("over the state cap: " if e.over_cap else "") + "; ".join(e.errors)
    except ValueError as e:
        return f"invalid JSON: {e}"


def task_row(task_dir):
    run = json.loads((task_dir / "run.json").read_text())
    usage = json.loads((task_dir / "usage.json").read_text()) if (task_dir / "usage.json").exists() else {}
    ev = json.loads((task_dir / "eval.json").read_text()) if (task_dir / "eval.json").exists() else {}
    events = load_events(task_dir / "events.jsonl")
    models = [e["payload"] for e in events if e["kind"] == "model"]
    compactions = [e["payload"] for e in events if e["kind"] == "compaction"]
    notes = [e["payload"] for e in events if e["kind"] == "note"]
    return {
        "instance_id": task_dir.name,
        "repo": task_dir.name.split("__")[0],
        "resolved": bool(ev.get("resolved")),
        "empty_patch": bool(ev.get("empty_patch")),
        "exit_status": run.get("exit_status"),
        "steps": usage.get("steps", 0),
        "cost": usage.get("cost", 0.0),
        "compactions": [c["mode"] for c in compactions],
        "first_try": sum(1 for c in compactions if c["mode"] != "fallback" and c["attempts"] == 1),
        "peak_tokens": max((m["usage"]["input"] + m["usage"]["output"] for m in models), default=0),
        "max_underestimate": max([m["usage"]["input"] - m["estimated_input"] for m in models] + [0]),
        "tight_truncations": sum(1 for n in notes if n.get("what") == "tight_truncation"),
        "hist_calls": usage.get("hist_calls", 0),
        "stale_marked": sum(len(c.get("stale_marked", [])) for c in compactions),
        "revived": sum(len(c.get("revived", [])) for c in compactions),
        "repeats": repeats(events),
        "compaction_cost": (usage.get("usage", {}).get("compaction") or {}).get("cost", 0.0),
    }


def summarize(rows):
    n = len(rows)
    k = sum(r["resolved"] for r in rows)
    low, high = wilson(k, n)
    exposed = [r for r in rows if r["repeats"] is not None]
    rep = sum(r["repeats"][0] for r in exposed)
    win = sum(r["repeats"][1] for r in exposed)
    modes = Counter(m for r in rows for m in r["compactions"])
    by_repo = defaultdict(lambda: [0, 0])
    for r in rows:
        by_repo[r["repo"]][0] += r["resolved"]
        by_repo[r["repo"]][1] += 1
    costs = [r["cost"] for r in rows]
    return {
        "tasks": n,
        "resolved": k,
        "resolved_rate": k / n if n else 0.0,
        "resolved_ci95": [low, high],
        "exit_status": dict(Counter(r["exit_status"] for r in rows)),
        "overflow_resolved_anyway": sum(r["resolved"] for r in rows if r["exit_status"] == "ContextOverflow"),
        "empty_patches": sum(r["empty_patch"] for r in rows),
        "cost_total": sum(costs),
        "cost_mean": statistics.mean(costs) if costs else 0.0,
        "cost_median": statistics.median(costs) if costs else 0.0,
        "compaction_cost": sum(r["compaction_cost"] for r in rows),
        "steps_mean": statistics.mean(r["steps"] for r in rows) if rows else 0.0,
        "tasks_with_compaction": sum(1 for r in rows if r["compactions"]),
        "compactions": dict(modes),
        "patches_first_try": sum(r["first_try"] for r in rows),
        "peak_tokens": max((r["peak_tokens"] for r in rows), default=0),
        "max_underestimate": max((r["max_underestimate"] for r in rows), default=0),
        "tight_truncations": sum(r["tight_truncations"] for r in rows),
        "hist_calls": sum(r["hist_calls"] for r in rows),
        "stale_marked": sum(r["stale_marked"] for r in rows),
        "revived": sum(r["revived"] for r in rows),
        "repeat_rate": rep / win if win else None,
        "repeat_counts": [rep, win, len(exposed)],
        "by_repo": {repo: f"{a}/{b}" for repo, (a, b) in sorted(by_repo.items())},
        "unresolved": sorted(r["instance_id"] for r in rows if not r["resolved"]),
    }


def load(results_dir):
    return [task_row(p.parent) for p in sorted(Path(results_dir).glob("*/run.json"))]


def escalate(rows, retry_rows):
    """One policy: run at the first budget, and rerun at the larger one only the tasks
    that overflowed. The rule looks at the overflow, never at the test result; both
    runs' costs count, since the first one was paid before it overflowed"""
    retry = {r["instance_id"]: r for r in retry_rows}
    out = []
    for r in rows:
        if r["exit_status"] == "ContextOverflow" and r["instance_id"] in retry:
            second = dict(retry[r["instance_id"]])
            second["cost"] = r["cost"] + second["cost"]
            second["compaction_cost"] = r["compaction_cost"] + second["compaction_cost"]
            second["steps"] = r["steps"] + second["steps"]
            second["escalated"] = True
            out.append(second)
        else:
            out.append(r)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", help="a results directory, e.g. results/c-formal")
    parser.add_argument("--compare", help="a second results directory to compare task by task")
    parser.add_argument("--json", help="write the summary here")
    parser.add_argument("--overflow-causes", action="store_true", help="replay why overflowed tasks' last patch was rejected")
    parser.add_argument("--escalate", help="results of rerunning the overflowed tasks at a larger budget; "
                                           "reports the combined policy over all tasks")
    args = parser.parse_args(argv)

    rows = load(args.results)
    if args.escalate:
        rows = escalate(rows, load(args.escalate))
    summary = summarize(rows)
    if args.escalate:
        summary["escalated"] = sorted(r["instance_id"] for r in rows if r.get("escalated"))
    out = {"results": args.results, "escalate": args.escalate, "summary": summary}
    if args.compare:
        other = {r["instance_id"]: r for r in load(args.compare)}
        out["comparison"] = [
            {"instance_id": r["instance_id"],
             "a": {k: r[k] for k in ("resolved", "exit_status", "steps", "cost", "compactions", "peak_tokens")},
             "b": {k: other[r["instance_id"]][k] for k in ("resolved", "exit_status", "steps", "cost", "compactions", "peak_tokens")}}
            for r in rows if r["instance_id"] in other
        ]
        out["compare_summary"] = summarize(list(other.values()))
    if args.overflow_causes:
        out["overflow_causes"] = {
            r["instance_id"]: overflow_cause(Path(args.results) / r["instance_id"])
            for r in rows if r["exit_status"] == "ContextOverflow"
        }
    text = json.dumps(out, indent=2, sort_keys=True, default=float)
    if args.json:
        Path(args.json).write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
