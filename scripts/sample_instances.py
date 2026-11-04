#!/usr/bin/env python3
"""Sample SWE-bench Verified instances by repo, reproducibly

Formal set: stratified by repo with randomized systematic rounding, so every
instance has the same inclusion probability and the plain mean is unbiased
for the 500 tasks, small repos included

Smoke set: drawn from the rest, one per repo where possible, then
proportional. It is for tuning and environment checks, never for estimates

All randomness comes from sha256, so results do not depend on Python version
"""
import argparse
import hashlib
import json
import math
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

from context_compaction import pins
from fetch_dataset import file_sha256, verify

SEED = 20261104
FORMAL_SIZE = 100
SMOKE_SIZE = 20


def rank(seed, salt, key):
    return hashlib.sha256(f"{seed}:{salt}:{key}".encode()).hexdigest()


def uniform(seed, salt):
    """Exact fraction in [0, 1) derived from the seed"""
    return Fraction(int(rank(seed, salt, "u")[:16], 16), 16**16)


def allocate(counts, size, u):
    """Split size across repos by systematic rounding with offset u in [0, 1)

    Each repo gets floor or ceil of its quota, the total is exact, and over
    uniform u the expected count equals the quota
    """
    total = sum(counts.values())
    if not 0 <= size <= total:
        raise ValueError(f"size must be in 0..{total}, got {size}")
    if not 0 <= u < 1:
        raise ValueError(f"u must be in [0, 1), got {u}")

    if size == 0:
        return {repo: 0 for repo in sorted(counts)}

    result, cum = {}, Fraction(0)
    for repo in sorted(counts):
        start = math.floor(cum + u)
        cum += Fraction(counts[repo] * size, total)
        result[repo] = math.floor(cum + u) - start
    return result


def pick(by_repo, allocation, seed, salt):
    chosen = []
    for repo, n in allocation.items():
        ordered = sorted(by_repo[repo], key=lambda i: rank(seed, salt, i))
        chosen.extend(ordered[:n])
    return sorted(chosen)


def group(ids_with_repo):
    by_repo = defaultdict(list)
    for instance_id, repo in ids_with_repo:
        by_repo[repo].append(instance_id)
    return dict(by_repo)


def records_sha256(records):
    """Fingerprint of every field of every record, independent of order"""
    rows = sorted(records, key=lambda r: r["instance_id"])
    text = "\n".join(json.dumps(r, sort_keys=True, default=str) for r in rows)
    return hashlib.sha256(text.encode()).hexdigest()


def sample(records, seed=SEED, formal_size=FORMAL_SIZE, smoke_size=SMOKE_SIZE):
    ids = [r["instance_id"] for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate instance_id in dataset")
    if formal_size + smoke_size > len(ids):
        raise ValueError("formal_size + smoke_size exceeds population")

    by_repo = group((r["instance_id"], r["repo"]) for r in records)
    counts = {repo: len(v) for repo, v in by_repo.items()}

    formal_alloc = allocate(counts, formal_size, uniform(seed, "formal"))
    formal = pick(by_repo, formal_alloc, seed, "formal")

    taken = set(formal)
    rest = {repo: [i for i in v if i not in taken] for repo, v in by_repo.items()}
    rest = {repo: v for repo, v in rest.items() if v}
    base = {repo: 1 for repo in rest} if smoke_size >= len(rest) else {}
    extra = allocate(
        {repo: len(v) - base.get(repo, 0) for repo, v in rest.items()},
        smoke_size - len(base),
        uniform(seed, "smoke"),
    )
    smoke_alloc = {repo: base.get(repo, 0) + extra[repo] for repo in sorted(rest)}
    smoke = pick(rest, smoke_alloc, seed, "smoke")

    formal_repos = {repo for repo, n in formal_alloc.items() if n}
    return {
        "seed": seed,
        "population": len(ids),
        "records_sha256": records_sha256(records),
        "repo_counts": dict(sorted(counts.items())),
        "formal": {
            "inclusion_probability": str(Fraction(formal_size, len(ids))),
            "allocation": formal_alloc,
            "ids": formal,
        },
        "smoke": {
            "allocation": smoke_alloc,
            "missing_formal_repos": sorted(formal_repos - {r for r, n in smoke_alloc.items() if n}),
            "ids": smoke,
        },
    }


def load_records(path):
    path = Path(path)
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq

        return pq.read_table(path).to_pylist()
    text = path.read_text()
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


def write_once(output, payload):
    output = Path(output)
    if output.exists():
        if output.read_text() != payload:
            raise SystemExit(f"{output} already exists with different content, refusing to overwrite")
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", help="local .parquet/.json/.jsonl instead of the pinned snapshot")
    parser.add_argument("--output", default=str(pins.INSTANCES_PATH))
    args = parser.parse_args(argv)

    if args.input:
        path, source = Path(args.input), {"dataset": "local", "file_sha256": file_sha256(args.input)}
    else:
        problem = verify()
        if problem:
            raise SystemExit(problem)
        path = pins.DATASET_PATH
        source = {
            "dataset": pins.DATASET,
            "revision": pins.DATASET_REVISION,
            "file_sha256": pins.DATASET_SHA256,
        }

    records = load_records(path)
    if not args.input and len(records) != pins.DATASET_SIZE:
        raise SystemExit(f"expected {pins.DATASET_SIZE} instances, got {len(records)}")

    result = {"source": source, **sample(records)}
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    wrote = write_once(args.output, payload)
    verb = "wrote" if wrote else "unchanged"
    print(f"{verb} {args.output}: {len(result['smoke']['ids'])} smoke, {len(result['formal']['ids'])} formal")


if __name__ == "__main__":
    main()
