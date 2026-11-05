"""mini-swe-agent integration for the structured-state groups

CompactingAgent keeps two message lists: `messages`, the full trajectory that
is saved, and `context`, what the model actually sees. Before each call it
checks the token budget; past the threshold it runs a compaction turn and
resets the context to system + task + state + the last few events.
Every shell action is logged with the repo files it changed; `hist` commands
are answered by the harness from that log
"""
import json
import logging
import subprocess
import time
from pathlib import Path

from minisweagent.agents.default import AgentConfig, DefaultAgent
from jinja2 import StrictUndefined, Template
from minisweagent.exceptions import FormatError, InterruptAgentFlow, LimitsExceeded, Submitted, TimeExceeded
from minisweagent.models.utils.actions_toolcall import BASH_TOOL
from minisweagent.models.utils.retry import retry

from . import hist
from .compactor import Compactor
from .context import Budget, context_tokens, recent_actions, reset_messages, truncate_output
from .event_log import EventLog
from .prompts import C_PROMPTS, HIST_HELP
from .repo_files import RepoFiles
from .state import StateStore, render
from .tokens import count_tokens

TOOLS = [BASH_TOOL]
logger = logging.getLogger("context_compaction.agent")


class ContextOverflow(InterruptAgentFlow):
    """A compaction turn cannot fit in the hard limit; the run ends with its current diff"""


class CompactingAgentConfig(AgentConfig):
    run_dir: Path
    hard_limit: int = 16_000
    threshold: int = 12_000
    state_cap: int = 3_000
    recent_events: int = 2
    tool_output_chars: int = 4_000
    max_patch_retries: int = 1


def finish_reason(response):
    try:
        return (response or {}).get("choices", [{}])[0].get("finish_reason")
    except (AttributeError, IndexError, TypeError):
        return None


def usage_of(response):
    """Token usage from a litellm response dict, with cached and reasoning tokens split out"""
    usage = (response or {}).get("usage") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {
        "input": usage.get("prompt_tokens", 0) or 0,
        "cached_input": prompt_details.get("cached_tokens", 0) or 0,
        "output": usage.get("completion_tokens", 0) or 0,
        "reasoning": completion_details.get("reasoning_tokens", 0) or 0,
    }


class CompactingAgent(DefaultAgent):
    def __init__(self, model, env, *, compaction_call=None, count=count_tokens, **kwargs):
        super().__init__(model, env, config_class=CompactingAgentConfig, **kwargs)
        cfg = self.config
        self.budget = Budget(cfg.hard_limit, cfg.threshold, cfg.state_cap)
        self.count = count
        self.compaction_call = compaction_call or self._litellm_compaction
        self.files = RepoFiles(env)
        self.context: list[dict] = []

    # ---------- lifecycle ----------

    def run(self, task="", **kwargs):
        run_dir = Path(self.config.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.log = EventLog(run_dir / "events.jsonl")
        if not len(self.log):
            self.log.snapshot(self.files.snapshot())
        self.store = StateStore(task, run_dir / "states")
        self.compactor = Compactor(
            self.store, self.log, self._compaction_model, prompts=C_PROMPTS,
            cap_tokens=self.config.state_cap, max_retries=self.config.max_patch_retries, count=self.count,
        )
        self.context = []
        self.extra_template_vars |= {"hist_help": HIST_HELP.format(limit=self.config.tool_output_chars)}
        info = super().run(task, **kwargs)
        if info.get("exit_status") != "Submitted" and not info.get("submission"):
            # the plan submits the current result on every stop, the same way for every group
            info["submission"] = self._current_diff()
            self.messages[-1].setdefault("extra", {})["submission"] = info["submission"]
            self.log.append("note", what="harness_submission", exit_status=info.get("exit_status"),
                            empty=not info["submission"])
            self.save(self.config.output_path)
        return info

    def add_messages(self, *messages):
        self.context.extend(messages)
        return super().add_messages(*messages)

    # ---------- model calls ----------

    def _check_limits(self, steps=True):
        """Checked before every model call, compaction calls included"""
        if (steps and 0 < self.config.step_limit <= self.n_calls) or 0 < self.config.cost_limit <= self.cost:
            raise LimitsExceeded({"role": "exit", "content": "LimitsExceeded", "extra": {"exit_status": "LimitsExceeded", "submission": ""}})
        if 0 < self.config.wall_time_limit_seconds <= int(time.time() - self._start_time):
            raise TimeExceeded({"role": "exit", "content": "TimeExceeded", "extra": {"exit_status": "TimeExceeded", "submission": ""}})

    def query(self):
        self._check_limits()
        tokens = context_tokens(self.context, TOOLS, self.count)
        if tokens > self.budget.threshold:
            self._compact()
            self._check_limits()
            tokens = context_tokens(self.context, TOOLS, self.count)
        self.n_calls += 1
        try:
            message = self.model.query(self.context, max_tokens=self.budget.normal_output(tokens))
        except FormatError as e:
            # the call was made and billed even though its output is unusable
            response = (e.messages[0].get("extra") or {}).get("response") if e.messages else None
            self._record_agent_call(tokens, response if isinstance(response, dict) else None, None, format_error=True)
            raise
        response = message.get("extra", {}).get("response")
        self._record_agent_call(tokens, response, message.get("extra", {}).get("cost"))
        if finish_reason(response) == "length":
            # a cut-off reply is never executed, even if a complete tool call came through
            # the raw reply stays in the saved trajectory; extra is never sent to the model
            raise FormatError({"role": "user", "content": self._format_error("output token limit reached", "length"),
                               "extra": {"interrupt_type": "FormatError", "finish_reason": "length", "rejected_response": response}})
        self.add_messages(message)
        return message

    def _record_agent_call(self, tokens, response, cost, format_error=False):
        usage = usage_of(response)
        if cost is None:
            cost = self.cost_of(usage)
        self.cost += cost
        self.log.append("model", purpose="agent", estimated_input=tokens, cost=cost, usage=usage,
                        finish_reason=finish_reason(response), format_error=format_error)

    def cost_of(self, usage):
        """List-price cost from token counts, for calls whose response skipped mini's accounting"""
        import litellm

        try:
            fresh = max(usage["input"] - usage["cached_input"], 0)
            prompt, completion = litellm.cost_per_token(
                model=self.model.config.model_name, prompt_tokens=fresh + usage["cached_input"],
                completion_tokens=usage["output"], cache_read_input_tokens=usage["cached_input"],
            )
            return prompt + completion
        except Exception:
            return 0.0

    def _format_error(self, error, reason):
        template = getattr(self.model.config, "format_error_template", "{{ error }}")
        return Template(template, undefined=StrictUndefined).render(error=error, actions=[], finish_reason=reason)

    def _compaction_model(self, messages, _max_output_tokens):
        """Compaction calls get whatever room the hard limit leaves, or the run overflows"""
        self._check_limits(steps=False)
        tokens = context_tokens(messages, TOOLS, self.count)
        room = self.budget.compaction_output(tokens)
        if room is None:
            raise self._overflow(f"compaction turn needs {tokens} input tokens, leaving under {self.budget.state_cap} for output")
        text, usage, cost = self.compaction_call(messages, room)
        self.cost += cost
        event = self.log.append("model", purpose="compaction", estimated_input=tokens, max_output=room, cost=cost, usage=usage)
        # full request and raw reply, so failed patches and the decision replay can be inspected later
        record = {"event": event.id, "max_output": room, "usage": usage, "cost": cost, "reply": text,
                  "request": [{k: v for k, v in m.items() if k != "extra"} for m in messages]}
        with (Path(self.config.run_dir) / "compactions.jsonl").open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return text

    def _litellm_compaction(self, messages, max_tokens):
        import litellm

        prepared = self.model._prepare_messages_for_api(messages)
        kwargs = {k: v for k, v in self.model.config.model_kwargs.items() if k != "parallel_tool_calls"}
        # same transport-level retry as agent calls, so every group recovers from API errors alike
        for attempt in retry(logger=logger, abort_exceptions=getattr(self.model, "abort_exceptions", [])):
            with attempt:
                response = litellm.completion(
                    model=self.model.config.model_name, messages=prepared, tools=TOOLS, tool_choice="none",
                    max_tokens=max_tokens, **kwargs,
                )
        cost = litellm.cost_calculator.completion_cost(response, model=self.model.config.model_name)
        return response.choices[0].message.content or "", usage_of(response.model_dump()), cost

    # ---------- compaction ----------

    def _compact(self):
        result = self.compactor.compact(self.context, 0)
        head = self.messages[:2]  # system prompt and task, never compacted away
        state = self.store.current()
        if self.store.in_fallback():
            body = "Handoff from your previous context (the structured state update failed):\n\n" + state["fallback_recap"]
        else:
            body = (
                f"Your context was compacted. Working state, version {state['state_version']}:\n\n"
                f"```json\n{render(state)}\n```\n\nContinue from next_action."
            )
        messages, dropped = reset_messages(
            head, body, recent_actions(self.log, self.config.recent_events),
            self.config.tool_output_chars, self.budget, TOOLS, self.count,
        )
        if messages is None:
            raise self._overflow("the reset context alone is over the threshold")
        self.context = messages
        self.log.append("note", what="context_reset", mode=result.mode, dropped_events=dropped,
                        tokens=context_tokens(messages, TOOLS, self.count))

    def _current_diff(self):
        """Tracked changes only: new helper or repro files are not part of a forced submission"""
        try:
            out = self.env.execute({"command": f"git -C {self.files.repo} diff"})
        except Exception:
            return ""
        return out.get("output", "") if out.get("returncode") == 0 else ""

    def _overflow(self, reason):
        diff = self._current_diff()
        self.log.append("note", what="context_overflow", reason=reason)
        return ContextOverflow({"role": "exit", "content": reason,
                                "extra": {"exit_status": "ContextOverflow", "submission": diff}})

    # ---------- actions ----------

    def execute_actions(self, message):
        limit = self.config.tool_output_chars
        outputs = []
        for action in message.get("extra", {}).get("actions", []):
            command = action.get("command", "")
            if hist.is_hist(command):
                code, text = hist.run(self.log, command, limit)
                event = self.log.append("hist", command=command, returncode=code, output=text)
                outputs.append({"output": f"[{event.id}]\n{text}", "returncode": code, "exception_info": ""})
                continue
            try:
                out = self.env.execute(action)
            except Submitted as e:
                self.log.action(command, e.messages[0].get("content", ""), self.files.current(), returncode=0, submitted=True)
                raise
            event = self.log.action(command, out.get("output", ""), self.files.current(),
                                    returncode=out.get("returncode"), exception_info=out.get("exception_info", ""))
            shown = truncate_output(out.get("output", ""), limit, event.id)
            outputs.append({**out, "output": f"[{event.id}]\n{shown}"})
        return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))

    # ---------- results ----------

    def summary(self):
        """Per-run counts for usage.json and run.json"""
        totals = {"agent": {}, "compaction": {}}
        for e in self.log.events:
            if e.kind != "model":
                continue
            bucket = totals[e.payload["purpose"]]
            bucket["calls"] = bucket.get("calls", 0) + 1
            bucket["cost"] = bucket.get("cost", 0.0) + (e.payload.get("cost") or 0.0)
            for k, v in (e.payload.get("usage") or {}).items():
                bucket[k] = bucket.get(k, 0) + v
        modes = [e.payload["mode"] for e in self.log.events if e.kind == "compaction"]
        return {
            "usage": totals,
            "cost": self.cost,
            "steps": self.n_calls,
            "compactions": len(modes),
            "fallbacks": modes.count("fallback"),
            "rebuilds": modes.count("rebuild"),
            "hist_calls": sum(1 for e in self.log.events if e.kind == "hist"),
            "overflow": any(e.kind == "note" and e.payload.get("what") == "context_overflow" for e in self.log.events),
        }


def git_commit(root):
    try:
        return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def dumps(obj):
    return json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n"
