"""Model governance registry (CLAUDE.md "Model Routing", ADR-020).

A repository may carry ``.agent/model_registry.yaml`` declaring per-role
primary/fallback models:

    agent_model_map:          # the top-level key is optional
      planner:
        primary: hermes3
        fallback: deepseek-r1   # string or list
      reviewer:
        primary: llama3

The registry is merged over the global config for every run against that
repository: ``llm.roles[role] = primary`` and
``llm.role_fallbacks[role] = [fallback, ...]``. The LLM client walks the
fallback chain when a model fails (see llm.py); ``local-ezai
evaluate-models`` probes every routed role and records the results.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentd.config import AgentdConfig
from agentd.logging_setup import get_logger

log = get_logger("model_registry")

REGISTRY_FILENAME = "model_registry.yaml"


def load_model_registry(agent_dir: Path) -> dict[str, dict[str, Any]] | None:
    """Parse ``.agent/model_registry.yaml``; None when absent."""
    path = Path(agent_dir) / REGISTRY_FILENAME
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    mapping = data.get("agent_model_map", data)
    if not isinstance(mapping, dict):
        raise ValueError(f"{path}: agent_model_map must be a mapping")

    registry: dict[str, dict[str, Any]] = {}
    for role, spec in mapping.items():
        if isinstance(spec, str):  # shorthand: role: model
            registry[str(role)] = {"primary": spec, "fallback": []}
            continue
        if not isinstance(spec, dict) or "primary" not in spec:
            raise ValueError(
                f"{path}: role '{role}' needs a 'primary' model"
            )
        fallback = spec.get("fallback") or []
        if isinstance(fallback, str):
            fallback = [fallback]
        registry[str(role)] = {"primary": str(spec["primary"]),
                               "fallback": [str(f) for f in fallback]}
    return registry


def apply_model_registry(config: AgentdConfig, repo_root: Path) -> AgentdConfig:
    """Merge a repo's model registry into the effective config (repo wins)."""
    agent_dir = Path(repo_root) / config.memory.dir
    registry = load_model_registry(agent_dir)
    if not registry:
        return config
    config = config.model_copy(deep=True)
    for role, spec in registry.items():
        config.llm.roles[role] = spec["primary"]
        if spec["fallback"]:
            config.llm.role_fallbacks[role] = spec["fallback"]
    log.info("model registry applied: %s",
             {r: s["primary"] for r, s in registry.items()})
    return config
