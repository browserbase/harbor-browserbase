# Adversarial review — Claude (harbor-browserbase)

Ground truth read: harbor 0.20.0 at `.venv/lib/python3.14/site-packages/harbor`, Stagehand at
`~/Documents/Browserbase/stagehand` (`8bb8d4262`). `pytest -q` → 24 passed.

Every Harbor symbol used by `bb_harbor` was checked against real source and **no fabricated Harbor
API was found** (see "Negative results"). The serious defects are elsewhere: the integration's
central mechanism (session hand-off) is inert against the real Stagehand CLI, and no LLM credential
can reach the container.

---

## 1. CONFIRMED — HIGH — Browserbase session injection is inert; Stagehand never reads those env vars

`bb_harbor/env.py:83-102` (`session_env` / `session_scope`), `bb_harbor/agent.py:186-226`.

The environment creates a Browserbase session with `keep_alive=True` and exports
`BROWSERBASE_SESSION_ID` / `BROWSERBASE_CONNECT_URL` into the container via `scoped_exec_env`.
Nothing in Stagehand reads either variable. Session reuse is only reachable through the
`browserbaseSessionID` **option**:

- `packages/core/lib/v3/types/public/options.ts:36` — `browserbaseSessionID?: string`
- `packages/evals/initV3.ts:118` — `browserbaseSessionID: configOverrides?.browserbaseSessionID`
- `packages/evals/framework/benchHarness.ts:175,197` — the only callers pass
  `configOverrides: { env: config.environment }` and **never** `browserbaseSessionID`.

A repo-wide grep for `BROWSERBASE_SESSION_ID` / `BROWSERBASE_CONNECT_URL` in Stagehand returns zero
hits outside our own files. `initV3.ts:100-102` only reads `BROWSERBASE_API_KEY` / `BROWSERBASE_PROJECT_ID`.

Failure scenario: run `job.yaml`. `BrowserbaseEnvironment.start()` creates session A (keep-alive, so
it is billed until `stop()` releases it or the project timeout fires). The agent then runs
`evals run agent/columbia_tuition --env browserbase`, which creates session **B** and drives that.
Every trial consumes two sessions and two concurrency slots; the Harbor-side `browserbase_session_id`
recorded in logs/metadata refers to a session in which nothing ever happened, so any later
correlation of trajectory → session replay/logs points at the wrong session.

Fix: pass the session through a channel Stagehand actually honours — either
(a) add `browserbaseSessionID` plumbing to the evals CLI/harness (env var → `configOverrides`) and
inject it, or (b) drop session creation from the environment entirely and let Stagehand own the
session, extracting its id from `task_data.json` / the recorded trajectory. Do not ship the current
shape: it pays for a session it cannot use.

## 2. CONFIRMED — HIGH — No LLM API key reaches the container, so both `evals run` and `evals verify` fail

`job.yaml:20-27`, `tasks/wtb-smoke/environment/Dockerfile`, `tasks/wtb-smoke/task.toml:16-18`.

`google/gemini-3-flash-preview` requires `GEMINI_API_KEY` / `GOOGLE_GENERATIVE_AI_API_KEY` /
`GOOGLE_API_KEY` (`packages/core/lib/utils.ts:696`, `packages/core/lib/v3Evaluator.ts:77`,
`packages/evals/initV3.ts:79-87`). The agent's `extra_env` carries only
`BROWSERBASE_API_KEY`/`BROWSERBASE_PROJECT_ID` (`bb_harbor/agent.py:335`); `[environment].env` and
`[verifier].env` are empty; the Dockerfile sets no key. Docker only forwards
`task_env_config.env` (`harbor/environments/docker/docker.py:258`), so the container sees nothing.

Failure scenario: first real run. `evals run` reaches `initV3` and dies on missing provider key (CUA
mode) or fails every LLM call (dom mode → dead-judge synthesis), and `evals verify` fails the same
way — a smoke task that can never pass. Fix: add the judge/agent key to the agent `extra_env` **and**
to the verifier env channel (see finding 3), e.g. `GEMINI_API_KEY: ${GEMINI_API_KEY}`. Keeping it in
`agent.extra_env` also gets it into Harbor's scrub set (`trial/trial.py:738-748`).

## 3. CONFIRMED — HIGH — `StagehandVerifier` never forwards `verifier_env` / `override_env` to `exec`, so the only supported credential channel is silently dropped

`bb_harbor/verifier.py:195-200` (and the same omission at `:276-281`, `:294-299`, `:327-332`).

Harbor's own verifier merges `task.config.verifier.env` + `verifier_env` + `override_env`, resolves
templates, and passes the result as `env=` to `exec` (`harbor/verifier/verifier.py:159-202`).
`StagehandVerifier` reads those dicts only to look up `BB_*` settings (`verifier.py:251-264`) and
calls `self.environment.exec(command, cwd=..., timeout_sec=..., user=...)` with **no `env=`**. The
agent-phase `scoped_exec_env` overlay does not cover the verifier phase either
(`harbor/trial/trial.py:445` wraps only the agent run), so the judge process runs with an empty env.

Failure scenario: an operator fixes finding 2 the documented way — `task.toml`
`[verifier] env = { GEMINI_API_KEY = "${GEMINI_API_KEY}" }` or `harbor run --verifier-kwarg`/job
`verifier.env`. The value is accepted, resolved, handed to the verifier… and never reaches the
container. `evals verify` still 404s/auth-fails, and because a dead judge is silent this surfaces as
either an exec error or (if the CLI exits 0) a synthesized zero. Fix: build
`{**self.task.config.verifier.env, **(self.verifier_env or {}), **self.override_env}`, run it
through `harbor.utils.env.resolve_env_vars`, and pass it as `env=` on every `exec` call.

## 4. CONFIRMED — MEDIUM — Session leaks when the trial is cancelled during `sessions.create`

`bb_harbor/env.py:59-63`.

`self.browserbase_session_id` is assigned only after `create()` returns. If the trial is cancelled
(or the SDK request times out / the connection drops) after the server created the session but
before the response is consumed, the id is never recorded, `stop()` sees `session_id = None`, and no
`REQUEST_RELEASE` is ever sent. With `keep_alive=True` the session then burns until the project
timeout — the worst leak shape, and exactly the case the shielded `stop()` was written to prevent.

Fix: after a failed/cancelled `create`, reconcile by listing running sessions for the project
(`sessions.list(status="RUNNING")`) and releasing any whose `userMetadata` carries this trial's
`self.session_id`; set `user_metadata={"harborSessionId": self.session_id}` on create so that
reconciliation is possible. At minimum, do not swallow the create-cancellation silently — log the
leak with the trial name.

## 5. CONFIRMED — MEDIUM — The concurrency tests assert the fixture, not the goal

`tests/test_components.py:66-111`, `tests/conftest.py:92-115`.

`test_browserbase_scopes_are_isolated_across_interleaved_tasks` proves a real property (Harbor's
per-instance `ContextVar` overlay is task-local — worth keeping), but the `find` branch of the test
script *fabricates* a trajectory path out of `merged_env["BROWSERBASE_SESSION_ID"]`
(`test_components.py:83-89`), so the assertion "each trial saw its own session" is an assertion about
the fixture's own arithmetic. Nothing in the suite executes, or even shapes itself around, the real
`evals` contract — which is why finding 1 (Stagehand ignores those variables entirely) passes 24
green tests. Same class of gap for the verifier: every payload is a 2-4 key hand-written dict
(`test_components.py:380,412,489`), so `perCriterion`/`maxPoints`/`processScore`/`rawSteps` naming is
never checked against real output.

Fix: add a contract test that fails if the injected variables are not consumed — e.g. assert that the
`evals run` argv contains `--browserbase-session-id <id>` (once finding 1 is fixed), and add a golden
`EvaluationResult` fixture captured from a real `evals verify --json` run.

## 6. CONFIRMED — MEDIUM — The trajectory is judged twice; the authoritative in-run score is discarded

`bb_harbor/verifier.py:186-206` vs `packages/evals/framework/verifierAdapter.ts:242-253` and
`packages/evals/framework/harnesses/persistTrajectory.ts:80-95`.

`agent/columbia_tuition` already runs `V3Evaluator.verify()` inside the eval
(`packages/evals/tasks/bench/agent/columbia_tuition.ts:30-46`) and persists the resulting
`EvaluationResult` to `<trajectory-dir>/scores/result.json`. The Harbor verifier ignores that file
and pays for a second full multimodal judge pass.

Failure scenario: two judge passes over the same trajectory disagree (non-zero temperature,
screenshot sampling, transient LLM failure). Braintrust/eval output says pass, Harbor's reward says
0, and the two are irreconcilable after the fact — plus double judge cost per trial. Fix: read
`scores/result.json` when present and fall back to `evals verify --json` only when it is missing;
whichever path is used, log which one produced the reward.

## 7. CONFIRMED — LOW — Startup-failure teardown hardcodes `delete=True`

`bb_harbor/env.py:76`. `await super().stop(delete=True)` ignores `EnvironmentConfig.delete`
(`harbor/models/trial/config.py:200`). Failure scenario: an operator sets `environment: {delete: false}`
to inspect the container after a failure; a Browserbase create error deletes it anyway, destroying
the evidence. Fix: thread the configured value (`self._keep_containers` / the `delete` the caller
would have used) instead of a literal.

## 8. CONFIRMED — LOW — The "keepalive" does not keep anything alive

`bb_harbor/env.py:104-114`. `sessions.retrieve(id)` is a read; the Browserbase API has no
lifetime-extension side effect, and `keep_alive=True` already covers idle survival (and requires a
paid plan tier — on a lower tier `create` fails and every trial dies in `start()`). The loop is a
60-second poll whose only effect is log noise and one extra API call per minute. Fix: delete it, or
repurpose it into an explicit health check that fails the trial when the session status leaves
`RUNNING`.

## 9. CONFIRMED — LOW — `AsyncBrowserbase` is never closed

`bb_harbor/env.py:48-53`. Already triaged in `review-codex.md` finding 1; still unfixed. One leaked
`httpx.AsyncClient` per trial. Fix: `await self._browserbase_client.close()` in `stop()`'s `finally`
and in the `start()` failure handler.

## 10. CONFIRMED — LOW — `process` reward fabricates 0.0 when `processScore` is absent

`bb_harbor/verifier.py:223-224`. `EvaluationResult.processScore` is optional (undefined on the
outcome-only path, `rubricVerifier.ts:851-856`). Emitting `0.0` makes "not measured" indistinguishable
from "scored zero", so any mean over `process` is silently dragged down. The fixed-key-set constraint
forbids omitting it; record the distinction explicitly instead (log it as `compute_criteria_fraction`
already does for the denominator, or emit a companion `process_measured` key).

## 11. CONFIRMED — LOW — The promised trajectory scrub is documented but not implemented

`job.yaml:3-5` and `bb_harbor/agent.py:14-19` both state that project id and the signed connect URL
must be stripped from the trajectory dir because Harbor's scrubber never reads `[environment].env`
(`harbor/trial/trial.py:738-748`) — but no code does it. Mitigating fact: because
`BROWSERBASE_API_KEY` is in `agent.extra_env`, the scrubber's substring replacement does redact the
key embedded in a connect URL; `BROWSERBASE_PROJECT_ID` (not key-regex-matching) survives verbatim.
Fix: either implement the strip as a trial hook, or downgrade the comments to state exactly what is
and is not scrubbed.

## 12. CONFIRMED — LOW — `has_browserbase_connection` is inferred from attribute presence

`bb_harbor/agent.py:186-201,216-217`. A callable `session_scope` attribute alone flips both
`--env browserbase` and the injection path on, without checking that a session exists. If `start()`
ever leaves `browserbase_session_id` unset, `stack.enter_context(session_scope())` raises the bare
`RuntimeError` from `session_env()` (`env.py:84-87`) rather than a `StagehandAgentFailedError`, so the
failure is unclassifiable by Harbor's retry filters. Fix: probe for a truthy
`environment.browserbase_session_id` and raise `StagehandAgentFailedError` when the environment
advertises Browserbase but has no session.

---

## Negative results (checked, found correct)

- **No scoring reimplementation.** `verify()` maps `outcomeSuccess → 1.0/0.0`, which is exactly
  `evaluationResultToSuccess(result, "outcome")` (`verifierAdapter.ts:435-453`); `processScore` is
  passed through un-thresholded; `compute_criteria_fraction` is pure aggregation of Stagehand's own
  `earnedPoints/maxPoints` and correctly excludes `earnedPoints: null` from **both** numerator and
  denominator (`verifier.py:126-141`), matching `CriterionScore` (`rubricVerifier.ts:392-411`,
  `1112-1121`).
- **Judge-health predicate is right.** All six literals in
  `_SYNTHESIZED_JUDGE_FAILURE_{REASONINGS,DESCRIPTIONS}` exist verbatim at `rubricVerifier.ts:803,
  810, 1270, 1277, 1388, 1395`. It reads `finding["description"]` (not `["message"]`), requires
  `category == "verifier_uncertainty"` **plus** an exact synthesized description, and covers both
  surfaces the reasoning can reach (`explanation` at `:852`, `rawSteps.reasoning` at `:681-683`).
  Presence of the category alone does not raise; `empty-trajectory` results do not raise. A
  fully-dead judge hits one of the three synthesized paths, so it does raise. Partial failure
  (per-criterion batch fails, outcome call succeeds) yields `earnedPoints: null` → excluded → a
  logged `denominator-was-zero`, which is the correct conservative behaviour.
- **Injection is genuinely ContextVar-scoped.** `scoped_exec_env` stacks overlay tuples in a
  per-instance `ContextVar` (`harbor/environments/base.py:435-457`); nested scopes layer rather than
  replace, so `session_scope()` + `scoped_exec_env(scoped_env)` in one `ExitStack`
  (`agent.py:222-226`) is correct. No `os.environ` mutation anywhere in `bb_harbor`.
- **`self.session_id` is never assigned.** The Browserbase id lives on
  `browserbase_session_id`/`browserbase_connect_url` only.
- **Release is shielded.** `stop()` cancels the keepalive, then double-shields the release across
  cancellation and re-raises (`env.py:138-174`); `start()` tears Docker down if the session step
  fails. The only uncovered leak is finding 4.
- **No fabricated APIs.** Verified: 9 `BaseEnvironment` abstract methods; `exec(command, cwd, env,
  timeout_sec, user) -> ExecResult`; `scoped_exec_env`; `preflight` classmethod; `DockerEnvironment.
  start(force_build)` / `stop(delete)`; `type() -> str` is deliberately `str` for third-party
  environments (`base.py:653-660`); `BaseAgent.{name,version,setup,run}`, `extra_env` property,
  `model_name`, `session_id`, `logger`; `BaseVerifier.__init__` kwargs and `VerifierResult(rewards=)`;
  `VerifierFactory.create_verifier_from_import_path` forwards `config.kwargs` (so `judge_model`
  arrives) and also injects `skip_tests_upload` in separate mode, which the `**kwargs` sink absorbs.
  Stagehand side: `evals verify <dir> --json --model <name>` exists with those exact flags and
  returns before the persist block under `--json` (`verify.ts:152-156`); `evals run` accepts
  `--trials/--concurrency/--agent-mode/--env/--model` (`parse.ts:104-119,159-164`);
  `agent/columbia_tuition` resolves via `registry.byName` and its `taskSpec.id` is
  `"agent/columbia_tuition"`, so the agent's `*/agent/columbia_tuition/*/trajectory.json` pattern
  matches the real `<root>/<group>/<taskId>/<runId>/` layout (`trajectoryGroup.ts:116-123`);
  `EVAL_TRAJECTORY_ROOT` is honoured while `EVAL_TRAJECTORY_GROUP` is overwritten by the runner
  (`runner.ts:365`) exactly as the agent's docstring claims; `VERIFIER_PERSIST_TRAJECTORIES=1` is
  truthy (`trajectory.ts:236-244`); `evals run` exits 0 on a legitimately failed eval, so a failed
  task is not misreported as an agent error.
- **No task.toml traps.** No `[[artifacts]]`, no `[verifier] import_path`; `environment/` +
  `instruction.md` + `tests/test.sh` present, `Task.is_valid_dir` passes,
  `[verifier].environment_mode = "shared"` is set (and job-level `VerifierConfig` correctly has no
  such field). `job.yaml` `${VAR}` templates are resolved by `AgentFactory` via `resolve_env_vars`
  (`harbor/agents/factory.py:147`), so the placeholders are not shipped literally.
