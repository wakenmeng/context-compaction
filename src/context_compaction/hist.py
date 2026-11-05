"""`hist`: read-only, bounded access to the event log, run by the harness

  hist search <regex>              matching lines across past commands and outputs
  hist read <event_id> [--lines A:B]  a slice of one event's full output (1-based, inclusive)

The model reaches the log only through this; nothing is mounted in the container
"""
import re
import shlex

USAGE = "usage: hist search <regex> | hist read <event_id> [--lines A:B]"
DEFAULT_LINES = 100
MAX_MATCHES = 50
MAX_LINE = 200


def is_hist(command):
    return command.strip().split(maxsplit=1)[:1] == ["hist"]


def _bounded(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated at {limit} characters; narrow the query]"


def _actions(log):
    return [e for e in log.events if e.kind == "action"]


def search(log, pattern, limit):
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return 1, f"invalid regex: {e}"
    hits = []
    for event in _actions(log):
        text = f"$ {event.payload.get('command', '')}\n{event.payload.get('output', '')}"
        for n, line in enumerate(text.splitlines(), start=0):
            if regex.search(line):
                shown = line if len(line) <= MAX_LINE else line[:MAX_LINE] + "..."
                hits.append(f"{event.id}:{n}: {shown}")
                if len(hits) >= MAX_MATCHES:
                    break
        if len(hits) >= MAX_MATCHES:
            hits.append(f"[stopped at {MAX_MATCHES} matches]")
            break
    if not hits:
        return 1, "no matches"
    header = "line 0 is the command; output lines start at 1\n"
    return 0, _bounded(header + "\n".join(hits), limit)


def read(log, ref, span, limit):
    event = log.get(ref)
    if event is None or event.kind != "action":
        return 1, f"{ref} is not an action event"
    lines = str(event.payload.get("output", "")).splitlines()
    if span:
        m = re.fullmatch(r"(\d+):(\d+)", span)
        if not m or int(m.group(1)) < 1 or int(m.group(2)) < int(m.group(1)):
            return 1, "--lines takes A:B with 1 <= A <= B"
        start, end = int(m.group(1)), int(m.group(2))
    else:
        start, end = 1, DEFAULT_LINES
    chunk = lines[start - 1:end]
    header = f"{event.id} $ {event.payload.get('command', '')}\nlines {start}-{start + len(chunk) - 1} of {len(lines)}\n"
    return 0, _bounded(header + "\n".join(chunk), limit)


def run(log, command, limit=4000):
    """Answer a hist command; returns (returncode, output)"""
    try:
        args = shlex.split(command)[1:]
    except ValueError as e:
        return 1, f"{USAGE}\n{e}"
    if len(args) == 2 and args[0] == "search":
        return search(log, args[1], limit)
    if args[:1] == ["read"] and len(args) in (2, 4):
        span = None
        if len(args) == 4:
            if args[2] != "--lines":
                return 1, USAGE
            span = args[3]
        return read(log, args[1], span, limit)
    return 1, USAGE
