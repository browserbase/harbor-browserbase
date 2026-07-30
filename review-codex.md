# Codex self-review — triaged

**Reviewed:** commit `cca169b` ("feat: task fixture, Dockerfile, job config, unit suite"), 12 files /
+2066 lines. Working tree was clean, so `codex exec review` had no diff to look at; the commit diff
was dumped to `/tmp/harbor-last-commit.diff` and Codex was given a review-only spec pointing at that
file plus the real repo state, `INTERFACES.md`, and the installed Harbor source.

**Codex raised 2 findings** (both `resource-leak`), and explicitly reported no correctness,
api-mismatch, concurrency, or test-correctness findings. Raw output: `/tmp/codex-review.md`.

**Supervisor gate:** working tree still clean after the review run (Codex modified nothing).
`.venv/bin/python -m pytest -q` → 24 passed.

---

## REAL

### 1. `AsyncBrowserbase` client is never closed — `bb_harbor/env.py:48`

`_client()` lazily constructs and caches an `AsyncBrowserbase` per environment instance. Neither
`stop()` (lines 138-174) nor the startup-failure path in `start()` (lines 74-81) closes it, and the
attribute is never cleared.

Confirmed: the SDK owns an `httpx.AsyncClient` and exposes `async close()`
(`.venv/.../browserbase/_base_client.py`). Nothing in `bb_harbor` calls it.

Severity: **low**. One leaked client + connection pool per trial, reclaimed only by GC. Matters for a
long-lived Harbor process running many trials (fd/socket accumulation, `ResourceWarning` noise), not
for a single-trial run. Correct fix is `await self._browserbase_client.close()` in `stop()`'s
`finally` and in the `start()` failure handler.

---

## NOISE

### 2. "Release failure is swallowed so Harbor will not retry" — `bb_harbor/env.py:165`

Codex is right that `except Exception: log` at 165-168 swallows a failed
`sessions.update(..., status="REQUEST_RELEASE")`, but its stated consequence is wrong, and the
consequence was the whole argument.

Codex claimed the swallow makes Harbor set `_is_agent_environment_stopped = True` and skip a retry.
Harbor sets that flag in **every** branch of `_stop_agent_environment`
(`.venv/.../harbor/trial/trial.py:1191-1210`) — the success path, the `except asyncio.CancelledError`
path, *and* the `except Exception` path. There is no retry to lose. Propagating the error instead of
logging it would change nothing except adding a Harbor-side debug line, and would risk masking the
Docker teardown that the current `finally: await super().stop(delete)` guarantees.

Best-effort release with an `.exception()` log is the right shape here. No change warranted.

---

## Independent supervisor check (Codex's "nothing found" claims)

Codex's review was thin, so its negative claims were spot-checked against the installed Harbor
0.20.0 source rather than taken on trust. No fabricated API found:

- `BaseVerifier` really does expose `environment`, `override_env`, `verifier_env`, `logger`
  (`harbor/verifier/base.py:33-36`) — `StagehandVerifier._resolve_setting` reads all four correctly.
  (`self.logger or _LOGGER` is dead-ish defensive code; `logger` is never `None`. Style, not a bug.)
- `VerifierResult(rewards=dict[str, float | int] | None)` matches what `verify()` returns
  (`harbor/models/verifier/result.py`).
- `exec(command, cwd, env, timeout_sec, user)` matches every call site in `agent.py` / `verifier.py`
  (`harbor/environments/base.py:1128`, `docker/docker.py:1088`).
- `BaseAgent` really exposes `model_name`, `session_id`, `extra_env` (property returning a copy),
  `logger` (`harbor/agents/base.py:31-73`).
- `DockerEnvironment.start(force_build)` / `stop(delete)` signatures match the overrides.
- **Nested `scoped_exec_env` was the one live concurrency suspicion** — `agent.py:222-226` enters
  `session_scope()` (which itself calls `scoped_exec_env`) and then a second `scoped_exec_env`,
  which would drop `BROWSERBASE_SESSION_ID` if the inner scope replaced rather than layered. Checked
  `harbor/environments/base.py:435-457`: overlays are pushed onto a tuple in a per-instance
  `ContextVar` and a nested scope takes precedence without discarding outer entries. Correct as
  written.

---

## Verdict

1 real low-severity finding (unclosed HTTP client), 1 finding whose premise does not survive reading
Harbor's cleanup code. Nothing blocking. Fold the client-close fix into the next Codex spec.
