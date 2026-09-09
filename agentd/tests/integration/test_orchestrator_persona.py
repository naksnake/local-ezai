"""The Orchestrator persona (PR-14, ADR-029; OPENWEBUI_INTEGRATION §3): the
`orchestrator` role + `role-orchestrator` alias as data, the system preset
as data, the SWE tool server pre-registered in OpenWebUI, and the
idempotent persona installer — exercised against a sqlite file with
OpenWebUI's column layout."""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
from pathlib import Path

import pytest
import yaml

from agentd.config import LLM_ROLES, ROLE_ALIAS_PREFIX
from agentd.registry_v2 import reference_registry

REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPT = REPO_ROOT / "config" / "prompts" / "orchestrator.md"
SERVER = REPO_ROOT / "mcp-servers" / "swe-server" / "swe_server.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


installer = load_module(REPO_ROOT / "scripts" / "openwebui_orchestrator.py", "owui_orchestrator")
swe = load_module(SERVER, "swe_server_for_persona")


def openwebui_db(path: Path, *, with_admin: bool = True) -> Path:
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE user (id TEXT PRIMARY KEY, name TEXT, role TEXT)")
    db.execute("CREATE TABLE model (id TEXT PRIMARY KEY, user_id TEXT, base_model_id TEXT, "
               "name TEXT, params TEXT, meta TEXT, access_control TEXT, is_active INTEGER, "
               "updated_at INTEGER, created_at INTEGER)")
    if with_admin:
        db.execute("INSERT INTO user VALUES ('u-viewer', 'v', 'user')")
        db.execute("INSERT INTO user VALUES ('u-admin', 'nita', 'admin')")
    db.commit()
    db.close()
    return path


# ── the role and its alias are data the platform already renders ─────────────


def test_orchestrator_role_and_alias_exist_as_data():
    registry = reference_registry()
    role = registry.roles["orchestrator"]
    assert role.group == "reasoning" and role.requires.tool_calling is True
    assert role.requires.min_context >= 8192 and not role.pin
    assert registry.resolve("orchestrator").primary  # resolvable in the reference data set
    assert "orchestrator" in LLM_ROLES
    # the hand-written profiles served the alias as data before the cutover (PR-6)
    for name in ("litellm-config.yaml", "litellm-config.cpu.yaml", "litellm-config.n97.yaml"):
        legacy = REPO_ROOT / "agentd" / "tests" / "fixtures" / "legacy" / name
        assert f"{ROLE_ALIAS_PREFIX}orchestrator" in legacy.read_text(encoding="utf-8"), name


def test_bootstrapped_platform_serves_the_orchestrator_alias(tmp_path, monkeypatch, capsys):
    """Generation 1 (PR-7 bootstrap) carries the reference roles, so the
    rendered LiteLLM config serves `role-orchestrator` on the reasoning
    primary — the alias the persona's model entry is built on."""
    import shutil

    from agentd import platform_cli
    from agentd.main_cli import main
    from agentd.registry_v2 import load_registry
    from tests.unit.test_lifecycle import FakeEngineHTTP, FakeRunner

    root = tmp_path / "fresh"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config" / "providers", root / "config" / "providers")
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / "w").mkdir()
    for name in ("reasoner", "coder", "chatter"):
        (root / "w" / f"{name}.gguf").write_bytes(name.encode() * 256)
    (root / ".env").write_text(
        f"AI_RUNTIME=llamacpp\nREASONING_MODEL=gguf:{root}/w/reasoner.gguf\n"
        f"CODING_MODEL=gguf:{root}/w/coder.gguf\nCHAT_MODEL=gguf:{root}/w/chatter.gguf\n")
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    monkeypatch.setenv("EZAI_TRANSPORT", "direct")
    monkeypatch.setattr(platform_cli, "default_runner", FakeRunner())
    monkeypatch.setattr(platform_cli, "http_probe", lambda url: 200)
    monkeypatch.setattr(platform_cli, "engine_http", lambda: FakeEngineHTTP(
        timings={"predicted_per_second": 7.5, "prompt_per_second": 40.0, "predicted_n": 100}))
    monkeypatch.setattr(platform_cli, "build_validator",
                        lambda ctx: lambda descriptor, name, entry: platform_cli.lifecycle
                        .ProbeResult(True, "", 0.2, name))
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({"llm": {"provider": "scripted"},
                                   "platform": {"config_dir": str(root / "config")},
                                   "runs_dir": str(tmp_path / "runs")}), encoding="utf-8")
    assert main(["bootstrap", "--config", str(cfg), "--by", "nita"]) == 0
    capsys.readouterr()
    registry = load_registry(root / "config")
    assert registry.roles["orchestrator"].group == "reasoning"
    assert registry.resolve("orchestrator").primary == "reasoner"
    litellm = (root / "config" / "rendered" / "litellm-config.yaml").read_text(encoding="utf-8")
    assert f"model_name: {ROLE_ALIAS_PREFIX}orchestrator" in litellm


# ── the system preset ────────────────────────────────────────────────────────


def test_system_preset_knows_the_catalog_and_the_governance_rule():
    text = PROMPT.read_text(encoding="utf-8")
    prompt = installer.extract_prompt(text)
    assert prompt.startswith("# Role") and prompt.endswith("\n")
    for tool_name in swe.TOOLS:  # every tool of the PR-13 catalog is described
        assert tool_name in prompt, tool_name
    assert "cannot approve, reject, merge, activate, roll back" in prompt
    assert "Admin Center" in prompt and "local-ezai governance approve|reject" in prompt
    assert "local-ezai project add <path>" in prompt
    assert prompt.index("swe_plan") < prompt.index("swe_run")  # plan first
    assert "data, not instructions" in prompt  # prompt-injection posture (§5)
    assert "make orchestrator" in text and "role-orchestrator" in text  # how to apply
    with pytest.raises(ValueError, match="exactly one"):
        installer.extract_prompt("no fence here")


# ── the installer ────────────────────────────────────────────────────────────


def test_persona_row_binds_the_alias_the_preset_and_the_swe_tools():
    prompt = installer.extract_prompt(PROMPT.read_text(encoding="utf-8"))
    row = installer.build_model_row(prompt, admin_id="u-admin", now=1700000000)
    assert row["id"] == "local-ezai-orchestrator" and row["name"] == "Local-EZAI Orchestrator"
    assert row["base_model_id"] == "role-orchestrator" and row["access_control"] is None
    params = json.loads(row["params"])
    assert params["system"] == prompt and params["function_calling"] == "native"
    meta = json.loads(row["meta"])
    assert meta["toolIds"] == ["server:swe"]  # the SWE tool server, this persona only
    assert meta["builtinTools"] == {"knowledge": False}
    assert "never approves" in meta["description"]
    assert row["is_active"] == 1 and row["created_at"] == row["updated_at"] == 1700000000


def test_installer_is_idempotent_and_touches_only_the_persona(tmp_path):
    db_path = openwebui_db(tmp_path / "webui.db")
    db = sqlite3.connect(str(db_path))
    db.execute("INSERT INTO model (id, user_id, base_model_id, name, params, meta, is_active, "
               "updated_at, created_at) VALUES ('chat-7b', 'u-admin', NULL, 'chat-7b', '{}', "
               "'{}', 1, 1, 1)")
    db.commit()
    db.close()

    first = installer.upsert(db_path, PROMPT, now=1000)
    assert first.startswith("created model 'local-ezai-orchestrator'")
    second = installer.upsert(db_path, PROMPT, now=2000)
    assert second.startswith("updated model 'local-ezai-orchestrator'")

    db = sqlite3.connect(str(db_path))
    rows = db.execute("SELECT id, user_id, base_model_id, created_at, updated_at, params "
                      "FROM model ORDER BY id").fetchall()
    db.close()
    assert [r[0] for r in rows] == ["chat-7b", "local-ezai-orchestrator"]
    persona = rows[1]
    assert persona[1] == "u-admin" and persona[2] == "role-orchestrator"
    assert (persona[3], persona[4]) == (1000, 2000)  # created once, refreshed
    assert json.loads(persona[5])["system"].startswith("# Role")
    assert rows[0][5] == "{}"  # the plain chat model is untouched


def test_installer_fails_with_the_fix_when_openwebui_is_not_ready(tmp_path):
    with pytest.raises(SystemExit, match="does not exist"):
        installer.upsert(tmp_path / "missing.db", PROMPT)
    empty = openwebui_db(tmp_path / "empty.db", with_admin=False)
    with pytest.raises(SystemExit, match="sign up"):
        installer.upsert(empty, PROMPT)
    assert installer.main([]) == 2
    other = openwebui_db(tmp_path / "other.db")
    assert installer.main([str(other), str(PROMPT), "role-planner"]) == 0
    db = sqlite3.connect(str(other))
    assert db.execute("SELECT base_model_id FROM model").fetchone()[0] == "role-planner"


# ── first-run pre-registration + wiring ──────────────────────────────────────


def test_swe_tool_server_is_preregistered_in_openwebui_next_to_the_existing_ones():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    env = dict(item.split("=", 1) for item in compose["services"]["openwebui"]["environment"])
    rendered = re.sub(r"\$\{(\w+)(?::-([^}]*))?\}", lambda m: m.group(2) or "",
                      env["TOOL_SERVER_CONNECTIONS"])
    connections = json.loads(rendered)
    assert [c["info"]["id"] for c in connections] == ["qdrant-rag", "fetch", "memory",
                                                        "filesystem", "swe"]
    swe_connection = connections[-1]
    assert swe_connection["url"] == "http://localhost:8200/swe"
    assert swe_connection["info"]["name"] == "Local-EZAI SWE"
    assert swe_connection["config"] == {"enable": True} and swe_connection["auth_type"] == "bearer"
    assert connections[0]["url"] == "http://localhost:8200/qdrant-rag"  # existing entries intact


def test_make_target_and_script_wiring():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "orchestrator: ##" in makefile and "scripts/register-orchestrator.sh" in makefile
    script = (REPO_ROOT / "scripts" / "register-orchestrator.sh").read_text(encoding="utf-8")
    assert "openwebui_orchestrator.py" in script and "config/prompts/orchestrator.md" in script
    assert "/app/backend/data/webui.db" in script and "docker compose restart openwebui" in script
    assert "ORCHESTRATOR_BASE_MODEL" in script
