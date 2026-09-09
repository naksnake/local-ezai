"""P3 exit criterion 4: existing chat / RAG / tool behavior byte-identical.

The recorded baseline (``tests/fixtures/chat_stack_baseline.json``, written
by ``scripts/chat-stack-baseline.py --update``) captures the chat stack's
surface — the auto-RAG hook, the search-engine settings, the pre-existing
mcpo servers, OpenWebUI's environment and its pre-existing tool
connections, the chat services' compose definitions. Any drift fails here;
a deliberate change regenerates the baseline in the same PR."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def load_tool():
    spec = importlib.util.spec_from_file_location(
        "chat_stack_baseline", REPO_ROOT / "scripts" / "chat-stack-baseline.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


baseline_tool = load_tool()


def test_chat_stack_surface_matches_the_recorded_baseline():
    current = baseline_tool.snapshot()
    recorded = baseline_tool.load_baseline()
    assert current == recorded, (
        "the chat/RAG stack surface drifted (ADR-002: the chat experience stays byte-identical) "
        "— if intended, review the change and run `python3 scripts/chat-stack-baseline.py "
        "--update` in the same PR")


def test_baseline_covers_the_chat_experience():
    recorded = baseline_tool.load_baseline()
    assert set(recorded["files"]) == {"config/litellm_custom_callbacks.py",
                                      "config/searxng/settings.yml"}
    assert set(recorded["mcp_servers"]) == {"filesystem", "memory", "fetch", "qdrant-rag"}
    assert set(recorded["services"]) == {"openwebui", "litellm", "embed-server", "qdrant",
                                         "searxng", "mcpo", "monitor"}
    openwebui = recorded["services"]["openwebui"]
    assert [c["info"]["id"] for c in openwebui["tool_server_connections"]] == [
        "qdrant-rag", "fetch", "memory", "filesystem"]  # the SWE connection is additive (PR-14)
    assert openwebui["environment"]["OPENAI_API_BASE_URL"] == "http://litellm:4000/v1"
    assert "auto_rag" in (REPO_ROOT / "config" / "litellm_custom_callbacks.py").read_text()
    # the LiteLLM service mounts the rendered config (P1 cutover) — part of the recorded surface
    assert any("config/rendered/litellm-config.yaml" in v
               for v in recorded["services"]["litellm"]["volumes"])


def test_drift_is_detected(tmp_path):
    """A one-character change to a chat-stack file changes the snapshot."""
    root = tmp_path / "repo"
    for rel in ("docker-compose.yml", "config/mcpo-config.json",
                "config/litellm_custom_callbacks.py", "config/searxng/settings.yml"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, root / rel)
    assert baseline_tool.snapshot(root) == baseline_tool.snapshot()
    hook = root / "config" / "litellm_custom_callbacks.py"
    hook.write_text(hook.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    drifted = baseline_tool.snapshot(root)
    assert drifted != baseline_tool.snapshot()
    assert drifted["files"]["config/litellm_custom_callbacks.py"] != \
        baseline_tool.snapshot()["files"]["config/litellm_custom_callbacks.py"]
    compose = json.loads(json.dumps(baseline_tool.snapshot()))  # deep copy
    assert compose["services"]["mcpo"]["environment"].keys() <= {"MCP_API_KEY", "RAG_COLLECTION"}
    # the monitor's control-plane wiring (Admin Center, PR-16) is additive too
    assert not any(k.startswith("EZAI_CONTROL_")
                   for k in compose["services"]["monitor"]["environment"])
    assert "extra_hosts" not in compose["services"]["monitor"]
