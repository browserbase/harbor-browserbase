# Session-id capture — live verification

**Verdict: attribution is provably correct.** Three trials ran concurrently, each recorded its own
Browserbase session id in both `AgentContext.metadata` and a per-trial JSON artifact, and each id is
confirmed by the Browserbase API to be a real session in this project whose lifetime brackets that
trial's own agent phase. Independently, each session's own network logs show the target site of the
task that recorded it — content-level attribution, not timing correlation.

> Session ids have been redacted to stable `<session-NN-redacted>` placeholders for publication,
> consistent with `LIVE-SMOKE.md` and `SMOKE-RESULTS.md`; no credential value appears here.

## Run

- Job `jobs/session-id-verify`, 2026-07-30T00:25:30Z → 00:26:44Z, total runtime 1m 7s.
- Temporary config `/tmp/job-session-verify.yaml` (a copy of `job.yaml` with
  `datasets[0].task_names = [agent-iframe-form, agent-github-react-version, agent-sf-library-card]`).
  Nothing was added to the repo; `job.yaml` is untouched.
- `n_concurrent_trials: 3`, `create_session: false`, image `hb__c72eb1bdf0c03ccdc50a7dec6562f512`.
- Gate before the run: `.venv/bin/python -m pytest -q` → **66 passed**.
- Job summary: `Trials 3, Exceptions 0, Reward 1.000` (every trial 1.0).

```bash
set -a; . ~/.envs/prod.env; . ../stagehand/.env; set +a
.venv/bin/harbor run --config /tmp/job-session-verify.yaml -n 3 -o jobs \
  --job-name session-id-verify --yes --debug
```

## Concurrency was real, and it defeats timing correlation

All three agent phases started inside 10 ms of each other, and Stagehand created all three sessions
inside **1.3 milliseconds**:

| session created (API) | id |
|---|---|
| 00:25:42.511673Z | `<session-01-redacted>` |
| 00:25:42.512305Z | `<session-02-redacted>` |
| 00:25:42.512941Z | `<session-03-redacted>` |

Correlating these on `createdAt` is impossible — sub-millisecond spacing. This is exactly the audit
gap the feature closes.

## Per-trial evidence

| Trial | Task | Recorded id (metadata **and** artifact — identical) | API: exists / project / status | Agent phase (own trial) | Session window | Bracketed | Session log hosts | Steps | Tokens in/out | Reward |
|---|---|---|---|---|---|---|---|---|---|---|
| `agent-github-react-version__R6xYmcd` | `agent/github_react_version` | `<session-02-redacted>` | yes / match / COMPLETED | 00:25:40.379 → 00:26:10.946 | 00:25:42.512 → 00:26:10.616 | yes | `github.com`, `api.github.com` | 2 | 20632 / 64 | 1.0 |
| `agent-iframe-form__W5zFKpK` | `agent/iframe_form` | `<session-03-redacted>` | yes / match / COMPLETED | 00:25:40.373 → 00:26:15.122 | 00:25:42.513 → 00:26:14.767 | yes | `browserbase.github.io`, `seanmcguire12.github.io` | 3 | 17444 / 113 | 1.0 |
| `agent-sf-library-card__Hgo9kDS` | `agent/sf_library_card` | `<session-01-redacted>` | yes / match / COMPLETED | 00:25:40.383 → 00:26:32.962 | 00:25:42.512 → 00:26:32.621 | yes | `sflib`/`bibliocommons`/`quipugroup` (SFPL card app) | 3 | 33111 / 69 | 1.0 |

Artifact path per trial: `<trial>/agent/browserbase_session.json`, e.g.

```json
{
  "session_id": "<session-03-redacted>",
  "session_url": "https://www.browserbase.com/sessions/<session-03-redacted>",
  "debug_url": null,
  "task_id": "agent/iframe_form",
  "mode": "dom",
  "all_session_ids": ["<session-03-redacted>"]
}
```

### Why the assignment is unique, not merely consistent

1. **Distinctness.** Three trials, three different ids, one id each (`all_session_ids` has length 1
   for every trial — no cross-trial bleed).
2. **Bracketing + constraint propagation.** Session end times are 10.616 / 14.767 / 32.621; the
   trials' own agent phases end at 10.946 / 15.122 / 32.962, each exactly ~0.34 s after its
   session's end. Only `…10.616` fits the react-version window, which forces `…14.767` to
   iframe-form (32.621 is outside its window), leaving `…32.621` for sf-library-card. The mapping is
   uniquely determined and equals the recorded mapping.
3. **Content proof, independent of all clocks.** Each session's `/v1/sessions/{id}/logs` shows only
   the hosts of the task that recorded it: GitHub for `github_react_version`, the
   `browserbase.github.io/stagehand-eval-sites/sites/iframe-form-filling/` fixture for `iframe_form`,
   and `sflib1.sfpl.org` / bibliocommons / quipugroup for `sf_library_card` (URLs confirmed against
   the upstream eval task definitions). No session shows another task's hosts.

## No secret or connect URL leaked

`grep -rlE "wss://|connect\?|X-BB-API-Key|apiKey=|signingKey|token="` over `jobs/session-id-verify/`
returns **nothing**. `debug_url` is `null` in all three artifacts. Only the unsigned
`https://www.browserbase.com/sessions/<uuid>` dashboard URL is written.

## Session accounting — clean

Project census before/after: exactly **3** new sessions in the run window, all three the recorded
ids, all `COMPLETED`, `keepAlive: false`, no `RUNNING`/unended session anywhere in the project. No
extra session was created (the `create_session: false` contract held — Stagehand owns the session).

## No regression

- 0 exceptions, 0 `StagehandRolloutFailedError`, every trajectory `metadata.json` `status: complete`.
- Steps 2/3/3, non-zero input and output tokens in all three, reward 1.0 in all three
  (`reward`, `outcome`, `process`, `process_measured`, `criteria_earned_frac` all 1.0).
- The eval's stdout is **not** persisted to `trial.log` (`grep -c browserbase.com/sessions
  <trial>/trial.log` → 0 for all three), so the id genuinely only survives because of the capture
  code; nothing else in the output records it.

## Cleanup

`docker ps -a` was empty before and after the run — no stray containers, nothing to remove. No
Docker volumes. The only temporary artifact is `/tmp/job-session-verify.yaml`, outside the repo.

## Defects found

1. **`debug_url` is dead — it can never be populated (cosmetic, non-blocking).**
   `_STAGEHAND_DEBUG_URL_RE` expects raw JSON `"debugUrl": { "value": "…" }`, but Stagehand's logger
   (`packages/core/lib/v3/logger.ts:80-95`) renders `auxiliary` as `    debugUrl: <value>` plain
   text, never as that JSON shape. Empirically the string `debugUrl` appears nowhere in captured
   output and all three artifacts carry `"debug_url": null`. The regex and its safe-URL allowlist are
   only exercised by unit tests against synthetic strings. It fails safe (null, never a signed URL),
   so the audit feature is unaffected — but the field and ~25 lines of extraction/allowlist code are
   unreachable and should be either removed or re-pointed at the real `sessionUrl`-style rendering.
2. **Failure-path return code was not exercised by a real failing eval.** All three trials exited 0.
   The wrapper's rc pass-through was instead confirmed directly in the task image —
   `script -q -e -c "stty cols 400; exit 7" /dev/null` → `rc=7` — so a failing eval will still be
   detected; but no live failing rollout ran through the wrapper.
