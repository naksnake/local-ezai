"""P3 exit criterion 2: governance actions provably absent from the chat
surface — at both layers (PR-15, ADR-029).

Layer 1, the tool catalog: through the real MCP protocol every governance,
lifecycle or cancel verb is an unknown tool, while the catalog answers.
Layer 2, the control plane: a caller identifying as the chat-ops client may
start runs and read; every other mutation is refused (`client_forbidden`)
and audited, whatever a future tool server might grow."""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from agentd.control import API_PREFIX, CLIENT_HEADER, USER_HEADER
from agentd.control.policy import CHAT_OPS_CLIENT, client_may, is_restricted, refusal
from tests.security.conftest import AUTH, wait_terminal

V1 = API_PREFIX
CHAT = {**AUTH, CLIENT_HEADER: CHAT_OPS_CLIENT}
CLI = {**AUTH, CLIENT_HEADER: "cli", USER_HEADER: "nita"}

#: What an attacker would ask the tool server for — none of it exists.
FORBIDDEN_TOOLS = [
    "governance_approve", "governance_reject", "approve", "reject", "approve_request",
    "model_activate", "model_upgrade", "model_rollback", "model_retire", "model_uninstall",
    "model_install", "activate", "rollback", "generation_rollback",
    "swe_cancel", "run_cancel", "cancel", "swe_push", "git_push", "push", "merge", "merge_pr",
    "project_add", "project_remove", "swe_commit", "exec", "shell", "fs_write",
]

#: Every mutating operation of the 1.0.0 contract except starting a run.
FORBIDDEN_CALLS = [
    ("POST", f"{V1}/models", {"ref": "gguf:/nowhere/x.gguf"}),
    ("POST", f"{V1}/models/beta/benchmark", {}),
    ("POST", f"{V1}/models/beta/activate", {"group": "coding"}),
    ("POST", f"{V1}/models/upgrade", {"old": "alpha", "new": "beta"}),
    ("POST", f"{V1}/generations/rollback", {"reason": "x"}),
    ("POST", f"{V1}/models/beta/retire", None),
    ("DELETE", f"{V1}/models/gamma", None),
    ("POST", f"{V1}/governance/cr-0001/approve", {"reason": "yes"}),
    ("POST", f"{V1}/governance/cr-0001/reject", {"reason": "no"}),
    ("POST", f"{V1}/projects", {"path": "/tmp"}),
    ("DELETE", f"{V1}/projects?target=repo", None),
    ("POST", f"{V1}/runs/20260101-000000-abcdef/cancel", None),
]


def call(world, name: str, arguments: dict) -> str:
    """Call a tool through the MCP server and return the text a chat client
    would see (FastMCP returns ``(content_blocks, structured)`` for ``str``
    tools)."""
    result = asyncio.run(world.server.call_tool(name, arguments))
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, dict):
        return json.dumps(result)
    return "".join(getattr(block, "text", "") for block in result)


# ── layer 1: the MCP surface ─────────────────────────────────────────────────


def test_mcp_surface_has_no_governance_verb(world):
    registered = {t.name for t in asyncio.run(world.server.list_tools())}
    assert registered == set(world.swe.TOOLS)
    for name in FORBIDDEN_TOOLS:
        assert name not in registered
        with pytest.raises(ToolError, match="Unknown tool"):
            asyncio.run(world.server.call_tool(name, {}))
    # the catalog answers through the same protocol
    projects = call(world, "swe_projects", {})
    assert "Registered projects" in projects and "| repo |" in projects
    queue = call(world, "governance_queue", {})
    assert "empty" in queue and "never from chat" in queue


def test_tool_arguments_cannot_smuggle_a_mutation(world):
    """Crafted arguments reach nothing: FastMCP validates a call against the
    tool's declared schema and drops what is not declared, the tool posts the
    project name and the task only, and the daemon forces push off."""
    started = call(world, "swe_run", {
        "project": "repo", "task": "x", "push": True, "allow_push": True,
        "branch": "main", "approve": "cr-0001", "activate": "beta"})
    run_id = re.search(r"`(\d{8}-\d{6}-[0-9a-f]{6})`", started).group(1)
    record = world.client.get(f"{V1}/runs/{run_id}", headers=AUTH).json()
    assert record["options"] == {} and record["request"] == "x"
    assert record["actor"] == CHAT_OPS_CLIENT
    assert wait_terminal(world.client, run_id)["status"] == "completed"
    report = world.client.get(f"{V1}/runs/{run_id}/report", headers=AUTH).json()["report"]
    assert report["commit"]["pushed"] is False
    # a path that is not a registered project is refused before anything happens
    refused = call(world, "swe_run", {"project": "../../etc", "task": "x"})
    assert refused.startswith("⚠️ **not_found**") and "project add" in refused
    assert world.ctx.queue.list() == []  # nothing entered the governance queue
    assert not any(r.event.startswith(("request.", "api.governance", "api.model"))
                   for r in world.ctx.queue.audit())


# ── layer 2: the control plane's client policy ───────────────────────────────


def test_policy_table():
    assert is_restricted(CHAT_OPS_CLIENT) and not is_restricted("cli")
    assert client_may(CHAT_OPS_CLIENT, "run_start", "POST")
    assert client_may(CHAT_OPS_CLIENT, "governance_list", "GET")
    assert not client_may(CHAT_OPS_CLIENT, "governance_approve", "POST")
    assert not client_may(CHAT_OPS_CLIENT, "run_cancel", "POST")
    assert not client_may(CHAT_OPS_CLIENT, "model_uninstall", "DELETE")
    assert client_may("cli", "governance_approve", "POST")
    message, fix = refusal(CHAT_OPS_CLIENT, "governance_approve")
    assert "start and inspect runs only" in message and "Admin Center" in fix


@pytest.mark.parametrize("method, path, body", FORBIDDEN_CALLS)
def test_daemon_refuses_every_other_mutation_from_the_chat_ops_client(world, method, path, body):
    before = [r.event for r in world.ctx.queue.audit()]
    response = world.client.request(method, path, headers=CHAT, json=body)
    assert response.status_code == 401, response.text
    error = response.json()["error"]
    assert error["code"] == "client_forbidden" and "Admin Center" in error["fix"]
    # two audit lines and nothing else: the refusal, then the access-log line
    # every authenticated call gets (status 401) — no operation ever ran
    after = world.ctx.queue.audit()
    refused, logged = after[-2], after[-1]
    assert refused.event == "client.forbidden" and refused.actor == CHAT_OPS_CLIENT
    assert refused.details["method"] == method and refused.details["path"] == path.split("?")[0]
    assert logged.event == f"api.{refused.details['operation']}"
    assert logged.details["status"] == 401 and logged.details["client"] == CHAT_OPS_CLIENT
    assert len(after) == len(before) + 2
    assert world.ctx.queue.list() == []


def test_chat_ops_client_may_start_runs_and_read_while_the_cli_governs(world):
    for path in (f"{V1}/models", f"{V1}/governance", f"{V1}/projects", f"{V1}/roles/coder",
                 f"{V1}/generations", f"{V1}/catalog", f"{V1}/runs", f"{V1}/health"):
        assert world.client.get(path, headers=CHAT).status_code == 200, path
    # the CLI (a human) may govern; the same request from the chat-ops client is refused
    proposal = world.client.post(f"{V1}/models/beta/activate", headers=CLI,
                                 json={"group": "coding"})
    assert proposal.status_code == 200 and proposal.json()["request"]["id"] == "cr-0001"
    chat_approve = world.client.post(f"{V1}/governance/cr-0001/approve", headers=CHAT, json={})
    assert chat_approve.status_code == 401
    assert chat_approve.json()["error"]["code"] == "client_forbidden"
    assert world.ctx.queue.get("cr-0001").status == "pending"
    cli_approve = world.client.post(f"{V1}/governance/cr-0001/approve", headers=CLI,
                                    json={"reason": "reviewed"})
    assert cli_approve.status_code == 200 and cli_approve.json()["applied"]["ok"]
    # a start from chat is allowed and audited as the chat-ops actor
    started = world.client.post(f"{V1}/runs", headers=CHAT,
                                json={"kind": "plan", "project": "repo", "task": "look around"})
    assert started.status_code == 202
    assert ("api.run_start", CHAT_OPS_CLIENT) in [(r.event, r.actor)
                                                  for r in world.ctx.queue.audit()]
