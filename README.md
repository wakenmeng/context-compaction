# context-compaction

English | [中文](README.zh-CN.md)

**Can a coding agent work in a 16K context without forgetting what it learned?** This project tests a structured, verifiable alternative to the text handoffs that current agents use when they compact their context.

## Conclusion: with a limited context window, is compaction good?

**Yes, on this evidence.** Working in a 16K window and compacting into a structured state, with a 24K fallback when a compaction does not fit, the agent solved as many tasks as the paper's agent that keeps the full history, at lower cost per task.

| Setup | Context the model sees | Resolved | Cost per task | Source |
| --- | --- | --- | --- | --- |
| **This project: compaction, 16K with 24K fallback** | 16K; 24K only for the 8 tasks that overflowed | **85/100** (95% CI 77–91%) | **$0.30** | 100 tasks |
| This project: compaction, 16K only | 16K | 82/100 (95% CI 73–88%) | $0.27 | 100 tasks |
| Paper: no compaction (full history) | Full history | 84.4% | $0.34 | 500 tasks |
| Paper: text-handoff compaction (EoCC) | 16K | 84.6% | $0.41 | 500 tasks |

Same model (GPT 5.6 Sol) and the same list prices ($4 / $0.40 / $20 per million new input / cached input / output tokens) throughout. In the 16K/24K row, a task moves to 24K only if it overflowed at 16K, never because of its test result, and both runs' costs count.

- **Accuracy holds.** 85% against 84.4% with the full history, while the model never sees more than 16K on 92 of the 100 tasks.
- **It costs less.** $0.30 per task against $0.34 for the full history and $0.41 for the paper's text handoff at 16K, because the state is patched field by field instead of rewritten, and contexts restart short.
- **It does not make the agent repeat itself.** 0.35% of actions after a compaction repeated earlier work.
- **16K alone is tight.** 8% of tasks overflowed when a rejected patch had no room for its retry; with a 24K fallback none did.

**What this does not show.** The paper's numbers come from all 500 tasks with its own prompts and harness, so this is an outside comparison, not a paired one; with a ±7-point interval, a difference of a few points cannot be seen. The 16K/24K row combines runs after the formal 16K result was in. A no-compaction baseline on these same 100 tasks would turn this into a paired result. Full analysis: [docs/results.md](docs/results.md).

## Why

[You don't need long context](https://arxiv.org/pdf/2611.05306) (arXiv 2611.05306) shows that frontier models stay close to their full-history accuracy on SWE-bench Verified when they compact often and keep contexts short. Their compaction rewrites the whole history into a fresh text handoff each time, which leaves three gaps the paper itself points to:

- losses compound over repeated rewrites
- the model cannot tell what it forgot, so retrieval tools alone do not help
- success rate is the only measure; nothing checks whether the compacted state could replace the history

## The idea

Treat compaction as maintaining a **Markov working state**: given the state and access to the environment, the next decision should not need the full history.

- **Structured state, patched field by field.** Goal, facts, changes, checks, dead ends, plan and next action live in a JSON state. The model submits patches; the harness validates them and keeps every version. Nothing is silently rewritten.
- **Evidence the harness can verify.** Facts and checks cite events in an append-only log kept outside the container. The harness hashes repo files after every action, so it knows when a file changed, and marks dependent facts and checks stale on its own. The model cannot revive stale evidence without a newer observation.
- **Pointers instead of copies.** Large outputs stay in the log; the model reads bounded slices with `hist` when it needs them.
- **Elastic context.** Normally 16K/12K; the agent may expand to 48K or 128K for a few steps when chunked reading cannot do the job, then compacts back.

## Experiment

| Group | Context policy | After compaction |
| --- | --- | --- |
| B | Paper EoCC baseline, fixed 16K, compacts at 12K | Text handoff from the paper's Appendix G.1 prompt |
| C | Same 16K/12K | Structured state with validated patches, event log and `hist` |
| E | C plus temporary expansion, at most 3 times per task | Same as C |

Only C is run: it is tuned on the 20 smoke tasks only, frozen, then run on the 100 formal tasks. B (the paper baseline) and E (C plus expansion) are specified in the plan but not run, so the paired hypotheses against B are not tested; C is reported descriptively (success rate with a confidence interval, repeated work after compaction, cost, compaction outcomes). The paper's 84.6% for EoCC 16K/12K on all 500 tasks is an outside reference only, not a paired comparison.

The plan, metrics and every amendment are pre-registered in [docs/plan.md](docs/plan.md) (Chinese) before the runs they affect. Amendments so far came from two smoke-task trials, never from formal tasks.

## Status

| Milestone | Content | State |
| --- | --- | --- |
| M0 | Sampling, pinned environment, gold validation on x86 | Done |
| M1 | Event log, state schema, patch validator, invalidation, fallback and rebuild | Done |
| M2 | Context assembly, token budget, `hist`, end-to-end tests on scripted traces | Done for C |
| M3 | mini-swe-agent integration, one task end to end | C done: two smoke trials, compaction verified on real runs |
| M4 | 20-task smoke run, tuning, then freeze the config | Done: 16/20 on both passes; frozen in `configs/frozen_c.json` |
| M5 | 100-task run for C, official evaluation | Done: 82/100 |
| M6 | Statistics and report | Done: [docs/results.md](docs/results.md); 24K rerun of the 8 overflows |

The formal run used the configuration frozen after two smoke passes (16/20 each). Eight formal tasks overflowed because a retry after a rejected patch no longer fit under 16K; at 24K/18K the same eight had no overflow and six resolved. The result stays as frozen; the flaw and a 16K vs 24K comparison on all 100 tasks are the next steps in [docs/results.md](docs/results.md).

## Engineering notes

- **Unbiased sample.** The 100 formal tasks are stratified by repo with randomized systematic rounding, so every task has inclusion probability exactly 1/5 and the plain mean is unbiased for all 500. Ranks come from sha256, so the draw does not depend on the Python version. ([sample_instances.py](scripts/sample_instances.py))
- **Pinned inputs.** The dataset is pinned to a Hugging Face revision and a parquet sha256, and the sample records a fingerprint over every field of every record. Dependencies are locked with `uv.lock`.
- **Environments proven before agents run.** All 120 sampled tasks were evaluated with their gold patches on x86: 119 resolve. The one failure needs the live internet and is frozen as excluded before any run. ([env_unsolvable.json](configs/env_unsolvable.json))
- **A state the model cannot game.** Evidence is dated by its newest cited action, not by a timestamp the model writes; a stale entry keeps a revalidation gate through any detour; malformed patches are rejections, never crashes. ([patch.py](src/context_compaction/patch.py))
- **Adversarial review.** The plan and the M1 code were reviewed by a second model (OpenAI Codex) in repeated rounds until no blocking issue remained. Each finding was reproduced before it was fixed and got a regression test; the M1 rounds closed about a dozen ways a model could have bypassed the evidence rules.

## Repository layout

| Path | Content |
| --- | --- |
| `docs/plan.md` | Pre-registered plan: hypotheses, metrics, statistics, milestones, amendments |
| `docs/results.md` | Results and analysis |
| `src/context_compaction/event_log.py` | Append-only event log with per-action file changes |
| `src/context_compaction/state.py` | State schema and versioned store |
| `src/context_compaction/patch.py` | Patch validation, invalidation, rebuild after a fallback |
| `src/context_compaction/compactor.py` | One compaction turn: patch, one shared retry, text fallback, rebuild |
| `src/context_compaction/agent.py` | mini-swe-agent integration: token budget, compaction and reset, `hist`, per-action logging |
| `src/context_compaction/context.py`, `hist.py`, `repo_files.py` | Context assembly, history tool, in-container file hashing |
| `src/context_compaction/pins.py` | Pinned versions and dataset snapshot |
| `scripts/` | Sampling, dataset download, environment check, gold evaluation, cloud VM, group runs, evaluation, analysis |
| `configs/` | Frozen task list and excluded tasks |
| `results/gold/` | Gold evaluation summaries |
| `results/summary/`, `results/c-formal/`, `results/c24k-overflow/` | Analysis summaries and per-task records (run, usage, evaluation, patch) of the reported runs |
| `tests/` | Unit tests |

## Reproduce

Development and unit tests run anywhere with [uv](https://docs.astral.sh/uv/):

```bash
uv sync --frozen --extra dev
uv run pytest
uv run python scripts/fetch_dataset.py
```

Machine-specific settings go in a gitignored `.env`; start from the template:

```bash
cp .env.example .env
```

The API key is read from `OPENAI_API_KEY`, or from `openai.txt` in `CC_CREDENTIALS_DIR`. Keep it outside the repo.

Anything that runs SWE-bench containers needs x86: the images are x86_64, and emulation on Apple Silicon makes environment failures look like model failures. `scripts/cloud/gcp.sh` manages a GCP VM (default `n2-standard-16`, Ubuntu 24.04, 500 GB) in the project set by `CC_PROJECT`:

```bash
scripts/cloud/gcp.sh create
scripts/cloud/gcp.sh setup
CC_JOB=check scripts/cloud/gcp.sh run python scripts/check_env.py
CC_JOB=gold scripts/cloud/gcp.sh run python scripts/gold_eval.py --set all --workers 8
scripts/cloud/gcp.sh logs gold
scripts/cloud/gcp.sh pull
scripts/cloud/gcp.sh stop
```

- `setup` syncs the code (not `.env`, data or results), copies the key file to the VM with mode 600, installs Docker, uv and the locked dependencies, runs the tests and downloads the dataset.
- `run` starts a job in tmux on the VM; `pull` brings `results/` back; `stop` leaves only the disk billed.
- `check_env.py` exits non-zero on any failed check and never prints the key. `gold_eval.py` exits non-zero if a gold patch does not resolve; `--run-id` starts a fresh run.
- `run_group.py --group c --set smoke` runs a group and writes `results/c/<instance_id>/` (events, states, compactions, trajectory, usage, patch); rerunning resumes, and unfinished attempts are archived, at most 3 per task. `evaluate_group.py --group c --set smoke` scores one frozen set with the official harness.

## Pinned versions

| Component | Version | Note |
| --- | --- | --- |
| Python | 3.12 | One interpreter, one lock resolution |
| mini-swe-agent | 2.4.0 | The paper says v2.4 |
| swebench | 5.0.2 | The paper gives no version |
| Dataset | SWE-bench/SWE-bench_Verified @ `78f471bf` | 500 tasks |

`SWE-bench/SWE-bench_Verified` is used instead of `princeton-nlp/SWE-bench_Verified` because swebench 5.x needs its `image`, `eval_script` and `log_parser` fields. The two share all 500 ids, problem statements, patches and base commits; `PASS_TO_PASS` is trimmed for `astropy__astropy-7606` and `django__django-10097`.
