# context-compaction

English | [中文](README.zh-CN.md)

**Can a coding agent work in a 16K context without forgetting what it learned?** This project tests a structured, verifiable alternative to the text handoffs that current agents use when they compact their context.

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

The formal runs compare **B and E** on the same 100 SWE-bench Verified tasks with the same model and step limits. C is implemented and used to validate the code. Because E changes both the state and the context budget, a win for E is a win for the package, not for either part alone.

The hypotheses and decision rules are pre-registered in [docs/plan.md](docs/plan.md) (Chinese) before any agent run: E must not solve more than 3 fewer tasks than B, must repeat less work after compaction (paired bootstrap on a frozen definition of a repeated action), and must stay under a cost ratio fixed before the smoke run. Results that do not meet these rules will be reported as such.

## Status

| Milestone | Content | State |
| --- | --- | --- |
| M0 | Sampling, pinned environment, gold validation on x86 | Done |
| M1 | Event log, state schema, patch validator, invalidation, fallback and rebuild | Done |
| M2 | Context assembly, token budget, `hist`, replay on recorded traces | Next |
| M3 | mini-swe-agent integration, B/C/E configs, one task end to end | |
| M4 | 20-task smoke run, tuning, then freeze the config | |
| M5 | 100-task runs for B and E, official evaluation | |
| M6 | Statistics and report | |

No agent run has happened yet, so there are no results.

## Engineering notes

- **Unbiased sample.** The 100 formal tasks are stratified by repo with randomized systematic rounding, so every task has inclusion probability exactly 1/5 and the plain mean is unbiased for all 500. Ranks come from sha256, so the draw does not depend on the Python version. ([sample_instances.py](scripts/sample_instances.py))
- **Pinned inputs.** The dataset is pinned to a Hugging Face revision and a parquet sha256, and the sample records a fingerprint over every field of every record. Dependencies are locked with `uv.lock`.
- **Environments proven before agents run.** All 120 sampled tasks were evaluated with their gold patches on x86: 119 resolve. The one failure needs the live internet and is frozen as excluded before any run. ([env_unsolvable.json](configs/env_unsolvable.json))
- **A state the model cannot game.** Evidence is dated by its newest cited action, not by a timestamp the model writes; a stale entry keeps a revalidation gate through any detour; malformed patches are rejections, never crashes. ([patch.py](src/context_compaction/patch.py))
- **Adversarial review.** The plan and the M1 code were reviewed by a second model (OpenAI Codex) in repeated rounds until no blocking issue remained. Each finding was reproduced before it was fixed and got a regression test; the M1 rounds closed about a dozen ways a model could have bypassed the evidence rules.

## Repository layout

| Path | Content |
| --- | --- |
| `docs/plan.md` | Pre-registered plan: hypotheses, metrics, statistics, milestones |
| `src/context_compaction/event_log.py` | Append-only event log with per-action file changes |
| `src/context_compaction/state.py` | State schema and versioned store |
| `src/context_compaction/patch.py` | Patch validation, invalidation, rebuild after a fallback |
| `src/context_compaction/compactor.py` | One compaction turn: patch, one shared retry, text fallback, rebuild |
| `src/context_compaction/pins.py` | Pinned versions and dataset snapshot |
| `scripts/` | Sampling, dataset download, environment check, gold evaluation, cloud VM |
| `configs/` | Frozen task list and excluded tasks |
| `results/gold/` | Gold evaluation summaries |
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

## Pinned versions

| Component | Version | Note |
| --- | --- | --- |
| Python | 3.12 | One interpreter, one lock resolution |
| mini-swe-agent | 2.4.0 | The paper says v2.4 |
| swebench | 5.0.2 | The paper gives no version |
| Dataset | SWE-bench/SWE-bench_Verified @ `78f471bf` | 500 tasks |

`SWE-bench/SWE-bench_Verified` is used instead of `princeton-nlp/SWE-bench_Verified` because swebench 5.x needs its `image`, `eval_script` and `log_parser` fields. The two share all 500 ids, problem statements, patches and base commits; `PASS_TO_PASS` is trimmed for `astropy__astropy-7606` and `django__django-10097`.
