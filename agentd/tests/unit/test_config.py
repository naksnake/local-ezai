from pathlib import Path

from agentd.config import (
    AgentdConfig,
    load_config,
    load_repo_overrides,
    merge_repo_overrides,
)


def test_defaults():
    cfg = load_config(None, environ={})
    assert cfg.llm.base_url == "http://localhost:4000/v1"
    assert cfg.llm.api_key == "sk-ai-service-2024"  # .env.example convention
    assert cfg.git.allow_push is False
    assert cfg.workspace.mode == "worktree"
    assert cfg.limits.max_fix_attempts == 2


def test_litellm_master_key_fallback():
    cfg = load_config(None, environ={"LITELLM_MASTER_KEY": "sk-secret"})
    assert cfg.llm.api_key == "sk-secret"


def test_yaml_overrides(tmp_path: Path):
    yaml_file = tmp_path / "cfg.yaml"
    yaml_file.write_text(
        "llm:\n  temperature: 0.7\ngit:\n  allow_push: true\n", encoding="utf-8"
    )
    cfg = load_config(yaml_file, environ={})
    assert cfg.llm.temperature == 0.7
    assert cfg.git.allow_push is True
    # untouched sections keep defaults
    assert cfg.limits.max_plan_tasks == 8


def test_env_beats_yaml(tmp_path: Path):
    yaml_file = tmp_path / "cfg.yaml"
    yaml_file.write_text("llm:\n  temperature: 0.7\n", encoding="utf-8")
    cfg = load_config(
        yaml_file,
        environ={"AGENTD_LLM__TEMPERATURE": "0.9", "AGENTD_GIT__ALLOW_PUSH": "true"},
    )
    assert cfg.llm.temperature == 0.9
    assert cfg.git.allow_push is True


def test_env_config_pointer(tmp_path: Path):
    yaml_file = tmp_path / "cfg.yaml"
    yaml_file.write_text("log_level: debug\n", encoding="utf-8")
    cfg = load_config(None, environ={"AGENTD_CONFIG": str(yaml_file)})
    assert cfg.log_level == "DEBUG"  # normalized to upper case


def test_repo_overrides_only_allowed_sections(tmp_path: Path):
    (tmp_path / ".agentd.yaml").write_text(
        "validation:\n  commands:\n    test: ['echo ok']\n"
        "limits:\n  max_fix_attempts: 5\n"
        "git:\n  allow_push: true\n",  # must be ignored — repo cannot self-grant push
        encoding="utf-8",
    )
    base = AgentdConfig()
    merged = merge_repo_overrides(base, load_repo_overrides(tmp_path))
    assert merged.validation.commands == {"test": ["echo ok"]}
    assert merged.limits.max_fix_attempts == 5
    assert merged.git.allow_push is False


def test_repo_overrides_absent(tmp_path: Path):
    base = AgentdConfig()
    assert merge_repo_overrides(base, load_repo_overrides(tmp_path)) == base
