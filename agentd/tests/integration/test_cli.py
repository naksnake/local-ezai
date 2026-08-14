"""CLI-level integration: config file + scripted LLM provider, in-process."""

import json

import yaml

from agentd.cli import main
from tests.conftest import git, happy_path_script, planner_response


def write_cli_config(tmp_path, script, extra=None):
    script_file = tmp_path / "script.json"
    script_file.write_text(json.dumps(script), encoding="utf-8")
    cfg = {
        "llm": {"provider": "scripted", "script_path": str(script_file)},
        "workspace": {"root": str(tmp_path / "ws")},
        "runs_dir": str(tmp_path / "runs"),
        "validation": {"autodetect": False},
    }
    for key, value in (extra or {}).items():
        cfg.setdefault(key, {}).update(value)
    cfg_file = tmp_path / "agentd.yaml"
    cfg_file.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return cfg_file


def test_cli_run_happy_path(tmp_repo, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    cfg = write_cli_config(tmp_path, happy_path_script())
    code = main(["run", "fix the add bug", "--repo", str(tmp_repo),
                 "--config", str(cfg), "--json"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "completed"
    assert report["commit"]["sha"]
    assert report["branch"].startswith("swe/")
    assert "return a + b" in git(tmp_repo, "show", f"{report['branch']}:calculator.py")


def test_cli_run_failure_exit_code(tmp_repo, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    script = [planner_response(), {"content": "FAILED: nope"}]
    cfg = write_cli_config(tmp_path, script)
    code = main(["run", "fix it", "--repo", str(tmp_repo), "--config", str(cfg)])
    assert code == 1


def test_cli_plan_only_leaves_no_traces(tmp_repo, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    cfg = write_cli_config(tmp_path, [planner_response()])
    code = main(["plan", "fix the add bug", "--repo", str(tmp_repo),
                 "--config", str(cfg)])
    assert code == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["tasks"][0]["id"] == "T1"
    # no branch, no changes, no worktree
    assert git(tmp_repo, "branch", "--list", "swe/*") == ""
    assert git(tmp_repo, "status", "--porcelain") == ""
    assert not (tmp_path / "ws").exists()


def test_cli_rejects_non_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    cfg = write_cli_config(tmp_path, [])
    plain = tmp_path / "plain"
    plain.mkdir()
    code = main(["run", "x", "--repo", str(plain), "--config", str(cfg)])
    assert code == 2


def test_cli_version(capsys):
    assert main(["version"]) == 0
    assert "agentd" in capsys.readouterr().out
