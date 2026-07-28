"""Translate the existing TypeScript Stagehand verifier result for Harbor.

This class invokes the Stagehand TypeScript verifier and translates its output; it
never scores a trajectory.  Every successful call returns exactly ``reward``,
``outcome``, ``process``, and ``criteria_earned_frac`` so Harbor reward constraints
never observe a missing key.  ``CriteriaFraction`` exposes aggregation metadata,
allowing callers to distinguish a zero denominator from criteria that all earned
zero; the same distinction is logged at INFO.

Known synthesized judge-failure results are raised as unhealthy instead of being
misreported as genuine zero rewards, which would poison an RL signal.  Process-mode
parity, if needed, must be implemented on the TypeScript side.  Canonical Stagehand
has no ``STAGEHAND_EVALUATOR_MODEL`` override, so ``evals verify --model`` is the
working judge-model override.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import posixpath
import shlex
from typing import Any, Mapping, TypeGuard

from harbor.models.verifier.result import VerifierResult
from harbor.verifier.base import BaseVerifier

from bb_harbor import TRAJECTORIES_ROOT, TRAJECTORY_POINTER_PATH


DEFAULT_JUDGE_MODEL = "google/gemini-3-flash-preview"
# Never fall back to gemini-2.5-flash: it was retired and now returns 404s.
DEFAULT_EVALS_BIN = "evals"
DEFAULT_TRAJECTORIES_ROOT = TRAJECTORIES_ROOT
DEFAULT_TIMEOUT_SEC = 900
_OUTPUT_TAIL_LIMIT = 4_000

_LOGGER = logging.getLogger(__name__)

# Stagehand rubricVerifier.ts ~1270, ~1388, and ~803, respectively.  These
# strings become EvaluationResult.explanation and/or rawSteps.reasoning at
# rubricVerifier.ts ~852 and ~683/~858.
_SYNTHESIZED_JUDGE_FAILURE_REASONINGS = frozenset(
    {
        "Fused judgment LLM call failed; returning evidence-insufficient result.",
        "Outcome LLM call failed; defaulting to output_success=false.",
        "Outcome-only LLM call failed; defaulting to output_success=false.",
    }
)

# Stagehand rubricVerifier.ts ~1277, ~1395, and ~810, respectively.  These are
# synthesized VerifierFinding.description values, not LLM-authored diagnostics.
_SYNTHESIZED_JUDGE_FAILURE_DESCRIPTIONS = frozenset(
    {
        "The fused judgment call did not return a parseable response.",
        "The outcome verification call did not return a parseable response.",
        "The outcome-only verification call did not return a parseable response.",
    }
)


class StagehandVerifierError(RuntimeError):
    """Base error for Stagehand verifier translation failures."""


class StagehandVerifierExecError(StagehandVerifierError):
    """Raised when the container-side Stagehand CLI exits unsuccessfully."""


class StagehandVerifierOutputError(StagehandVerifierError):
    """Raised when Stagehand emits missing, malformed, or unparseable output."""


class StagehandVerifierUnhealthyError(StagehandVerifierError):
    """Raised when Stagehand reports one of its synthesized dead-judge results."""

    def __init__(
        self,
        *,
        judge_model: str,
        trajectory_dir: str,
        matched_signature: str,
    ) -> None:
        self.judge_model = judge_model
        self.trajectory_dir = trajectory_dir
        self.matched_signature = matched_signature
        super().__init__(
            "Stagehand synthesized a judge-failure result matching "
            f"{matched_signature!r} for judge model {judge_model!r} and trajectory "
            f"{trajectory_dir!r}. A retired or 404-ing judge model is the most "
            "likely cause; select a healthy model and retry verification."
        )


@dataclass(frozen=True, slots=True)
class CriteriaFraction:
    """Pure aggregation details for scores already produced by Stagehand.

    ``total_max == 0.0`` means no usable criterion was present, while a positive
    ``total_max`` with ``total_earned == 0.0`` means applicable criteria all scored
    zero.  No criterion is judged or rescored here.
    """

    fraction: float
    kept_count: int
    total_count: int
    total_earned: float
    total_max: float


def compute_criteria_fraction(per_criterion: object) -> CriteriaFraction:
    """Aggregate Stagehand ``earnedPoints / maxPoints`` for applicable criteria."""

    if per_criterion is None:
        criteria: list[object] = []
    elif isinstance(per_criterion, list):
        criteria = per_criterion
    else:
        # EvaluationResult.perCriterion is an array in verifier/types.ts ~357.
        raise StagehandVerifierOutputError(
            "Stagehand result field 'perCriterion' must be an array when present."
        )

    earned_values: list[float] = []
    max_values: list[float] = []
    for criterion in criteria:
        if not isinstance(criterion, dict):
            continue
        # CriterionScore uses earnedPoints and maxPoints (types.ts ~223-232).
        earned = criterion.get("earnedPoints")
        maximum = criterion.get("maxPoints")
        if not _is_number(earned) or not _is_number(maximum) or maximum <= 0:
            continue
        earned_values.append(float(earned))
        max_values.append(float(maximum))

    total_earned = sum(earned_values)
    total_max = sum(max_values)
    fraction = total_earned / total_max if total_max != 0.0 else 0.0
    return CriteriaFraction(
        fraction=fraction,
        kept_count=len(earned_values),
        total_count=len(criteria),
        total_earned=total_earned,
        total_max=total_max,
    )


class StagehandVerifier(BaseVerifier):
    """Run Stagehand verification in the Harbor environment and translate JSON."""

    def __init__(
        self,
        *,
        judge_model: str | None = None,
        trajectory_dir: str | None = None,
        trajectories_root: str | None = None,
        trajectory_pointer_path: str | None = TRAJECTORY_POINTER_PATH,
        evals_bin: str = DEFAULT_EVALS_BIN,
        cwd: str | None = None,
        timeout_sec: int | str | None = DEFAULT_TIMEOUT_SEC,
        user: str | int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.judge_model = self._resolve_setting(
            judge_model,
            "BB_JUDGE_MODEL",
            DEFAULT_JUDGE_MODEL,
        )
        # An explicit container path is intentionally preserved byte-for-byte.
        self.trajectory_dir = trajectory_dir
        self.trajectories_root = self._resolve_setting(
            trajectories_root,
            "BB_TRAJECTORIES_ROOT",
            DEFAULT_TRAJECTORIES_ROOT,
        )
        self.trajectory_pointer_path = trajectory_pointer_path
        self.evals_bin = _nonempty_stripped(evals_bin) or DEFAULT_EVALS_BIN
        self.cwd = cwd
        self.timeout_sec = _coerce_timeout(timeout_sec)
        self.user = user

    async def verify(self) -> VerifierResult:
        trajectory_dir = await self._resolve_trajectory_dir()
        command = (
            f"{shlex.quote(self.evals_bin)} verify {shlex.quote(trajectory_dir)} "
            f"--json --model {shlex.quote(self.judge_model)}"
        )
        logger = self.logger or _LOGGER
        logger.info("Running Stagehand verifier command: %s", command)

        exec_result = await self.environment.exec(
            command,
            cwd=self.cwd,
            timeout_sec=self.timeout_sec,
            user=self.user,
        )
        if exec_result.return_code != 0:
            raise StagehandVerifierExecError(
                _exec_failure_message("Stagehand verifier command", exec_result)
            )

        result = _parse_result(exec_result.stdout, exec_result.stderr)
        matched_signature = _judge_failure_signature(result)
        if matched_signature is not None:
            raise StagehandVerifierUnhealthyError(
                judge_model=self.judge_model,
                trajectory_dir=trajectory_dir,
                matched_signature=matched_signature,
            )

        # EvaluationResult fields are camelCase in verifier/types.ts ~344-365.
        outcome_success = result.get("outcomeSuccess")
        if not isinstance(outcome_success, bool):
            raise StagehandVerifierOutputError(
                "Stagehand result requires boolean field 'outcomeSuccess'."
            )
        outcome_reward = 1.0 if outcome_success else 0.0

        process_score = result.get("processScore")
        process_reward = float(process_score) if _is_number(process_score) else 0.0

        criteria = compute_criteria_fraction(result.get("perCriterion"))
        if criteria.total_max == 0.0:
            criteria_case = "denominator-was-zero"
        elif criteria.total_earned == 0.0:
            criteria_case = "all-kept-criteria-scored-zero"
        else:
            criteria_case = "positive-earned-total"
        logger.info(
            "Stagehand criteria aggregation: case=%s kept=%d total=%d earned=%s max=%s",
            criteria_case,
            criteria.kept_count,
            criteria.total_count,
            criteria.total_earned,
            criteria.total_max,
        )

        return VerifierResult(
            rewards={
                "reward": outcome_reward,
                "outcome": outcome_reward,
                "process": process_reward,
                "criteria_earned_frac": criteria.fraction,
            }
        )

    def _resolve_setting(
        self,
        explicit: str | None,
        environment_name: str,
        default: str,
    ) -> str:
        explicit_value = _nonempty_stripped(explicit)
        if explicit_value is not None:
            return explicit_value
        for mapping in (self.verifier_env, self.override_env, os.environ):
            value = _mapping_value(mapping, environment_name)
            if value is not None:
                return value
        return default

    async def _resolve_trajectory_dir(self) -> str:
        if self.trajectory_dir is not None:
            return self.trajectory_dir

        for mapping in (self.verifier_env, self.override_env, os.environ):
            configured = _mapping_value(mapping, "BB_TRAJECTORY_DIR", verbatim=True)
            if configured is not None:
                return configured

        if self.trajectory_pointer_path:
            pointer_result = await self.environment.exec(
                f"cat {shlex.quote(self.trajectory_pointer_path)}",
                cwd=self.cwd,
                timeout_sec=self.timeout_sec,
                user=self.user,
            )
            if pointer_result.return_code == 0:
                lines = (pointer_result.stdout or "").splitlines()
                if lines and lines[0].strip():
                    return lines[0].rstrip("\r")

        return await self._discover_trajectory_dir(self.trajectories_root)

    async def _discover_trajectory_dir(self, root: str) -> str:
        find_command = (
            f"find {shlex.quote(root)} -maxdepth 8 -type f "
            "-name trajectory.json -print"
        )
        find_result = await self.environment.exec(
            find_command,
            cwd=self.cwd,
            timeout_sec=self.timeout_sec,
            user=self.user,
        )
        if find_result.return_code != 0:
            raise StagehandVerifierExecError(
                _exec_failure_message("Trajectory discovery command", find_result)
            )

        trajectory_dirs = sorted(
            {
                posixpath.dirname(line)
                for line in (find_result.stdout or "").splitlines()
                if line
            }
        )
        if not trajectory_dirs:
            raise StagehandVerifierOutputError(
                f"No trajectory.json found under container path {root!r}."
            )
        if len(trajectory_dirs) == 1:
            return trajectory_dirs[0]

        (self.logger or _LOGGER).warning(
            "Multiple Stagehand trajectory directories found under %s: %s; "
            "selecting the newest by container-side directory mtime",
            root,
            trajectory_dirs,
        )
        quoted_dirs = " ".join(shlex.quote(path) for path in trajectory_dirs)
        stat_command = f"stat -c '%Y\\t%n' -- {quoted_dirs}"
        stat_result = await self.environment.exec(
            stat_command,
            cwd=self.cwd,
            timeout_sec=self.timeout_sec,
            user=self.user,
        )
        if stat_result.return_code != 0:
            raise StagehandVerifierExecError(
                _exec_failure_message("Trajectory mtime command", stat_result)
            )

        mtimes: list[tuple[float, str]] = []
        for line in (stat_result.stdout or "").splitlines():
            timestamp, separator, path = line.partition("\t")
            if not separator or path not in trajectory_dirs:
                continue
            try:
                mtimes.append((float(timestamp), path))
            except ValueError:
                continue
        if len(mtimes) != len(trajectory_dirs):
            raise StagehandVerifierOutputError(
                "Could not parse container-side mtimes for all discovered trajectory "
                f"directories. stdout tail: {_tail(stat_result.stdout)!r}; "
                f"stderr tail: {_tail(stat_result.stderr)!r}"
            )
        return max(mtimes, key=lambda item: (item[0], item[1]))[1]


def _judge_failure_signature(result: dict[str, object]) -> str | None:
    normalized_reasonings = {
        _normalize(signature): signature
        for signature in _SYNTHESIZED_JUDGE_FAILURE_REASONINGS
    }
    normalized_descriptions = {
        _normalize(signature): signature
        for signature in _SYNTHESIZED_JUDGE_FAILURE_DESCRIPTIONS
    }

    # EvaluationResult.explanation and rawSteps.reasoning are defined in
    # verifier/types.ts ~348 and ~306; the fallback values originate at the
    # rubricVerifier.ts lines cited above.
    explanation = result.get("explanation")
    if isinstance(explanation, str):
        matched = normalized_reasonings.get(_normalize(explanation))
        if matched is not None:
            return matched
    raw_steps = result.get("rawSteps")
    if isinstance(raw_steps, dict):
        reasoning = raw_steps.get("reasoning")
        if isinstance(reasoning, str):
            matched = normalized_reasonings.get(_normalize(reasoning))
            if matched is not None:
                return matched

    # VerifierFinding.category and .description are defined in types.ts
    # ~273-298.  Category alone is intentionally insufficient.
    findings = result.get("findings")
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            if finding.get("category") != "verifier_uncertainty":
                continue
            description = finding.get("description")
            if isinstance(description, str):
                matched = normalized_descriptions.get(_normalize(description))
                if matched is not None:
                    return matched

    # Explicitly healthy/ordinary: empty-trajectory results (reason
    # "empty-trajectory" and explanation "No trajectory steps or final answer were
    # captured; skipped verifier LLM calls." at rubricVerifier.ts ~701/~714),
    # trajectory_capture blocking findings from legacyInsufficientEvidenceResult,
    # other LLM-authored verifier_uncertainty descriptions, and
    # outcomeSuccess=false by itself.  None of these broad conditions is used as a
    # health signature above.
    return None


def _parse_result(stdout: str | None, stderr: str | None) -> dict[str, object]:
    if stdout is None or not stdout.strip():
        raise StagehandVerifierOutputError(
            "Stagehand verifier emitted blank stdout. "
            f"stderr tail: {_tail(stderr)!r}"
        )

    stripped = stdout.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as first_error:
        first_brace = stripped.find("{")
        last_brace = stripped.rfind("}")
        if first_brace == -1 or last_brace < first_brace:
            raise StagehandVerifierOutputError(
                "Stagehand verifier stdout was not JSON. "
                f"stdout tail: {_tail(stdout)!r}; stderr tail: {_tail(stderr)!r}"
            ) from first_error
        try:
            parsed = json.loads(stripped[first_brace : last_brace + 1])
        except json.JSONDecodeError as fallback_error:
            raise StagehandVerifierOutputError(
                "Stagehand verifier stdout was not parseable JSON. "
                f"stdout tail: {_tail(stdout)!r}; stderr tail: {_tail(stderr)!r}"
            ) from fallback_error

    if not isinstance(parsed, dict):
        raise StagehandVerifierOutputError(
            "Stagehand verifier JSON must be an object, "
            f"not {type(parsed).__name__}."
        )
    return parsed


def _mapping_value(
    mapping: Mapping[str, str] | None,
    key: str,
    *,
    verbatim: bool = False,
) -> str | None:
    if mapping is None:
        return None
    value = mapping.get(key)
    return _nonempty_verbatim(value) if verbatim else _nonempty_stripped(value)


def _nonempty_stripped(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _nonempty_verbatim(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _coerce_timeout(value: int | str | None) -> int:
    if value is None:
        return DEFAULT_TIMEOUT_SEC
    if isinstance(value, bool):
        raise ValueError("timeout_sec must be a positive integer, not a boolean")
    try:
        timeout = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"timeout_sec must be a positive integer, got {value!r}"
        ) from error
    if timeout <= 0:
        raise ValueError(f"timeout_sec must be positive, got {timeout!r}")
    return timeout


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _normalize(value: str) -> str:
    return value.strip().casefold()


def _tail(value: str | None) -> str:
    return (value or "")[-_OUTPUT_TAIL_LIMIT:]


def _exec_failure_message(context: str, result: object) -> str:
    return_code = getattr(result, "return_code")
    stdout = getattr(result, "stdout", None)
    stderr = getattr(result, "stderr", None)
    return (
        f"{context} exited with return code {return_code}. "
        f"stderr tail: {_tail(stderr)!r}; stdout tail: {_tail(stdout)!r}"
    )


__all__ = [
    "CriteriaFraction",
    "DEFAULT_EVALS_BIN",
    "DEFAULT_JUDGE_MODEL",
    "DEFAULT_TRAJECTORIES_ROOT",
    "StagehandVerifier",
    "StagehandVerifierError",
    "StagehandVerifierExecError",
    "StagehandVerifierOutputError",
    "StagehandVerifierUnhealthyError",
    "compute_criteria_fraction",
]
