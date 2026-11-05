"""Token budget and context assembly, shared by every group

Context length is the full input of one call: system prompt, tool schema and
all messages. Output limits include reasoning tokens
"""
import json
import math
from dataclasses import dataclass

from .tokens import count_tokens

MESSAGE_OVERHEAD = 4  # role and separators per message, as in OpenAI's accounting


@dataclass(frozen=True)
class Budget:
    hard_limit: int = 16_000
    threshold: int = 12_000
    state_cap: int = 3_000
    # a compaction turn is attempted with whatever room is left, as in the paper;
    # below this it cannot produce anything useful, so the run overflows instead
    min_compaction_output: int = 1_024

    def normal_output(self, input_tokens):
        return self.hard_limit - input_tokens

    def compaction_output(self, input_tokens):
        """Output room for a compaction turn, or None when it is too small to try"""
        room = self.hard_limit - input_tokens
        return room if room >= self.min_compaction_output else None


def message_tokens(message, count=count_tokens):
    total = MESSAGE_OVERHEAD
    content = message.get("content")
    if isinstance(content, str):
        total += count(content)
    elif isinstance(content, list):
        total += sum(count(part.get("text", "")) for part in content if isinstance(part, dict))
    for call in message.get("tool_calls") or []:
        fn = call.get("function", {}) if isinstance(call, dict) else {}
        total += count(fn.get("name", "")) + count(fn.get("arguments", ""))
    return total


def context_tokens(messages, tools=(), count=count_tokens):
    schema = count(json.dumps(list(tools))) if tools else 0
    return schema + sum(message_tokens(m, count) for m in messages)


def truncate_output(text, limit, event_id):
    """Cap one tool output; the full text stays in the log under event_id"""
    if len(text) <= limit:
        return text
    head = limit * 3 // 4
    tail = limit - head
    return (
        f"{text[:head]}\n[... {len(text) - limit} characters omitted; full output is {event_id}, "
        f"use `hist read {event_id} --lines A:B` ...]\n{text[-tail:]}"
    )


def render_event(event, limit):
    output = truncate_output(str(event.payload.get("output", "")), limit, event.id)
    return f"[{event.id}] $ {event.payload.get('command', '')}\nreturncode {event.payload.get('returncode')}\n{output}"


def recent_actions(log, n):
    return [e for e in log.events if e.kind == "action"][-n:] if n else []


def reset_messages(head, body, recent, limit, budget, tools=(), count=count_tokens, margin=0.0):
    """Context after a compaction: system + task, the state or handoff, recent events

    Drops recent events one by one until the reset fits under the threshold;
    returns (messages, dropped) or (None, dropped) when even the bare reset does not fit
    """
    recent = list(recent)
    dropped = 0
    while True:
        parts = [body]
        if recent:
            parts.append("Most recent events (oldest first):\n\n" + "\n\n".join(render_event(e, limit) for e in recent))
        messages = list(head) + [{"role": "user", "content": "\n\n".join(parts)}]
        if math.ceil(context_tokens(messages, tools, count) * (1 + margin)) <= budget.threshold:
            return messages, dropped
        if not recent:
            return None, dropped
        recent.pop(0)
        dropped += 1
