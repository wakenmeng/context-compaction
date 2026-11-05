import copy
import json

import pytest

from context_compaction.compactor import Compactor, parse_json
from context_compaction.event_log import EventLog, parse_id
from context_compaction.patch import PatchRejected, apply_patch, invalidate, rebuild, validate_state
from context_compaction.state import StateStore, new_state

CHARS = len  # deterministic size for cap tests


def make_log(tmp_path=None):
    log = EventLog(tmp_path / "events.jsonl" if tmp_path else None)
    log.snapshot({"src/a.py": "h1", "src/b.py": "h1", "tests/test_a.py": "h1"})  # e0
    log.action("cat src/a.py", "...", {"src/a.py": "h1", "src/b.py": "h1", "tests/test_a.py": "h1"})  # e1
    log.action("pytest tests/test_a.py", "1 failed", {"src/a.py": "h1", "src/b.py": "h1", "tests/test_a.py": "h1"})  # e2
    return log


def good_state():
    """A valid state at version 0, as if the first compaction had produced it"""
    s = new_state("fix the bug")
    s["done_when"] = ["tests pass"]
    s["constraints"] = [{"id": "c1", "text": "keep API", "reason": "public"}]
    s["facts"] = [
        {"id": "f1", "text": "a.py parses headers", "status": "observed", "sources": ["e1"], "at_event": "e1", "files": ["src/a.py"]},
        {"id": "f2", "text": "python 3.11 in image", "status": "observed", "sources": ["e1"], "at_event": "e1", "files": []},
    ]
    s["checks"] = [{"id": "k1", "command": "pytest tests/test_a.py", "result": "failed", "status": "valid", "sources": ["e2"], "at_event": "e2", "files": []}]
    s["dead_ends"] = [{"id": "d1", "approach": "patch b.py", "reason": "wrong module", "at_event": "e2", "files": ["src/b.py"]}]
    s["plan"] = ["fix parse_header"]
    s["next_action"] = "edit src/a.py"
    s["pointers"] = [{"label": "failing log", "ref": "e2"}]
    return s


def patch(*ops, base=0):
    return {"base_version": base, "ops": list(ops)}


def rejected(state, p, log, **kw):
    before = copy.deepcopy(state)
    with pytest.raises(PatchRejected) as exc:
        apply_patch(state, p, log, cap_tokens=10**6, count=CHARS, **kw)
    assert state == before, "a rejected patch must not touch the input"
    return " | ".join(exc.value.errors)


# ---------- EventLog ----------


def test_log_requires_snapshot_first_and_reloads(tmp_path):
    with pytest.raises(ValueError):
        EventLog().action("ls", "", {})
    log = make_log(tmp_path)
    log.action("sed -i ...", "", {"src/a.py": "h2", "tests/test_a.py": "h1"})  # e3: a changed, b deleted
    again = EventLog(tmp_path / "events.jsonl")
    assert [e.id for e in again.events] == ["e0", "e1", "e2", "e3"]
    assert again.get("e3").changes == {"src/a.py": "h2", "src/b.py": None}
    assert again.files == {"src/a.py": "h2", "tests/test_a.py": "h1"}


def test_log_changed_after():
    log = make_log()
    assert log.changed_after("e1", ["src/a.py"]) is None
    log.action("edit b", "", {"src/a.py": "h1", "src/b.py": "h2", "tests/test_a.py": "h1"})  # e3
    assert log.changed_after("e1", ["src/a.py"]) is None
    assert log.changed_after("e1", ["src/b.py"]).id == "e3"
    assert log.changed_after("e1", []).id == "e3"  # empty means the whole repo
    assert log.repo_changed_between("e1", "e2") is False
    assert log.repo_changed_between("e1", "e3") is True


def test_parse_id_is_strict():
    assert parse_id("e0") == 0 and parse_id("e12") == 12
    assert parse_id("e01") is None and parse_id("12") is None and parse_id("f1") is None


# ---------- validation rules ----------


def test_good_state_is_valid():
    assert validate_state(good_state(), make_log()) == []


def test_rule_base_version_mismatch():
    assert "base_version" in rejected(good_state(), patch(base=3), make_log())


@pytest.mark.parametrize(
    "op, needle",
    [
        ({"op": "set", "field": "task", "value": "other"}, "harness only"),
        ({"op": "set", "field": "fallback_recap", "value": "x"}, "harness only"),
        ({"op": "update", "field": "done_when", "id": 0, "value": {}}, "append-only"),
        ({"op": "update", "field": "dead_ends", "id": "d1", "value": {"reason": "x"}}, "append-only"),
        ({"op": "remove", "field": "constraints", "id": "c1"}, "remove is only allowed"),
        ({"op": "set", "field": "facts", "value": []}, "set only applies"),
        ({"op": "frobnicate", "field": "facts"}, "unknown op"),
        ({"op": "add", "field": "nonsense", "value": {}}, "unknown field"),
    ],
)
def test_rule_field_permissions(op, needle):
    assert needle in rejected(good_state(), patch(op), make_log())


def test_rule_constraints_need_reason():
    op = {"op": "add", "field": "constraints", "value": {"id": "c2", "text": "no new deps"}}
    assert "needs text and reason" in rejected(good_state(), patch(op), make_log())


def test_rule_evidenced_fact_needs_valid_sources():
    log = make_log()
    for sources in ([], ["e99"], "e1"):
        op = {"op": "add", "field": "facts", "value": {"id": "f9", "text": "x", "status": "verified", "sources": sources, "at_event": "e1", "files": []}}
        assert "source" in rejected(good_state(), patch(op), log)


def test_rule_source_not_later_than_at_event():
    op = {"op": "add", "field": "facts", "value": {"id": "f9", "text": "x", "status": "observed", "sources": ["e2"], "at_event": "e1", "files": []}}
    assert "later than at_event" in rejected(good_state(), patch(op), make_log())


def test_rule_plan_and_next_action_not_empty():
    log = make_log()
    assert "next_action" in rejected(good_state(), patch({"op": "set", "field": "next_action", "value": " "}), log)
    assert "plan" in rejected(good_state(), patch({"op": "set", "field": "plan", "value": []}), log)


def test_rule_first_compaction_must_fill_done_when():
    s = new_state("t")
    p = patch({"op": "set", "field": "plan", "value": ["a"]}, {"op": "set", "field": "next_action", "value": "ls"})
    assert "done_when" in rejected(s, p, make_log())


def test_rule_events_must_exist():
    log = make_log()
    op = {"op": "add", "field": "checks", "value": {"id": "k2", "command": "x", "result": "ok", "status": "valid", "sources": ["e1"], "at_event": "e42", "files": []}}
    assert "at_event 'e42'" in rejected(good_state(), patch(op), log)
    op = {"op": "add", "field": "pointers", "value": {"label": "gone", "ref": "e42"}}
    assert "existing event id" in rejected(good_state(), patch(op), log)


def test_rule_over_cap_is_flagged():
    with pytest.raises(PatchRejected) as exc:
        apply_patch(good_state(), patch(), make_log(), cap_tokens=10, count=CHARS)
    assert exc.value.over_cap


@pytest.mark.parametrize("missing", ["at_event", "files"])
def test_rule_evidence_needs_at_event_and_files(missing):
    value = {"id": "k2", "command": "x", "result": "ok", "status": "valid", "sources": ["e1"], "at_event": "e1", "files": []}
    del value[missing]
    assert missing in rejected(good_state(), patch({"op": "add", "field": "checks", "value": value}), make_log())


def test_rule_model_cannot_mark_stale():
    op = {"op": "update", "field": "facts", "id": "f1", "value": {"status": "stale"}}
    assert "only the harness" in rejected(good_state(), patch(op), make_log())


def stale_state(log):
    log.action("edit a", "", {"src/a.py": "h2", "src/b.py": "h1", "tests/test_a.py": "h1"})  # e3
    state, marked = invalidate(good_state(), log)
    assert set(marked) == {"f1", "k1"}
    return state


def test_rule_revive_needs_newer_action():
    log = make_log()
    state = stale_state(log)
    old = {"op": "update", "field": "facts", "id": "f1", "value": {"status": "observed", "sources": ["e1"], "at_event": "e1"}}
    assert "reviving a stale entry" in rejected(state, patch(old), log)
    log.action("cat src/a.py", "...", dict(log.files))  # e4, a read after the change
    fresh = {"op": "update", "field": "facts", "id": "f1", "value": {"status": "observed", "sources": ["e4"], "at_event": "e4"}}
    new = apply_patch(state, patch(fresh), log, cap_tokens=10**6, count=CHARS)
    revived = new["facts"][0]
    assert revived["status"] == "observed" and "stale_since" not in revived


def test_rule_revive_cannot_cite_a_non_action_event():
    log = make_log()
    state = stale_state(log)
    log.append("note", text="harness note")  # e4 is not an observation
    op = {"op": "update", "field": "checks", "id": "k1", "value": {"status": "valid", "sources": ["e4"], "at_event": "e4"}}
    assert "reviving a stale entry" in rejected(state, patch(op), log)


@pytest.mark.parametrize(
    "pointer, needle",
    [
        ({"label": "x", "path": "src/a.py:12"}, "line numbers"),
        ({"label": "x", "path": "src/a.py#L12"}, "line numbers"),
        ({"label": "x", "path": "src/a.py", "symbol": "line 40"}, "line numbers"),
        ({"label": "x", "path": "/etc/passwd"}, "relative"),
        ({"label": "x", "path": "../outside.py"}, "relative"),
        ({"label": "x", "ref": "log-2"}, "existing event id"),
        ({"label": "x", "ref": "e1", "path": "src/a.py"}, "exactly one"),
    ],
)
def test_rule_pointer_kinds(pointer, needle):
    assert needle in rejected(good_state(), patch({"op": "add", "field": "pointers", "value": pointer}), make_log())


def test_rule_superseded_must_point_to_another_fact():
    op = {"op": "update", "field": "facts", "id": "f2", "value": {"status": "superseded", "superseded_by": "f7"}}
    assert "superseded_by" in rejected(good_state(), patch(op), make_log())
    ok = {"op": "update", "field": "facts", "id": "f2", "value": {"status": "superseded", "superseded_by": "f1"}}
    assert apply_patch(good_state(), patch(ok), make_log(), cap_tokens=10**6, count=CHARS)["facts"][1]["status"] == "superseded"


def test_rule_duplicate_ids():
    op = {"op": "add", "field": "facts", "value": {"id": "f1", "text": "x", "status": "assumed"}}
    assert "duplicate" in rejected(good_state(), patch(op), make_log())


# ---------- shrinking ----------


def test_remove_only_when_shrinking_and_only_stale_or_superseded():
    log = make_log()
    state = stale_state(log)
    rm = {"op": "remove", "field": "facts", "id": "f1"}
    assert "only allowed when shrinking" in rejected(state, patch(rm), log)
    assert "only stale or superseded" in rejected(state, patch({"op": "remove", "field": "facts", "id": "f2"}), log, shrink=True)
    new = apply_patch(state, patch(rm), log, cap_tokens=10**6, count=CHARS, shrink=True)
    assert [f["id"] for f in new["facts"]] == ["f2"]


def test_dead_end_merge_when_shrinking():
    log = make_log()
    rm = {"op": "remove", "field": "dead_ends", "id": "d1"}
    assert "merged_from" in rejected(good_state(), patch(rm), log, shrink=True)
    merged = {"op": "add", "field": "dead_ends", "value": {"id": "d2", "approach": "patch b.py or c.py", "reason": "wrong module", "at_event": "e2", "files": ["src/b.py"], "merged_from": ["d1"]}}
    new = apply_patch(good_state(), patch(rm, merged), log, cap_tokens=10**6, count=CHARS, shrink=True)
    assert [d["id"] for d in new["dead_ends"]] == ["d2"]


# ---------- invalidation ----------


def test_invalidation_marks_dependents_only():
    log = make_log()
    log.action("edit b", "", {"src/a.py": "h1", "src/b.py": "h2", "tests/test_a.py": "h1"})  # e3
    state, marked = invalidate(good_state(), log)
    # f1 depends on a.py (unchanged), f2 on no file, k1 on the whole repo
    assert marked == ["k1"]
    assert state["checks"][0]["stale_since"] == "e3"
    assert state["facts"][0]["status"] == "observed" and state["facts"][1]["status"] == "observed"
    assert state["dead_ends"][0]["premise_changed"] is True


def test_invalidation_is_idempotent_and_survives_unrelated_patches():
    log = make_log()
    state = stale_state(log)
    again, marked = invalidate(state, log)
    assert again == state and marked == []
    p = patch({"op": "set", "field": "next_action", "value": "rerun tests"})
    new = apply_patch(state, p, log, cap_tokens=10**6, count=CHARS)
    assert new["facts"][0]["status"] == "stale" and new["facts"][0]["stale_since"] == "e3"


# ---------- rebuild ----------


def rebuild_proposal(**over):
    p = {
        "facts": [{"id": "f1", "text": "a.py parses headers", "status": "observed", "sources": ["e1"], "at_event": "e1", "files": ["src/a.py"]}],
        "checks": [],
        "dead_ends": [],
        "changes": [],
        "plan": ["re-run tests"],
        "next_action": "pytest tests/test_a.py",
        "pointers": [],
        "done_when": ["tests pass", "no new warnings"],
        "constraints": [{"id": "c2", "text": "no new deps", "reason": "policy"}],
    }
    p.update(over)
    return p


def test_rebuild_keeps_carried_fields_and_extends_them():
    last = good_state()
    new = rebuild(last, rebuild_proposal(), make_log(), cap_tokens=10**6, count=CHARS)
    assert new["task"] == last["task"]
    assert new["done_when"] == ["tests pass", "no new warnings"]
    assert [c["id"] for c in new["constraints"]] == ["c1", "c2"]
    assert new["dead_ends"] == [] and new["fallback_recap"] is None


def test_rebuild_cannot_change_carried_constraint_or_write_harness_fields():
    log = make_log()
    bad = rebuild_proposal(constraints=[{"id": "c1", "text": "drop API", "reason": "x"}])
    with pytest.raises(PatchRejected, match="cannot be changed"):
        rebuild(good_state(), bad, log, cap_tokens=10**6, count=CHARS)
    with pytest.raises(PatchRejected, match="harness only"):
        rebuild(good_state(), {**rebuild_proposal(), "task": "other"}, log, cap_tokens=10**6, count=CHARS)


def test_rebuild_recomputes_stale_so_new_ids_cannot_revive_evidence():
    log = make_log()
    log.action("edit a", "", {"src/a.py": "h2", "src/b.py": "h1", "tests/test_a.py": "h1"})  # e3
    renamed = rebuild_proposal(facts=[{"id": "f77", "text": "a.py parses headers", "status": "verified", "sources": ["e1"], "at_event": "e1", "files": ["src/a.py"]}])
    new = rebuild(good_state(), renamed, log, cap_tokens=10**6, count=CHARS)
    assert new["facts"][0]["status"] == "stale" and new["facts"][0]["stale_since"] == "e3"
    with pytest.raises(PatchRejected, match="only the harness"):
        rebuild(good_state(), rebuild_proposal(checks=[{"id": "k9", "command": "x", "result": "ok", "status": "stale", "sources": ["e1"], "at_event": "e1", "files": []}]), log, cap_tokens=10**6, count=CHARS)


# ---------- compactor ----------


class Scripted:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, max_output_tokens, purpose=None):
        self.calls.append(copy.deepcopy(messages))
        return self.replies.pop(0)


def first_patch():
    return json.dumps(patch(
        {"op": "add", "field": "done_when", "value": "tests pass"},
        {"op": "set", "field": "plan", "value": ["fix parse_header"]},
        {"op": "set", "field": "next_action", "value": "edit src/a.py"},
        {"op": "add", "field": "facts", "value": {"id": "f1", "text": "a.py parses headers", "status": "observed", "sources": ["e1"], "at_event": "e1", "files": ["src/a.py"]}},
    ))


def compactor(model, cap=10**6, log=None):
    log = log or make_log()
    store = StateStore("fix the bug")
    return Compactor(store, log, model, cap_tokens=cap, count=CHARS), store, log


def test_compactor_accepts_valid_patch_and_logs_it():
    model = Scripted("```json\n" + first_patch() + "\n```")
    c, store, log = compactor(model)
    result = c.compact([{"role": "user", "content": "task"}], max_output_tokens=3000)
    assert result.mode == "patch" and result.attempts == 1
    assert store.current_kind() == "patch" and store.current()["state_version"] == 1
    assert log.events[-1].kind == "compaction" and log.events[-1].payload["mode"] == "patch"


def test_compactor_retries_once_with_errors():
    model = Scripted('{"base_version": 0, "ops": []}', first_patch())
    c, store, _ = compactor(model)
    result = c.compact([], 3000)
    assert result.mode == "patch" and result.attempts == 2
    assert "rejected" in model.calls[1][-1]["content"]


def test_compactor_falls_back_then_rebuilds():
    model = Scripted("not json", '{"base_version": 0, "ops": []}', "handoff: fix parse_header next")
    c, store, _ = compactor(model)
    result = c.compact([], 3000)
    assert result.mode == "fallback" and store.in_fallback()
    assert store.current()["fallback_recap"].startswith("handoff")
    assert store.current()["facts"] == store.last_valid()["facts"]
    assert c.consecutive_fallbacks() == 1

    model.replies = [json.dumps(rebuild_proposal())]
    result = c.compact([], 3000)
    assert result.mode == "rebuild" and not store.in_fallback()
    assert store.current()["fallback_recap"] is None
    assert "carry over" in model.calls[-1][-1]["content"]
    assert c.consecutive_fallbacks() == 0


def test_compactor_shrink_shares_the_single_retry():
    log = make_log()
    big = json.loads(first_patch())
    big["ops"].append({"op": "add", "field": "facts", "value": {"id": "f2", "text": "x" * 500, "status": "assumed"}})
    model = Scripted(json.dumps(big), first_patch())
    c, store, _ = compactor(model, cap=700, log=log)
    result = c.compact([], 3000)
    assert result.mode == "patch" and result.attempts == 2
    assert "over the cap" in model.calls[1][-1]["content"]

    model2 = Scripted(json.dumps(big), json.dumps(big), "recap")
    c2, _, _ = compactor(model2, cap=700)
    assert c2.compact([], 3000).mode == "fallback"


def test_compactor_commits_invalidation_before_asking():
    log = make_log()
    model = Scripted(first_patch())
    c, store, _ = compactor(model, log=log)
    c.compact([], 3000)
    log.action("edit a", "", {"src/a.py": "h2", "src/b.py": "h1", "tests/test_a.py": "h1"})
    model.replies = [json.dumps(patch({"op": "set", "field": "next_action", "value": "cat src/a.py"}, base=2))]
    result = c.compact([], 3000)
    assert result.stale_marked == ["f1"]
    assert [k for k, _ in store.versions] == ["initial", "patch", "invalidate", "patch"]
    assert "Stale entries: ['f1']" in model.calls[-1][-1]["content"]


def test_parse_json_tolerates_prose_and_fences():
    assert parse_json('Sure:\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json("no object here")


# ---------- regressions from the M1 review ----------


def edit_a(log):
    files = log.files
    files["src/a.py"] = "h" + str(len(log))
    return log.action("edit a", "", files)


def ok(state, p, log, **kw):
    return apply_patch(state, p, log, cap_tokens=10**6, count=CHARS, **kw)


def test_new_entry_citing_pre_change_evidence_is_stale_at_once():
    log = make_log()
    edit_a(log)  # e3
    add = {"op": "add", "field": "facts", "value": {"id": "f9", "text": "a.py ok", "status": "verified", "sources": ["e1"], "at_event": "e1", "files": ["src/a.py"]}}
    new = ok(good_state(), patch(add), log)
    assert _by(new, "facts", "f9")["status"] == "stale"
    check = {"op": "add", "field": "checks", "value": {"id": "k9", "command": "pytest", "result": "ok", "status": "valid", "sources": ["e2"], "at_event": "e2", "files": []}}
    assert _by(ok(good_state(), patch(check), log), "checks", "k9")["status"] == "stale"


def test_inflated_at_event_cannot_launder_old_evidence():
    log = make_log()
    edit_a(log)  # e3
    log.action("ls", "", log.files)  # e4, touches nothing
    add = {"op": "add", "field": "facts", "value": {"id": "f9", "text": "a.py ok", "status": "verified", "sources": ["e1"], "at_event": "e4", "files": ["src/a.py"]}}
    assert _by(ok(good_state(), patch(add), log), "facts", "f9")["status"] == "stale"
    renamed = rebuild_proposal(facts=[add["value"]])
    assert rebuild(good_state(), renamed, log, cap_tokens=10**6, count=CHARS)["facts"][0]["status"] == "stale"


def test_stale_to_assumed_to_verified_does_not_revive_old_evidence():
    log = make_log()
    state = stale_state(log)
    down = ok(state, patch({"op": "update", "field": "facts", "id": "f1", "value": {"status": "assumed"}}, base=state["state_version"]), log)
    # the downgrade is allowed, but its files changed after its sources, so the harness re-marks it
    assert _by(down, "facts", "f1")["status"] == "stale"
    up = {"op": "update", "field": "facts", "id": "f1", "value": {"status": "verified", "sources": ["e1"], "at_event": "e1"}}
    assert "reviving a stale entry" in rejected(down, patch(up, base=down["state_version"]), log)


@pytest.mark.parametrize("key", ["stale_since", "stale_from", "premise_changed", "_removed"])
def test_harness_bookkeeping_cannot_be_written_by_the_model(key):
    log = make_log()
    state = stale_state(log)
    upd = {"op": "update", "field": "facts", "id": "f1", "value": {key: "e0"}}
    assert "harness only" in rejected(state, patch(upd), log)
    add = {"op": "add", "field": "facts", "value": {"id": "f9", "text": "x", "status": "assumed", key: "e0"}}
    assert "harness only" in rejected(state, patch(add), log)


def test_assumed_entry_that_went_stale_can_stay_stale():
    log = make_log()
    s = good_state()
    s["facts"].append({"id": "f3", "text": "maybe b.py", "status": "assumed", "at_event": "e1", "files": ["src/a.py"]})
    edit_a(log)  # e3
    stale, marked = invalidate(s, log)
    assert "f3" in marked and _by(stale, "facts", "f3")["stale_from"] == "assumed"
    ok(stale, patch({"op": "set", "field": "next_action", "value": "rerun"}), log)


@pytest.mark.parametrize(
    "bad",
    [
        patch({"op": "update", "field": "facts", "id": "f1", "value": {"status": []}}),
        patch({"op": [], "field": "facts"}),
        patch({"op": "add", "field": [], "value": {}}),
        patch({"op": "add", "field": "facts", "value": {"id": ["f9"], "text": "x", "status": "assumed"}}),
        patch({"op": "set", "field": "plan", "value": "not a list"}),
        {"base_version": 0, "ops": "nope"},
        [],
    ],
)
def test_malformed_patches_are_rejections_not_crashes(bad):
    rejected(good_state(), bad, make_log())


@pytest.mark.parametrize(
    "over",
    [
        {"facts": None},
        {"facts": ["not an object"]},
        {"constraints": [{"text": "no id", "reason": "x"}]},
        {"done_when": "tests pass"},
        {"plan": None},
    ],
)
def test_malformed_rebuilds_are_rejections_not_crashes(over):
    with pytest.raises(PatchRejected):
        rebuild(good_state(), rebuild_proposal(**over), make_log(), cap_tokens=10**6, count=CHARS)


def test_compactor_survives_malformed_json_and_leaves_state_alone():
    bad = json.dumps(patch({"op": "update", "field": "facts", "id": "f1", "value": {"status": []}}))
    model = Scripted(bad, bad, "recap")
    c, store, _ = compactor(model)
    before = store.last_valid()
    assert c.compact([], 3000).mode == "fallback"
    assert store.last_valid() == before


def test_pointers_follow_the_remove_rule():
    log = make_log()
    rm = {"op": "remove", "field": "pointers", "label": "failing log"}
    assert "only allowed when shrinking" in rejected(good_state(), patch(rm), log)
    # the protocol only lets shrinking drop stale or superseded entries; pointers have neither
    swap = {"op": "add", "field": "pointers", "value": {"label": "failing log", "ref": "e1"}}
    assert "cannot be removed" in rejected(good_state(), patch(rm, swap), log, shrink=True)
    assert "duplicate" in rejected(good_state(), patch(swap), log)


def test_event_log_reads_cannot_alter_evidence(tmp_path):
    log = make_log(tmp_path)
    log.get("e1").payload["output"] = "forged"
    log.events[2].changes["src/a.py"] = "forged"
    log.files["src/a.py"] = "forged"
    returned = log.action("ls", "", log.files)
    returned.payload["command"] = "rm -rf /"
    assert log.get("e1").payload["output"] == "..."
    assert log.get("e2").changes == {} and log.files["src/a.py"] == "h1"
    assert log.get("e3").payload["command"] == "ls"
    assert EventLog(tmp_path / "events.jsonl").get("e1").payload["output"] == "..."


def test_compaction_records_stale_revive_and_dependency_metrics():
    log = make_log()
    model = Scripted(first_patch())
    c, store, _ = compactor(model, log=log)
    c.compact([], 3000)
    edit_a(log)  # e3 after the patch commit and its compaction event
    log.action("cat src/a.py", "...", log.files)
    fresh = log.events[-1].id
    revive = patch({"op": "update", "field": "facts", "id": "f1", "value": {"status": "observed", "sources": [fresh], "at_event": fresh}}, base=2)
    model.replies = [json.dumps(revive)]
    result = c.compact([], 3000)
    assert result.stale_marked == ["f1"] and result.revived == ["f1"]
    record = log.events[-1].payload
    assert record["revived"] == ["f1"] and record["dependencies"] == {"evidenced": 1, "empty_files": 0}


def test_rebuild_records_entries_it_found_stale():
    log = make_log()
    model = Scripted("x", "x", "recap")
    c, store, _ = compactor(model, log=log)
    c.compact([], 3000)
    edit_a(log)
    model.replies = [json.dumps(rebuild_proposal())]
    result = c.compact([], 3000)
    assert result.mode == "rebuild" and result.stale_marked == ["f1"]


def _by(state, field, ident):
    return next(i for i in state[field] if i.get("id") == ident)


def test_evidence_must_cite_actions():
    log = make_log()
    edit_a(log)  # e3
    note = log.append("note", text="harness note")  # e4
    add = {"op": "add", "field": "facts", "value": {"id": "f9", "text": "a.py ok", "status": "observed", "sources": ["e1", note.id], "at_event": note.id, "files": ["src/a.py"]}}
    assert "evidence must cite actions" in rejected(good_state(), patch(add), log)
    with pytest.raises(PatchRejected, match="evidence must cite actions"):
        rebuild(good_state(), rebuild_proposal(facts=[add["value"]]), log, cap_tokens=10**6, count=CHARS)


def test_assumed_detour_keeps_the_revalidation_gate():
    log = make_log()
    state = stale_state(log)  # f1 stale since e3
    # an assumed fact claims no evidence; point at_event past the change so the harness leaves it assumed
    log.action("ls", "", log.files)  # e4, reads nothing about a.py
    down = ok(state, patch({"op": "update", "field": "facts", "id": "f1", "value": {"status": "assumed", "sources": [], "at_event": "e4"}}, base=state["state_version"]), log)
    assert _by(down, "facts", "f1")["status"] == "assumed"
    assert _by(down, "facts", "f1")["revalidate_after"] == "e3"
    old = {"op": "update", "field": "facts", "id": "f1", "value": {"status": "verified", "sources": ["e1"], "at_event": "e1"}}
    assert "reviving a stale entry" in rejected(down, patch(old, base=down["state_version"]), log)
    fresh = {"op": "update", "field": "facts", "id": "f1", "value": {"status": "verified", "sources": ["e4"], "at_event": "e4"}}
    up = ok(down, patch(fresh, base=down["state_version"]), log)
    assert _by(up, "facts", "f1")["status"] == "verified" and "revalidate_after" not in _by(up, "facts", "f1")
    from context_compaction.compactor import diff_stale
    assert diff_stale(down, up) == ([], ["f1"])


@pytest.mark.parametrize("path", ["./src/a.py", "src//a.py", "src/./a.py", "src/x/../a.py", "src\\a.py", "/src/a.py", ""])
def test_dependency_paths_must_be_canonical(path):
    log = make_log()
    fact = {"op": "add", "field": "facts", "value": {"id": "f9", "text": "x", "status": "observed", "sources": ["e1"], "at_event": "e1", "files": [path]}}
    assert "canonical" in rejected(good_state(), patch(fact), log)
    check = {"op": "add", "field": "checks", "value": {"id": "k9", "command": "x", "result": "ok", "status": "valid", "sources": ["e1"], "at_event": "e1", "files": [path]}}
    assert "canonical" in rejected(good_state(), patch(check), log)
    dead = {"op": "add", "field": "dead_ends", "value": {"id": "d9", "approach": "x", "reason": "y", "at_event": "e1", "files": [path]}}
    assert "canonical" in rejected(good_state(), patch(dead), log)
    if path:
        change = {"op": "add", "field": "changes", "value": {"path": path, "summary": "x", "verified": False}}
        assert "canonical" in rejected(good_state(), patch(change), log)
        pointer = {"op": "add", "field": "pointers", "value": {"label": "p", "path": path}}
        assert "canonical" in rejected(good_state(), patch(pointer), log)


def test_alias_free_paths_still_invalidate():
    log = make_log()
    edit_a(log)
    add = {"op": "add", "field": "facts", "value": {"id": "f9", "text": "x", "status": "observed", "sources": ["e1"], "at_event": "e1", "files": ["src/a.py"]}}
    assert _by(ok(good_state(), patch(add), log), "facts", "f9")["status"] == "stale"


def test_superseded_detour_keeps_the_revalidation_gate():
    log = make_log()
    state = stale_state(log)  # f1 and k1 stale since e3
    side = ok(state, patch({"op": "update", "field": "facts", "id": "f1", "value": {"status": "superseded", "superseded_by": "f2"}}, base=state["state_version"]), log)
    assert _by(side, "facts", "f1")["revalidate_after"] == "e3"
    back = {"op": "update", "field": "facts", "id": "f1", "value": {"status": "observed", "sources": ["e3"], "at_event": "e3"}}
    assert "reviving a stale entry" in rejected(side, patch(back, base=side["state_version"]), log)


def test_assumed_fact_files_must_be_canonical_too():
    add = {"op": "add", "field": "facts", "value": {"id": "f9", "text": "x", "status": "assumed", "at_event": "e1", "files": ["./src/a.py"]}}
    assert "canonical" in rejected(good_state(), patch(add), make_log())


def test_revive_metric_keys_by_field_and_id():
    from context_compaction.compactor import diff_stale
    log = make_log()
    state = stale_state(log)
    state["checks"][0]["id"] = "f1"  # same id as the stale fact
    log.action("cat src/a.py", "...", log.files)  # e4
    after = copy.deepcopy(state)
    after["facts"][0].update(status="observed", sources=["e4"], at_event="e4")
    assert diff_stale(state, after) == ([], ["f1"])


@pytest.mark.parametrize("value", [
    {"sources": ["e999"], "at_event": "e1", "files": ["src/a.py"]},
    {"sources": ["e1"], "at_event": "e999", "files": ["src/a.py"]},
    {"sources": "e1", "at_event": "e1"},
])
def test_assumed_fact_refs_must_resolve(value):
    add = {"op": "add", "field": "facts", "value": {"id": "f9", "text": "x", "status": "assumed", **value}}
    assert "existing event" in rejected(good_state(), patch(add), make_log())


@pytest.mark.parametrize("pointer", [
    {"label": "p", "path": "src/a.py", "line": 12},
    {"label": "p", "ref": "e1", "symbol": "parse_header"},
])
def test_pointers_only_carry_their_own_keys(pointer):
    assert "unexpected keys" in rejected(good_state(), patch({"op": "add", "field": "pointers", "value": pointer}), make_log())


@pytest.mark.parametrize("field, value, needle", [
    ("changes", {"path": "src/new.py", "verified": "false"}, "boolean verified"),
    ("changes", {"path": "src/new.py", "summary": "x", "verified": 1}, "boolean verified"),
    ("facts", {"id": "f9", "text": {"nested": 1}, "status": "assumed"}, "text string"),
    ("checks", {"id": "k9", "command": "pytest", "result": 0, "status": "valid", "sources": ["e1"], "at_event": "e1", "files": []}, "result strings"),
    ("constraints", {"id": "c9", "text": "x", "reason": ["why"]}, "reason strings"),
    ("dead_ends", {"id": "d9", "approach": "x", "reason": " ", "at_event": "e1"}, "reason strings"),
])
def test_entry_fields_have_the_schema_types(field, value, needle):
    assert needle in rejected(good_state(), patch({"op": "add", "field": field, "value": value}), make_log())
    with pytest.raises(PatchRejected, match=needle):
        rebuild(good_state(), rebuild_proposal(**{field: [value]}), make_log(), cap_tokens=10**6, count=CHARS)
