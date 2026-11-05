import analyze_group as A


def ev(seq, kind, changes=None, **payload):
    return {"seq": seq, "kind": kind, "changes": changes or {}, "payload": payload}


def test_wilson_matches_the_reported_interval():
    low, high = A.wilson(82, 100)
    assert round(low, 3) == 0.733 and round(high, 3) == 0.883


def test_repeat_needs_same_command_and_unchanged_repo():
    events = [
        ev(0, "snapshot"),
        ev(1, "action", command="cat a.py"),
        ev(2, "action", command="pytest  tests"),
        ev(3, "compaction", mode="patch"),
        ev(4, "action", command="cat   a.py"),          # same after normalization, repo unchanged: repeat
        ev(5, "action", command="sed -i x a.py", changes={"a.py": "h2"}),
        ev(6, "action", command="pytest tests"),        # same command, but a.py changed since: not a repeat
        ev(7, "hist", command="hist read e1"),          # not an action
    ]
    assert A.repeats(events) == (1, 3)


def test_windows_cap_at_ten_and_count_each_action_once():
    events = [ev(0, "snapshot"), ev(1, "action", command="ls"), ev(2, "compaction", mode="patch")]
    events += [ev(3 + i, "action", command="ls") for i in range(5)]
    events += [ev(8, "compaction", mode="patch")]
    events += [ev(9 + i, "action", command="ls") for i in range(12)]
    rep, window = A.repeats(events)
    # first window: actions 3-7 and 9-13 (10); second window: 9-18, of which 14-18 are new
    assert window == 15 and rep == 15


def test_no_compaction_means_no_exposure():
    assert A.repeats([ev(0, "snapshot"), ev(1, "action", command="ls")]) is None


def row(iid, resolved, exit_status="Submitted", cost=1.0):
    return {"instance_id": iid, "repo": iid.split("__")[0], "resolved": resolved, "exit_status": exit_status,
            "cost": cost, "compaction_cost": 0.0, "steps": 10}


def test_escalation_swaps_in_overflowed_tasks_and_adds_both_costs():
    first = [row("a__1", True), row("a__2", False, "ContextOverflow", 0.5), row("a__3", True, "ContextOverflow", 0.4)]
    retry = [row("a__2", True, cost=0.7), row("a__3", False, cost=0.6), row("a__9", True)]
    combined = {r["instance_id"]: r for r in A.escalate(first, retry)}
    assert [combined[i]["resolved"] for i in ("a__1", "a__2", "a__3")] == [True, True, False]
    assert combined["a__2"]["cost"] == 1.2 and combined["a__3"]["cost"] == 1.0
    assert "a__9" not in combined, "only tasks that overflowed are rerun"
