"""Repo file hashes, taken by the harness inside the container after every action

The probe is a small Python script (3.5 compatible, since SWE-bench images ship
old interpreters) that prints git blob hashes as JSON. These commands are
harness bookkeeping: they are not agent steps and are not logged as actions
"""
import json

REPO = "/testbed"

# all tracked files at HEAD, then working-tree changes on top
_PROBE = r'''
import hashlib, json, os, subprocess, sys
os.chdir(sys.argv[1])
def blob(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except (IOError, OSError):
        return None
    h = hashlib.sha1(("blob %d\0" % len(data)).encode())
    h.update(data)
    return h.hexdigest()
out = {}
if sys.argv[2] == "tracked":
    raw = subprocess.check_output(["git", "ls-files", "-s", "-z"])
    for entry in raw.split(b"\0"):
        if entry:
            meta, path = entry.split(b"\t", 1)
            out[path.decode("utf-8", "surrogateescape")] = meta.split()[1].decode()
    sys.stdout.write("CC_FILES " + json.dumps(out, sort_keys=True))
    sys.exit(0)
raw = subprocess.check_output(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"])
items = raw.split(b"\0")
i = 0
while i < len(items):
    item = items[i]
    i += 1
    if len(item) < 4:
        continue
    code, path = item[:2], item[3:].decode("utf-8", "surrogateescape")
    if code[:1] in (b"R", b"C"):
        old = items[i].decode("utf-8", "surrogateescape")
        i += 1
        if code[:1] == b"R":
            out[old] = None
    out[path] = blob(path) if os.path.isfile(path) else None
sys.stdout.write("CC_FILES " + json.dumps(out, sort_keys=True))
'''


def _command(mode, repo=REPO):
    script = _PROBE.replace("'", "'\"'\"'")
    return f"python3 -c '{script}' {repo} {mode}"


def parse(output):
    marker = output.rfind("CC_FILES ")
    if marker < 0:
        raise RuntimeError(f"file probe failed: {output[-500:]}")
    return json.loads(output[marker + len("CC_FILES "):])


class RepoFiles:
    """Tracks the full path -> blob hash map of the repo across actions"""

    def __init__(self, env, repo=REPO, timeout=120):
        self.env, self.repo, self.timeout = env, repo, timeout
        self.base = {}

    def _run(self, mode):
        out = self.env.execute({"command": _command(mode, self.repo)}, timeout=self.timeout)
        if out.get("returncode") != 0:
            raise RuntimeError(f"file probe exited {out.get('returncode')}: {out.get('output', '')[-500:]}")
        return parse(out.get("output", ""))

    def snapshot(self):
        """Hashes of every file at the start; later calls diff against the tracked set"""
        self.base = self._run("tracked")
        return self.current()

    def current(self):
        """Hashes now: the tracked index with every working-tree change applied"""
        files = dict(self.base)
        for path, digest in self._run("changes").items():
            if digest is None:
                files.pop(path, None)
            else:
                files[path] = digest
        return files
