# Live 10-eval smoke suite — results

**Verdict: the integration works at concurrency.** All 10 trials did real work (steps > 0 and
tokens > 0), 6 of 10 scored reward > 0, exactly 10 Browserbase sessions were created and all 10
ended, and no session leaked. Zero Harbor exceptions. The previous run's headline bug (0/10 doing
real work, empty trajectories scoring positive) is fully resolved.

- Job: `jobs/live-smoke-10`, job id `<job-id-redacted>`
- Window: 2026-07-29T23:41:55Z → 23:53:36Z (11m 38s), `n_concurrent_trials: 3`
- Image: `hb__c72eb1bdf0c03ccdc50a7dec6562f512` (one tag served all 10 fixtures, no rebuild)
- Repo HEAD at run time: `f6bdd01` ("fix: use positional evals target, trials=1, reject zero-work rollouts")
- Aggregate: 86 agent steps, 1,330,149 input tokens, 2,676 output tokens

> Harbor job ids and Browserbase session ids in this document have been replaced with
> `<...-redacted>` placeholders for publication. No credential value ever appeared here.

---

## Commands

```bash
# 1. Install + test gate
VIRTUAL_ENV=.venv uv pip install -e . --no-deps      # -> installed bb-harbor 0.1.0
.venv/bin/python -m pytest -q                        # -> 56 passed

# 2. Credential gates (names only, never values)
set -a; . ~/.envs/prod.env; . ../stagehand/.env; set +a
curl -s -o /dev/null -w '%{http_code}' -H "X-BB-API-Key: $BROWSERBASE_API_KEY" \
  "$BROWSERBASE_BASE_URL/v1/projects"                                    # -> 200
curl -s -o /dev/null -w '%{http_code}' \
  "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY"  # -> 200

# 3. FREE PRE-FLIGHT — the check whose absence cost the last two runs
docker run --rm hb__c72eb1bdf0c03ccdc50a7dec6562f512 sh -c \
  "cd /opt/stagehand && evals run agent/iframe_form --trials 1 --concurrency 1 \
   --agent-mode dom --env browserbase --model google/gemini-3-flash-preview --preview"
# -> Target: agent/iframe_form -> bench (1 task)
#    Env: BROWSERBASE  Concurrency: 1  Trials: 1  Harness: stagehand
#    Total: 1 run                                     PLAN RESOLVED, proceeded to spend.

# 4. Session census before
curl -s -H "X-BB-API-Key: $BROWSERBASE_API_KEY" "$BROWSERBASE_BASE_URL/v1/sessions?status=RUNNING"
# -> 0 running

# 5. The live run
.venv/bin/harbor run -c job.yaml -o jobs --job-name live-smoke-10 --yes --debug
# -> exit 0, 10/10 trials, total runtime 11m 38s
```

The pre-flight matters: the agent now also runs `--preview` in-container before every trial
(`_validate_eval_plan`), so a mis-resolved target aborts the trial instead of burning a session.

---

## Per-task results

| task | traj status | steps | input_tokens | reward | outcome | process | criteria_frac | reward_source |
|---|---|---|---|---|---|---|---|---|
| `agent/all_recipes` | complete | 8 | 220,728 | 0.0 | 0.0 | 1.0 | 1.0 | persisted `scores/result.json` |
| `agent/arxiv_gpt_report` | complete | 8 | 167,118 | 1.0 | 1.0 | 1.0 | 1.0 | persisted `scores/result.json` |
| `agent/columbia_tuition` | complete | 18 | 199,782 | 0.0 | 0.0 | 0.25 | 0.25 | persisted `scores/result.json` |
| `agent/github` | complete | 6 | 142,068 | 1.0 | 1.0 | 1.0 | 1.0 | persisted `scores/result.json` |
| `agent/github_react_version` | complete | 3 | 36,807 | 1.0 | 1.0 | 1.0 | 1.0 | persisted `scores/result.json` |
| `agent/hugging_face` | complete | 20 | 311,530 | 0.0 | 0.0 | 0.0 | 0.0 | persisted `scores/result.json` |
| `agent/iframe_form` | complete | 3 | 17,441 | 1.0 | 1.0 | 1.0 | 1.0 | persisted `scores/result.json` |
| `agent/nba_trades` | complete | 8 | 71,470 | 0.0 | 0.0 | 1.0 | 1.0 | persisted `scores/result.json` |
| `agent/sf_library_card` | complete | 6 | 54,576 | 1.0 | 1.0 | 1.0 | 1.0 | persisted `scores/result.json` |
| `agent/thegamer_opinion_article` | complete | 6 | 108,629 | 1.0 | 1.0 | 1.0 | 1.0 | persisted `scores/result.json` |

**Headline: 10 of 10 took steps > 0 AND tokens > 0.** Last run it was 0/10.
**6 of 10 scored reward > 0.** Every trajectory reported `status: "complete"`; none `error`.

### The 4 reward-0 trials are genuine agent failures, not integration defects

Each was read from its judge output in `scores/result.json` and each verdict is evidence-backed:

- `agent/hugging_face` — agent clicked "Reset Licenses" at step 17, dropping the Apache-2.0
  constraint the task required, then fabricated `gpt2` as the answer. Constraint-handling failure.
- `agent/columbia_tuition` — 18 steps of repetitive navigation, then a fabricated answer claiming
  tuition figures for schools/years never visited. Hallucination (`errorCode 2.3`).
- `agent/nba_trades` — espn.com transactions page errored, agent pivoted to nba.com, then failed to
  dismiss the "Your Privacy Choices" modal covering the content and fabricated the transactions.
  Part live-web breakage (a bot/consent wall), part hallucination.
- `agent/all_recipes` — found a qualifying Beef Wellington recipe but never listed the ingredients
  it claimed to have extracted. Incomplete deliverable.

None of these touch Harbor, the environment, or the verifier wiring.

---

## Session accounting

Interval sweep over the Browserbase API, restricted to sessions created inside the run window and
absent from the pre-run census:

- **New sessions in window: exactly 10** — one per trial. No double-billing.
- **All 10 status `COMPLETED`.** `?status=RUNNING` after the run: **0**. No leaks.
- **Max concurrent sessions: 3**, by two independent methods: an interval sweep over
  `createdAt`/`endedAt`, and a 67-sample 10s poller of `?status=RUNNING` (max observed 3).
  Matches `n_concurrent_trials: 3` exactly — the cap is respected, nothing over-subscribes.
- Each trial directory contains exactly **1** trajectory (`n_trajs == 1` for all 10), so no
  cross-trial trajectory bleed. `--trials 1` held: no trial paid the 3x default.

Sessions (created → ended, UTC):

```
<session-01-redacted>  23:42:01 -> 23:45:43
<session-02-redacted>  23:42:01 -> 23:42:56
<session-03-redacted>  23:42:01 -> 23:46:12
<session-04-redacted>  23:43:13 -> 23:43:47
<session-05-redacted>  23:44:03 -> 23:50:51
<session-06-redacted>  23:45:59 -> 23:48:37
<session-07-redacted>  23:46:28 -> 23:47:36
<session-08-redacted>  23:47:53 -> 23:48:24
<session-09-redacted>  23:48:40 -> 23:49:35
<session-10-redacted>  23:48:53 -> 23:53:23
```

---

## Error surfaces

| Surface | Count | Notes |
|---|---|---|
| `StagehandRolloutFailedError` | 0 | No rollout was empty or errored, so the guard never fired. Untested on this run. |
| `StagehandVerifierUnhealthyError` | 0 | Judge scored all 10 rubrics. No false positive, no genuine unhealthy. |
| Python tracebacks in run log | 0 | |
| `Unknown option` from the evals CLI | 0 | Positional-target fix confirmed live, not just in preview. |

### Empty-trajectory false positive: RESOLVED

Last run, `agent-github-react-version` and `agent-hugging-face` scored `process: 1.0` and
`criteria_earned_frac: 1.0` on 0-step trajectories. This run, **zero trials have 0 steps**, so the
condition cannot arise, and the explicit check for "0 steps with positive process or criteria"
returns empty. Both previously-affected tasks now have real trajectories: `github_react_version`
3 steps / reward 1.0, `hugging_face` 20 steps / reward 0.0 with a correctly-reasoned failure.

---

## Defects and caveats found this run

1. **No Browserbase session ID is persisted anywhere in trial output.** A recursive grep of a trial
   directory for any UUID, `sessionId`, `sessionUrl`, or `debuggerUrl` finds nothing. Proving
   trajectory-to-session mapping therefore required the Browserbase API plus timing correlation
   rather than a direct lookup. Concurrency safety was demonstrable here only because the counts
   were clean (10 sessions, 10 trials, max 3 concurrent). Recording the session id in the agent's
   `metadata.stagehand` block would make this auditable directly and is worth doing.

2. **`process` / `criteria_earned_frac` overstate partial work on single-criterion rubrics.**
   `agent/all_recipes` and `agent/nba_trades` both scored `process: 1.0` and
   `criteria_earned_frac: 1.0` while `outcome: 0.0`. That is not the empty-trajectory bug — both
   have real trajectories and the earned criterion was genuinely satisfied. The cause is rubric
   shape: their rubric has exactly one criterion, and it is a navigation-only criterion ("did the
   agent make it to the nba transactions page?"), so merely arriving scores full process. Read
   `process` as "fraction of rubric criteria earned," not "did most of the task." `reward` (which
   tracks `outcome`) is the trustworthy primary metric.

3. **The scrubbing test was inconclusive because nothing leaked to scrub.** Neither the API key
   literal (0 files) nor `BROWSERBASE_PROJECT_ID` (0 files) appears anywhere in
   `jobs/live-smoke-10`. The expectation was that PROJECT_ID would survive verbatim, since its name
   misses Harbor's `/(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH)/i` filter. It does not appear at
   all — `config.json` persists the env block as unexpanded `${BROWSERBASE_PROJECT_ID}`
   placeholders, so the value never reaches disk on this path and Harbor's scrubber was never
   exercised. The gap documented in `job.yaml` remains theoretically open for any path that *does*
   emit the value (e.g. a connect URL in agent stdout); this run produced no such path. The only
   `redacted` markers in the output (10) are our own inline-image-payload elision in
   `trajectory.json`, not Harbor secret scrubbing.

---

## Cleanup

Nothing to remove. After the run, `docker ps -a` was empty and `docker compose ls -a` listed no
projects — Harbor tore down all 10 task containers itself. No orphaned clients or containers, and
no `docker desktop restart` was performed at any point.
