"""Sprint execution — run a markdown spec's tasks sequentially on one
shared branch, so each task builds on the previous one (Phase 5).

Spec parsing accepts, in priority order:

1. unchecked markdown checklist items:  ``- [ ] implement login``
   (checked ``- [x]`` items are treated as already done and skipped)
2. plain bullets:                        ``- implement login`` / ``* ...``
3. numbered items:                       ``1. implement login`` / ``2) ...``

The sprint creates one worktree on branch ``sprint/<id>``, then executes
each task as a full run pipeline (plan → code → validate → self-heal →
commit) *in place* on that worktree — one commit per completed task,
cumulative on the sprint branch. The origin repo's project memory is shared
by every task (memory always resolves to the origin repository).
"""

from __future__ import annotations

import re
from pathlib import Path

_CHECKLIST_RE = re.compile(r"^\s*[-*]\s*\[( |x|X)\]\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(?!\[)(.+?)\s*$")
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+(.+?)\s*$")


def parse_sprint_tasks(text: str) -> list[str]:
    """Extract executable task descriptions from a sprint spec."""
    lines = text.splitlines()

    checklist: list[str] = []
    has_any_checklist = False
    for line in lines:
        match = _CHECKLIST_RE.match(line)
        if match:
            has_any_checklist = True
            if match.group(1) == " ":  # unchecked only
                checklist.append(match.group(2))
    if has_any_checklist:
        return checklist

    bullets = [m.group(1) for line in lines if (m := _BULLET_RE.match(line))]
    if bullets:
        return bullets

    return [m.group(1) for line in lines if (m := _NUMBERED_RE.match(line))]


def load_sprint_tasks(spec_file: Path) -> list[str]:
    """Read and parse a spec file; raises ValueError when no tasks found."""
    text = Path(spec_file).read_text(encoding="utf-8")
    tasks = parse_sprint_tasks(text)
    if not tasks:
        raise ValueError(
            f"no tasks found in {spec_file} — use markdown checklist items "
            "('- [ ] task'), bullets ('- task'), or numbered items ('1. task')"
        )
    return tasks


# ── dependency graph (Phase 6, deterministic) ────────────────────────────────


def validate_dependencies(tasks) -> list[str]:
    """Structural errors of a task DAG: duplicate ids, unknown or self
    dependencies, cycles. Empty list == valid."""
    errors: list[str] = []
    ids = [t.id for t in tasks]
    seen: set[str] = set()
    for task_id in ids:
        if task_id in seen:
            errors.append(f"duplicate task id: {task_id}")
        seen.add(task_id)
    known = set(ids)
    for task in tasks:
        for dep in task.depends_on:
            if dep == task.id:
                errors.append(f"task {task.id} depends on itself")
            elif dep not in known:
                errors.append(f"task {task.id} depends on unknown task '{dep}'")
    if errors:
        return errors

    # Kahn's algorithm — anything left unprocessed sits on a cycle.
    remaining = {t.id: set(t.depends_on) for t in tasks}
    while True:
        ready = [tid for tid, deps in remaining.items() if not deps]
        if not ready:
            break
        for tid in ready:
            del remaining[tid]
        for deps in remaining.values():
            deps.difference_update(ready)
    if remaining:
        errors.append(
            "dependency cycle involving: " + ", ".join(sorted(remaining))
        )
    return errors


def topological_waves(tasks) -> list[list]:
    """Group tasks into execution waves: every task in wave N depends only
    on tasks from waves < N; tasks within one wave are independent and can
    run in parallel. Requires a valid DAG (validate_dependencies first)."""
    by_id = {t.id: t for t in tasks}
    level: dict[str, int] = {}

    def resolve(task_id: str) -> int:
        if task_id in level:
            return level[task_id]
        deps = by_id[task_id].depends_on
        level[task_id] = 1 + max((resolve(d) for d in deps), default=-1)
        return level[task_id]

    for task in tasks:
        resolve(task.id)
    waves: list[list] = [[] for _ in range(max(level.values()) + 1)]
    for task in tasks:  # keep the plan's task order within each wave
        waves[level[task.id]].append(task)
    return waves
