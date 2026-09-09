"""The release train (PR-26, P6): the artifacts a human signs are kept
honest by tests — one version everywhere, a release-notes entry for it, the
product DoD checked item by item, a soak driver whose schedule is what the
runbook says, the guides carrying the V1 surfaces, and no tag applied by a
machine (the tag is the human's act, GOVERNANCE.md)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from agentd import __version__

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS = REPO_ROOT / "docs"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_version_is_one_and_the_same_everywhere():
    assert __version__ == "1.0.0"
    pyproject = read(REPO_ROOT / "agentd" / "pyproject.toml")   # no tomllib on 3.10
    assert re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE).group(1) == __version__
    notes = read(DOCS / "RELEASE_NOTES.md")
    headings = re.findall(r"^## v(\S+) — (\d{4}-\d{2}-\d{2})", notes, re.MULTILINE)
    assert headings and headings[0][0] == __version__          # newest first, dated
    assert [h[0] for h in headings] == ["1.0.0", "1.0.0rc1", "0.7.0"]
    report = read(DOCS / "V1_RELEASE_REPORT.md")
    assert f"agentd {__version__}" in report and "contract 1.2.0" in report


def test_the_dod_checklist_covers_every_target_product_item():
    target = read(DOCS / "TARGET_PRODUCT_V1.md").split("## 8. Definition of Done", 1)[1]
    items = re.findall(r"^(\d)\. ", target, re.MULTILINE)
    assert items == ["1", "2", "3", "4", "5", "6"]
    report = read(DOCS / "V1_RELEASE_REPORT.md")
    checklist = report.split("## 3. Definition of Done", 1)[1].split("## 4.", 1)[0]
    for item in items:
        assert re.search(rf"^\| ✅[^|]*\| {item}\. ", checklist, re.MULTILINE), f"DoD item {item}"
    # the P6 exit criteria are all stated, the two human ones explicitly pending
    criteria = report.split("## 4. P6 exit criteria", 1)[1].split("## 5.", 1)[0]
    assert "| 1 | Parity harness" in criteria and "✅" in criteria
    assert "pending the hardware run" in criteria and "Human release sign-off" in criteria


def test_the_soak_driver_schedule_is_what_the_runbook_says():
    script = REPO_ROOT / "scripts" / "soak.sh"
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    full = subprocess.run(["bash", str(script), "--dry-run", "--hours", "72"],
                          capture_output=True, text=True, check=True).stdout
    assert "288 ticks" in full and "dry run: nothing executed" in full
    counts = dict(re.findall(r"^\s+(\w+)\s+× (\d+)$", full, re.MULTILINE))
    assert counts == {"health": "288", "status": "288", "bench": "72", "run": "36",
                      "churn": "12", "evolve": "3", "stats": "3"}
    assert "tick   0  +   0.0 h  health status bench run churn evolve stats" in full
    rehearsal = subprocess.run(["bash", str(script), "--dry-run", "--hours", "0.5", "--tick", "60"],
                               capture_output=True, text=True, check=True).stdout
    assert "30 ticks" in rehearsal
    unknown = subprocess.run(["bash", str(script), "--bogus"], capture_output=True, text=True)
    assert unknown.returncode == 2 and "unknown argument" in unknown.stderr
    runbook = read(DOCS / "SOAK_RUNBOOK.md")
    for phrase in ("make soak HOURS=72", "results.md", "Rollback exercised under load",
                   "Zero unexplained failures", "Results template", "V1_RELEASE_REPORT.md"):
        assert phrase in runbook, phrase
    makefile = read(REPO_ROOT / "Makefile")
    assert "\nsoak:" in makefile and "scripts/soak.sh" in makefile
    assert re.search(r"\.PHONY:.*\bsoak\b", makefile.replace("\\\n", " "))


def test_the_guides_carry_the_v1_surfaces():
    anchors = {
        "USER_GUIDE.md": ("make setup", "Orchestrator", "Admin Center", "governance approve",
                          "local-ezai model install", "make bundle", "config/rendered"),
        "OPERATION_MANUAL.md": ("make release-gate", "make soak", "make swe-gates",
                                "make swe-parity", "SOAK_RUNBOOK.md"),
        "CLI_REFERENCE.md": ("first_run_report", "project_memory_add", "1.2.0",
                             "active slot's runtime"),
        "TROUBLESHOOTING.md": ("install.sh", "control plane unreachable", "Admin Center",
                               "decisions are made once", "unknown accelerator kind",
                               "make release-gate"),
        "MAINTENANCE_GUIDE.md": ("config/rendered", "model rollback", "make release-gate",
                                 "SOAK_RUNBOOK.md", "RELEASE_NOTES.md", "config/providers",
                                 "CONTRACT_VERSION"),
    }
    for guide, phrases in anchors.items():
        text = read(DOCS / guide)
        for phrase in phrases:
            assert phrase in text, f"{guide} lacks {phrase!r}"
    # the guides the Documentation Agent maintains all exist (CLAUDE.md mandate)
    for guide in ("USER_GUIDE.md", "OPERATION_MANUAL.md", "MAINTENANCE_GUIDE.md",
                  "RELEASE_NOTES.md"):
        assert (DOCS / guide).is_file(), guide


def test_the_tag_is_the_humans_act():
    report = read(DOCS / "V1_RELEASE_REPORT.md")
    assert "git tag v1.0.0" in report and "READY FOR HUMAN SIGN-OFF" in report
    assert "approve / hold" in report                     # every sign-off row is still open
    tags = subprocess.run(["git", "-C", str(REPO_ROOT), "tag", "--list", "v1.0.0"],
                          capture_output=True, text=True)
    assert tags.returncode == 0 and tags.stdout.strip() == ""   # no machine ever tags a release
