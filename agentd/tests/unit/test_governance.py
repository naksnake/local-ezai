"""Model governance (registry, fallback routing, evaluation), forge PR
delivery, and the Documentation Agent."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agentd.agents import DocumentationAgent
from agentd.config import AgentdConfig, LLMConfig
from agentd.evaluate import PROBES, evaluate_models
from agentd.forge import create_pull_request
from agentd.journal import NullJournal
from agentd.llm import LLMError, LLMResponse, OpenAICompatLLM, ScriptedLLM
from agentd.model_registry import apply_model_registry, load_model_registry
from agentd.runner import build_registry

REGISTRY_YAML = """\
agent_model_map:
  planner:
    primary: hermes3
    fallback: deepseek-r1
  coder:
    primary: qwen3-coder
    fallback: [deepseek-r1, hermes3]
  reviewer:
    primary: llama3
  memory: hermes3
"""


# ── registry parsing / application ───────────────────────────────────────────


def test_registry_parses_all_forms(tmp_path):
    (tmp_path / "model_registry.yaml").write_text(REGISTRY_YAML)
    registry = load_model_registry(tmp_path)
    assert registry["planner"] == {"primary": "hermes3",
                                   "fallback": ["deepseek-r1"]}
    assert registry["coder"]["fallback"] == ["deepseek-r1", "hermes3"]
    assert registry["reviewer"] == {"primary": "llama3", "fallback": []}
    assert registry["memory"]["primary"] == "hermes3"  # shorthand string


def test_registry_bare_mapping_without_top_key(tmp_path):
    (tmp_path / "model_registry.yaml").write_text(
        "planner:\n  primary: hermes3\n")
    assert load_model_registry(tmp_path)["planner"]["primary"] == "hermes3"


def test_registry_missing_primary_rejected(tmp_path):
    (tmp_path / "model_registry.yaml").write_text(
        "planner:\n  fallback: x\n")
    with pytest.raises(ValueError, match="needs a 'primary'"):
        load_model_registry(tmp_path)


def test_registry_absent_returns_none(tmp_path):
    assert load_model_registry(tmp_path) is None


def test_apply_model_registry(tmp_path):
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    (agent_dir / "model_registry.yaml").write_text(REGISTRY_YAML)
    config = AgentdConfig()
    merged = apply_model_registry(config, tmp_path)
    assert merged.llm.roles["planner"] == "hermes3"
    assert merged.llm.role_fallbacks["planner"] == ["deepseek-r1"]
    assert merged.llm.roles["coder"] == "qwen3-coder"
    assert "reviewer" not in merged.llm.role_fallbacks  # no fallback declared
    # original untouched; unrelated roles keep defaults
    assert config.llm.roles["planner"] == "qwen2.5-7b"
    assert merged.llm.roles["debugger"] == "qwen2.5-7b"


def test_repo_registry_applied_by_prepare_run(config, tmp_repo):
    from agentd.runner import prepare_run

    agent_dir = tmp_repo / ".agent"
    agent_dir.mkdir()
    (agent_dir / "model_registry.yaml").write_text(REGISTRY_YAML)
    run_config, _, _ = prepare_run(config, tmp_repo, "reg1")
    assert run_config.llm.roles["planner"] == "hermes3"
    assert run_config.llm.role_fallbacks["coder"] == ["deepseek-r1", "hermes3"]


# ── fallback chain in the LLM client ─────────────────────────────────────────


def make_flaky_client(failing_models, monkeypatch, on_fallback=None):
    config = LLMConfig(roles={"default": "primary-model",
                              "planner": "primary-model"},
                       role_fallbacks={"planner": ["backup-1", "backup-2"]})
    client = OpenAICompatLLM(config, on_fallback=on_fallback)
    calls = []

    def fake_chat_model(self, model, messages, tools=None):
        calls.append(model)
        if model in failing_models:
            raise LLMError(f"{model} unavailable")
        return LLMResponse(content=f"answer from {model}")

    monkeypatch.setattr(OpenAICompatLLM, "_chat_model", fake_chat_model)
    return client, calls


def test_fallback_chain_used_in_order(monkeypatch):
    events = []
    client, calls = make_flaky_client(
        {"primary-model", "backup-1"}, monkeypatch,
        on_fallback=lambda *a: events.append(a))
    response = client.chat("planner", [])
    assert response.content == "answer from backup-2"
    assert calls == ["primary-model", "backup-1", "backup-2"]
    assert client.fallbacks_used == 2
    assert events[0][0] == "planner" and events[0][1] == "primary-model"


def test_primary_success_skips_fallbacks(monkeypatch):
    client, calls = make_flaky_client(set(), monkeypatch)
    assert client.chat("planner", []).content == "answer from primary-model"
    assert calls == ["primary-model"]


def test_all_models_failing_raises_last_error(monkeypatch):
    client, calls = make_flaky_client(
        {"primary-model", "backup-1", "backup-2"}, monkeypatch)
    with pytest.raises(LLMError, match="backup-2 unavailable"):
        client.chat("planner", [])
    assert len(calls) == 3


def test_role_without_fallbacks_fails_directly(monkeypatch):
    client, calls = make_flaky_client({"primary-model"}, monkeypatch)
    with pytest.raises(LLMError):
        client.chat("default", [])
    assert calls == ["primary-model"]


# ── evaluate-models ───────────────────────────────────────────────────────────


def _probe_responses(bad_roles=()):
    responses = []
    for role, (_, expects_json) in PROBES.items():
        if role in bad_roles:
            responses.append({"content": "not json at all"})
        elif expects_json:
            responses.append({"content": '{"probe": true, "goal": "g", '
                                         '"tasks": [], "improvements": [], '
                                         '"verdict": "approve", '
                                         '"root_cause": "r", '
                                         '"findings": [], '
                                         '"confidence": "high"}'})
        else:
            responses.append({"content": "ready"})
    return responses


def test_evaluate_models_all_pass(config, tmp_repo):
    report = evaluate_models(config, tmp_repo,
                             llm=ScriptedLLM(_probe_responses()))
    assert report.passed
    assert len(report.results) == len(PROBES)
    benchmarks = json.loads(
        (tmp_repo / ".agent" / "model_benchmarks.json").read_text())
    assert benchmarks["passed"] is True
    assert {r["role"] for r in benchmarks["results"]} == set(PROBES)


def test_evaluate_models_flags_bad_json_role(config, tmp_repo):
    report = evaluate_models(config, tmp_repo,
                             llm=ScriptedLLM(_probe_responses(
                                 bad_roles={"planner"})))
    assert not report.passed
    planner = next(r for r in report.results if r.role == "planner")
    assert not planner.ok and "ValueError" in planner.error


# ── forge PR delivery ─────────────────────────────────────────────────────────


def test_forge_none_writes_bundle(config, tmp_path):
    result = create_pull_request(config, tmp_path, "evolve/x",
                                 "evolve: better docs", "body text", tmp_path)
    assert not result.created
    bundle = (tmp_path / "PR_PROPOSAL.md").read_text()
    assert "evolve: better docs" in bundle
    assert "`evolve/x`" in bundle
    assert "git push -u origin evolve/x" in bundle
    assert result.bundle_path.endswith("PR_PROPOSAL.md")


def test_forge_api_missing_token(config, tmp_path, monkeypatch):
    monkeypatch.delenv("FORGE_TOKEN", raising=False)
    config.forge.kind = "api"
    config.forge.api_base = "http://forge.local/api/v1"
    config.forge.repo = "team/app"
    result = create_pull_request(config, tmp_path, "b", "t", "b", tmp_path)
    assert not result.created and "FORGE_TOKEN" in result.note


def test_forge_api_creates_pr_via_rest(config, tmp_path, monkeypatch):
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["path"] = self.path
            received["auth"] = self.headers.get("Authorization")
            length = int(self.headers["Content-Length"])
            received["body"] = json.loads(self.rfile.read(length))
            payload = json.dumps(
                {"html_url": "http://forge.local/team/app/pulls/7"}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("FORGE_TOKEN", "sekret")
        config.forge.kind = "api"
        config.forge.api_base = f"http://127.0.0.1:{server.server_port}"
        config.forge.repo = "team/app"
        result = create_pull_request(config, tmp_path, "evolve/x",
                                     "evolve: t", "the body", tmp_path)
    finally:
        server.shutdown()
    assert result.created
    assert result.url == "http://forge.local/team/app/pulls/7"
    assert received["path"] == "/repos/team/app/pulls"
    assert received["auth"] == "token sekret"
    assert received["body"]["head"] == "evolve/x"
    assert received["body"]["base"] == "main"


def test_forge_gh_cli_with_fake_binary(config, tmp_path, monkeypatch):
    fake_gh = tmp_path / "bin" / "gh"
    fake_gh.parent.mkdir()
    fake_gh.write_text("#!/bin/sh\necho https://github.com/team/app/pull/9\n")
    fake_gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_gh.parent}:" +
                       subprocess.os.environ["PATH"])
    config.forge.kind = "gh"
    result = create_pull_request(config, tmp_path, "b", "t", "body", tmp_path)
    assert result.created
    assert result.url == "https://github.com/team/app/pull/9"


# ── Documentation Agent ───────────────────────────────────────────────────────


GUIDE_SCRIPT = [
    {"tool_calls": [
        {"name": "fs_write", "arguments": {
            "path": "docs/USER_GUIDE.md",
            "content": "# User Guide\n\nHow to use this project.\n"}},
        {"name": "fs_write", "arguments": {
            "path": "docs/RELEASE_NOTES.md",
            "content": "# Release Notes\n\n## 2026-08-17\n- initial\n"}},
    ]},
    {"content": "Created USER_GUIDE and RELEASE_NOTES grounded in the repo."},
]


def test_documentation_agent_writes_guides(config, inplace_ws):
    agent = DocumentationAgent(config, ScriptedLLM(GUIDE_SCRIPT),
                               build_registry(config, NullJournal()),
                               NullJournal())
    result = agent.run(inplace_ws)
    assert result.status == "done"
    assert "docs/USER_GUIDE.md" in result.files_written
    assert "docs/RELEASE_NOTES.md" in result.files_written
    assert (inplace_ws.root / "docs" / "USER_GUIDE.md").is_file()


def test_documentation_agent_reports_missing_guides_in_prompt(config, inplace_ws):
    llm = ScriptedLLM(GUIDE_SCRIPT)
    agent = DocumentationAgent(config, llm,
                               build_registry(config, NullJournal()),
                               NullJournal())
    agent.run(inplace_ws, focus="user guide first")
    prompt = llm.calls[0]["messages"][1]["content"]
    assert "Guides missing: USER_GUIDE.md" in prompt
    assert "user guide first" in prompt


def test_documentation_agent_failed_marker(config, inplace_ws):
    agent = DocumentationAgent(config,
                               ScriptedLLM([{"content": "FAILED: no access"}]),
                               build_registry(config, NullJournal()),
                               NullJournal())
    assert agent.run(inplace_ws).status == "failed"
