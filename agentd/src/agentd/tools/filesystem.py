"""Filesystem tools — all paths resolve through Workspace (no escapes).

The edit tool follows the exact-match-replace contract that works well for
code agents: the model must read a file first, then provide a unique
``old_string`` — ambiguous or stale matches fail loudly instead of guessing.
"""

from __future__ import annotations

from typing import Any

from agentd.permissions import ToolTier
from agentd.tools.base import Tool, ToolResult
from agentd.workspace import Workspace

_MAX_READ_CHARS = 60_000
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".ruff_cache",
              ".pytest_cache", ".mypy_cache", "dist", "build"}


class FsRead(Tool):
    name = "fs_read"
    description = (
        "Read a text file from the workspace. Returns the content with line numbers. "
        "Always read a file before editing it."
    )
    tier = ToolTier.T0_READ_WORKSPACE
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to the workspace root"},
            "offset": {"type": "integer", "description": "1-based first line to read"},
            "limit": {"type": "integer", "description": "Maximum number of lines"},
        },
        "required": ["path"],
    }

    def run(self, workspace: Workspace, path: str, offset: int = 1,
            limit: int = 2000) -> ToolResult:
        target = workspace.resolve(path)
        if not target.is_file():
            return ToolResult(ok=False, error=f"file not found: {path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        offset = max(1, int(offset))
        selected = lines[offset - 1 : offset - 1 + max(1, int(limit))]
        numbered = "\n".join(f"{i}\t{line}" for i, line in enumerate(selected, start=offset))
        if len(numbered) > _MAX_READ_CHARS:
            numbered = numbered[:_MAX_READ_CHARS]
            return ToolResult(ok=True, output=numbered, truncated=True)
        return ToolResult(ok=True, output=numbered)


class FsWrite(Tool):
    name = "fs_write"
    description = (
        "Create or overwrite a file in the workspace with the given content. "
        "Parent directories are created automatically. Prefer fs_edit for "
        "modifying existing files."
    )
    tier = ToolTier.T2_MUTATE_WORKSPACE
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to the workspace root"},
            "content": {"type": "string", "description": "Full file content"},
        },
        "required": ["path", "content"],
    }

    def run(self, workspace: Workspace, path: str, content: str) -> ToolResult:
        target = workspace.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(ok=True, output=f"wrote {len(content)} chars to {path}")


class FsEdit(Tool):
    name = "fs_edit"
    description = (
        "Edit a file by exact string replacement. old_string must appear exactly "
        "once in the file (or set replace_all=true). Include enough surrounding "
        "context to make it unique. Read the file first."
    )
    tier = ToolTier.T2_MUTATE_WORKSPACE
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "Exact text to replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def run(
        self,
        workspace: Workspace,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> ToolResult:
        target = workspace.resolve(path)
        if not target.is_file():
            return ToolResult(ok=False, error=f"file not found: {path}")
        if old_string == new_string:
            return ToolResult(ok=False, error="old_string and new_string are identical")
        text = target.read_text(encoding="utf-8", errors="replace")
        count = text.count(old_string)
        if count == 0:
            return ToolResult(
                ok=False,
                error="old_string not found in file — re-read the file and match exactly",
            )
        if count > 1 and not replace_all:
            return ToolResult(
                ok=False,
                error=f"old_string occurs {count} times — add context to make it "
                      "unique, or set replace_all=true",
            )
        new_text = text.replace(old_string, new_string) if replace_all else text.replace(
            old_string, new_string, 1
        )
        target.write_text(new_text, encoding="utf-8")
        replaced = count if replace_all else 1
        return ToolResult(ok=True, output=f"replaced {replaced} occurrence(s) in {path}")


class FsLs(Tool):
    name = "fs_ls"
    description = "List files and directories at a workspace path (directories end with /)."
    tier = ToolTier.T0_READ_WORKSPACE
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory, default workspace root"},
        },
    }

    def run(self, workspace: Workspace, path: str = ".") -> ToolResult:
        target = workspace.resolve(path)
        if not target.is_dir():
            return ToolResult(ok=False, error=f"not a directory: {path}")
        entries = []
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
            if child.name in _SKIP_DIRS:
                continue
            entries.append(child.name + "/" if child.is_dir() else child.name)
        return ToolResult(ok=True, output="\n".join(entries) or "(empty)")


class FsGlob(Tool):
    name = "fs_glob"
    description = "Find files matching a glob pattern, e.g. '**/*.py' or 'src/**/*.ts'."
    tier = ToolTier.T0_READ_WORKSPACE
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    }

    def run(self, workspace: Workspace, pattern: str, limit: int = 200) -> ToolResult:
        root = workspace.root
        matches = []
        for match in sorted(root.glob(pattern)):
            rel = match.relative_to(root)
            if any(part in _SKIP_DIRS for part in rel.parts):
                continue
            matches.append(str(rel))
            if len(matches) >= limit:
                break
        return ToolResult(ok=True, output="\n".join(matches) or "(no matches)")
