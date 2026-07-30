# Live smoke test — attempt 3

**Verdict: YES, a real end-to-end rollout happened.** One live Harbor trial ran to completion
against a live Browserbase session, produced a trajectory, ran the real Gemini judge, and returned
all five reward keys with zero Harbor exceptions. The *task itself* failed (reward 0.0) because of
an **upstream Stagehand defect** in the cloned `main` — not because of the Harbor integration.

Run: `jobs/live-smoke-a4`, job id `<job-id-redacted>`,
2026-07-29T00:14:13Z to 00:14:34Z (19s total runtime).

> Harbor job ids and Browserbase session ids in this document have been replaced with
> `<...-redacted>` placeholders for publication. No credential value ever appeared here.

Attempt 2's report was moved to `/tmp/LIVE-SMOKE-attempt2.md`.

---

## Pipeline progress

| Stage | Result |
|---|---|
| Credential gate (HTTP 200 on `/v1/projects`) | PASS |
| `uv pip install -e . --no-deps` | PASS (was Defect A) |
| Two Codex-authored fixes + full pytest | PASS, 35 passed, committed `3d9c345` |
| Docker image (`bb-harbor-wtb-smoke:local`, from-source monorepo build) | PASS, cache hit, no rebuild needed |
| `evals` CLI in container, target resolution | PASS |
| Harbor environment start + `create_session=False` | PASS |
| Agent phase: `evals run agent/columbia_tuition` | Ran; exit 0; **rollout status = `error`** |
| Trajectory at shared path, survived to verification | PASS |
| Verifier phase (`environment_mode="shared"`) | PASS |
| Judge (`google/gemini-3-flash-preview`) health | HEALTHY — scored the rubric, no unhealthy raise |
| Reward keys | All 5 present and numeric |
| Session accounting | Exactly 1 session, released, no leak |

No fallback to a prebuilt host `dist` was used. The image is the **production shape**: a from-source
`pnpm install --frozen-lockfile && pnpm run build` of the monorepo cloned inside the Dockerfile.
Image stagehand commit: `0cce9dbfd4bdb0cc1a51b1b83151efbea6649b6f` (2026-07-28), evals `2.1.0`.

---

## Exact commands

```bash
# 1. Credential gate (names only, never values)
set -a; . ~/.envs/prod.env; set +a
curl -s -o /dev/null -w "%{http_code}\n" -H "X-BB-API-Key: $BROWSERBASE_API_KEY" \
  "$BROWSERBASE_BASE_URL/v1/projects"        # -> 200

# 2. REQUIRED install (Defect A from attempt 2)
VIRTUAL_ENV=.venv uv pip install -e . --no-deps

# 3. Codex fixes, then the gate
.venv/bin/python -m pytest -q                # 35 passed

# 4. Image reuse check (no rebuild)
docker run --rm bb-harbor-wtb-smoke:local evals --help
docker run --rm bb-harbor-wtb-smoke:local sh -c \
  "cd /opt/stagehand && evals run agent/columbia_tuition --preview"

# 5. The live trial
set -a; . ~/.envs/prod.env; . ../stagehand/.env; set +a
.venv/bin/harbor run --config job.yaml -n 1 -o jobs --job-name live-smoke-a4 --yes --debug
```

---

## Results

```
Trials  Exceptions  Criteria_Earned_Frac  Outcome  Process  Process_Measured  Reward
     1           0                 0.000    0.000    0.000             1.000   0.000
```

- **All five keys present and numeric**: `reward`, `outcome`, `process`, `process_measured`,
  `criteria_earned_frac`. `reward` = 0.0. The fixed-key-set contract held; nothing evaluated to
  `-inf` against `min_reward`.
- **Reward source: `evals verify --json`.** `self.reward_source` recorded
  `"reward source: evals verify --json"`. The persisted-result path did **not** fire — the in-run
  `scores/result.json` was absent, because the rollout threw before `runWithVerifier` could persist
  an `EvaluationResult`. The fallback worked exactly as designed and is what produced the score.
- **Judge was healthy.** `Stagehand criteria aggregation: case=all-kept-criteria-scored-zero
  kept=1 total=1 earned=0.0 max=1.0` — the LLM actually judged one rubric criterion and gave it
  zero. `StagehandVerifierUnhealthyError` was correctly NOT raised. `gemini-3-flash-preview` is
  alive; no 404.
- `process_measured=1.0` — `processScore` came back numeric even for an errored trajectory.

## Session accounting — clean, and the `create_session=False` default HELD

Enumerated project sessions before and after via the Browserbase API.

**Exactly ONE session for the trial:**

| id | created | ended | status |
|---|---|---|---|
| `<session-01-redacted>` | 00:14:18Z | 00:14:21Z | COMPLETED |

- **No double-billing.** Harbor logged `Stagehand will create and own the Browserbase session for
  this trial` and created none of its own. Stagehand created exactly one.
- **Released, not leaked.** Ended 3s after creation, `keep_alive: false`, status COMPLETED. No
  RUNNING or unended session remained. No stray Docker containers either.
- A second session `<session-redacted>` (00:15:34Z to 00:15:37Z, also COMPLETED) exists in the census;
  that is **mine**, from the diagnostic re-run below, not the trial's.

---

## The one genuine failure: an upstream Stagehand defect

The rollout produced `metadata.json` `"status": "error"`, `trajectory.json` with **0 steps** and
`usage {input_tokens: 0, output_tokens: 0}`. The browser session opened and closed in 3 seconds.

I re-ran the same eval directly in the container to capture the error text that the harness had
discarded. The failure is:

```
x agent/columbia_tuition  google/gemini-3-flash-preview  failed  Feature "Agent ca...
```

i.e. `ExperimentalNotConfiguredError("Agent callbacks")`. The chain, all verified in the image's
own source:

1. `packages/evals/framework/verifierAdapter.ts:334,344` — `runWithVerifier` **unconditionally**
   injects `callbacks: { onEvidence }` into `agent.execute()` to feed the trajectory recorder.
2. `packages/evals/initV3.ts:111-113` — `experimental` is `configOverrides.experimental` or
   `false`; `benchHarness.ts:175,197` pass only `{ env: config.environment }`, so it is **false**.
3. `packages/core/lib/v3/agent/utils/validateExperimentalFeatures.ts:121` — with
   `isExperimental === false`, `callbacks` is an unsupported feature and it throws
   `ExperimentalNotConfiguredError("Agent callbacks")`.

`benchHarness.ts:93-94` does set `disableAPI: true, experimental: true`, but only on
`buildVerifierCarrierV3` — the never-`init()`-ed LLM carrier — not on the browser-driving instance.

This is **environment-independent** (nothing in the chain depends on `browserbase` vs `local`) and
is a defect in stagehand `main` @ `0cce9db`, not in `bb_harbor/`. The error message's wording
("cannot be configured when `disableAPI: false`") is itself misleading: `disableAPI` was in fact
`true` here, since `USE_API` was not `"true"`.

**Nothing in the Harbor integration can fix this.** It needs an upstream change — either
`experimental: true` on the bench browser instance, or dropping the experimental gate for recorder
callbacks.

### Integration defect worth fixing on our side

`evals run` **exits 0 when the task fails.** `StagehandAgent.run` only surfaces the eval's
stdout/stderr on a non-zero return code, so the real error text was thrown away and the trial
reported "0 exceptions" for a rollout that never took a single step. I had to re-run the eval by
hand to learn why. Recommended: always log a tail of the eval stdout, and/or read `metadata.json`'s
`status` and fail (or at minimum warn loudly) when it is `error`. This is a *reporting* defect only
— the reward pipeline handled it correctly.

---

## Credential scrubbing — NOT exercised; claim neither confirmed nor refuted

Grepped every file under `jobs/live-smoke-a4/` for the literal API key, the literal project id, and
any connect URL / `wss://`:

```
API_KEY 0    PROJECT_ID 0    CONNECT_URL 0    REDACTED_MARKER 0
```

Zero hits for *everything*, including the project id, so this is the **absence of a test**, not a
passing test. Two independent reasons the leak conditions never arose:

1. Harbor persists agent/verifier env as **unresolved templates** — `jobs/.../config.json` contains
   `"BROWSERBASE_PROJECT_ID": "${BROWSERBASE_PROJECT_ID}"`, not the value. Nothing to scrub.
2. With `create_session=False`, no signed connect URL is ever produced host-side.

The source-level claim stands unchanged: `BROWSERBASE_PROJECT_ID` does not match Harbor's
`/(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH)/i` filter, so it *would* survive verbatim — but that
only bites if an operator writes a **literal** value into `job.yaml` instead of a `${VAR}` template,
or if a future `create_session=True` path emits a connect URL. Confirming it empirically requires a
trial deliberately configured with literal values.

---

## Codex-authored changes this attempt (commit `3d9c345`)

Allowlist honoured exactly: `bb_harbor/env.py`, `README.md`. Nothing else touched.

- **`preflight()` now authenticates.** Keeps the missing-variable check first, then makes one
  `Browserbase(...).projects.retrieve(project_id)` call, honouring `BROWSERBASE_BASE_URL`. Raises a
  new typed `BrowserbaseCredentialError` naming the offending variable on 401 / 403 / 404, and a
  plain `RuntimeError` (reachability, not credentials) on connection errors and 5xx. Never prints or
  embeds the key. `BB_SKIP_PREFLIGHT_API_CHECK=1` opts out for offline use.
- **README** documents the mandatory `VIRTUAL_ENV=.venv uv pip install -e . --no-deps`, the required
  env var **names**, the new preflight behaviour, and that components wire via `job.yaml`, never
  `task.toml`.

Verified empirically, not by self-report:

```
GOOD KEY: preflight PASSED
BAD KEY raised BrowserbaseCredentialError: Browserbase rejected the credential configured by
  BROWSERBASE_API_KEY with HTTP 401; the credential must be replaced.
```

Codex correctly reported one real-signature divergence from my spec: `projects.retrieve` names its
positional parameter `id`, not `project_id`, and lives in `browserbase/resources/projects.py`.

**Coverage gap:** the new authenticated branch has no unit test. The existing preflight test
(`tests/test_components.py:445`) only covers the missing-variable path and still passes because it
never reaches the network call. A follow-up spec should add mocked 401/403/404/connection cases.

---

## Two minor operational notes

- `harbor run --job-name live-smoke-a3` failed instantly with
  `FileExistsError: Job directory jobs/live-smoke-a3 already exists and cannot be resumed with a
  different config.` Attempt 2 had already consumed both `live-smoke-a2` and `live-smoke-a3`. Job
  names must be unique per jobs dir; re-ran as `live-smoke-a4`.
- `Skipping image OS validation for hb__...: docker inspect returned 1` appears once at start and is
  benign.

---

## Next concrete action

1. **Upstream Stagehand fix (blocks any non-zero reward from this pipeline).** Either set
   `experimental: true` on the bench browser-driving V3 in `benchHarness.ts:175,197`, or stop gating
   recorder `callbacks` behind `experimental` in `validateExperimentalFeatures.ts`. Until then every
   `runWithVerifier` bench agent task fails at step 0 with
   `ExperimentalNotConfiguredError("Agent callbacks")` regardless of harness. Worth confirming
   against stagehand CI — if their bench suite is green, something in their run configuration sets
   `experimental` that a bare `evals run` does not.
2. **Codex spec: make `StagehandAgent` fail loudly on a silent eval failure** — log an eval stdout
   tail unconditionally and treat `metadata.json` `status == "error"` as an agent failure.
   Allowlist: `bb_harbor/agent.py`, `tests/test_components.py`.
3. **Codex spec: unit tests for the new authenticated preflight** (mocked 401/403/404/connection).
   Allowlist: `tests/test_components.py`.
4. **Re-run this smoke test after (1)** to get the first *scoring* rollout — everything downstream
   of the agent is already proven, so that run only needs to confirm a non-zero reward path and the
   `scores/result.json` (persisted) reward source, which has still never fired.
