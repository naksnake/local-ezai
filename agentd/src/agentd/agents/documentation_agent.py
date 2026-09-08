"""Documentation Agent (CLAUDE.md roster, Phase 7).

Maintains the repository's ``docs/``: generates or refreshes the four
mandated guides — USER_GUIDE.md, OPERATION_MANUAL.md, MAINTENANCE_GUIDE.md,
RELEASE_NOTES.md — grounded in the actual repository content (read tools)
and project memory. Write access is the standard T2 workspace tier; the
agent is instructed to write only under ``docs/`` and the result reports
the files actually changed (derived from git, not model claims).
"""

from __future__ import annotations

from agentd.agents.base import BaseAgent
from agentd.agents.prompts import load_prompt
from agentd.schemas import DocsResult
from agentd.tools.git import git_run
from agentd.workspace import Workspace

GUIDES = ("USER_GUIDE.md", "OPERATION_MANUAL.md",
          "MAINTENANCE_GUIDE.md", "RELEASE_NOTES.md")


class DocumentationAgent(BaseAgent):
    agent_name = "documentation"
    role = "documentation"
    tool_names = ["fs_read", "fs_ls", "fs_glob", "code_grep",
                  "fs_write", "fs_edit", "git_status", "git_diff"]

    def run(self, workspace: Workspace, focus: str = "") -> DocsResult:
        existing = [g for g in GUIDES if (workspace.root / "docs" / g).is_file()]
        missing = [g for g in GUIDES if g not in existing]
        user = (
            f"Repository: {workspace.root.name} (branch {workspace.branch})\n"
            f"Guides already present: {', '.join(existing) or 'none'}\n"
            f"Guides missing: {', '.join(missing) or 'none'}\n"
        )
        if focus:
            user += f"Focus for this pass: {focus}\n"
        user += self._memory_block()
        user += ("\nExplore the repository, then create the missing guides "
                 "and refresh stale sections of the existing ones.")

        before = self._docs_changed(workspace)
        outcome = self.run_loop(load_prompt("documentation"), user, workspace)
        after = self._docs_changed(workspace)
        files = sorted(after - before) or sorted(after)

        failed = (outcome.budget_exhausted
                  or outcome.final_text.strip().startswith("FAILED:"))
        result = DocsResult(
            status="failed" if failed else "done",
            summary=(outcome.final_text.strip()
                     or "turn budget exhausted")[:1500],
            files_written=files,
        )
        self.journal.append("DOCS_GENERATED", status=result.status,
                            files=result.files_written)
        self.log.info("documentation: %s (%d file(s))", result.status,
                      len(result.files_written))
        return result

    def _memory_block(self) -> str:
        if self.memory is None:
            return ""
        from agentd.memory import KIND_ARCHITECTURE, KIND_IMPLEMENTATION

        records = self.memory.recent([KIND_ARCHITECTURE, KIND_IMPLEMENTATION],
                                     limit=8)
        if not records:
            return ""
        return "\nProject memory (recent history and decisions):\n" + "\n".join(
            f"- [{r.kind}] {r.title}: {r.content.splitlines()[0][:150]}"
            for r in records
        ) + "\n"

    @staticmethod
    def _docs_changed(workspace: Workspace) -> set[str]:
        # -uall: list files inside untracked directories (a brand-new docs/
        # would otherwise appear as just '?? docs/')
        status = git_run(workspace, "status", "--porcelain", "-uall",
                         "--", "docs")
        files: set[str] = set()
        if status.ok:
            for line in status.output.splitlines():
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2:
                    files.add(parts[1].strip().strip('"'))
        return files
