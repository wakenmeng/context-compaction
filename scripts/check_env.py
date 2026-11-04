#!/usr/bin/env python3
"""Environment check for M0, exits non-zero on any failure, never prints secrets

Fast checks only. Whether the harness and repo tests work under emulation is
answered by scripts/gold_eval.py
"""
import importlib.metadata
import os
import platform
import subprocess
import sys

from context_compaction import credentials, pins
from fetch_dataset import verify

PROBE_IMAGE = "docker.io/swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"


def run(cmd, timeout=600):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    # stdout carries the answer, stderr only matters on failure (pull progress lands there)
    text = out.stdout.strip() if out.returncode == 0 else (out.stderr or out.stdout).strip()
    return out.returncode, text


def check_python():
    found = sys.version_info[:2]
    want = pins.PYTHON
    return found == want, f"python {platform.python_version()}, pinned {want[0]}.{want[1]}"


def check_packages():
    results = []
    for name, want in pins.PACKAGES.items():
        try:
            found = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            found = None
        results.append((found == want, f"{name} {found or 'missing'}, pinned {want}"))
    return results


def check_api_key(env=os.environ):
    # only the source is reported, the value is never formatted anywhere
    source = credentials.load(env)["OPENAI_API_KEY"]
    where = {"env": "set from environment", "file": f"loaded from {credentials.credentials_dir(env)}"}
    return source is not None, "OPENAI_API_KEY " + where.get(source, "missing")


def check_dataset():
    problem = verify()
    return problem is None, problem or f"dataset snapshot ok @ {pins.DATASET_REVISION[:12]}"


def check_docker():
    code, out = run(["docker", "version", "--format", "{{.Server.Arch}}"], timeout=30)
    if code:
        return [(False, f"docker not running: {out.splitlines()[-1] if out else code}")]
    results = [(True, f"docker running, server arch {out}")]
    code, out = run(["docker", "run", "--rm", "--platform", "linux/amd64", PROBE_IMAGE, "python", "--version"])
    last = out.splitlines()[-1] if out else f"exit {code}"
    results.append((code == 0 and last.startswith("Python "), f"amd64 probe image: {last}"))
    return results


def checks():
    yield True, f"host arch {platform.machine()}"
    yield check_python()
    yield from check_packages()
    yield check_api_key()
    yield check_dataset()
    yield from check_docker()


def main():
    failed = 0
    for ok, message in checks():
        print(("ok   " if ok else "FAIL ") + message)
        failed += not ok
    if failed:
        sys.exit(f"{failed} check(s) failed")
    print("all checks passed, next: scripts/gold_eval.py")


if __name__ == "__main__":
    main()
