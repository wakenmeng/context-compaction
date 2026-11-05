"""Working state schema and its versioned store

The state is a plain JSON dict. Every change produces a new version, and old
versions are never edited, so a rejected patch cannot alter history
"""
import copy
import json
from pathlib import Path

SCHEMA_VERSION = 2

FACT_STATUS = {"observed", "verified", "assumed", "stale", "superseded"}
CHECK_STATUS = {"valid", "stale"}
EVIDENCED = {"observed", "verified"}

# fields that live in the state, in render order
LIST_FIELDS = ("done_when", "constraints", "facts", "changes", "checks", "dead_ends", "pointers")
HARNESS_FIELDS = ("schema_version", "state_version", "task", "fallback_recap")
MODEL_FIELDS = LIST_FIELDS + ("plan", "next_action")
ID_FIELDS = {"constraints": "c", "facts": "f", "checks": "k", "dead_ends": "d"}

# kinds of versions kept by the store
KINDS = {"initial", "patch", "rebuild", "fallback", "invalidate"}


def new_state(task):
    state = {"schema_version": SCHEMA_VERSION, "state_version": 0, "task": task}
    state.update({name: [] for name in LIST_FIELDS})
    state.update({"plan": [], "next_action": "", "fallback_recap": None})
    return state


def visible(state):
    """What the model sees as the state: everything but the task, which is shown on its own"""
    return {k: v for k, v in state.items() if k != "task"}


def render(state):
    return json.dumps(visible(state), ensure_ascii=False, indent=1)


class StateStore:
    def __init__(self, task, directory=None):
        self.dir = Path(directory) if directory else None
        self.versions: list[tuple[str, dict]] = []
        self._commit("initial", new_state(task))

    def _commit(self, kind, state):
        if kind not in KINDS:
            raise ValueError(f"unknown version kind {kind}")
        state = copy.deepcopy(state)
        state["state_version"] = len(self.versions)
        self.versions.append((kind, state))
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / f"v{state['state_version']:04d}.json"
            path.write_text(json.dumps({"kind": kind, "state": state}, ensure_ascii=False, indent=1))
        return copy.deepcopy(state)

    def commit(self, kind, state):
        if kind == "initial":
            raise ValueError("initial is created by the store")
        return self._commit(kind, state)

    def current(self):
        return copy.deepcopy(self.versions[-1][1])

    def current_kind(self):
        return self.versions[-1][0]

    def last_valid(self):
        """Latest version that is a real state, i.e. not a text fallback"""
        for kind, state in reversed(self.versions):
            if kind != "fallback":
                return copy.deepcopy(state)
        raise AssertionError("store always has an initial version")

    def in_fallback(self):
        return self.current_kind() == "fallback"
