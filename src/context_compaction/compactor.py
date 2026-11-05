"""Compaction turn for groups C and E

patch mode: ask for a JSON patch against the current version
rebuild mode: after a fallback, ask for a whole new state cycle
Either mode gets one retry, shared between fixing errors and shrinking an
over-cap state. If that fails too, fall back to a text handoff
"""
import json
import re
from dataclasses import dataclass, field

from . import patch as patching
from .state import render
from .tokens import count_tokens, truncate_tokens

DEFAULT_PROMPTS = {
    "patch": (
        "Stop working and update the working state. Do not run commands or call tools.\n"
        "Reply with one JSON object {{\"base_version\": {version}, \"ops\": [...]}} using ops "
        "add, update, set (and remove only when asked to shrink).\n"
        "Entries marked stale had their files change after they were recorded; re-check them "
        "before relying on them.\nStale entries: {stale}\nState cap: {cap} tokens.\n\n"
        "Current state (version {version}):\n{state}"
    ),
    "rebuild": (
        "Stop working and write a complete new working state. Do not run commands or call tools.\n"
        "Reply with one JSON object holding the fields facts, changes, checks, dead_ends, plan, "
        "next_action, pointers, and any new done_when or constraints entries.\n"
        "These carry over unchanged and may only be extended:\n{carried}\n"
        "Your last handoff:\n{recap}\nState cap: {cap} tokens."
    ),
    "retry": "The state update was rejected:\n{errors}\nReply with a corrected JSON object only.",
    "shrink": (
        "The state would be {size} tokens, over the cap of {cap}. Reply with a corrected JSON "
        "object that shrinks it: first remove superseded and stale facts and checks, then merge "
        "dead_ends (new entry with merged_from), then shorten fact texts. task, done_when and "
        "constraints cannot be removed."
    ),
    "fallback": "Stop working and write a concise handoff recap. Do not run commands or call tools.",
}


@dataclass
class CompactionResult:
    mode: str  # patch | rebuild | fallback
    state: dict
    attempts: int
    errors: list = field(default_factory=list)
    stale_marked: list = field(default_factory=list)
    revived: list = field(default_factory=list)


def _entries(state):
    # facts and checks may reuse an id, so key by field too
    return {(n, i["id"]): i for n in ("facts", "checks") for i in state[n]}


def diff_stale(before, after):
    """Ids that became stale, and ids revalidated, between two states

    An entry counts as revalidated when it reaches observed/verified/valid while
    it was stale or carried a revalidation gate (stale, then assumed)
    """
    old, new = _entries(before), _entries(after)
    marked = [k[1] for k, e in new.items() if e.get("status") == "stale" and old.get(k, {}).get("status") != "stale"]
    revived = [
        k[1] for k, e in new.items()
        if e.get("status") in ("observed", "verified", "valid") and patching.revalidation_gate(old.get(k))
    ]
    return marked, revived


def dependency_stats(state):
    """Process metric: evidenced entries and how many declare no files"""
    live = [f for f in state["facts"] if f.get("status") in ("observed", "verified")]
    live += [k for k in state["checks"] if k.get("status") == "valid"]
    return {"evidenced": len(live), "empty_files": sum(1 for i in live if not i.get("files"))}


def parse_json(text):
    """Pull the JSON object out of a model reply, tolerating code fences"""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(text[start:end + 1])


class Compactor:
    def __init__(self, store, log, model, prompts=None, cap_tokens=3000, max_retries=1, count=count_tokens):
        self.store, self.log, self.model = store, log, model
        self.prompts = {**DEFAULT_PROMPTS, **(prompts or {})}
        self.cap, self.max_retries, self.count = cap_tokens, max_retries, count

    def consecutive_fallbacks(self):
        n = 0
        for kind, _ in reversed(self.store.versions):
            if kind == "fallback":
                n += 1
            elif kind in ("patch", "rebuild"):
                break
        return n

    def compact(self, context, max_output_tokens):
        """Run one compaction turn on top of `context` (the messages so far)"""
        rebuilding = self.store.in_fallback()
        marked = []
        if rebuilding:
            base = self.store.last_valid()
            request = self.prompts["rebuild"].format(
                carried=json.dumps(
                    {"done_when": base["done_when"], "constraints": base["constraints"]}, ensure_ascii=False
                ),
                recap=self.store.current()["fallback_recap"],
                cap=self.cap,
            )
        else:
            base, marked = patching.invalidate(self.store.current(), self.log)
            if base != self.store.current():
                base = self.store.commit("invalidate", base)
            stale = [i["id"] for n in ("facts", "checks") for i in base[n] if i.get("status") == "stale"]
            request = self.prompts["patch"].format(
                version=base["state_version"], stale=stale or "none", cap=self.cap, state=render(base)
            )

        messages = list(context) + [{"role": "user", "content": request}]
        errors, shrink = [], False
        for attempt in range(1, self.max_retries + 2):
            reply = self.model(messages, max_output_tokens)
            try:
                proposal = parse_json(reply)
                if rebuilding:
                    new = patching.rebuild(base, proposal, self.log, self.cap, self.count)
                else:
                    new = patching.apply_patch(base, proposal, self.log, self.cap, self.count, shrink=shrink)
            except (ValueError, patching.PatchRejected) as e:
                errs = getattr(e, "errors", [f"invalid JSON: {e}"])
                errors.append(errs)
                over = getattr(e, "over_cap", False)
                if over:
                    shrink = True
                    size = int(re.search(r"is (\d+) tokens", errs[0]).group(1))
                    follow = self.prompts["shrink"].format(size=size, cap=self.cap)
                else:
                    follow = self.prompts["retry"].format(errors="\n".join(f"- {x}" for x in errs))
                messages += [{"role": "assistant", "content": reply}, {"role": "user", "content": follow}]
                continue
            mode = "rebuild" if rebuilding else "patch"
            later, revived = diff_stale(base, new)
            marked = marked + [i for i in later if i not in marked]
            state = self.store.commit(mode, new)
            self._record(mode, attempt, errors, marked, revived, dependency_stats(state))
            return CompactionResult(mode, state, attempt, errors, marked, revived)

        return self._fallback(context, max_output_tokens, attempt, errors, marked)

    def _fallback(self, context, max_output_tokens, attempts, errors, marked):
        messages = list(context) + [{"role": "user", "content": self.prompts["fallback"]}]
        recap = truncate_tokens(self.model(messages, max_output_tokens), self.cap)
        state = self.store.last_valid()
        state["fallback_recap"] = recap
        state = self.store.commit("fallback", state)
        self._record("fallback", attempts + 1, errors, marked, [], dependency_stats(state))
        return CompactionResult("fallback", state, attempts + 1, errors, marked)

    def _record(self, mode, attempts, errors, marked, revived, deps):
        self.log.append(
            "compaction",
            mode=mode,
            attempts=attempts,
            errors=errors,
            stale_marked=marked,
            revived=revived,
            dependencies=deps,
            state_version=self.store.versions[-1][1]["state_version"],
            consecutive_fallbacks=self.consecutive_fallbacks(),
        )
