"""Code search — regex grep over workspace text files (portable, no ripgrep
dependency; swaps for ripgrep/codeidx hybrid search in later phases)."""

from __future__ import annotations

import re
from typing import Any

from agentd.permissions import ToolTier
from agentd.tools.base import Tool, ToolResult
from agentd.tools.filesystem import _SKIP_DIRS
from agentd.workspace import Workspace

_MAX_MATCHES = 100
_MAX_FILE_BYTES = 1_000_000


class CodeGrep(Tool):
    name = "code_grep"
    description = (
        "Search workspace files for a regular expression. Returns file:line: match "
        "lines. Optionally restrict to a glob (e.g. '**/*.py')."
    )
    tier = ToolTier.T0_READ_WORKSPACE
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python regular expression"},
            "glob": {"type": "string", "description": "Restrict files, default all files"},
            "ignore_case": {"type": "boolean"},
        },
        "required": ["pattern"],
    }

    def run(
        self,
        workspace: Workspace,
        pattern: str,
        glob: str = "**/*",
        ignore_case: bool = False,
    ) -> ToolResult:
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            return ToolResult(ok=False, error=f"invalid regex: {exc}")

        root = workspace.root
        matches: list[str] = []
        for path in sorted(root.glob(glob)):
            rel = path.relative_to(root)
            if not path.is_file() or any(part in _SKIP_DIRS for part in rel.parts):
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    if len(matches) >= _MAX_MATCHES:
                        return ToolResult(
                            ok=True,
                            output="\n".join(matches),
                            truncated=True,
                        )
        return ToolResult(ok=True, output="\n".join(matches) or "(no matches)")


class CodeSymbols(Tool):
    """Symbol lookup backed by the semantic code index (ADR-023)."""

    name = "code_symbols"
    description = (
        "Look up classes/functions/methods by name in the repository's "
        "semantic code index. Returns 'file:line kind name — signature' "
        "matches. Much faster and more precise than grepping for "
        "definitions."
    )
    tier = ToolTier.T0_READ_WORKSPACE
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "Symbol name or substring"},
            "limit": {"type": "integer", "description": "Max matches, default 20"},
        },
        "required": ["query"],
    }

    def run(self, workspace: Workspace, query: str, limit: int = 20) -> ToolResult:
        index = workspace.code_index
        if index is None:
            return ToolResult(
                ok=False,
                error="code index not available for this run "
                      "(code_intel.enabled is off or indexing failed) — "
                      "use code_grep instead",
            )
        limit = min(max(1, int(limit)), 100)
        matches = index.find(query, limit=limit)
        if not matches:
            return ToolResult(ok=True, output=f"no symbols match '{query}'")
        lines = [
            f"{m['file']}:{m['line']} {m['kind']} {m['name']} — {m.get('signature', '')}"
            for m in matches
        ]
        return ToolResult(ok=True, output="\n".join(lines))
