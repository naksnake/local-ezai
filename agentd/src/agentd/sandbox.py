"""sandboxd — the per-run execution sandbox (Phase H1, ADR-021).

Closes ADR-014: every agent-driven shell execution (the ``exec_run`` tool,
validation/build/test checks, debugging reproductions, benchmark runs) goes
through ONE executor with three layers, active in every mode:

1. **Command allowlist** (``sandbox.command_allowlist``) — a non-matching
   command is refused before anything runs (fail-closed when configured).
2. **Execution** — ``host`` (subprocess in the workspace, the ADR-014
   interim behavior) or ``docker`` (a disposable container that mounts only
   the workspace, with resource limits and default-deny networking).
3. **Audit log** — every execution (including refused ones) is appended to
   ``<run-dir>/exec_audit.jsonl``.

Mode resolution (``sandbox.mode: auto``, the default): docker **whenever
possible** — the daemon answers AND an ``image`` is configured; the host
executor otherwise. The image gate is deliberate: project checks need the
project's toolchain, so the sandbox image is an operator decision
(see docs/SANDBOX_GUIDE.md), never a guess.

Docker mounts the workspace at its host-identical absolute path so error
messages, configured commands, and git worktree pointers stay coherent;
for a linked worktree the origin repo's ``.git`` directory is mounted too
(a worktree's ``.git`` file points there by absolute path).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from agentd.config import SandboxConfig
from agentd.logging_setup import get_logger
from agentd.tools.base import ToolResult

log = get_logger("sandbox")

_OUTPUT_CAP = 20_000


class SandboxError(RuntimeError):
    """The configured sandbox mode cannot be satisfied (strict docker)."""


class Sandbox:
    """One executor per run, shared by every agent of that run."""

    def __init__(
        self,
        config: SandboxConfig,
        root: Path,
        audit_path: Path | None = None,
        journal=None,
    ) -> None:
        self.config = config
        self.root = Path(root)
        self.audit_path = audit_path
        self.journal = journal
        self._mode: str | None = None

    # ── mode resolution ──────────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        """Resolved execution mode ('docker' or 'host'), decided once."""
        if self._mode is None:
            self._mode = self._resolve_mode()
            log.info("sandbox mode: %s (configured: %s, image: %s)",
                     self._mode, self.config.mode, self.config.image or "-")
            if self.journal is not None:
                self.journal.append(
                    "SANDBOX_MODE",
                    mode=self._mode,
                    configured=self.config.mode,
                    image=self.config.image,
                    network=self.config.network,
                    allowlist_rules=len(self.config.command_allowlist),
                )
        return self._mode

    def _resolve_mode(self) -> str:
        cfg = self.config
        if cfg.mode == "host":
            return "host"
        if cfg.mode == "docker":
            if not cfg.image:
                raise SandboxError(
                    "sandbox.mode is 'docker' but sandbox.image is not set"
                )
            if not self._docker_available():
                raise SandboxError(
                    "sandbox.mode is 'docker' but the Docker daemon is "
                    "unreachable — start it or use mode 'auto'/'host'"
                )
            return "docker"
        # auto: container execution whenever possible.
        if cfg.image and self._docker_available():
            return "docker"
        return "host"

    def _docker_available(self) -> bool:
        binary = shutil.which(self.config.docker_binary)
        if not binary:
            return False
        try:
            proc = subprocess.run(
                [binary, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    # ── the one entry point ──────────────────────────────────────────────────

    def run(self, command: str, timeout: float) -> ToolResult:
        """Execute one shell command under the resolved mode."""
        if not self._allowed(command):
            result = ToolResult(
                ok=False,
                error="command blocked by sandbox allowlist "
                      "(sandbox.command_allowlist)",
                exit_code=None,
            )
            self._audit(command, result, allowed=False)
            return result
        start = time.monotonic()
        if self.mode == "docker":
            result = self._run_docker(command, timeout)
        else:
            result = self._run_host(command, timeout)
        result.duration_ms = int((time.monotonic() - start) * 1000)
        self._audit(command, result, allowed=True)
        return result

    def _allowed(self, command: str) -> bool:
        patterns = self.config.command_allowlist
        if not patterns:
            return True
        return any(re.search(p, command) for p in patterns)

    # ── executors ────────────────────────────────────────────────────────────

    def _run_host(self, command: str, timeout: float) -> ToolResult:
        env = dict(os.environ)
        # Agent loops edit files and re-run checks within the same second,
        # and CPython's bytecode cache validates by (mtime seconds, size) —
        # keeping agent-run commands from writing bytecode keeps checks
        # hermetic.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            return self._timeout_result(exc, timeout)
        return self._proc_result(proc)

    def _run_docker(self, command: str, timeout: float) -> ToolResult:
        cfg = self.config
        binary = shutil.which(cfg.docker_binary) or cfg.docker_binary
        name = f"agentd-{uuid.uuid4().hex[:12]}"
        args = [
            binary, "run", "--rm", "--name", name,
            # Host-identical mount path: commands, error messages, and git
            # worktree pointers resolve the same inside and outside.
            "-v", f"{self.root}:{self.root}",
            "-w", str(self.root),
            "--network", cfg.network,
            "-e", "PYTHONDONTWRITEBYTECODE=1",
        ]
        if cfg.memory:
            args += ["--memory", cfg.memory]
        if cfg.cpus:
            args += ["--cpus", str(cfg.cpus)]
        if cfg.pids_limit:
            args += ["--pids-limit", str(cfg.pids_limit)]
        common_git = self._worktree_git_dir()
        if common_git is not None:
            args += ["-v", f"{common_git}:{common_git}"]
        for key in cfg.env_passthrough:
            if key in os.environ:
                args += ["-e", key]
        args += [cfg.image, "sh", "-c", command]
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            # --rm cleans up on exit, but a wedged container must be killed.
            subprocess.run([binary, "kill", name], capture_output=True)
            return self._timeout_result(exc, timeout)
        return self._proc_result(proc)

    def _worktree_git_dir(self) -> Path | None:
        """For a linked git worktree, the origin ``.git`` directory that its
        ``.git`` file points to (mounted so git works in-container)."""
        gitfile = self.root / ".git"
        if not gitfile.is_file():
            return None
        try:
            text = gitfile.read_text(encoding="utf-8")
        except OSError:
            return None
        match = re.match(r"gitdir:\s*(.+)", text.strip())
        if not match:
            return None
        gitdir = Path(match.group(1))  # <origin>/.git/worktrees/<id>
        for parent in gitdir.parents:
            if parent.name == ".git":
                return parent
        return None

    # ── result/audit plumbing ────────────────────────────────────────────────

    @staticmethod
    def _proc_result(proc: subprocess.CompletedProcess) -> ToolResult:
        output = (proc.stdout or "") + (proc.stderr or "")
        truncated = len(output) > _OUTPUT_CAP
        if truncated:
            output = output[-_OUTPUT_CAP:]
        ok = proc.returncode == 0
        return ToolResult(
            ok=ok,
            output=output,
            error=None if ok else f"exit code {proc.returncode}",
            exit_code=proc.returncode,
            truncated=truncated,
        )

    @staticmethod
    def _timeout_result(exc: subprocess.TimeoutExpired, timeout: float) -> ToolResult:
        partial = ""
        for stream in (exc.stdout, exc.stderr):
            if stream:
                partial += stream if isinstance(stream, str) else stream.decode(errors="replace")
        return ToolResult(
            ok=False,
            error=f"command timed out after {timeout:.0f}s",
            output=partial[-_OUTPUT_CAP:],
            exit_code=None,
        )

    def _audit(self, command: str, result: ToolResult, allowed: bool) -> None:
        if not self.config.audit or self.audit_path is None:
            return
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "mode": self._mode or "unresolved",
            "workspace": str(self.root),
            "command": command,
            "allowed": allowed,
            "ok": result.ok,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
        }
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:  # audit must never fail an execution
            log.warning("exec audit write failed: %s", exc)
