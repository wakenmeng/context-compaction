"""Patch validation, harness-side invalidation and rebuild

apply_patch and rebuild never mutate their input: they work on a copy and
either return the new state or raise PatchRejected listing every failed rule.
Both finish with the harness invalidation pass, so evidence is judged by the
harness against the event log, never by what the model claims
"""
import copy
import posixpath
import re

from .event_log import parse_id
from .state import (
    CHECK_STATUS,
    EVIDENCED,
    FACT_STATUS,
    HARNESS_FIELDS,
    ID_FIELDS,
    LIST_FIELDS,
    MODEL_FIELDS,
    render,
)
from .tokens import count_tokens

OPS = {"add", "update", "remove", "set"}
LINE_NUMBER = re.compile(r"(:\d+(:\d+)?$)|(#L\d+)|(\bline\s*\d+)", re.IGNORECASE)
# per-entry bookkeeping only the harness may write
HARNESS_KEYS = {"stale_since", "stale_from", "revalidate_after", "premise_changed", "premise_changed_at"}


class PatchRejected(Exception):
    def __init__(self, errors, over_cap=False):
        super().__init__("; ".join(errors))
        self.errors = list(errors)
        self.over_cap = over_cap


def state_tokens(state, count=count_tokens):
    return count(render(state))


def _by_id(items, ident, key="id"):
    for item in items or []:
        if isinstance(item, dict) and item.get(key) == ident:
            return item
    return None


def _index(items, key, value):
    for i, item in enumerate(items):
        if isinstance(item, dict) and item.get(key) == value:
            return i
    return None


def canonical_path(path):
    """Repo-relative POSIX path exactly as the harness records it"""
    return (
        isinstance(path, str)
        and bool(path)
        and "\\" not in path
        and not path.startswith("/")
        and posixpath.normpath(path) == path
        and not path.startswith("../")
        and path != ".."
    )


def _key(field):
    return {"changes": "path", "pointers": "label"}.get(field, "id")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _harness_keys(value):
    return sorted(k for k in value if k in HARNESS_KEYS or str(k).startswith("_"))


# ---------- applying ops ----------


def _apply_op(state, op, shrink, errors, where):
    kind, field = op.get("op"), op.get("field")
    if not isinstance(kind, str) or kind not in OPS:
        return errors.append(f"{where}: unknown op {kind!r}")
    if field in HARNESS_FIELDS:
        return errors.append(f"{where}: {field} is written by the harness only")
    if not isinstance(field, str) or field not in MODEL_FIELDS:
        return errors.append(f"{where}: unknown field {field!r}")

    if kind == "set":
        if field not in ("plan", "next_action"):
            return errors.append(f"{where}: set only applies to plan and next_action")
        state[field] = copy.deepcopy(op.get("value"))
        return
    if field in ("plan", "next_action"):
        return errors.append(f"{where}: {field} only supports set")

    items, value, key = state[field], op.get("value"), _key(field)

    if kind == "add":
        if field == "done_when":
            if not isinstance(value, str) or not value.strip():
                return errors.append(f"{where}: done_when entries are non-empty strings")
            items.append(value)
            return
        if not isinstance(value, dict):
            return errors.append(f"{where}: {field} entries are objects")
        if _harness_keys(value):
            return errors.append(f"{where}: keys {_harness_keys(value)} are written by the harness only")
        if not isinstance(value.get(key), str) or not value[key]:
            return errors.append(f"{where}: {field} entry needs a string {key}")
        if _index(items, key, value[key]) is not None:
            return errors.append(f"{where}: duplicate {field} {key} {value[key]!r}")
        items.append(copy.deepcopy(value))
        return

    if kind == "update":
        if field in ("done_when", "dead_ends", "pointers"):
            return errors.append(f"{where}: {field} is append-only")
        target = op.get(key)
        index = _index(items, key, target)
        if index is None:
            return errors.append(f"{where}: no {field} entry {target!r}")
        if not isinstance(value, dict) or (key in value and value[key] != target):
            return errors.append(f"{where}: update value must be an object and keep {key}")
        if _harness_keys(value):
            return errors.append(f"{where}: keys {_harness_keys(value)} are written by the harness only")
        items[index] = {**items[index], **copy.deepcopy(value)}
        return

    # remove: only while shrinking an over-cap state
    if not shrink:
        return errors.append(f"{where}: remove is only allowed when shrinking an over-cap state")
    index = _index(items, key, op.get(key))
    if field == "pointers":
        return errors.append(f"{where}: pointers have no status, so they cannot be removed")
    if field in ("facts", "checks", "dead_ends") and index is None:
        return errors.append(f"{where}: no {field} entry {op.get(key)!r}")
    if field in ("facts", "checks"):
        if items[index].get("status") not in ("stale", "superseded"):
            return errors.append(f"{where}: only stale or superseded {field} can be removed")
        del items[index]
    elif field == "dead_ends":
        items[index] = {**items[index], "_removed": True}  # resolved after all ops
    else:
        errors.append(f"{where}: {field} entries cannot be removed")


def _resolve_dead_end_merges(state, errors):
    removed = [d for d in state["dead_ends"] if d.get("_removed")]
    merged = {i for d in state["dead_ends"] if not d.get("_removed") for i in d.get("merged_from", [])}
    for d in removed:
        if d["id"] not in merged:
            errors.append(f"dead_end {d['id']} removed without a new entry listing it in merged_from")
    state["dead_ends"] = [d for d in state["dead_ends"] if not d.get("_removed")]


# ---------- validation ----------


def evidence_point(item, log):
    """Latest event backing an entry: its newest existing source, else at_event

    Judging freshness by the sources, not the model-chosen at_event, stops an
    old observation from being relabelled as new
    """
    seqs = [parse_id(r) for r in item.get("sources") or [] if isinstance(r, str) and log.kind(r) == "action"]
    if seqs:
        return f"e{max(seqs)}"
    at = item.get("at_event")
    return at if log.exists(at) else None


def _check_evidence(item, log, where, errors):
    at = item.get("at_event")
    if not log.exists(at):
        errors.append(f"{where}: at_event {at!r} is not an existing event")
    files = item.get("files")
    if not isinstance(files, list) or not all(canonical_path(f) for f in files):
        errors.append(f"{where}: files must be canonical repo-relative paths (empty means the whole repo for checks)")
    refs = item.get("sources")
    if not isinstance(refs, list) or not refs:
        return errors.append(f"{where}: sources must be a non-empty list of event ids")
    for ref in refs:
        if not log.exists(ref):
            errors.append(f"{where}: source {ref!r} is not an existing event")
        elif log.kind(ref) != "action":
            errors.append(f"{where}: source {ref} is a {log.kind(ref)} event, evidence must cite actions")
        elif log.exists(at) and parse_id(ref) > parse_id(at):
            errors.append(f"{where}: source {ref} is later than at_event {at}")


def _check_status_change(item, before, ok_status, log, where, errors):
    """The model may not mark anything stale, and may revive a stale entry only with newer evidence"""
    status, old = item.get("status"), (before or {}).get("status")
    for k in HARNESS_KEYS:
        if (before or {}).get(k) != item.get(k):
            errors.append(f"{where}: {k} is written by the harness only")
    if status == "stale" and old != "stale":
        errors.append(f"{where}: only the harness marks entries stale")
    gate = revalidation_gate(before)
    if gate and status in ok_status:
        since = parse_id(gate)
        fresh = [
            r for r in item.get("sources") or []
            if isinstance(r, str) and log.kind(r) == "action" and parse_id(r) > since
        ]
        if not fresh:
            errors.append(f"{where}: reviving a stale entry needs a source action after {gate}")


def revalidation_gate(item):
    """Event after which new evidence is required: set while stale, kept if downgraded to assumed"""
    if not item:
        return None
    if item.get("status") == "stale":
        return item.get("stale_since")
    return item.get("revalidate_after")


def validate_state(state, log, previous=None, require_complete=True):
    """Check a whole candidate state; `previous` enables the cross-version rules"""
    errors = []
    prev = previous or {}

    for name in LIST_FIELDS:
        items = state.get(name)
        if not isinstance(items, list):
            errors.append(f"{name} must be a list")
            state[name] = []
            continue
        want = str if name == "done_when" else dict
        if not all(isinstance(i, want) for i in items):
            errors.append(f"{name} entries must be {want.__name__}s")
            return errors  # later checks assume the right entry types
    for name in (*ID_FIELDS, "pointers"):
        key = _key(name)
        ids = [i.get(key) for i in state[name]]
        if not all(isinstance(i, str) and i for i in ids):
            errors.append(f"{name} entries need a string {key}")
        elif len(ids) != len(set(ids)):
            errors.append(f"{name} {key}s must be unique")

    if previous is not None:
        if state.get("task") != previous.get("task"):
            errors.append("task changed")
        old_done = previous.get("done_when", [])
        if state["done_when"][: len(old_done)] != old_done:
            errors.append("done_when is append-only")
        missing = {c["id"] for c in previous.get("constraints", [])} - {c.get("id") for c in state["constraints"]}
        if missing:
            errors.append(f"constraints cannot be removed: {sorted(missing)}")
        for d in state["dead_ends"]:
            before = _by_id(previous.get("dead_ends"), d.get("id"))
            if before is not None and before != d:
                errors.append(f"dead_end {d.get('id')} is append-only")

    if require_complete:
        if not state["done_when"]:
            errors.append("done_when must not be empty")
        plan = state.get("plan")
        if not isinstance(plan, list) or not plan or not all(isinstance(s, str) and s.strip() for s in plan):
            errors.append("plan must be a non-empty list of strings")
        if not isinstance(state.get("next_action"), str) or not state["next_action"].strip():
            errors.append("next_action must not be empty")

    for c in state["constraints"]:
        if not _text(c.get("text")) or not _text(c.get("reason")):
            errors.append(f"constraint {c.get('id')}: needs text and reason strings")

    for f in state["facts"]:
        where, status = f"fact {f.get('id')}", f.get("status")
        if not isinstance(status, str) or status not in FACT_STATUS:
            errors.append(f"{where}: status must be one of {sorted(FACT_STATUS)}")
            continue
        if not _text(f.get("text")):
            errors.append(f"{where}: needs a text string")
        before = _by_id(prev.get("facts"), f.get("id"))
        _check_status_change(f, before, EVIDENCED, log, where, errors)
        claims_evidence = status in EVIDENCED or (status == "stale" and f.get("stale_from") in EVIDENCED)
        if claims_evidence:
            _check_evidence(f, log, where, errors)
        else:
            # no evidence claimed, but whatever is given must still resolve
            if "files" in f and (not isinstance(f["files"], list) or not all(canonical_path(x) for x in f["files"])):
                errors.append(f"{where}: files must be canonical repo-relative paths")
            if "at_event" in f and not log.exists(f["at_event"]):
                errors.append(f"{where}: at_event {f['at_event']!r} is not an existing event")
            refs = f.get("sources", [])
            if not isinstance(refs, list) or any(not log.exists(r) for r in refs):
                errors.append(f"{where}: sources must be existing event ids")
        if status == "superseded":
            target = f.get("superseded_by")
            if target == f.get("id") or _by_id(state["facts"], target) is None:
                errors.append(f"{where}: superseded_by must name another fact")

    for k in state["checks"]:
        where, status = f"check {k.get('id')}", k.get("status")
        if not isinstance(status, str) or status not in CHECK_STATUS:
            errors.append(f"{where}: status must be one of {sorted(CHECK_STATUS)}")
            continue
        if not _text(k.get("command")) or not _text(k.get("result")):
            errors.append(f"{where}: needs command and result strings")
        _check_status_change(k, _by_id(prev.get("checks"), k.get("id")), {"valid"}, log, where, errors)
        _check_evidence(k, log, where, errors)

    for d in state["dead_ends"]:
        where = f"dead_end {d.get('id')}"
        if not _text(d.get("approach")) or not _text(d.get("reason")):
            errors.append(f"{where}: needs approach and reason strings")
        if not log.exists(d.get("at_event")):
            errors.append(f"{where}: at_event {d.get('at_event')!r} is not an existing event")
        if not isinstance(d.get("files", []), list) or not all(canonical_path(f) for f in d.get("files", [])):
            errors.append(f"{where}: files must be a list of canonical repo-relative paths")
        if not isinstance(d.get("merged_from", []), list):
            errors.append(f"{where}: merged_from must be a list")

    for ch in state["changes"]:
        if not canonical_path(ch.get("path")):
            errors.append(f"changes path {ch.get('path')!r} must be a canonical repo-relative path")
        elif not _text(ch.get("summary")) or not isinstance(ch.get("verified"), bool):
            errors.append(f"change {ch.get('path')}: needs a summary string and a boolean verified")

    for p in state["pointers"]:
        where = f"pointer {p.get('label')!r}"
        has_ref, has_path = "ref" in p, "path" in p
        allowed = {"label", "ref"} if has_ref else {"label", "path", "symbol"}
        if has_ref != has_path and set(p) - allowed:
            errors.append(f"{where}: unexpected keys {sorted(set(p) - allowed)}")
        if has_ref == has_path:
            errors.append(f"{where}: give exactly one of ref (event id) or path")
        elif has_ref and not log.exists(p["ref"]):
            errors.append(f"{where}: ref must be an existing event id, got {p['ref']!r}")
        elif has_path:
            path = p["path"]
            if LINE_NUMBER.search(str(path)) or LINE_NUMBER.search(str(p.get("symbol", ""))):
                errors.append(f"{where}: environment refs must not carry line numbers")
            elif not canonical_path(path):
                errors.append(f"{where}: path must be a canonical repo-relative path")

    return errors


def _finish(state, previous):
    """Update stale bookkeeping after an accepted patch

    Leaving stale for any status other than a revalidated one keeps the gate, so
    a later upgrade still needs evidence newer than the change
    """
    for name in ("facts", "checks"):
        for item in state[name]:
            if item.get("status") == "stale":
                continue
            before = _by_id(previous.get(name), item.get("id")) if previous else None
            gate = revalidation_gate(before)
            item.pop("stale_since", None)
            item.pop("stale_from", None)
            # the gate stays until the entry is revalidated (assumed, superseded, ...)
            if item.get("status") in ("observed", "verified", "valid"):
                item.pop("revalidate_after", None)
            elif gate:
                item["revalidate_after"] = gate


# ---------- public operations ----------


def invalidate(state, log):
    """Harness pass: mark evidence stale when its files changed after the evidence

    Returns (new_state, ids newly marked stale). Checks with empty files depend
    on the whole repo; facts with empty files depend on no repo file
    """
    new = copy.deepcopy(state)
    marked = []
    for name, live in (("facts", EVIDENCED | {"assumed"}), ("checks", {"valid"})):
        for item in new[name]:
            if item.get("status") not in live:
                continue
            point = evidence_point(item, log)
            files = item.get("files") or []
            if point is None or (name == "facts" and not files):
                continue
            hit = log.changed_after(point, files)
            if hit is not None:
                item["stale_from"] = item["status"]
                item["status"] = "stale"
                item["stale_since"] = hit.id
                marked.append(item["id"])
    for d in new["dead_ends"]:
        if d.get("premise_changed") or not d.get("files") or not log.exists(d.get("at_event")):
            continue
        hit = log.changed_after(d["at_event"], d["files"])
        if hit is not None:
            d["premise_changed"] = True
            d["premise_changed_at"] = hit.id
    return new, marked


def _guard(fn):
    """Malformed but parseable JSON must be a rejection, never a crash"""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except PatchRejected:
            raise
        except (TypeError, AttributeError, KeyError, ValueError) as e:
            raise PatchRejected([f"malformed input: {type(e).__name__}: {e}"]) from e

    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


def _check_size(new, cap_tokens, count):
    size = state_tokens(new, count)
    if size > cap_tokens:
        raise PatchRejected([f"state is {size} tokens, cap is {cap_tokens}"], over_cap=True)


@_guard
def apply_patch(state, patch, log, cap_tokens=3000, count=count_tokens, shrink=False):
    if not isinstance(patch, dict) or not isinstance(patch.get("ops"), list):
        raise PatchRejected(["patch must be an object with an ops list"])
    errors = []
    if patch.get("base_version") != state["state_version"]:
        errors.append(f"base_version {patch.get('base_version')} != current {state['state_version']}")
    new = copy.deepcopy(state)
    for i, op in enumerate(patch["ops"]):
        if not isinstance(op, dict):
            errors.append(f"op {i}: must be an object")
            continue
        _apply_op(new, op, shrink, errors, f"op {i}")
    _resolve_dead_end_merges(new, errors)
    if not errors:
        errors += validate_state(new, log, previous=state)
    if errors:
        raise PatchRejected(errors)
    _finish(new, state)
    new, _ = invalidate(new, log)
    _check_size(new, cap_tokens, count)
    return new


@_guard
def rebuild(last_valid, proposal, log, cap_tokens=3000, count=count_tokens):
    """New state cycle after a fallback: the model rewrites its fields from scratch

    task, done_when and constraints carry over from the last valid version and
    can only be extended; everything else may drop old entries. Stale status is
    recomputed by the harness afterwards, so a new id cannot revive old evidence
    """
    if not isinstance(proposal, dict):
        raise PatchRejected(["rebuild must be a JSON object with the state fields"])
    errors = [f"{k} is written by the harness only" for k in proposal if k in HARNESS_FIELDS and k != "state_version"]
    errors += [f"unknown field {k!r}" for k in proposal if k not in MODEL_FIELDS and k not in HARNESS_FIELDS]

    new = copy.deepcopy(last_valid)
    new["fallback_recap"] = None
    for name in MODEL_FIELDS:
        if name not in ("done_when", "constraints"):
            new[name] = copy.deepcopy(proposal.get(name, "" if name == "next_action" else []))

    extra_done = proposal.get("done_when", [])
    if not isinstance(extra_done, list):
        errors.append("done_when must be a list")
        extra_done = []
    new["done_when"] = last_valid["done_when"] + [d for d in extra_done if d not in last_valid["done_when"]]
    extra_constraints = proposal.get("constraints", [])
    if not isinstance(extra_constraints, list):
        errors.append("constraints must be a list")
        extra_constraints = []
    old = {c["id"]: c for c in last_valid["constraints"]}
    for c in extra_constraints:
        if isinstance(c, dict) and c.get("id") in old:
            if c != old[c["id"]]:
                errors.append(f"constraint {c['id']} cannot be changed in a rebuild")
        else:
            new["constraints"].append(copy.deepcopy(c))

    for name in LIST_FIELDS:
        for item in new[name] if isinstance(new[name], list) else []:
            if isinstance(item, dict) and _harness_keys(item) and name not in ("done_when", "constraints"):
                errors.append(f"{name} entry {item.get(_key(name))!r}: keys {_harness_keys(item)} are harness only")
            if name in ("facts", "checks") and isinstance(item, dict) and item.get("status") == "stale":
                errors.append(f"{name[:-1]} {item.get('id')}: only the harness marks entries stale")
    if not errors:
        errors += validate_state(new, log, previous=None)
    if errors:
        raise PatchRejected(errors)
    new, _ = invalidate(new, log)
    _check_size(new, cap_tokens, count)
    return new
