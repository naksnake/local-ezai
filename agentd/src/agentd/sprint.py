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
