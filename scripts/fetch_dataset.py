#!/usr/bin/env python3
"""Download the pinned SWE-bench Verified snapshot and verify its sha256"""
import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

from context_compaction import pins


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def url():
    return (
        f"https://huggingface.co/datasets/{pins.DATASET}/resolve/"
        f"{pins.DATASET_REVISION}/{pins.DATASET_REMOTE_FILE}"
    )


def verify(path=pins.DATASET_PATH):
    """Return None if the snapshot is intact, else the reason"""
    path = Path(path)
    if not path.exists():
        return f"{path} missing, run scripts/fetch_dataset.py"
    actual = file_sha256(path)
    if actual != pins.DATASET_SHA256:
        return f"{path} sha256 {actual} != pinned {pins.DATASET_SHA256}"
    return None


def fetch(path=pins.DATASET_PATH):
    path = Path(path)
    if verify(path) is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    urllib.request.urlretrieve(url(), tmp)
    actual = file_sha256(tmp)
    if actual != pins.DATASET_SHA256:
        tmp.unlink()
        raise SystemExit(f"downloaded sha256 {actual} != pinned {pins.DATASET_SHA256}")
    tmp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="only check, do not download")
    args = parser.parse_args()
    if not args.verify:
        fetch()
    problem = verify()
    if problem:
        sys.exit(problem)
    print(f"ok {pins.DATASET_PATH} @ {pins.DATASET_REVISION[:12]}")


if __name__ == "__main__":
    main()
