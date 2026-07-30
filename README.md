# Browserbase + Stagehand evals for Harbor

This repository runs Browserbase-backed Stagehand browser-agent evals as Harbor tasks. It
contains a custom Harbor environment, agent, and verifier in `bb_harbor/`, plus ten task
fixtures under `tasks/`.

## Architecture

The integration has three host-side Python components:

- `bb_harbor.env:BrowserbaseEnvironment` extends Harbor's Docker environment. It validates
  Browserbase configuration, performs the authenticated preflight check, and starts the task
  container. By default it leaves Browserbase session creation to Stagehand.
- `bb_harbor.agent:StagehandAgent` resolves the Stagehand task identity, validates the eval plan
  with `--preview`, runs one Stagehand eval in the selected agent mode, and publishes the flushed
  trajectory for verification.
- `bb_harbor.verifier:StagehandVerifier` translates Stagehand's persisted verifier result, or
  falls back to `evals verify --json`. The default judge is
  `google/gemini-3-flash-preview`, as configured in `job.yaml`. It returns `reward`, `outcome`,
  `process`, `process_measured`, and `criteria_earned_frac`.

These components are wired out of tree through their Python `import_path`s. Custom environment,
agent, and verifier import paths must be declared in the job-level `job.yaml`, or supplied with
`harbor run --env`, `--agent`, and `--verifier`. They cannot be declared in `task.toml`: Harbor's
task-level models silently drop those keys.

Harbor also does not expose task TOML metadata to a custom agent. Each fixture therefore carries
an explicit marker line in its `instruction.md`:

```text
stagehand-task-id: agent/<name>
```

The agent reads this marker and passes the selected task to `evals run`.

## Agent modalities

The agent's `mode` constructor kwarg selects the Stagehand agent modality. The valid values are
exactly `dom`, `hybrid`, and `cua`. `dom` is the default and is set explicitly by `job.yaml`.

To switch modality, change the agent kwargs in `job.yaml`:

```yaml
agents:
  - import_path: bb_harbor.agent:StagehandAgent
    kwargs:
      mode: hybrid
```

The selected value is forwarded to Stagehand as `--agent-mode`.

## Setup

Install this package into Harbor's virtual environment before using the custom import paths:

```bash
VIRTUAL_ENV=.venv uv pip install -e . --no-deps
```

This editable install is mandatory. Harbor's console script imports `bb_harbor` from
site-packages, not from the working tree, so the `import_path`s fail to resolve without it. The
virtual environment has no `pip`; use `uv pip`. Re-run the command after every edit to
`bb_harbor/`.

## Environment variables

Set variable values in the shell that invokes Harbor. Do not commit credentials.

| Variable | Requirement | Purpose |
| --- | --- | --- |
| `BROWSERBASE_API_KEY` | Required | Authenticates Browserbase API and Stagehand session use. |
| `BROWSERBASE_PROJECT_ID` | Required | Selects the Browserbase project. |
| `BROWSERBASE_BASE_URL` | Optional | Overrides the Browserbase API base URL. |
| `GEMINI_API_KEY` | Required by the default configuration | Authenticates the default Stagehand judge. |
| `GOOGLE_GENERATIVE_AI_API_KEY` | Required by the default agent model | Authenticates the AI SDK Google provider used by the agent model. |

Environment preflight validates the two required Browserbase variables and makes one
authenticated API call to retrieve the configured project. Set `BB_SKIP_PREFLIGHT_API_CHECK=1`
to skip only that API call; required-variable validation still runs.

## Running the smoke suite

Run the local gates before spending Browserbase or model-provider resources:

```bash
# Refresh the editable install.
VIRTUAL_ENV=.venv uv pip install -e . --no-deps

# Run unit tests.
.venv/bin/python -m pytest -q

# Confirm all ten Harbor fixtures load and share one environment definition.
.venv/bin/python scripts/check_task_fixtures.py
```

Then run a free in-container Stagehand plan preview against the task image. Replace
`<task-image>` with the locally built image tag used by the fixtures:

```bash
docker run --rm <task-image> sh -c \
  'cd /opt/stagehand && evals run agent/iframe_form --trials 1 --concurrency 1 \
  --agent-mode dom --env browserbase --model google/gemini-3-flash-preview --preview'
```

The preview must resolve the requested target and report at least one task before a live run.
The agent repeats this check inside each trial.

Run all ten fixtures with:

```bash
.venv/bin/harbor run -c job.yaml -o jobs --job-name <name> --yes
```

`job.yaml` defaults to three concurrent trials. Each trial holds a real Browserbase session, so
keep concurrency within the Browserbase plan's session limit. Harbor silently skips incomplete
task directories; confirm that run output reports ten discovered tasks.

See `SMOKE-SUITE.md` for the long-form procedure and `SMOKE-RESULTS.md` for the authoritative
record of the latest verified run. That run completed real work in 10/10 trials and 6/10 scored
`reward > 0`. The four zero-reward trials were genuine agent failures — hallucinated answers, a
dropped task constraint, a consent wall — not integration defects.

The gitignored `jobs/` directory contains run artifacts and real session identifiers. Treat it as
sensitive operational output.

## Known limitations

### Temporary `benchHarness` patch

Every fixture applies `environment/patches/benchharness-config-overrides.patch` inside its task
image so Stagehand's `benchHarness` honors config overrides. This in-tree patch is a stopgap
pending the upstream Stagehand branch `fix/benchharness-config-overrides`. Remove the fixture
patch once that fix lands upstream and is present in the task image.

### Browserbase session ownership

Canonical Stagehand owns the Browserbase session, so `BrowserbaseEnvironment.create_session`
defaults to `false` and `job.yaml` preserves that setting. Setting it to `true` creates an inert
second session and causes double billing unless Stagehand is patched to forward
`browserbaseSessionID` to `initV3`.

### `BROWSERBASE_PROJECT_ID` scrubbing gap

Harbor's secret scrubber collects only values whose key name matches its sensitive-key pattern:
`KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `CREDENTIAL`, or `AUTH`. `BROWSERBASE_API_KEY` matches and
is redacted, including when embedded in a signed connect URL. `BROWSERBASE_PROJECT_ID` does not
match and can survive verbatim in `jobs/` output. Operators must strip the project ID before
sharing run artifacts.

## Further reference

`INTERFACES.md` records the Harbor interfaces and implementation constraints used by these custom
components.
