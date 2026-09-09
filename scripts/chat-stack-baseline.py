#!/usr/bin/env python3
"""The chat/RAG stack's byte-identical baseline (V1 P3 close, PR-15, ADR-029;
V1_IMPLEMENTATION_PLAN §P3 exit criterion 4, ADR-002).

Records the surface the chat experience depends on — the auto-RAG hook, the
search-engine settings, the pre-existing mcpo servers, OpenWebUI's
environment and its pre-existing tool connections, and the chat services'
compose definitions — as ``agentd/tests/fixtures/chat_stack_baseline.json``.
The regression test (``agentd/tests/security/test_chat_stack_regression.py``)
recomputes the same snapshot and fails on ANY difference, so a change to
the chat stack is a deliberate act: change the files, run this script with
``--update``, review the diff of the baseline in the same PR.

    python3 scripts/chat-stack-baseline.py            # print the snapshot
    python3 scripts/chat-stack-baseline.py --update   # rewrite the baseline
    python3 scripts/chat-stack-baseline.py --check    # exit 1 on drift
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE = REPO_ROOT / "agentd" / "tests" / "fixtures" / "chat_stack_baseline.json"

#: Files whose bytes ARE the chat behavior.
HASHED_FILES = ("config/litellm_custom_callbacks.py", "config/searxng/settings.yml")
#: mcpo servers that existed before the SWE tool server (PR-13).
CHAT_MCP_SERVERS = ("filesystem", "memory", "fetch", "qdrant-rag")
#: Services of the chat/RAG stack (the engine slot and the control plane are V1 planes).
CHAT_SERVICES = ("openwebui", "litellm", "embed-server", "qdrant", "searxng", "mcpo", "monitor")
#: mcpo environment keys that existed before PR-13 (the SWE keys are additive).
MCPO_CHAT_ENV = ("MCP_API_KEY", "RAG_COLLECTION")
#: Control-plane wiring of the monitor's Admin Center pages (PR-16) — additive
#: to the dashboard the chat experience relies on (health view, KB upload).
CONTROL_ENV_PREFIX = "EZAI_CONTROL_"
#: OpenWebUI tool-server connections that existed before PR-14.
CHAT_TOOL_CONNECTIONS = ("qdrant-rag", "fetch", "memory", "filesystem")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_placeholders(text: str) -> str:
    """``${VAR:-default}`` → default (the compose/entrypoint convention)."""
    return re.sub(r"\$\{(\w+)(?::-([^}]*))?\}", lambda m: m.group(2) or "", text)


def env_map(service: dict[str, Any]) -> dict[str, str]:
    env = service.get("environment", [])
    if isinstance(env, dict):
        return {k: str(v) for k, v in env.items()}
    return dict(item.split("=", 1) if "=" in item else (item, "") for item in env)


def snapshot(root: Path = REPO_ROOT) -> dict[str, Any]:
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    mcpo_config = json.loads((root / "config" / "mcpo-config.json").read_text(encoding="utf-8"))
    services: dict[str, Any] = {}
    for name in CHAT_SERVICES:
        service = dict(compose["services"][name])
        env = env_map(service)
        if name == "openwebui":
            connections = json.loads(render_placeholders(env.pop("TOOL_SERVER_CONNECTIONS", "[]")))
            service["tool_server_connections"] = [
                c for c in connections if c["info"]["id"] in CHAT_TOOL_CONNECTIONS]
        if name == "mcpo":
            env = {k: v for k, v in env.items() if k in MCPO_CHAT_ENV}
            service.pop("extra_hosts", None)  # PR-13 additive reachability for a host daemon
        if name == "monitor":
            env = {k: v for k, v in env.items() if not k.startswith(CONTROL_ENV_PREFIX)}
            service.pop("extra_hosts", None)  # PR-16 additive reachability for a host daemon
        service["environment"] = env
        services[name] = service
    return {
        "files": {rel: sha256(root / rel) for rel in HASHED_FILES},
        "mcp_servers": {name: mcpo_config["mcpServers"][name] for name in CHAT_MCP_SERVERS},
        "services": services,
    }


def load_baseline(path: Path = BASELINE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    current = snapshot()
    if "--update" in args:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(dump(current), encoding="utf-8")
        print(f"wrote {BASELINE.relative_to(REPO_ROOT)}")
        return 0
    if "--check" in args:
        if not BASELINE.is_file():
            print(f"no baseline at {BASELINE} — run with --update", file=sys.stderr)
            return 2
        if load_baseline() != current:
            print("chat-stack surface drifted from the baseline — review the change, then "
                  "`python3 scripts/chat-stack-baseline.py --update` in the same PR",
                  file=sys.stderr)
            return 1
        print("chat-stack surface matches the baseline")
        return 0
    print(dump(current), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
