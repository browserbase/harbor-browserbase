# harbor-browserbase

Harbor integration for Browserbase + Stagehand evals. See ../.claude/plans/harbor-browserbase-integration.html

## Setup

Install this package into the Harbor virtual environment:

```sh
VIRTUAL_ENV=.venv uv pip install -e . --no-deps
```

This is required because the `harbor` console script runs from the virtual environment and does not have the repository working directory on `sys.path`.

Set `BROWSERBASE_API_KEY` and `BROWSERBASE_PROJECT_ID`. `BROWSERBASE_BASE_URL` is optional. Also set the appropriate model-provider key, such as `GEMINI_API_KEY` for the default judge model.

## Usage

Preflight makes one authenticated Browserbase API call to retrieve the configured project. Set `BB_SKIP_PREFLIGHT_API_CHECK=1` to skip that call for offline use after required-variable validation.

Custom environment, agent, and verifier components are wired through `job.yaml` (or `harbor run --env/--agent/--verifier`); they cannot be wired from `task.toml`.
