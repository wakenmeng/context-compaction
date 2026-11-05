# Results: group C on SWE-bench Verified

English | [中文](results.zh-CN.md)

Every number here is recomputed from `results/` by `scripts/analyze_group.py`; the summaries it produced are in `results/summary/`. The plan and every amendment, each registered before the run it affects, are in [plan.md](plan.md).

## Setup

| | |
| --- | --- |
| Model | GPT 5.6 Sol, reasoning effort medium, via mini-swe-agent 2.4.0 |
| Context budget | 16K hard limit, compaction at 12K |
| Tasks | 100 SWE-bench Verified tasks, a stratified sample with inclusion probability 1/5 each; 20 more for tuning only |
| Method (group C) | Structured working state updated by validated JSON patches; append-only event log outside the container; evidence goes stale when the files it depends on change; `hist` reads the log back |
| Configuration | Frozen after two smoke passes (code `61e345b`, `configs/frozen_c.json`); the formal run checks both before it starts |
| Evaluation | Official swebench 5.0.2 harness on x86 |
| Runs | One run per task; only C was run (see the scope amendment in the plan) |

## Headline

**C resolved 82 of the 100 formal tasks (95% Wilson interval 73.3%–88.3%) for $26.54 in API cost, $0.27 per task.**

For reference only, the paper reports 84.6% for its text-handoff compaction (EoCC) at 16K/12K with the same model on all 500 Verified tasks. That figure is inside our interval, but the sample, prompts and harness differ and there is no paired baseline here, so no claim is made that C is better or worse.

## How compaction behaved

| | Formal set |
| --- | --- |
| Tasks that compacted at least once | 72 of 100 |
| Compactions | 123: 122 accepted patches, 1 fallback to a text handoff |
| Patches accepted on the first try | 107 of 122 |
| Peak tokens in one call | 15,414; no call over 16K |
| Largest input underestimate | 240 tokens, inside the 300-token output safety margin |
| Steps per task | 14.5 on average |
| Cost of compaction calls | $5.01 of $26.54 (19%) |
| Entries marked stale by the harness | 218, of which 61 were re-verified with new evidence |
| Outputs cut shorter to keep room for compaction | 9 times in 7 tasks |
| `hist` calls | 10 |

**Repeated work after compaction is rare.** In the 10 actions after each compaction, 3 of 877 actions (0.34%, over 72 tasks) repeated a command from before the compaction with no repo change in between.

Resolution by repository: django 40/46, sympy 13/15, scikit-learn 6/6, sphinx 6/9, matplotlib 4/7, astropy 4/5, pytest 4/4, xarray 3/4, requests 2/2, pylint 0/2.

## The eight overflows

Eight tasks ended with `ContextOverflow`; three of them still resolved from the current diff the harness submits on every stop. All eight failed the same way: the first patch was rejected, and the retry, which carries the rejected reply (900 to 1,800 tokens) and the error, no longer fit under 16K, so the run ended before it could fall back to a text handoff. The smoke runs never reached a retry (36 of 36 patches were accepted first try), so this path was first exercised on the formal set. The formal result stays as frozen.

Why the first patch was rejected (replayed offline with `--overflow-causes`):

| Reason | Tasks |
| --- | --- |
| Reviving a stale fact without evidence newer than the change | 5 (3 cited the very action that made the change, 2 cited only older events) |
| `done_when` left empty on the first compaction | 1 |
| State over the 3K cap | 1 |
| Malformed JSON in a rebuild | 1 |

The three that cited the change event suggest the revival rule is stricter than it needs to be: the action that edited a file is itself evidence of what the file now says. The two that cited older events were rightly rejected.

## 16K vs 24K on those eight tasks

The same eight tasks were rerun with only the budget changed, to 24K/18K (`configs/group_c_24k.yaml`, registered before the run).

| | 16K/12K | 24K/18K |
| --- | --- | --- |
| Resolved | 3 of 8 (all from the forced diff) | 6 of 8 |
| Overflows | 8 | 0 |
| Compactions | | 13: 12 patches accepted first try, 1 fallback that then completed |
| Peak tokens | 15,331 | 22,576 |
| Cost | | $3.91 |

At 24K the retry and fallback path had room and worked: `sphinx-doc__sphinx-9461` had two rejected patches, fell back to a handoff, and resolved. One task went the other way (`sympy__sympy-13091` resolved at 16K from its forced diff, not at 24K), which is within single-run noise.

These eight were selected because they failed at 16K, so this is not an estimate of 24K on the full sample. Comparing 16K and 24K properly needs all 100 tasks at 24K.

## What changed during tuning

All changes came from the two tuning tasks and the 20 smoke tasks, never from the formal set, and each is registered in the plan with the data behind it:

- Token estimates are anchored on the API's input count; the local tokenizer undercounted chat formatting by up to 7.7%
- A compaction turn uses whatever room is left (as in the paper) instead of requiring 3K; the old rule overflowed at the first compaction
- Each step's outputs are cut shorter when needed so the next compaction still fits; normal replies keep room for it too
- A 300-token safety margin on every output limit

## Limitations

- One run per task, one model, one benchmark, 100 tasks
- No baseline was run, so the paired hypotheses in the plan are not tested
- The retry-room flaw and the revival rule were found on the formal set; fixing them and measuring the effect needs fresh tasks, not a rerun of these
- `hist` was almost never used, so this run says little about on-demand retrieval

## Next steps

- Run all 100 formal tasks at 24K/18K for a paired 16K vs 24K comparison
- Fix the retry path (fall back when a retry cannot fit) and accept the change event as revival evidence, then evaluate on 100 unused tasks, reported as a post-hoc variant

## Reproduce

```bash
uv run python scripts/analyze_group.py results/c-formal --overflow-causes
uv run python scripts/analyze_group.py results/c24k-overflow --compare results/c-formal
```

The committed per-task summaries (`run.json`, `usage.json`, `eval.json`, `patch.diff`) reproduce the headline and cost figures; the compaction, repeated-work and overflow figures also need the per-task event logs.
