"""End-to-end checks of the group C agent with a scripted model in a local git repo"""
import json
import math
import re
import subprocess

import pytest
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.utils.actions_toolcall import format_toolcall_observation_messages

from context_compaction import context, hist
from context_compaction.agent import CompactingAgent
from context_compaction.event_log import EventLog

COUNT = lambda text: len(text) // 4  # noqa: E731, deterministic stand-in for the tokenizer
API_SURCHARGE = 6  # tokens per message the fake API counts beyond the local estimate
OBS = "<returncode>{{output.returncode}}</returncode>\n<output>\n{{output.output}}</output>"


class ScriptedModel:
    """Minimal mini-swe-agent model: each step is a list of shell commands"""

    class config:
        model_name = "scripted"
        model_kwargs = {}

    def __init__(self, steps):
        self.steps = list(steps)
        self.max_tokens = []
        self.seen = []

    def query(self, messages, **kwargs):
        self.max_tokens.append(kwargs.get("max_tokens"))
        self.seen.append(list(messages))
        commands = self.steps.pop(0)
        n = len(self.seen)
        actions = [{"command": c, "tool_call_id": f"call_{n}_{i}"} for i, c in enumerate(commands)]
        calls = [
            {"id": a["tool_call_id"], "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": a["command"]})}}
            for a in actions
        ]
        # the API counts chat formatting the local tokenizer misses; mimic that with a per-message surcharge
        from context_compaction.agent import TOOLS
        prompt = context.context_tokens(messages, TOOLS, COUNT) + API_SURCHARGE * len(messages)
        usage = {"prompt_tokens": prompt, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 40}}
        return {"role": "assistant", "content": "thinking", "tool_calls": calls,
                "extra": {"actions": actions, "cost": 0.01, "response": {"usage": usage}}}

    def format_message(self, **kwargs):
        return dict(kwargs)

    def format_observation_messages(self, message, outputs, template_vars=None):
        return format_toolcall_observation_messages(
            actions=message["extra"]["actions"], outputs=outputs, observation_template=OBS, template_vars=template_vars)

    def get_template_vars(self, **kwargs):
        return {}

    def serialize(self):
        return {}


class ScriptedCompaction:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, max_tokens):
        from context_compaction.agent import TOOLS

        self.calls.append((list(messages), max_tokens))
        reply = self.replies.pop(0)
        prompt = context.context_tokens(messages, TOOLS, COUNT) + API_SURCHARGE * len(messages)
        usage = {"input": prompt, "cached_input": 0, "output": 20, "reasoning": 0}
        return (reply(messages) if callable(reply) else reply), usage, 0.02


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    (path / "a.py").write_text("def parse():\n    return 1\n")
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("add", "-A")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return path


def make_agent(repo, tmp_path, steps, compaction, **over):
    env = LocalEnvironment(cwd=str(repo))
    kwargs = {
        "run_dir": tmp_path / "run", "system_template": "system prompt",
        "instance_template": "Task: {{task}}\n{{hist_help}}", "step_limit": 30, "cost_limit": 10.0,
        "hard_limit": 2400, "threshold": 400, "state_cap": 200, "recent_events": 2, "tool_output_chars": 300,
        # reserves scaled down with the toy budget
        "anchor_reserve": 10, "anchor_reserve_per_message": 4,
    }
    agent = CompactingAgent(ScriptedModel(steps), env, compaction_call=compaction, count=COUNT, **{**kwargs, **over})
    agent.files.repo = str(repo)
    return agent


SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && git diff"


def edit(old, new):
    """Portable in-place edit (BSD and GNU sed disagree on -i)"""
    return f"python3 -c \"import pathlib; p = pathlib.Path('a.py'); p.write_text(p.read_text().replace('{old}', '{new}'))\""


def event_of(messages, needle):
    """Event id tag of the tool output whose call ran `needle`, read off the context as a model would"""
    calls = {c["id"]: json.loads(c["function"]["arguments"])["command"]
             for m in messages for c in (m.get("tool_calls") or [])}
    for m in messages:
        if m.get("role") == "tool" and needle in calls.get(m.get("tool_call_id"), ""):
            return re.search(r"\[(e\d+)\]", m["content"]).group(1)
    raise AssertionError(f"no output for {needle!r} in context")


def first_patch(messages):
    """The model reads event ids off its context, as a real model would"""
    cat = event_of(messages, "cat a.py")
    return json.dumps({"base_version": 0, "ops": [
        {"op": "add", "field": "done_when", "value": "parse returns 2"},
        {"op": "add", "field": "facts", "value": {"id": "f1", "text": "parse returns 1", "status": "observed",
                                                    "sources": [cat], "at_event": cat, "files": ["a.py"]}},
        {"op": "set", "field": "plan", "value": ["edit parse", "submit"]},
        {"op": "set", "field": "next_action", "value": "sed -i s/1/2/ a.py"},
    ]})


def test_full_run_logs_truncates_compacts_and_submits(repo, tmp_path):
    big = "python3 -c \"print('x' * 3000)\""
    steps = [["cat a.py"], [big], ["hist read e4 --lines 1:1"], [edit("1", "2")], [big], [big], [SUBMIT]]
    def adaptive(messages):
        version = int(re.search(r"Current state, version (\d+)", messages[-1]["content"]).group(1))
        if version == 0:
            return first_patch(messages)
        return json.dumps({"base_version": version, "ops": [{"op": "set", "field": "next_action", "value": "submit"}]})
    compaction = ScriptedCompaction(*[adaptive] * 10)
    agent = make_agent(repo, tmp_path, steps, compaction)
    info = agent.run("make parse return 2")

    assert info["exit_status"] == "Submitted" and "+    return 2" in info["submission"]
    log = EventLog(tmp_path / "run" / "events.jsonl")
    kinds = [e.kind for e in log.events]
    assert kinds[0] == "snapshot" and "hist" in kinds and kinds.count("compaction") >= 2

    # the big output is cut in context but kept whole in the log
    big_event = log.get("e4")
    assert big_event.kind == "action" and len(big_event.payload["output"]) > 3000
    tool_texts = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert any("characters omitted; full output is e4" in t for t in tool_texts)
    assert any("e4 $ python3" in t and "lines 1-1 of 1" in t for t in tool_texts)

    # the edit after the first compaction makes f1 stale at the next one
    edit_event = next(e for e in log.events if e.kind == "action" and e.payload["command"].startswith("python3 -c \"import pathlib"))
    assert list(edit_event.changes) == ["a.py"]
    states = sorted((tmp_path / "run" / "states").glob("v*.json"))
    last = json.loads(states[-1].read_text())["state"]
    assert last["facts"][0]["status"] == "stale" and last["facts"][0]["stale_since"] == edit_event.id

    # after a reset the model sees system, task, then the state with recent events
    reset = agent.model.seen[-1]
    assert reset[0]["content"] == "system prompt" and reset[1]["content"].startswith("Task:")
    assert "Working state, version" in reset[2]["content"]
    # this tiny budget cannot hold the recent events, so the reset drops and records them
    notes = [e.payload for e in log.events if e.kind == "note" and e.payload.get("what") == "context_reset"]
    assert all(n["tokens"] <= 400 for n in notes) and notes[0]["dropped_events"] >= 1

    summary = agent.summary()
    n = kinds.count("compaction")
    assert summary["compactions"] == n == len(notes) and summary["hist_calls"] == 1 and summary["steps"] == len(steps)
    assert summary["usage"]["compaction"]["calls"] == n and summary["usage"]["agent"]["cached_input"] == 40 * len(steps)


def test_output_limit_tracks_the_hard_limit(repo, tmp_path):
    agent = make_agent(repo, tmp_path, [["ls"], ["ls -a"], [SUBMIT]], ScriptedCompaction())
    agent.run("t")
    calls = [e.payload for e in EventLog(tmp_path / "run" / "events.jsonl").events if e.kind == "model"]
    assert agent.model.max_tokens == [2400 - c["estimated_input"] for c in calls]


def test_estimates_anchor_on_the_api_count(repo, tmp_path):
    from context_compaction.agent import TOOLS

    agent = make_agent(repo, tmp_path, [["ls"], ["ls -a"], [SUBMIT]], ScriptedCompaction())
    agent.run("t")
    calls = [e.payload for e in EventLog(tmp_path / "run" / "events.jsonl").events if e.kind == "model"]
    seen = agent.model.seen
    # first call: no API count yet, so the local estimate plus the margin
    assert calls[0]["estimated_input"] == math.ceil(context.context_tokens(seen[0], TOOLS, COUNT) * 1.05)
    # later calls: last call's API count plus an estimate of what was added since
    for prev, cur, n in ((calls[0], calls[1], 1), (calls[1], calls[2], 2)):
        new = seen[n][len(seen[n - 1]):]
        added = context.context_tokens(new, (), COUNT) + 10 + 4 * len(new)
        assert cur["estimated_input"] == prev["usage"]["input"] + added
    # the reserve keeps the estimate at or above what the API counts
    assert all(c["estimated_input"] >= c["usage"]["input"] for c in calls[1:])


def test_compaction_without_room_overflows_with_current_diff(repo, tmp_path):
    big = "python3 -c \"print('y' * 3000)\""
    steps = [[edit("1", "3")], [big], [big], [big]]
    agent = make_agent(repo, tmp_path, steps, ScriptedCompaction(), tool_output_chars=3000, hard_limit=1400, state_cap=600)
    info = agent.run("t")
    assert info["exit_status"] == "ContextOverflow" and "+    return 3" in info["submission"]
    assert agent.summary()["overflow"] is True


def test_failed_patches_fall_back_and_reset_with_the_handoff(repo, tmp_path):
    big = "python3 -c \"print('z' * 3000)\""
    steps = [["cat a.py"], [big], [big], [SUBMIT]]
    compaction = ScriptedCompaction("not json", '{"base_version": 0, "ops": []}', "handoff: edit parse next")
    agent = make_agent(repo, tmp_path, steps, compaction, threshold=480)
    agent.run("t")
    reset = agent.model.seen[-1][2]["content"]
    assert reset.startswith("Handoff from your previous context") and "edit parse next" in reset
    assert "Working state" not in reset and agent.summary()["fallbacks"] == 1


def test_compaction_calls_do_not_use_agent_steps(repo, tmp_path):
    big = "python3 -c \"print('w' * 3000)\""
    steps = [["cat a.py"], [big], [big], [SUBMIT]]
    agent = make_agent(repo, tmp_path, steps, ScriptedCompaction(first_patch), step_limit=4, threshold=480)
    assert agent.run("t")["exit_status"] == "Submitted"


# ---------- hist ----------


def hist_log():
    log = EventLog()
    log.snapshot({})
    log.action("grep -rn parse", "a.py:1:def parse():\nb.py:9:parse()", {})
    log.action("cat big.log", "\n".join(f"line {i}" for i in range(1, 301)), {})
    log.append("note", text="not searchable")
    return log


def test_hist_search_and_read():
    log = hist_log()
    code, out = hist.run(log, "hist search 'def parse'")
    assert code == 0 and "e1:1: a.py:1:def parse():" in out
    code, out = hist.run(log, "hist read e2 --lines 10:12")
    assert code == 0 and out.splitlines()[-3:] == ["line 10", "line 11", "line 12"] and "of 300" in out
    code, out = hist.run(log, "hist read e2")
    assert out.count("\nline ") == 100


@pytest.mark.parametrize("command, needle", [
    ("hist read e3", "not an action event"),
    ("hist read e9", "not an action event"),
    ("hist read e2 --lines 5:2", "1 <= A <= B"),
    ("hist search '('", "invalid regex"),
    ("hist delete e1", "usage"),
    ("hist search nothing-matches-this", "no matches"),
])
def test_hist_errors(command, needle):
    code, out = hist.run(hist_log(), command)
    assert code == 1 and needle in out


def test_hist_output_is_bounded():
    log = EventLog()
    log.snapshot({})
    log.action("cat wide", "\n".join("x" * 150 for _ in range(500)), {})
    code, out = hist.run(log, "hist read e1 --lines 1:500", limit=1000)
    assert len(out) < 1100 and "truncated at 1000" in out


def test_is_hist_only_matches_the_command_word():
    assert hist.is_hist("hist search x") and hist.is_hist("  hist read e1")
    assert not hist.is_hist("history") and not hist.is_hist("echo hist")


# ---------- context helpers ----------


def test_reset_drops_recent_events_before_giving_up():
    log = EventLog()
    log.snapshot({})
    for i in range(3):
        log.action(f"cmd{i}", "o" * 400, {})
    budget = context.Budget(hard_limit=1000, threshold=200, state_cap=100)
    head = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    msgs, dropped = context.reset_messages(head, "state", context.recent_actions(log, 2), 400, budget, count=COUNT)
    assert msgs is not None and dropped == 1 and "[e3]" in msgs[2]["content"] and "[e2]" not in msgs[2]["content"]
    msgs, _ = context.reset_messages(head, "s" * 2000, [], 400, budget, count=COUNT)
    assert msgs is None


def test_truncate_keeps_head_and_tail():
    text = "H" * 500 + "T" * 500
    out = context.truncate_output(text, 400, "e7")
    assert out.startswith("H" * 300) and out.endswith("T" * 100) and "600 characters omitted" in out


def test_reset_keeps_recent_events_when_they_fit(repo, tmp_path):
    big = "python3 -c \"print('v' * 3000)\""
    steps = [["cat a.py"], [big], [big], [big], [SUBMIT]]
    agent = make_agent(repo, tmp_path, steps, ScriptedCompaction(first_patch), threshold=500, tool_output_chars=200)
    assert agent.run("t")["exit_status"] == "Submitted"
    reset = agent.model.seen[-1][2]["content"]
    assert "Working state, version" in reset and "Most recent events" in reset and reset.count("$ python3 -c") >= 1


# ---------- regressions from the integration review ----------


def test_limit_stop_submits_the_current_diff(repo, tmp_path):
    steps = [[edit("1", "5")], ["ls"], ["ls"]]
    agent = make_agent(repo, tmp_path, steps, ScriptedCompaction(), step_limit=2)
    info = agent.run("t")
    assert info["exit_status"] == "LimitsExceeded" and "+    return 5" in info["submission"]
    traj = json.loads((tmp_path / "run" / "traj.json").read_text()) if (tmp_path / "run" / "traj.json").exists() else None
    notes = [e.payload for e in EventLog(tmp_path / "run" / "events.jsonl").events if e.kind == "note"]
    assert any(n.get("what") == "harness_submission" and not n["empty"] for n in notes)
    assert traj is None or traj["messages"][-1]["extra"]["submission"] == info["submission"]


class FlakyModel(ScriptedModel):
    """First reply fails to parse (with a billed response), second is cut off by the length limit"""

    def __init__(self, steps):
        super().__init__(steps)
        self.n = 0

    def query(self, messages, **kwargs):
        from minisweagent.exceptions import FormatError

        self.n += 1
        usage = {"prompt_tokens": 300, "completion_tokens": 50}
        if self.n == 1:
            raise FormatError({"role": "user", "content": "no tool call", "extra": {"response": {"usage": usage, "choices": [{"finish_reason": "stop"}]}}})
        message = super().query(messages, **kwargs)
        if self.n == 2:
            message["extra"]["response"]["choices"] = [{"finish_reason": "length"}]
        return message


def test_unusable_replies_are_billed_and_never_executed(repo, tmp_path):
    env = LocalEnvironment(cwd=str(repo))
    model = FlakyModel([[edit("1", "9")], [SUBMIT]])
    agent = CompactingAgent(model, env, compaction_call=ScriptedCompaction(), count=COUNT, run_dir=tmp_path / "run",
                            system_template="s", instance_template="{{task}}", step_limit=10, cost_limit=10.0)
    agent.files.repo = str(repo)
    agent.cost_of = lambda usage: usage["input"] * 0.001  # stand-in price table
    info = agent.run("t")
    calls = [e.payload for e in EventLog(tmp_path / "run" / "events.jsonl").events if e.kind == "model"]
    assert calls[0]["format_error"] and calls[0]["cost"] == pytest.approx(0.3)
    assert calls[1]["finish_reason"] == "length"
    assert "+    return 9" not in info["submission"], "the cut-off edit must not have run"
    assert agent.n_calls == 3 and agent.cost == pytest.approx(0.3 + 0.01 + 0.01)
    rejected = [m for m in agent.messages if (m.get("extra") or {}).get("finish_reason") == "length"]
    assert rejected and rejected[0]["extra"]["rejected_response"]["choices"][0]["finish_reason"] == "length"


def test_cost_limit_is_checked_before_each_compaction_call(repo, tmp_path):
    big = "python3 -c \"print('u' * 3000)\""
    steps = [["cat a.py"], [big], [big], [SUBMIT]]
    pricey = ScriptedCompaction("not json", "not json", "recap")
    pricey_call = pricey.__call__
    agent = make_agent(repo, tmp_path, steps, lambda m, n: (*pricey_call(m, n)[:2], 5.0), cost_limit=5.0)
    info = agent.run("t")
    assert info["exit_status"] == "LimitsExceeded"
    assert len(pricey.calls) == 1, "no retry, fallback or agent call after the limit is hit"


def test_compaction_requests_and_replies_are_kept(repo, tmp_path):
    big = "python3 -c \"print('t' * 3000)\""
    steps = [["cat a.py"], [big], [big], [SUBMIT]]
    agent = make_agent(repo, tmp_path, steps, ScriptedCompaction("not json", first_patch), threshold=480)
    agent.run("t")
    rows = [json.loads(line) for line in (tmp_path / "run" / "compactions.jsonl").read_text().splitlines()]
    assert [r["reply"][:8] for r in rows] == ["not json", '{"base_v']
    assert rows[0]["request"][-1]["content"].startswith("Stop working and update the working state")
    assert rows[1]["request"][-1]["content"].startswith("The state update was rejected")
    assert all("extra" not in m for r in rows for m in r["request"])


def test_out_dir_refuses_a_different_config(tmp_path):
    import run_group

    out = tmp_path / "c"
    sha = run_group.claim_out_dir(out, {"a": 1})
    assert run_group.claim_out_dir(out, {"a": 1}) == sha
    with pytest.raises(SystemExit, match="another config"):
        run_group.claim_out_dir(out, {"a": 2})
    (out / "x").mkdir()
    (out / "x" / "run.json").write_text(json.dumps({"exit_status": "Submitted", "config_sha256": "other"}))
    assert not run_group.finished(out / "x", sha)


def test_eval_run_id_follows_the_predictions():
    import evaluate_group

    first = evaluate_group.default_run_id("c", "formal", {"a": {"model_patch": "x"}})
    second = evaluate_group.default_run_id("c", "formal", {"a": {"model_patch": "y"}})
    assert first != second and first.startswith("group-c-formal-")


def test_eval_scores_one_frozen_set_only(tmp_path):
    import evaluate_group

    frozen = tmp_path / "instances.json"
    frozen.write_text(json.dumps({"smoke": {"ids": ["s1"]}, "formal": {"ids": ["f1", "f2"]}}))
    preds = {i: {"instance_id": i} for i in ("s1", "f1", "f2", "other")}
    assert sorted(evaluate_group.select_preds(preds, "formal", frozen)) == ["f1", "f2"]
    assert sorted(evaluate_group.select_preds(preds, "smoke", frozen)) == ["s1"]


def finish(run_dir, status="Submitted", sha="abc", patch="diff"):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({"exit_status": status, "config_sha256": sha}))
    (run_dir / "patch.diff").write_text(patch)


def test_preds_are_rebuilt_from_finished_dirs(tmp_path):
    import run_group

    finish(tmp_path / "a", patch="pa")
    finish(tmp_path / "b", status="RuntimeError")
    finish(tmp_path / "c", sha="other")
    (tmp_path / "preds.json").write_text("{broken")
    preds = run_group.write_preds(tmp_path, "abc", "m")
    assert list(preds) == ["a"] and preds["a"]["model_patch"] == "pa"
    assert json.loads((tmp_path / "preds.json").read_text()) == preds
    assert not (tmp_path / "preds.json.tmp").exists()


def test_unfinished_attempts_are_archived_and_capped(tmp_path):
    import run_group

    run_dir, n = run_group.prepare_run_dir(tmp_path, "x", max_attempts=3)
    assert n == 1
    (run_dir / "usage.json").write_text('{"cost": 0.4}')  # paid, then the container died
    run_dir, n = run_group.prepare_run_dir(tmp_path, "x", max_attempts=3)
    assert n == 2 and json.loads((tmp_path / "attempts" / "x.1" / "usage.json").read_text())["cost"] == 0.4
    assert run_group.prepare_run_dir(tmp_path, "x", max_attempts=3)[1] == 3
    assert run_group.prepare_run_dir(tmp_path, "x", max_attempts=3) is None
    assert sorted(p.name for p in (tmp_path / "attempts").iterdir()) == ["x.1", "x.2", "x.3"]


# ---------- compaction room and truncation (after the sympy__sympy-17630 trial) ----------


def test_compaction_uses_the_room_left_as_in_the_paper():
    budget = context.Budget()
    # the trial's compaction turn: 13,286 input tokens left 2,714, under the old 3K rule it never ran
    assert budget.compaction_output(13_286) == 2_714
    assert budget.compaction_output(15_100) is None


class Truncating(ScriptedCompaction):
    """Replies flagged cut off by the output limit for the first `n` calls"""

    def __init__(self, n, *replies):
        super().__init__(*replies)
        self.n = n

    def __call__(self, messages, max_tokens):
        text, usage, cost = super().__call__(messages, max_tokens)
        return text, usage, cost, ("length" if len(self.calls) <= self.n else "stop")


def test_cut_off_patch_is_a_failed_attempt(repo, tmp_path):
    big = "python3 -c \"print('s' * 3000)\""
    steps = [["cat a.py"], [big], [big], [SUBMIT]]
    cut = lambda m: first_patch(m)[:40]  # noqa: E731, what a reply cut off mid-JSON looks like
    agent = make_agent(repo, tmp_path, steps, Truncating(1, cut, first_patch), threshold=480)
    assert agent.run("t")["exit_status"] == "Submitted"
    record = next(e.payload for e in EventLog(tmp_path / "run" / "events.jsonl").events if e.kind == "compaction")
    assert record["mode"] == "patch" and record["attempts"] == 2


def test_cut_off_handoff_ends_the_run(repo, tmp_path):
    big = "python3 -c \"print('r' * 3000)\""
    steps = [[edit("1", "4")], [big], [big], [SUBMIT]]
    agent = make_agent(repo, tmp_path, steps, Truncating(3, "x", "x", "half a handoff"), threshold=480)
    info = agent.run("t")
    assert info["exit_status"] == "ContextOverflow" and "+    return 4" in info["submission"]


def test_later_patch_prompts_point_at_the_state_in_context(repo, tmp_path):
    big = "python3 -c \"print('q' * 3000)\""
    steps = [["cat a.py"], [big], [big], [big], [big], [SUBMIT]]

    def adaptive(messages):
        version = int(re.search(r"Current state, version (\d+)", messages[-1]["content"]).group(1))
        if version == 0:
            return first_patch(messages)
        return json.dumps({"base_version": version, "ops": [{"op": "set", "field": "next_action", "value": "go"}]})

    compaction = ScriptedCompaction(*[adaptive] * 10)
    agent = make_agent(repo, tmp_path, steps, compaction)
    agent.run("t")
    prompts_sent = [m[-1]["content"] for m, _ in compaction.calls]
    assert len(prompts_sent) >= 2
    assert '"done_when": []' in prompts_sent[0], "the empty first state is shown inline"
    assert all("latest working-state message above" in p and "```json" not in p for p in prompts_sent[1:])


def test_cut_off_reply_fails_even_when_it_parses(repo, tmp_path):
    big = "python3 -c \"print('p' * 3000)\""
    steps = [["cat a.py"], [big], [big], [SUBMIT]]
    # complete JSON, but the stop reason says the output limit cut it
    agent = make_agent(repo, tmp_path, steps, Truncating(1, first_patch, first_patch), threshold=480)
    agent.run("t")
    record = next(e.payload for e in EventLog(tmp_path / "run" / "events.jsonl").events if e.kind == "compaction")
    assert record["attempts"] == 2 and "cut off" in record["errors"][0][0]


def test_patch_prompt_lists_dead_ends_with_changed_premises():
    from context_compaction.compactor import Compactor
    from context_compaction.event_log import EventLog as Log
    from context_compaction.state import StateStore

    log = Log()
    log.snapshot({"a.py": "h1"})
    log.action("cat a.py", "x", {"a.py": "h1"})  # e1
    store = StateStore("t")
    s = store.current()
    s.update(done_when=["d"], plan=["p"], next_action="n",
             dead_ends=[{"id": "d1", "approach": "edit a", "reason": "no", "at_event": "e1", "files": ["a.py"]}])
    store.commit("patch", s)
    log.action("edit a", "", {"a.py": "h2"})  # e2 changes the dead end's premise
    seen = []

    def model(messages, max_output_tokens, purpose=None):
        seen.append(messages[-1]["content"])
        return json.dumps({"base_version": store.current()["state_version"], "ops": []})

    Compactor(store, log, model, state_in_context=True).compact([], 3000)
    assert "Dead ends whose files changed since: ['d1']" in seen[0]
    assert "changed-premise dead ends" in seen[0]


def test_retries_anchor_on_the_compaction_calls_api_count(repo, tmp_path):
    big = "python3 -c \"print('o' * 3000)\""
    steps = [["cat a.py"], [big], [big], [SUBMIT]]
    agent = make_agent(repo, tmp_path, steps, ScriptedCompaction("not json", first_patch), threshold=480)
    agent.run("t")
    calls = [e.payload for e in EventLog(tmp_path / "run" / "events.jsonl").events
             if e.kind == "model" and e.payload["purpose"] == "compaction"]
    first, retry = calls[0], calls[1]
    # the retry adds the rejected reply and the error message to the first request
    rows = [json.loads(line) for line in (tmp_path / "run" / "compactions.jsonl").read_text().splitlines()]
    added = context.context_tokens(rows[1]["request"][len(rows[0]["request"]):], (), COUNT)
    assert retry["estimated_input"] == first["usage"]["input"] + added + 10 + 4 * 2
