# context-compaction

English | [中文](README.zh-CN.md)

An experiment on Markov-state context compaction for coding agents, built on [You don't need long context](https://arxiv.org/pdf/2611.05306) (arXiv 2611.05306).

The paper keeps contexts short by periodically rewriting the history into a text handoff (EoCC). We test whether a structured working state, updated field by field with evidence and invalidation rules, loses less information and repeats less work under the same 16K/12K budget, without lowering the success rate.

The full plan is in [docs/plan.md](docs/plan.md) (Chinese). It is the pre-registered version, committed before any agent run; it defines what counts as success or failure.

## Status

| Milestone | Content | State |
| --- | --- | --- |
| M0 | Sampling, pinned environment, x86 gold validation | Done |
| M1 | Event log, state schema, patch validator, invalidation, fallback and rebuild | Done |
| M2 | Context assembly, token budget, `hist` retrieval, replay on recorded traces | Next |
| M3 | mini-swe-agent integration, B/C/E configs, one task end to end | |
| M4 | 20-task smoke run, tuning, then freeze the config | |
| M5 | 100-task formal runs and official evaluation | |
| M6 | Decision replay, statistics, report | |

No agent run has happened yet.

## Groups

| Group | Context policy | After compaction |
| --- | --- | --- |
| B | Paper EoCC, fixed 16K limit, compacts at 12K | Text handoff written with the paper's Appendix G.1 prompt |
| C | Same 16K/12K | Structured state JSON; the model submits field-level patches that the harness validates; full history goes to an event log the model can query with `hist` |
| E | C plus temporary expansion to 48K or 128K, at most 3 times per task | Same as C |

All three are implemented. The formal runs compare B and E only; C is used to validate the code. This scope change is being written into the pre-registration before any run.

## Pinned versions

Every pinned value lives in `src/context_compaction/pins.py`; all dependencies are locked by `uv.lock`.

| Component | Version | Note |
| --- | --- | --- |
| Python | 3.12 | One interpreter, so the lock file has a single resolution |
| mini-swe-agent | 2.4.0 | The paper only says v2.4; the patch version is our choice |
| swebench | 5.0.2 | Official evaluation; the paper does not give a version |
| Dataset | SWE-bench/SWE-bench_Verified @ `78f471bf` | 500 tasks; the parquet sha256 is in pins.py |

We do not use `princeton-nlp/SWE-bench_Verified`: swebench 5.x needs the `image`, `eval_script` and `log_parser` fields, which only the `SWE-bench/` dataset has. The two share all 500 ids, problem statements, patches and base commits; `PASS_TO_PASS` is trimmed for `astropy__astropy-7606` and `django__django-10097`. Agent runs and evaluation read the same local snapshot, `data/SWE-bench_Verified`.

## Sampling

`configs/instances.json` is generated and frozen, seed 20261104.

- Formal set, 100 tasks: stratified by repo with randomized systematic rounding, so every task has inclusion probability 1/5 and the plain mean is an unbiased estimate for all 500. Small repos can get 0 tasks in one draw (Flask and Seaborn here); that is sampling variance, not bias.
- Smoke set, 20 tasks: drawn from the remaining 400, one per repo first, the rest proportional. Used only for tuning and environment checks, never in estimates.
- `records_sha256` covers every field of every record, so any change to the data is detected.

## Gold validation

All 120 sampled tasks were run through the official harness with their gold patches on x86: 119 resolve. `psf__requests-1724` (smoke set) failed on all 5 runs because its `PASS_TO_PASS` tests call the live httpbin.org (HTTP 429 and an expired CA bundle). It is frozen in `configs/env_unsolvable.json` and excluded from the primary analysis of every group. All 100 formal tasks resolve. Summaries are in `results/gold/`.

## Code

| Path | Content |
| --- | --- |
| `src/context_compaction/event_log.py` | Append-only event log kept outside the container; records which files each action changed |
| `src/context_compaction/state.py` | State schema and versioned store |
| `src/context_compaction/patch.py` | Patch validation, invalidation of stale evidence, rebuild after a fallback |
| `src/context_compaction/compactor.py` | One compaction turn: patch, one shared retry, text fallback, rebuild |
| `src/context_compaction/pins.py`, `credentials.py`, `tokens.py` | Pinned values, API key loading, token counting |
| `scripts/` | Sampling, dataset download, environment check, gold evaluation, cloud VM management |
| `tests/` | Unit tests (`pytest`) |

## Running environment

All Docker work (gold evaluation, smoke and formal runs, official evaluation) runs on an x86 VM on GCP. SWE-bench images are x86_64; on an Apple Silicon Mac they run under emulation, which makes environment failures indistinguishable from model failures. The Mac is only for development and unit tests.

Default VM: `n2-standard-16` (16 vCPU / 64 GB), Ubuntu 24.04 amd64, 500 GB pd-balanced. Set `CC_PROJECT` to your GCP project id; `CC_ZONE`, `CC_VM`, `CC_MACHINE` and `CC_DISK_GB` override the other defaults in `scripts/cloud/gcp.sh`. A dedicated project makes cleanup a single `gcloud projects delete`.

```bash
scripts/cloud/gcp.sh create
scripts/cloud/gcp.sh setup
CC_JOB=check scripts/cloud/gcp.sh run python scripts/check_env.py
CC_JOB=gold-smoke scripts/cloud/gcp.sh run python scripts/gold_eval.py --set smoke --workers 8
scripts/cloud/gcp.sh logs gold-smoke
scripts/cloud/gcp.sh pull
scripts/cloud/gcp.sh stop
```

- `setup` runs `push` first: it rsyncs the code (with `.git`, without `.venv`, `data`, `results`), copies the API key file (`CC_KEY_FILE`) to the VM with mode 600, then installs Docker, uv and the locked dependencies, runs the tests and downloads the dataset snapshot.
- `run` starts the job in tmux, so it survives SSH disconnects; the log goes to `logs/<CC_JOB>.log` on the VM.
- After changing code, `push` before `run`; `pull` brings `results/` back.
- `stop` when idle: a stopped VM only bills for its disk. `delete` removes the disk too.
- `gcloud compute config-ssh` adds a `cc-x86.<zone>.<project>` host entry to `~/.ssh/config`.
- Docker uses the `mirror.gcr.io` pull-through cache; if image pulls hit HTTP 429, run `docker login` once on the VM.

## Developing on a Mac

```bash
uv sync --frozen --extra dev
source .venv/bin/activate
pytest
python scripts/fetch_dataset.py
```

- API key: read from `OPENAI_API_KEY` if set, else from `openai.txt` (one bare key) in the directory named by `CC_CREDENTIALS_DIR`. Keep the key outside the repo.
- `check_env.py`: fast checks; exits non-zero if any check fails and never prints the key.
- `gold_eval.py`: runs the official harness with gold patches and lists tasks that do not resolve, exiting non-zero if there are any. Run the smoke set first, then all 120; rerunning the same `--set` skips finished tasks, and `--run-id` starts a fresh run. Results go to `results/gold/`. A task that still fails after 2 reruns goes into `configs/env_unsolvable.json`, frozen before any agent run.
- `sample_instances.py`: refuses to overwrite the frozen file if a rerun gives a different result.
