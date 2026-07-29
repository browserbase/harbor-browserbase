#!/usr/bin/env python3
"""Validate the Harbor task fixtures without network access."""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

from harbor.environments.definition import environment_content_hash
from harbor.models.task.task import Task


TASKS_DIR = Path("tasks")
MARKER_RE = re.compile(
    r"^[ \t]*stagehand-task-id[ \t]*:[ \t]*([A-Za-z0-9._\-/]+)[ \t]*$",
    re.MULTILINE,
)
MARKER_LINE_RE = re.compile(r"^[ \t]*stagehand-task-id[ \t]*:.*$", re.MULTILINE)


def main() -> int:
    task_dirs = sorted(path for path in TASKS_DIR.iterdir() if path.is_dir())
    failures: list[str] = []
    hashes: dict[Path, str] = {}

    if not task_dirs:
        print(f"ERROR: no task fixture directories found under {TASKS_DIR}", file=sys.stderr)
        return 1

    for task_dir in task_dirs:
        try:
            is_valid = Task.is_valid_dir(task_dir)
        except Exception as exc:
            failures.append(
                f"{task_dir}: Task.is_valid_dir raised {type(exc).__name__}: {exc}"
            )
        else:
            if not is_valid:
                failures.append(f"{task_dir}: Task.is_valid_dir returned False")

        try:
            Task(task_dir)
        except Exception as exc:
            failures.append(
                f"{task_dir}: Task construction failed: {type(exc).__name__}: {exc}"
            )

        instruction_path = task_dir / "instruction.md"
        try:
            instruction = instruction_path.read_text()
        except Exception as exc:
            failures.append(
                f"{instruction_path}: could not read file: {type(exc).__name__}: {exc}"
            )
        else:
            marker_lines = MARKER_LINE_RE.findall(instruction)
            markers = MARKER_RE.findall(instruction)
            if len(marker_lines) != 1 or len(markers) != 1:
                failures.append(
                    f"{instruction_path}: expected exactly one stagehand-task-id marker, "
                    f"found {len(marker_lines)} marker lines and {len(markers)} valid markers"
                )
            else:
                print(f"{task_dir.name}: stagehand-task-id: {markers[0]}")

        try:
            hashes[task_dir] = environment_content_hash(task_dir / "environment")
        except Exception as exc:
            failures.append(
                f"{task_dir / 'environment'}: hash failed: {type(exc).__name__}: {exc}"
            )

    hashes_to_tasks: dict[str, list[str]] = defaultdict(list)
    for task_dir, content_hash in hashes.items():
        hashes_to_tasks[content_hash].append(task_dir.name)

    if len(hashes) != len(task_dirs):
        failures.append(
            f"computed environment hashes for {len(hashes)} of {len(task_dirs)} fixtures"
        )
    elif len(hashes_to_tasks) != 1:
        failures.append(
            f"environment content drift: found {len(hashes_to_tasks)} distinct hashes"
        )

    if len(hashes_to_tasks) == 1 and len(hashes) == len(task_dirs):
        print(f"environment hash: {next(iter(hashes_to_tasks))}")
    elif hashes_to_tasks:
        print("Distinct environment hashes:", file=sys.stderr)
        for content_hash, task_names in sorted(hashes_to_tasks.items()):
            print(f"  {content_hash}:", file=sys.stderr)
            for task_name in sorted(task_names):
                print(f"    - {task_name}", file=sys.stderr)

    print(f"total fixtures: {len(task_dirs)}")

    if failures:
        print("Fixture validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
