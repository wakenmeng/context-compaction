"""Append-only event log kept by the harness outside the container

The first event is a snapshot of repo file hashes; every action event records
which files it changed (new hash, or None when deleted), so the harness, not
the model, decides what changed and when
"""
import copy
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

KINDS = {"snapshot", "action", "model", "compaction", "note"}


@dataclass(frozen=True)
class Event:
    seq: int
    kind: str
    payload: dict = field(default_factory=dict)
    changes: dict = field(default_factory=dict)  # path -> sha256 or None
    ts: float = 0.0

    @property
    def id(self):
        return event_id(self.seq)


def event_id(seq):
    return f"e{seq}"


def parse_id(ref):
    """Return the seq of an event id like 'e12', or None if it is not one"""
    if isinstance(ref, str) and len(ref) > 1 and ref[0] == "e" and ref[1:].isdigit():
        if ref[1:] == "0" or not ref[1:].startswith("0"):
            return int(ref[1:])
    return None


class EventLog:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._events: list[Event] = []
        self._files: dict[str, str] = {}
        if self.path and self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    self._add(Event(**json.loads(line)), persist=False)

    def __len__(self):
        return len(self._events)

    # read access hands out copies, so evidence cannot be edited through it
    @property
    def events(self):
        return copy.deepcopy(self._events)

    @property
    def files(self):
        return dict(self._files)

    def _add(self, event, persist=True):
        if event.seq != len(self._events):
            raise ValueError(f"event seq {event.seq} out of order, expected {len(self._events)}")
        if event.kind not in KINDS:
            raise ValueError(f"unknown event kind {event.kind}")
        event = copy.deepcopy(event)
        self._events.append(event)
        for path, digest in event.changes.items():
            if digest is None:
                self._files.pop(path, None)
            else:
                self._files[path] = digest
        if persist and self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(asdict(event), sort_keys=True) + "\n")
        return event

    def append(self, kind, changes=None, **payload):
        if kind == "snapshot" and self._events:
            raise ValueError("snapshot must be the first event")
        if kind != "snapshot" and not self._events:
            raise ValueError("first event must be a snapshot")
        if changes and kind not in ("snapshot", "action"):
            raise ValueError("only snapshot and action events may change files")
        event = Event(len(self._events), kind, copy.deepcopy(payload), dict(changes or {}), time.time())
        return copy.deepcopy(self._add(event))

    def snapshot(self, files):
        return self.append("snapshot", changes=files)

    def action(self, command, output, files_now, **extra):
        """Record a shell command, diffing the repo hashes the harness took after it"""
        changes = {p: h for p, h in files_now.items() if self._files.get(p) != h}
        changes.update({p: None for p in self._files if p not in files_now})
        return self.append("action", changes=changes, command=command, output=output, **extra)

    def get(self, ref):
        seq = parse_id(ref)
        if seq is None or seq >= len(self._events):
            return None
        return copy.deepcopy(self._events[seq])

    def exists(self, ref):
        seq = parse_id(ref)
        return seq is not None and seq < len(self._events)

    def kind(self, ref):
        return self._events[parse_id(ref)].kind if self.exists(ref) else None

    def changed_after(self, ref, files=None):
        """First event after `ref` that changed any of `files` (any repo file if empty)"""
        start = parse_id(ref)
        wanted = set(files or ())
        for event in self._events[start + 1:]:
            if event.kind != "action" or not event.changes:
                continue
            if not wanted or wanted & set(event.changes):
                return copy.deepcopy(event)
        return None

    def repo_changed_between(self, start_ref, end_ref):
        """Whether any repo file changed after start_ref, up to and including end_ref"""
        start, end = parse_id(start_ref), parse_id(end_ref)
        return any(e.changes for e in self._events[start + 1:end + 1] if e.kind == "action")
