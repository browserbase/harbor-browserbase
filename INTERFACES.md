# Harbor 0.20.0 interface contract

This contract is transcribed from the Harbor source installed at `.venv/lib/python3.14/site-packages/harbor`. Citations below are relative to `site-packages`.

## `BaseEnvironment`

Runtime inspection reports these exact `BaseEnvironment.__abstractmethods__` names: `_validate_definition`, `download_dir`, `download_file`, `exec`, `start`, `stop`, `type`, `upload_dir`, and `upload_file`.

`harbor/environments/base.py:652`

```python
    @staticmethod
    @abstractmethod
    def type() -> str:
```

`type` is both `@staticmethod` and `@abstractmethod`.

`harbor/environments/base.py:728`

```python
    @abstractmethod
    def _validate_definition(self):
```

`harbor/environments/base.py:900`

```python
    @abstractmethod
    async def start(self, force_build: bool) -> None:
```

`harbor/environments/base.py:904`

```python
    @abstractmethod
    async def stop(self, delete: bool):
```

`harbor/environments/base.py:916`

```python
    @abstractmethod
    async def upload_file(self, source_path: Path | str, target_path: str):
```

`harbor/environments/base.py:926`

```python
    @abstractmethod
    async def upload_dir(self, source_dir: Path | str, target_dir: str):
```

`harbor/environments/base.py:936`

```python
    @abstractmethod
    async def download_file(self, source_path: str, target_path: Path | str):
```

`harbor/environments/base.py:946`

```python
    @abstractmethod
    async def download_dir(self, source_dir: str, target_dir: Path | str):
```

`harbor/environments/base.py:1127`

```python
    @abstractmethod
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
```

The `ExecResult` returned by `exec` has exactly these fields and types.

`harbor/environments/base.py:78`

```python
class ExecResult(BaseModel):
    stdout: str | None = None
    stderr: str | None = None
    return_code: int
```

`scoped_exec_env` is a synchronous `@contextlib.contextmanager` with this signature.

`harbor/environments/base.py:434`

```python
    @contextlib.contextmanager
    def scoped_exec_env(self, env: dict[str, str]) -> Generator[None, None, None]:
```

Its state is stored on each environment instance in a `contextvars.ContextVar` containing a tuple of environment overlays; entry appends a copied mapping and exit resets with the token, so nested scopes stack and concurrent asyncio tasks remain isolated (`harbor/environments/base.py:203-208`, `harbor/environments/base.py:438-457`).

The merge implementation is:

`harbor/environments/base.py:416`

```python
    def _merge_env(self, env: dict[str, str] | None) -> dict[str, str] | None:
        """Merge persistent, per-exec, and scoped env vars.

        Precedence is persistent env < per-exec env < scoped env. This preserves
        installed-agent behavior where ``AgentConfig.env`` can override command
        defaults such as ``IS_SANDBOX`` while keeping the scope off verifier and
        artifact commands.
        """
        overlays = self._exec_env_overlays.get()
        if not self._persistent_env and not env and not overlays:
            return None
        merged = {**self._persistent_env}
        if env:
            merged.update(env)
        for scoped_env in overlays:
            merged.update(scoped_env)
        return merged or None
```

Precedence rule: persistent environment < per-exec environment < scoped overlays, with later nested scoped overlays winning.

The public, non-underscore attributes assigned directly on `self` by `BaseEnvironment.__init__` are `environment_dir`, `environment_name`, `session_id`, `trial_paths`, `default_user`, `extra_docker_compose_paths`, `task_env_config`, and `logger` (`harbor/environments/base.py:178-210`). `context_id` is declared as a class attribute, then assigned by orchestration after construction rather than by `BaseEnvironment.__init__` (`harbor/environments/base.py:108`, `harbor/trial/trial.py:837`).

Harbor orchestration owns the environment `session_id`: `BaseEnvironment` accepts and stores it, while `Trial` supplies `{trial_name}__env` for the agent environment (`harbor/environments/base.py:116-138`, `harbor/environments/base.py:178-181`, `harbor/trial/trial.py:825-836`). A separate verifier environment uses `{trial_name}__verifier__{key}`, replaces characters outside alphanumeric/`-._` with `_`, and truncates beyond 63 characters with an eight-hex SHA-1 suffix (`harbor/trial/trial.py:674-683`). Trial names default to `{task_name_up_to_32_chars}__{7-character_shortuuid}` (`harbor/models/trial/config.py:453-462`).

## `BaseAgent`

Runtime inspection reports these exact `BaseAgent.__abstractmethods__` names: `name`, `run`, `setup`, and `version`.

`harbor/agents/base.py:104`

```python
    @staticmethod
    @abstractmethod
    def name() -> str:
```

`name` is `@staticmethod`; it is not a classmethod or property.

`harbor/agents/base.py:109`

```python
    @abstractmethod
    def version(self) -> str | None:
```

`version` is an instance method; it is not static, class, or property decorated.

`harbor/agents/base.py:120`

```python
    @abstractmethod
    async def setup(self, environment: BaseEnvironment) -> None:
```

`setup` is an async instance method.

`harbor/agents/base.py:136`

```python
    @abstractmethod
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
```

`run` is an async instance method taking an `instruction: str`, an `environment: BaseEnvironment`, and a mutable `context: AgentContext`; none of the abstract methods is a classmethod or property.

The agent Python method executes host-side in Harbor's process: `Trial` directly awaits `self.agent.run(...)` (or `resume`) and passes the `BaseEnvironment` object (`harbor/trial/trial.py:449-457`). Container-side work is reached through that object—for example, `BaseEnvironment.exec` is explicitly documented as executing a command “in the environment” (`harbor/environments/base.py:1127-1147`). Thus “agent runs inside an environment” describes where its commanded workload runs, not where the Python `run` coroutine itself is hosted.

## `BaseVerifier`

The complete constructor signature is:

`harbor/verifier/base.py:17`

```python
    def __init__(
        self,
        *,
        task: Task,
        trial_paths: TrialPaths,
        environment: BaseEnvironment,
        override_env: dict[str, str] | None = None,
        logger: logging.Logger | None = None,
        verifier_env: dict[str, str] | None = None,
        step_name: str | None = None,
        include_logs: list[str] | None = None,
        exclude_logs: list[str] | None = None,
        **_: Any,
    ) -> None:
```

Runtime inspection reports `verify` as the sole member of `BaseVerifier.__abstractmethods__`.

`harbor/verifier/base.py:41`

```python
    @abstractmethod
    async def verify(self) -> VerifierResult:
```

The returned model definition is:

`harbor/models/verifier/result.py:4`

```python
class VerifierResult(BaseModel):
    rewards: dict[str, float | int] | None = None
```

The exact public attributes assigned for subclasses are `task`, `trial_paths`, `environment`, `override_env`, `logger`, `verifier_env`, `step_name`, `include_logs`, and `exclude_logs` (`harbor/verifier/base.py:31-39`).

Custom verifier resolution uses the run-level `harbor.models.trial.config.VerifierConfig`, not the task-level model. If `config.import_path` is non-`None`, the factory imports a class constrained to subclass `BaseVerifier`, forwards the standard constructor arguments, the optional log filters, `config.kwargs`, and caller kwargs; otherwise it constructs Harbor's built-in `Verifier`, and rejects verifier kwargs without an import path (`harbor/verifier/factory.py:14-43`, `harbor/verifier/factory.py:45-99`). The run-level keys are `verifier.import_path`, `verifier.kwargs`, `verifier.include_logs`, and `verifier.exclude_logs` (`harbor/models/trial/config.py:319-342`). The corresponding CLI flags are `--verifier`, deprecated hidden `--verifier-import-path`, repeated `--verifier-kwarg`, `--verifier-include-logs`, and `--verifier-exclude-logs` (`harbor/cli/trials.py:451-499`); the CLI writes them into `config.verifier` (`harbor/cli/trials.py:638-652`).

## Task config (`models/task/config.py`)

The config-aware task-directory validation first requires `task.toml` and `environment/`, then parses the TOML.

`harbor/models/task/task.py:101`

```python
        paths = TaskPaths(task_dir)
        if not paths.config_path.exists() or not paths.environment_dir.exists():
            return False

        try:
            config = TaskConfig.model_validate_toml(paths.config_path.read_text())
        except (OSError, tomllib.TOMLDecodeError, ValidationError):
            return False
```

For a single-step task, `instruction.md` is required. When verification is enabled and no separate verifier environment resolves, an OS-compatible `tests/test.sh` or `tests/test.bat` is also required; a separate verifier environment bypasses that host test-script requirement.

`harbor/models/task/task.py:125`

```python
    @staticmethod
    def _validate_tests(config: TaskConfig, paths: TaskPaths) -> None:
        """Raise FileNotFoundError if host artifacts required at runtime are missing."""
        if not config.steps:
            if not paths.instruction_path.exists():
                raise FileNotFoundError(
                    f"Task directory {paths.task_dir} is missing instruction.md."
                )
            verifier_env = resolve_effective_verifier_env_config(config, step_cfg=None)
            if verifier_env is not None:
                return
            verifier_os = config.environment.os
            if paths.discovered_test_path_for(verifier_os) is None:
                expected = paths.test_path_for(verifier_os).relative_to(paths.task_dir)
                raise FileNotFoundError(
                    f"Task directory {paths.task_dir} declares [environment].os = "
                    f"{verifier_os.value!r} but does not contain "
                    f"{expected.as_posix()}."
                )
            return
```

For multi-step tasks, each configured step requires `steps/{name}/` and `steps/{name}/instruction.md`; absent a separate verifier environment, each step also needs an OS-compatible step test or shared root test (`harbor/models/task/task.py:146-173`). With verification disabled, single-step validation only requires root `instruction.md`, while multi-step validation requires each step directory and instruction (`harbor/models/task/task.py:110-117`).

`ArtifactConfig` has exactly four fields:

`harbor/models/task/config.py:634`

```python
class ArtifactConfig(BaseModel):
    source: str
    destination: str | None = None
    exclude: list[str] = Field(
        default_factory=list,
        description="Patterns to exclude when downloading a directory artifact "
        "(passed as tar --exclude flags).",
    )
    service: str | None = Field(
        default=None,
        description="Docker Compose service to collect this artifact from. "
        "None or 'main' targets the agent's container. Any other value "
        "requires a compose-capable environment provider and an absolute "
        "source path.",
    )
```

There is no `path` field.

The task-level `VerifierConfig` inherits these two fields:

`harbor/models/task/config.py:218`

```python
class PhaseNetworkPolicyConfig(AllowedHostsValidationMixin, BaseModel):
    """Network policy fields for [agent] and [verifier] phase overrides."""

    network_mode: NetworkMode | None = Field(
        default=None,
        description="Network access policy. [agent] and [verifier] use this only "
        "as an explicit phase override when set.",
    )
    allowed_hosts: list[str] | None = Field(
        default=None,
        description="Hostnames, IP address literals/CIDR ranges, or leading "
        "wildcard patterns reachable when network_mode='allowlist'.",
    )
```

Its directly declared fields are:

`harbor/models/task/config.py:556`

```python
class VerifierConfig(PhaseNetworkPolicyConfig):
    timeout_sec: float = 600.0
    env: dict[str, str] = Field(default_factory=dict)
    user: str | int | None = Field(
        default=None,
        description="Username or UID to run the verifier as. None uses the environment's default USER (e.g., root).",
    )
    environment_mode: VerifierEnvironmentMode | None = Field(
        default=None,
        description=(
            "Whether the verifier runs in the agent's environment ('shared') "
            "or in a dedicated container ('separate'). When omitted: defaults "
            "to 'separate' if a verifier 'environment' is set, otherwise "
            "'shared'."
        ),
    )
    environment: EnvironmentConfig | None = Field(
        default=None,
        description=(
            "Environment definition for the separate verifier container. "
            "Same schema as the top-level [environment] section. When set "
            "without an explicit environment_mode, implies "
            "environment_mode='separate'. When unset with "
            "environment_mode='separate', a fresh copy of the top-level "
            "[environment] is used. Conflicts with "
            "environment_mode='shared'."
        ),
    )
    collect: list["VerifierCollectConfig"] = Field(
        default_factory=list,
        description=(
            "Commands run in compose services after the agent phase ends and "
            "before artifact collection ([[verifier.collect]] blocks in "
            "task.toml). Use these to snapshot runtime state into files that "
            "artifact entries can then collect."
        ),
    )
```

The complete effective field list is therefore `network_mode`, `allowed_hosts`, `timeout_sec`, `env`, `user`, `environment_mode`, `environment`, and `collect`. `import_path` is not among them. Because this Pydantic model does not forbid extras, an `import_path` key placed under `[verifier]` in `task.toml` is silently ignored rather than selecting a custom verifier; custom verifier selection must come from the run-level config described above.

Task directories use TOML only: the filename constant is `task.toml` (`harbor/models/task/paths.py:30`, `harbor/models/task/paths.py:56-59`), and parsing calls `tomllib.loads`.

`harbor/models/task/config.py:898`

```python
    @classmethod
    def model_validate_toml(cls, toml_data: str) -> "TaskConfig":
        toml_dict = tomllib.loads(toml_data)
        return cls.model_validate(toml_dict)
```

There is no task-config YAML loader in this installed model.

## Credential scrubbing (`trial/trial.py`)

The scrubber collects candidate secret values from exactly three sources: the instantiated agent's `extra_env` (resolved from run-level agent env), task-level verifier env, and run-level verifier env. It resolves templates, keeps nonempty values only when the key is classified sensitive, and skips unresolved templates.

`harbor/trial/trial.py:738`

```python
    def _scrub_jobs_dir(self) -> None:
        secrets: set[str] = set()
        for env in (
            self.agent.extra_env,
            self.task.config.verifier.env,
            self.config.verifier.env,
        ):
            for key, value in env.items():
                if is_sensitive_env_key(key):
                    try:
                        value = resolve_env_vars({key: value})[key]
                        if value:
                            secrets.add(value)
                    except ValueError:
                        continue
```

The sensitive key-name pattern is:

`harbor/utils/env.py:5`

```python
_SENSITIVE_KEY_RE = re.compile(
    r"(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH)", re.IGNORECASE
)
```

The scrub pass is called during finalization (`harbor/trial/trial.py:365-372`) and replaces literal matches with `[REDACTED]` in non-symlink, UTF-8-looking, non-binary files under the trial directory (`harbor/trial/trial.py:756-778`). It does **not** collect values from task-level `[environment].env`, run-level `environment.env`, `solution.env`, arbitrary host environment variables that are not referenced by one of the three collected mappings, verifier/agent kwargs, or any key whose name does not match the pattern.

## Gotchas

- Implementing only `exec` is insufficient: a concrete environment must implement all nine members in `BaseEnvironment.__abstractmethods__`.
- Treating the agent's Python coroutine as container-resident breaks the execution model; it is host-side code that receives a container environment handle.
- Putting `import_path` in task-level `[verifier]` does not load a custom verifier because that key is silently dropped there.
- Using `path` in an artifact table fails to identify an artifact because the model uses `source` and optional `destination` instead.
- Passing secrets through environment-level env mappings does not make them eligible for Harbor's trial-output scrubber.
- Assuming per-exec env overrides agent-scoped env reverses the real merge order; scoped env wins.
