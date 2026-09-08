"""Versioned role prompts (AGENT_DESIGN.md §6)."""

from importlib import resources


def load_prompt(name: str) -> str:
    """Load a prompt file (e.g. 'planner') from this package."""
    return (
        resources.files("agentd.agents.prompts")
        .joinpath(f"{name}.md")
        .read_text(encoding="utf-8")
    )
