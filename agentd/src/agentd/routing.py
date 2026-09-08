"""Effective model routing for a run (PR-6, ADR-026 R-1 / ADR-007 completed;
docs/MODEL_ROUTING_DESIGN.md §5).

Three layers, lowest to highest precedence::

    1. code defaults      role → ``role-<role>`` alias      (no model name in code)
    2. platform routing   Registry v2 → concrete primary + fallback chain
                          (the rendered role map when present, else the
                          registry's own resolution)
    3. per-repo override  ``.agent/model_registry.yaml`` (ADR-020) — a role it
                          declares is taken EXACTLY as declared (a pin is an
                          explicit chain: the platform's fallbacks for that
                          role are not inherited)

The platform is found through ``platform.config_dir`` (config / env
``AGENTD_PLATFORM__CONFIG_DIR``) or by walking up from the project to a
directory holding ``docker-compose.yml`` and ``config/providers/`` (the
self-hosting case). Nothing is guessed from the package location or the
current directory, so runs against arbitrary repositories stay hermetic.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from agentd.config import AgentdConfig
from agentd.logging_setup import get_logger
from agentd.model_registry import load_model_registry, parse_agent_model_map
from agentd.registry_v2 import RegistryError, load_registry, registry_path
from agentd.render import RENDERED_DIRNAME, ROLE_MAP_FILENAME

log = get_logger("routing")

PLATFORM_ENV = "AGENTD_PLATFORM__CONFIG_DIR"
PLATFORM_MARKERS = ("docker-compose.yml", "config/providers")


def is_platform_root(path: Path) -> bool:
    return all((path / marker).exists() for marker in PLATFORM_MARKERS)


def find_platform_config(config: AgentdConfig, project: Path | None = None,
                         environ: Mapping[str, str] | None = None) -> Path | None:
    """The platform's ``config/`` directory, or None when this run is not
    attached to a platform (aliases stay as they are)."""
    explicit = config.platform.config_dir or (os.environ if environ is None
                                              else environ).get(PLATFORM_ENV)
    if explicit:
        return Path(explicit).expanduser().resolve()
    if project is not None:
        start = Path(project).expanduser().resolve()
        for candidate in (start, *start.parents):
            if is_platform_root(candidate):
                return candidate / "config"
    return None


def platform_role_map(config_dir: Path) -> tuple[dict[str, dict[str, Any]], str] | None:
    """The platform's role → primary/fallback map and where it came from:
    the rendered ``role_map.yaml`` (what LiteLLM serves after cutover) when
    present, else the registry's resolution; None before bootstrap."""
    rendered = Path(config_dir) / RENDERED_DIRNAME / ROLE_MAP_FILENAME
    if rendered.is_file():
        data = yaml.safe_load(rendered.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict) and isinstance(data.get("agent_model_map"), dict):
            mapping = parse_agent_model_map(data["agent_model_map"], str(rendered))
            return mapping, f"rendered role map (generation {data.get('generation', '?')})"
    if registry_path(config_dir).is_file():
        try:
            registry = load_registry(config_dir)
            roles, fallbacks = registry.role_map()
        except RegistryError as exc:
            log.warning("platform registry unusable for routing: %s", exc)
            return None
        mapping = {role: {"primary": primary, "fallback": list(fallbacks.get(role, []))}
                   for role, primary in roles.items()}
        return mapping, f"registry generation {registry.generation}"
    return None


def apply_platform_routing(config: AgentdConfig, config_dir: Path | None, *,
                           skip_roles: set[str] | None = None) -> tuple[AgentdConfig, str | None]:
    """Seed ``llm.roles`` / ``llm.role_fallbacks`` from the platform (layer
    2). Roles in ``skip_roles`` (declared by the repo, layer 3) are left to
    the repo so a repo pin is an exact chain. Returns (config, source)."""
    if config_dir is None:
        return config, None
    found = platform_role_map(config_dir)
    if found is None:
        return config, None
    mapping, source = found
    config = config.model_copy(deep=True)
    for role, spec in mapping.items():
        if skip_roles and role in skip_roles:
            continue
        config.llm.roles[role] = spec["primary"]
        if spec["fallback"]:
            config.llm.role_fallbacks[role] = list(spec["fallback"])
        else:
            config.llm.role_fallbacks.pop(role, None)
    log.info("platform routing applied from %s", source)
    return config, source


def effective_routing(config: AgentdConfig, repo_root: Path, *,
                      project: Path | None = None) -> tuple[AgentdConfig, list[str]]:
    """Layers 1–3 for a repository: aliases < platform < repo registry.
    Returns the effective config and the sources applied (for display)."""
    from agentd.model_registry import apply_model_registry

    sources: list[str] = []
    repo_map = load_model_registry(Path(repo_root) / config.memory.dir) or {}
    config_dir = find_platform_config(config, project or repo_root)
    config, source = apply_platform_routing(config, config_dir, skip_roles=set(repo_map))
    if source:
        sources.append(f"platform: {source}")
    if repo_map:
        config = apply_model_registry(config, repo_root)
        sources.append(f"repo: {Path(repo_root) / config.memory.dir / 'model_registry.yaml'}")
    return config, sources
