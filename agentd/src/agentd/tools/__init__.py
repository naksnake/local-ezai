"""Tool abstraction layer — see base.py for the contracts."""

from agentd.tools.base import Tool, ToolRegistry, ToolResult
from agentd.tools.filesystem import FsEdit, FsGlob, FsLs, FsRead, FsWrite
from agentd.tools.git import GitAdd, GitCommit, GitDiff, GitPush, GitStatus
from agentd.tools.search import CodeGrep
from agentd.tools.shell import ExecRun

ALL_TOOL_CLASSES = [
    FsRead,
    FsWrite,
    FsEdit,
    FsLs,
    FsGlob,
    CodeGrep,
    ExecRun,
    GitStatus,
    GitDiff,
    GitAdd,
    GitCommit,
    GitPush,
]

__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "ALL_TOOL_CLASSES",
    "FsRead",
    "FsWrite",
    "FsEdit",
    "FsLs",
    "FsGlob",
    "CodeGrep",
    "ExecRun",
    "GitStatus",
    "GitDiff",
    "GitAdd",
    "GitCommit",
    "GitPush",
]
