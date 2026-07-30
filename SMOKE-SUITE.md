# Browserbase × Harbor — 10-eval smoke suite

How to run the full fixture suite, and what still blocks it from measuring anything.

Commit: `79be7cc` — *feat: fail loudly on errored rollouts; add 9 task fixtures for smoke suite*

---

> **Superseded in part.** The "READ THIS FIRST" blocker below was resolved after this document was
> written. A later live run (see `SMOKE-RESULTS.md`) had 10/10 trials do real work with 6/10 scoring
> `reward > 0`. The prerequisites and procedure here remain accurate; the blocker section is kept as
> a historical record of the `ExperimentalNotConfiguredError` failure mode.

## READ THIS FIRST (historical — resolved, see `SMOKE-RESULTS.md`)

**A 10-eval run today measures nothing.** All ten evals fail identically with
`ExperimentalNotConfiguredError("Agent callbacks")` before any browser work happens. That is an
upstream Stagehand defect, not a harness defect — see [Still blocking](#still-blocking) below.

What *has* changed is that the failure is now **loud**. Before this commit `evals run` exited 0 on a
failed task and the agent only surfaced output on a non-zero exit, so ten identical failures would
have rendered as a clean green suite of zeros. Run the suite now and you get ten explicit
`StagehandRolloutFailedError`s with the real error text attached.

---

## Prerequisites

### 1. Editable install (mandatory, easy to forget)

Harbor's console script imports `bb_harbor` from site-packages, not from the working tree. Without
this step the custom `import_path`s in `job.yaml` fail to resolve.

```bash
cd /Users/miguel-browserbase/Documents/Browserbase/harbor-browserbase
VIRTUAL_ENV=.venv uv pip install -e . --no-deps
```

There is no `pip` in this venv — use `uv pip`. Re-run this after any edit to `bb_harbor/`.

### 2. Credentials

`~/.envs/prod.env` uses `export`-prefixed lines, so plain sourcing is enough — but it supplies
**only** the Browserbase variables:

| Variable | Source |
|---|---|
| `BROWSERBASE_API_KEY` | `~/.envs/prod.env` |
| `BROWSERBASE_PROJECT_ID` | `~/.envs/prod.env` |
| `BROWSERBASE_BASE_URL` | `~/.envs/prod.env` |
| `STAGEHAND_API_URL` | `~/.envs/prod.env` |
| `GEMINI_API_KEY` | **NOT in `prod.env`** — comes from `../stagehand/.env` |

`job.yaml` requires `GEMINI_API_KEY` for both the agent and the verifier's judge
(`google/gemini-3-flash-preview`). Sourcing only `prod.env` will fail. The verified working
incantation from the `live-smoke-a4` run sources both files:

```bash
set -a; . ~/.envs/prod.env; . ../stagehand/.env; set +a
```

`set -a` is harmless for `prod.env`'s already-`export`ed lines and is what makes the
non-`export`ed lines in `../stagehand/.env` visible to Harbor.

Sanity-check before spending money:

```bash
for v in BROWSERBASE_API_KEY BROWSERBASE_PROJECT_ID GEMINI_API_KEY; do
  [ -n "${!v}" ] && echo "ok   $v" || echo "MISS $v"
done
```

---

## The command

```bash
cd /Users/miguel-browserbase/Documents/Browserbase/harbor-browserbase
VIRTUAL_ENV=.venv uv pip install -e . --no-deps

set -a; . ~/.envs/prod.env; . ../stagehand/.env; set +a

.venv/bin/harbor run \
  --config job.yaml \
  --n-concurrent 3 \
  --jobs-dir jobs \
  --job-name smoke-suite-01 \
  --yes
```

Add `--debug` when triaging a failure.

### On `--n-concurrent 3`

Each trial holds **one real Browserbase session**, so concurrency is bounded by the Browserbase
plan's concurrent-session limit — not by local CPU or Docker. 3 is the deliberate default in
`job.yaml`. Raise it only after confirming plan headroom; every concurrent trial is a billed
session. `--n-concurrent` overrides `n_concurrent_trials` from the config.

### Narrowing the run

Task discovery is by dataset auto-discovery (`datasets: - path: tasks`), so these filters work
without editing `job.yaml`:

```bash
# one task
.venv/bin/harbor run -c job.yaml -i wtb-smoke -n 1 --yes

# first three tasks only
.venv/bin/harbor run -c job.yaml -l 3 -n 3 --yes

# everything except the slow one
.venv/bin/harbor run -c job.yaml -x agent-arxiv-gpt-report --yes
```

---

## Pre-flight checks that cost nothing

Run all four before any live invocation.

```bash
# 1. Unit suite (47 tests)
.venv/bin/python -m pytest -q

# 2. All 10 fixtures load via Harbor's real Task loader; environments are identical
.venv/bin/python scripts/check_task_fixtures.py

# 3. Config validates and the three custom import paths resolve
.venv/bin/harbor run -c job.yaml --print-config
```

Expected from #2: `total fixtures: 10` and a single `environment hash`. Harbor validates each child
dir with `Task.is_valid_dir` and **silently skips** anything incomplete — a malformed fixture
shrinks the suite rather than erroring, so always confirm the count is 10.
`tests/test_components.py::test_job_config_datasets_discover_all_ten_fixtures` guards this in CI.

---

## The 10 fixtures

Each maps to a Stagehand eval task id via a `stagehand-task-id:` line in its `instruction.md`
(deliberately *not* derived from `session_id`).

| Fixture dir | Stagehand task id |
|---|---|
| `tasks/wtb-smoke` | `agent/columbia_tuition` |
| `tasks/agent-all-recipes` | `agent/all_recipes` |
| `tasks/agent-arxiv-gpt-report` | `agent/arxiv_gpt_report` |
| `tasks/agent-github-react-version` | `agent/github_react_version` |
| `tasks/agent-github-ruby-repo` | `agent/github` |
| `tasks/agent-hugging-face` | `agent/hugging_face` |
| `tasks/agent-iframe-form` | `agent/iframe_form` |
| `tasks/agent-nba-trades` | `agent/nba_trades` |
| `tasks/agent-sf-library-card` | `agent/sf_library_card` |
| `tasks/agent-thegamer-opinion` | `agent/thegamer_opinion_article` |

---

## Still blocking

### 1. BLOCKER — Stagehand `benchHarness.ts` drops `experimental` (all 10 evals fail)

Until this lands, **every eval fails identically and the suite measures nothing.** Do not read a
pass rate off a run made before this fix.

The chain:

- `verifierAdapter.ts:334,344` inject recorder callbacks **unconditionally**.
- `benchHarness.ts:175,197` construct the agent passing only `{env}` — `experimental` is never
  forwarded.
- `initV3.ts:111` therefore sets `experimental: false`.
- `validateExperimentalFeatures.ts:121` sees callbacks on a non-experimental agent and throws
  `ExperimentalNotConfiguredError("Agent callbacks")`.

The failure happens at agent construction, before any navigation — so trajectories come back as
`{"status": "error", "steps": [], "usage": {"input_tokens": 0, "output_tokens": 0}}`. Zero browser
work, zero tokens, but a real Browserbase session is still created and billed.

The fix belongs in Stagehand (`benchHarness.ts` must forward `experimental` into both `initV3`
calls), **not** in this repo. A candidate patch was prototyped out-of-tree at
`/tmp/sh-benchharness-fix/` and is deliberately not vendored here.

Note the validator actually lives at `packages/core/lib/v3/agent/utils/`, not the path some earlier
notes cite.

### 2. No live 10-eval run has ever been executed

The only live rollout to date is `live-smoke-a4`: **1 trial, 1 task** (`agent/columbia_tuition`).
The 9 new fixtures have been validated **statically only** — they load via Harbor's real `Task`
loader and their task-id markers parse, but no fixture other than `wtb-smoke` has ever reached
Stagehand. Expect first-run surprises: task ids that don't exist in the eval registry, per-task
timeouts, sites that bot-wall.

### 3. Nine task ids are unverified against the Stagehand eval registry

`scripts/check_task_fixtures.py` confirms the marker *parses*; it cannot confirm the id *resolves*.
A typo'd id will surface as a Stagehand-side "unknown task" error on first live run, not at
validation time.

### 4. The persisted-result verifier path has still never fired

`StagehandVerifier` prefers `scores/result.json` when present, but every run so far has fallen
through to `evals verify --json`. That branch is unit-tested and untested-in-anger.

### 5. `BROWSERBASE_PROJECT_ID` is not scrubbed from job output

Harbor scrubs `BROWSERBASE_API_KEY` (including when embedded in a signed connect URL) but
`BROWSERBASE_PROJECT_ID` does not match its sensitive-key regex and survives in `jobs/`. Strip it
before sharing artifacts — operator responsibility, known gap.

### 6. Cost is incurred even on total failure

A 10-eval run creates 10 real Browserbase sessions even though all 10 currently fail instantly.
`create_session: false` is intentional (Stagehand owns the session, so there is no double-bill and
no leak), but the sessions are still billed. Don't loop the suite while blocker #1 is open.

---

## Definition of done for a *meaningful* run

1. Stagehand `benchHarness.ts` forwards `experimental` — merged and built into the image.
2. `harbor run` reports **10 discovered tasks**.
3. Zero `StagehandRolloutFailedError`s (i.e. no trajectory has `status: error`).
4. All five reward keys present on every trial:
   `reward`, `outcome`, `process`, `process_measured`, `criteria_earned_frac`.
5. Judge healthy — no `StagehandVerifierUnhealthyError`.
6. Session count equals trial count; none left `RUNNING` after the job.
