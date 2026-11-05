"""Compaction prompts for the structured-state groups (C, E)

Written for this project. Group B uses the paper's Appendix G.1 handoff prompt,
which is not redistributed here; see docs/plan.md
"""

SCHEMA = """\
State fields you maintain (task is written by the harness and never changes):
- done_when: list of strings, the completion conditions; append only
- constraints: [{{"id": "c1", "text": ..., "reason": ...}}]; add or update, never remove
- facts: [{{"id": "f1", "text": ..., "status": "observed|verified|assumed|superseded", "sources": ["e12"], "at_event": "e12", "files": ["src/a.py"]}}]
  observed/verified facts must cite the action events that show them (sources) and the repo files they depend on (files, [] if none)
  mark a wrong fact superseded with superseded_by instead of deleting it
- changes: [{{"path": "src/a.py", "summary": ..., "verified": false}}]
- checks: [{{"id": "k1", "command": ..., "result": ..., "status": "valid", "sources": ["e20"], "at_event": "e20", "files": []}}]
  files [] means the check depends on the whole repo
- dead_ends: [{{"id": "d1", "approach": ..., "reason": ..., "at_event": "e15", "files": [...]}}]; append only
- plan: ordered list of remaining subgoals; next_action: the very next command or edit
- pointers: [{{"label": ..., "ref": "e30"}}] for history, or [{{"label": ..., "path": "src/a.py", "symbol": "func"}}] for code; no line numbers

Event ids (e12, ...) are the [eN] tags on commands and outputs above. Only the harness marks entries stale: their files changed
after the evidence. To rely on a stale entry again, re-run the command or re-read the file and cite that new event"""

PATCH = (
    "Stop working and update the working state for the next context. Do not run commands or call tools.\n\n"
    "The next context will contain only the task, this state and the last few events, so record what you need to continue: "
    "established facts with evidence, what you changed and whether it is verified, checks and their results, approaches that "
    "failed, and the exact next step. Keep large outputs out of the state; point to their event ids instead.\n\n"
    + SCHEMA
    + "\n\nReply with one JSON object and nothing else:\n"
    '{{"base_version": {version}, "ops": [\n'
    '  {{"op": "add", "field": "facts", "value": {{...}}}},\n'
    '  {{"op": "update", "field": "facts", "id": "f3", "value": {{"status": "verified", "sources": ["e40"], "at_event": "e40"}}}},\n'
    '  {{"op": "set", "field": "next_action", "value": "..."}}\n'
    "]}}\n"
    "Ops: add, update (by id, or by path for changes), set (plan, next_action only).\n"
    "Stale entries now: {stale}. Dead ends whose files changed since they were recorded: {premise}.\n"
    "The state must stay under {cap} tokens.\n\n"
    "Current state, version {version}:\n{state}"
)

REBUILD = (
    "Stop working and write the working state from scratch. Do not run commands or call tools.\n\n"
    "The last state update failed, so the context after this one holds only your text handoff. Rebuild a complete state "
    "from what you know now.\n\n"
    + SCHEMA
    + "\n\nThese carry over unchanged; you may only add to them:\n{carried}\n\n"
    "Your last handoff:\n{recap}\n\n"
    "Reply with one JSON object holding facts, changes, checks, dead_ends, plan, next_action and pointers, plus any new "
    "done_when or constraints entries. The state must stay under {cap} tokens."
)

RETRY = "The state update was rejected:\n{errors}\n\nReply with the corrected JSON object only."

SHRINK = (
    "The state would be {size} tokens, over the cap of {cap}. Reply with a corrected JSON object that is smaller. In order: "
    'remove stale and superseded facts and checks ({{"op": "remove", "field": "facts", "id": "f2"}}), merge dead_ends into '
    "one new entry with merged_from listing the old ids (then remove them), and shorten fact texts. done_when and "
    "constraints cannot be removed."
)

FALLBACK = (
    "Stop working and write a concise handoff for continuing this task in a fresh context. Do not run commands or call "
    "tools. Cover, briefly: the objective in one line; facts you established, kept apart from guesses; files you changed "
    "and whether the change is verified; checks you ran and their results; approaches that failed; and the remaining plan "
    "with the very next command and the condition for being done. Cite event ids for anything long."
)

C_PROMPTS = {"patch": PATCH, "rebuild": REBUILD, "retry": RETRY, "shrink": SHRINK, "fallback": FALLBACK}

HIST_HELP = """\
## History

Outputs longer than {limit} characters are cut; the full text stays in the event log. Each command result is tagged
with an event id like [e12]. When context is compacted you keep a working state and the last few events; anything else
can be read back with these commands (they run outside the sandbox and do not touch the repo):

- `hist search <regex>`: matching lines across all past commands and outputs
- `hist read <event_id> --lines A:B`: lines A to B of one event's full output"""
