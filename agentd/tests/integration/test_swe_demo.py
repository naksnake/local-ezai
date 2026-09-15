"""The offline SWE demo (`scripts/swe-demo.sh`, `make swe-demo`,
docs/SWE_DEMO.md) runs green end to end: a scripted model, a real worktree,
the sample project's own unittest gate, the review gate, a commit on a
`swe/<run-id>` branch — so the walkthrough newcomers are sent to cannot rot
as the pipeline evolves. Runs the shell script as shipped, with the CLI of
this test environment behind a shim."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "swe-demo.sh"
RESPONSES = REPO_ROOT / "examples" / "swe-demo" / "responses.json"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def test_the_scripted_responses_are_a_well_formed_script():
    script = json.loads(RESPONSES.read_text(encoding="utf-8"))
    assert isinstance(script, list) and len(script) == 5
    plan = json.loads(script[0]["content"])
    assert plan["tasks"][0]["check"] == "python3 -m unittest discover -s tests -q"
    edit = next(call for call in script[2]["tool_calls"] if call["name"] == "fs_edit")
    app = (REPO_ROOT / "examples" / "sample-project" / "app.py").read_text(encoding="utf-8")
    assert app.count(edit["arguments"]["old_string"]) == 1  # fs_edit needs exactly one match
    assert json.loads(script[4]["content"])["verdict"] == "approve"


def test_the_offline_demo_runs_green(tmp_path: Path):
    shim = tmp_path / "local-ezai"
    shim.write_text("#!/bin/sh\n"
                    f'exec "{sys.executable}" -c '
                    "'import sys; from agentd.main_cli import main; sys.exit(main())' \"$@\"\n",
                    encoding="utf-8")
    shim.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "AGENTD_CONFIG"}
    env["EZAI_CLI"] = str(shim)
    demo = tmp_path / "demo"

    proc = subprocess.run(["bash", str(SCRIPT), "--dir", str(demo)], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[COMPLETED]" in proc.stdout and "Ran 3 tests" in proc.stdout
    assert "Validation:   deterministic harness" in proc.stdout  # explain-run ran

    project = demo / "project"
    # refs, not `git branch` (which marks the branch the run's worktree has checked out)
    branches = git(project, "for-each-ref", "--format=%(refname:short)", "refs/heads/swe/").split()
    assert len(branches) == 1, proc.stdout
    branch = branches[0]
    history = git(project, "log", "--oneline", "--all")
    assert git(project, "rev-list", "--count", "main") == "1", history  # main untouched
    assert git(project, "rev-list", "--count", f"main..{branch}") == "1", history
    diff = git(project, "diff", f"main...{branch}", "--", "app.py")
    assert '+    return {"/": GREETING, "/health": {"status": "ok"}}' in diff
    assert "tests/test_health.py" in git(project, "diff", "--stat", f"main...{branch}")

    reports = list((demo / "state" / "runs").glob("*/report.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["status"] == "completed" and report["validation"]["passed"]
    assert report["review"]["verdict"] == "approve" and report["commit"]["sha"]
    assert set(report["models_used"].values()) == {"scripted"}
    assert (project / ".agent" / "memory.db").is_file()  # the run left its lesson behind
