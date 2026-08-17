"""Configuration system.

Layered, lowest to highest precedence:

1. Pydantic model defaults (below)
2. A YAML config file (``--config``, or ``AGENTD_CONFIG`` env var)
3. Environment variables prefixed ``AGENTD_``, nested with ``__``
   (e.g. ``AGENTD_GIT__ALLOW_PUSH=true``, ``AGENTD_LLM__BASE_URL=...``)

A target repository may additionally carry a ``.agentd.yaml`` at its root;
its ``validation:`` and ``limits:`` sections override the global config for
runs against that repo (merged by :func:`merge_repo_overrides`).

Convention alignment with the existing stack: if ``llm.api_key`` is not set
explicitly, the ``LITELLM_MASTER_KEY`` environment variable (the stack's
master key, see .env.example) is used.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

ENV_PREFIX = "AGENTD_"
REPO_CONFIG_FILENAME = ".agentd.yaml"


class LLMConfig(BaseModel):
    """How agents reach a model. Default: the LiteLLM proxy of the stack."""

    provider: Literal["openai", "scripted"] = "openai"
    #: OpenAI-compatible base URL. The stack default is the LiteLLM proxy;
    #: point directly at the engine (http://localhost:8000/v1) to bypass the
    #: chat-oriented auto-RAG hook for agent runs.
    base_url: str = "http://localhost:4000/v1"
    api_key: str = ""
    #: Path to a JSON list of scripted responses (provider="scripted").
    script_path: Path | None = None
    timeout: float = 180.0
    temperature: float = 0.2
    max_tokens: int = 4096
    #: Bounded retries for transport errors and malformed structured output.
    retries: int = 2
    #: Role → model alias (ADR-007). All roles default to the stack's
    #: default chat model; profiles override per role.
    roles: dict[str, str] = Field(
        default_factory=lambda: {
            "default": "qwen2.5-7b",
            "planner": "qwen2.5-7b",
            "coder": "qwen2.5-7b",
            "validator": "qwen2.5-7b",
            "git": "qwen2.5-7b",
            "debugger": "qwen2.5-7b",
            "memory": "qwen2.5-7b",
            "reviewer": "qwen2.5-7b",
            "chat": "qwen2.5-7b",
            "sprint": "qwen2.5-7b",
            "documentation": "qwen2.5-7b",
            "evolution": "qwen2.5-7b",
        }
    )
    #: Ordered fallback models per role (ADR-020): tried when the primary
    #: fails. Populated from .agent/model_registry.yaml or set directly.
    role_fallbacks: dict[str, list[str]] = Field(default_factory=dict)

    def model_for_role(self, role: str) -> str:
        return self.roles.get(role) or self.roles.get("default") or "qwen2.5-7b"


class LimitsConfig(BaseModel):
    """Budgets enforced by the orchestrator (ADR-010: budgets are code)."""

    max_plan_tasks: int = 8
    max_agent_turns: int = 24
    #: Hard cap on DEBUG → FIX → REVALIDATE self-healing cycles per run.
    max_heal_iterations: int = 10
    #: Abort early when the identical failure signature persists for this
    #: many consecutive validations (symptom-patching detector, ADR-015).
    stall_threshold: int = 3
    tool_output_max_chars: int = 8_000
    recursion_limit: int = 150


class ValidationConfig(BaseModel):
    """What the Validation Agent runs. Keys: test / lint / build."""

    commands: dict[str, list[str]] = Field(default_factory=dict)
    #: When no commands are configured, detect common Python checks.
    autodetect: bool = True
    command_timeout: float = 600.0


class BrowserAppConfig(BaseModel):
    """How to launch the application under test."""

    #: Shell command started in the workspace root; the literal ``{port}``
    #: is replaced by a free port. Empty → the app is assumed to already be
    #: running at ``url`` (externally managed).
    start: str = ""
    #: Base URL of the app; ``{port}`` is replaced like in ``start``.
    url: str = "http://127.0.0.1:{port}"
    #: Path polled until it answers with HTTP < 400.
    ready_path: str = "/"
    startup_timeout: float = 30.0


class BrowserWorkflowSpec(BaseModel):
    """One declarative user workflow (e.g. login, create-customer)."""

    name: str
    steps: list[dict] = Field(min_length=1)


class BrowserQAConfig(BaseModel):
    """Browser QA stage of the validation pipeline (Phase 3, ADR-016).

    Typically supplied per-repo via ``.agentd.yaml``. When enabled, git
    commits are blocked until every workflow passes with zero console
    errors.
    """

    enabled: bool = False
    app: BrowserAppConfig = Field(default_factory=BrowserAppConfig)
    workflows: list[BrowserWorkflowSpec] = Field(default_factory=list)
    headless: bool = True
    #: Per-action timeout in seconds (click/fill/expect_* retries).
    step_timeout: float = 10.0
    #: Explicit Chromium binary. Empty → Playwright's managed browser, with
    #: an automatic fallback to $PLAYWRIGHT_BROWSERS_PATH/chromium when the
    #: managed download is missing (pre-provisioned environments).
    chromium_executable: str = ""
    #: Regexes for console errors to IGNORE (e.g. a known-noisy 404 on an
    #: optional asset). Default empty: ANY console error fails validation.
    ignore_console_patterns: list[str] = Field(default_factory=list)


class ForgeConfig(BaseModel):
    """Pull-request delivery (Evolution workflow, ADR-020). Fail-closed:
    kind 'none' produces a reviewable PR proposal bundle instead of any
    network action."""

    kind: Literal["none", "gh", "api"] = "none"
    #: 'api' kind: forge REST base, e.g. https://api.github.com or
    #: https://gitea.lan/api/v1 (both use POST {api_base}/repos/{repo}/pulls)
    api_base: str = ""
    repo: str = ""  # owner/name
    token_env: str = "FORGE_TOKEN"
    base_branch: str = "main"


class SprintConfig(BaseModel):
    """Autonomous sprint execution (Phase 6, ADR-019)."""

    #: Concurrent task runs within one dependency wave.
    max_parallel: int = 3


class MemoryConfig(BaseModel):
    """Project memory (Phase 4, ADR-017) — persisted in the target repo's
    ``.agent/`` directory (``memory.db`` + ``lessons_learned.json``)."""

    enabled: bool = True
    #: Directory inside the ORIGIN repository (not the worktree).
    dir: str = ".agent"
    #: Max records per section injected into planner/debugger prompts.
    max_context_items: int = 5
    #: After a completed run, ask the LLM to distill up to 3 durable
    #: observations (coding styles / project rules / architecture decisions).
    #: Off by default: recording from run outcomes is fully deterministic.
    distill: bool = False


class GitConfig(BaseModel):
    branch_prefix: str = "swe/"
    remote: str = "origin"
    #: T3 action — fail-closed by default (ADR-008). CLI --push enables it.
    allow_push: bool = False
    #: Use the LLM to write the commit message (else a deterministic template).
    llm_commit_message: bool = False
    user_name: str = "agentd"
    user_email: str = "agentd@local-ezai"


class WorkspaceConfig(BaseModel):
    #: "worktree" (default): work on a git worktree on branch swe/<run-id>,
    #: leaving the user's checkout untouched. "in-place": edit the repo
    #: directly on the current branch (explicit opt-in).
    mode: Literal["worktree", "in-place"] = "worktree"
    root: Path = Field(default_factory=lambda: Path.home() / ".agentd" / "workspaces")


class AgentdConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    browser_qa: BrowserQAConfig = Field(default_factory=BrowserQAConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    sprint: SprintConfig = Field(default_factory=SprintConfig)
    forge: ForgeConfig = Field(default_factory=ForgeConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    runs_dir: Path = Field(default_factory=lambda: Path.home() / ".agentd" / "runs")
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base`` (override wins)."""
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Collect AGENTD_* env vars into a nested dict (``__`` separates levels)."""
    environ = environ if environ is not None else dict(os.environ)
    out: dict[str, Any] = {}
    for key, value in environ.items():
        if not key.startswith(ENV_PREFIX) or key == ENV_PREFIX + "CONFIG":
            continue
        path = key[len(ENV_PREFIX):].lower().split("__")
        cursor = out
        for part in path[:-1]:
            nxt = cursor.setdefault(part, {})
            if not isinstance(nxt, dict):  # scalar set earlier; env wins as dict path
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[path[-1]] = value
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path} must contain a YAML mapping")
    return data


def load_config(
    config_file: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> AgentdConfig:
    """Build the effective configuration (defaults < YAML < environment)."""
    environ = environ if environ is not None else dict(os.environ)
    merged: dict[str, Any] = {}

    path = config_file or environ.get(ENV_PREFIX + "CONFIG")
    if path:
        merged = _deep_merge(merged, _load_yaml(Path(path)))

    merged = _deep_merge(merged, _env_overrides(environ))

    config = AgentdConfig.model_validate(merged)

    # Stack convention: reuse the LiteLLM master key unless set explicitly.
    if not config.llm.api_key:
        config.llm.api_key = environ.get("LITELLM_MASTER_KEY", "sk-ai-service-2024")
    return config


def load_repo_overrides(repo_path: Path) -> dict[str, Any]:
    """Read the target repo's ``.agentd.yaml`` (empty dict if absent)."""
    candidate = repo_path / REPO_CONFIG_FILENAME
    if not candidate.is_file():
        return {}
    return _load_yaml(candidate)


def merge_repo_overrides(config: AgentdConfig, overrides: dict[str, Any]) -> AgentdConfig:
    """Apply a repo's ``validation:`` / ``limits:`` / ``browser_qa:``
    sections onto the config (never ``git:`` — a repo cannot self-grant
    push or change delivery behavior)."""
    allowed = {
        k: v for k, v in overrides.items()
        if k in ("validation", "limits", "browser_qa")
    }
    if not allowed:
        return config
    merged = _deep_merge(config.model_dump(mode="python"), allowed)
    return AgentdConfig.model_validate(merged)
