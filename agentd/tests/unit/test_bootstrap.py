"""Bootstrap: .env seeds → generation 1 (PR-7, ADR-027 accepted;
docs/FINAL_FIRST_RUN_EXPERIENCE.md §2–§3, F8/F10/F11).

Offline: fetchers, validators, benchmark, reload and health are fakes; the
platform lives in tmp_path. Model names are fixtures except where the
shipped .env.example is the subject."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from agentd.activation import Platform, apply, propose
from agentd.bootstrap import (
    CONSUMED_KEY,
    BootstrapError,
    Seeds,
    bootstrap,
    parse_env,
    plan_generation,
    read_seeds,
    stamp_env,
    validate_seeds,
)
from agentd.capability import CapabilityVector
from agentd.catalog import packaged_catalog
from agentd.governance import GovernanceQueue
from agentd.lifecycle import LifecycleError
from agentd.registry_v2 import RegistryV2, list_generations, load_registry, save_generation
from agentd.render import ENGINE_COMPOSE_FILENAME, LITELLM_FILENAME, PRESET_FILENAME
from agentd.runtime_descriptor import load_descriptors
from tests.unit.test_lifecycle import FakeFetcherFactory, ok_validator

REPO_ROOT = Path(__file__).resolve().parents[3]
DESCRIPTORS = load_descriptors(REPO_ROOT / "config")
CATALOG = packaged_catalog()
CPU_LOW = CapabilityVector(system_memory_gb=15.5, cpu_cores=4, cpu_flags=["avx2"])
ACCEL_LARGE = CapabilityVector(accelerator="cuda", accel_memory_gb=24, system_memory_gb=64,
                               cpu_cores=16, cpu_flags=["avx2"])

NEW_ENV = """
AI_RUNTIME=llamacpp
REASONING_MODEL=gguf:{root}/w/reasoner.gguf
CODING_MODEL=gguf:{root}/w/coder.gguf   # inline comment
CHAT_MODEL="gguf:{root}/w/chatter.gguf"
EZAI_ROLE_PIN_reviewer=coder
LITELLM_MASTER_KEY=sk-x
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A fresh platform: descriptors, compose marker, three tiny GGUF files,
    no registry."""
    root = tmp_path / "platform"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config" / "providers", root / "config" / "providers")
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / "w").mkdir()
    for name in ("reasoner", "coder", "chatter"):
        (root / "w" / f"{name}.gguf").write_bytes(name.encode() * 512)
    return root


def platform_for(root: Path, vector: CapabilityVector = CPU_LOW,
                 klass: str = "cpu-low") -> Platform:
    return Platform(config_dir=root / "config", platform_root=root, descriptors=DESCRIPTORS,
                    vector=vector, capability_class=klass, accelerator=vector.accelerator)


def fake_benchmark(registry: RegistryV2, name: str) -> RegistryV2:
    updated = registry.model_copy(deep=True)
    updated.models[name].state = "benchmarked"
    updated.models[name].benchmarks = {"tokens_per_s": 9.0, "via": "fake"}
    return updated


def run_bootstrap(root: Path, env_text: str, **kwargs):
    env_path = root / ".env"
    env_path.write_text(env_text)
    seeds = read_seeds(parse_env(env_text))
    platform = kwargs.pop("platform", platform_for(root))
    benchmark_fn = kwargs.pop("benchmark_fn", fake_benchmark)
    return bootstrap(seeds, platform, GovernanceQueue(root / "config"), CATALOG, actor="nita",
                     validator=ok_validator, env_path=env_path,
                     fetcher_factory=FakeFetcherFactory(b"w" * 64),
                     benchmark_fn=benchmark_fn, **kwargs), seeds


# ── .env parsing and seeds ───────────────────────────────────────────────────


def test_parse_env_handles_quotes_comments_and_export():
    env = parse_env("# c\nexport A=1\nB='two words'\nC=\"q\"\nD=val # trailing\n\nE=\n")
    assert env == {"A": "1", "B": "two words", "C": "q", "D": "val", "E": ""}


def test_read_seeds_new_form_with_pin(root):
    seeds = read_seeds(parse_env(NEW_ENV.format(root=root)))
    assert seeds.runtime == "llamacpp" and not seeds.problems and seeds.migrated_from is None
    assert set(seeds.models) == {"reasoning", "coding", "chat"}
    assert seeds.models["coding"].ref == f"gguf:{root}/w/coder.gguf"  # inline comment stripped
    assert seeds.models["chat"].ref == f"gguf:{root}/w/chatter.gguf"  # quotes stripped
    assert seeds.pins == {"reviewer": "coder"}
    assert seeds.consumed is None


def test_read_seeds_missing_group_is_a_problem_with_the_fix():
    seeds = read_seeds({"AI_RUNTIME": "llamacpp", "REASONING_MODEL": "auto",
                        "CHAT_MODEL": "not a ref!"})
    assert any(p.startswith("CODING_MODEL is missing") for p in seeds.problems)
    assert any("CHAT_MODEL is not a model reference (not a ref!)" in p for p in seeds.problems)
    assert all("hf:<org/repo>" in p for p in seeds.problems)


def test_read_seeds_detects_consumption_stamp():
    seeds = read_seeds({"AI_RUNTIME": "vllm", "REASONING_MODEL": "auto", "CODING_MODEL": "auto",
                        "CHAT_MODEL": "auto", CONSUMED_KEY: "1@2026-09-08T00:00:00"})
    assert seeds.consumed == "1@2026-09-08T00:00:00"


# ── F11: legacy families migrate ─────────────────────────────────────────────


def test_legacy_accelerator_family_migrates_with_its_served_name():
    seeds = read_seeds({"CHAT_MODEL": "Org/Chat-7B-Instruct", "CHAT_MODEL_NAME": "chat-7b",
                        "MAX_MODEL_LEN": "4096"})
    assert seeds.migrated_from == "accelerator-hub" and seeds.runtime == "vllm"
    assert {s.ref for s in seeds.models.values()} == {"hf:Org/Chat-7B-Instruct"}
    assert all(s.name == "chat-7b" for s in seeds.models.values())  # existing chats keep it
    assert all(s.origin == "legacy:accelerator-hub" for s in seeds.models.values())


def test_legacy_cpu_family_and_explicit_runtime_win_order(tmp_path):
    seeds = read_seeds({"CPU_CHAT_MODEL": "Org/Small-1.5B", "CPU_CHAT_MODEL_NAME": "small",
                        "CHAT_MODEL": "Org/Big-7B"})
    assert seeds.migrated_from == "cpu-hub" and seeds.models["chat"].ref == "hf:Org/Small-1.5B"
    forced = read_seeds({"AI_RUNTIME": "llamacpp", "CPU_CHAT_MODEL": "Org/Small-1.5B"})
    assert forced.runtime == "llamacpp"  # AI_RUNTIME always wins over the family default


def test_legacy_gguf_family_prefers_the_downloaded_file(tmp_path):
    gguf_dir = tmp_path / "gguf"
    gguf_dir.mkdir()
    (gguf_dir / "small-q4.gguf").write_bytes(b"g")
    local = read_seeds({"N97_GGUF_DIR": str(gguf_dir), "N97_MODEL_FILE": "small-q4.gguf",
                        "N97_GGUF_REPO": "Org/Small-GGUF", "N97_MODEL_NAME": "small"})
    assert local.migrated_from == "low-power-gguf" and local.runtime == "llamacpp"
    assert local.models["chat"].ref == f"gguf:{gguf_dir / 'small-q4.gguf'}"
    assert local.models["chat"].name == "small"
    remote = read_seeds({"N97_GGUF_DIR": str(tmp_path / "nowhere"),
                         "N97_MODEL_FILE": "small-q4.gguf", "N97_GGUF_REPO": "Org/Small-GGUF"})
    assert remote.models["chat"].ref == "gguf:hf://Org/Small-GGUF/small-q4.gguf"


def test_no_seeds_at_all_is_one_clear_problem():
    seeds = read_seeds({"LITELLM_MASTER_KEY": "x"})
    assert seeds.models == {} and len(seeds.problems) == 1
    assert "no model seeds found" in seeds.problems[0]


def test_shipped_env_example_bootstraps_by_migration(root):
    """Tripwire: the repository's .env.example must always yield valid seeds
    (today: the legacy accelerator family, migrated) on an accelerator host."""
    env = parse_env((REPO_ROOT / ".env.example").read_text(encoding="utf-8"))
    seeds = read_seeds(env)
    assert seeds.migrated_from == "accelerator-hub" and seeds.runtime == "vllm"
    assert len(seeds.distinct_refs()) == 1 and seeds.models["chat"].ref.startswith("hf:")
    assert seeds.models["chat"].name == env["CHAT_MODEL_NAME"]
    assert validate_seeds(seeds, DESCRIPTORS, CATALOG,
                          platform_for(root, ACCEL_LARGE, "accel-large")) == []


# ── F8: validation names every problem and its fix, before any download ──────


def seeds_with(**models) -> Seeds:
    from agentd.bootstrap import ModelSeed

    runtime = models.pop("runtime", "llamacpp")
    return Seeds(runtime=runtime, models={
        group: ModelSeed(f"{group.upper()}_MODEL", group, ref) for group, ref in models.items()},
        pins=models.pop("pins", {}) if "pins" in models else {})


def test_validate_seeds_matrix(root):
    platform = platform_for(root)
    ok = seeds_with(reasoning=f"gguf:{root}/w/a.gguf", coding=f"gguf:{root}/w/b.gguf",
                    chat=f"gguf:{root}/w/c.gguf")
    assert validate_seeds(ok, DESCRIPTORS, CATALOG, platform) == []

    unknown_runtime = seeds_with(runtime="tgi", reasoning="auto", coding="auto", chat="auto")
    assert "AI_RUNTIME=tgi is not a shipped runtime" in \
        validate_seeds(unknown_runtime, DESCRIPTORS, CATALOG, platform)[0]
    missing_runtime = seeds_with(runtime=None, reasoning="auto", coding="auto", chat="auto")
    assert "AI_RUNTIME is missing" in validate_seeds(missing_runtime, DESCRIPTORS, CATALOG,
                                                     platform)[0]

    wrong_format = seeds_with(runtime="vllm", reasoning="gguf:https://x.invalid/a.gguf",
                              coding="hf:org/a", chat="hf:org/a")
    for seed in wrong_format.models.values():
        seed.tool_call_format = "hermes"
    problems = validate_seeds(wrong_format, DESCRIPTORS, CATALOG,
                              platform_for(root, ACCEL_LARGE, "accel-large"))
    fmt = [p for p in problems if p.startswith("REASONING_MODEL is gguf")]
    assert len(fmt) == 1
    assert "AI_RUNTIME=vllm serves hf, awq, gptq" in fmt[0]
    assert "set AI_RUNTIME to a runtime that serves gguf" in fmt[0]

    # user sources on a runtime without a generic handler must declare a parser
    undeclared = seeds_with(runtime="vllm", reasoning="hf:org/a", coding="hf:org/a",
                            chat="hf:org/a")
    problems = validate_seeds(undeclared, DESCRIPTORS, CATALOG,
                              platform_for(root, ACCEL_LARGE, "accel-large"))
    assert any("REASONING_MODEL: no tool-call format declared" in p
               and "REASONING_MODEL_TOOL_FORMAT=<one of: hermes" in p for p in problems)
    assert not any(p.startswith("CHAT_MODEL:") for p in problems)  # chat needs no tools
    for seed in undeclared.models.values():
        seed.tool_call_format = "mystery"
    problems = validate_seeds(undeclared, DESCRIPTORS, CATALOG,
                              platform_for(root, ACCEL_LARGE, "accel-large"))
    assert any("CODING_MODEL_TOOL_FORMAT=mystery is not a tool-call format vllm parses" in p
               for p in problems)

    too_many = seeds_with(runtime="vllm", reasoning="hf:org/a", coding="hf:org/b", chat="hf:org/c")
    problems = validate_seeds(too_many, DESCRIPTORS, CATALOG,
                              platform_for(root, ACCEL_LARGE, "accel-large"))
    assert any("one model per engine slot but the seeds name 3 distinct models" in p
               for p in problems)

    bad = seeds_with(reasoning="s3:bucket/x", coding=f"gguf:{root}/w/b.gguf",
                     chat="not-a-catalog-id")
    problems = validate_seeds(bad, DESCRIPTORS, CATALOG, platform)
    assert any("REASONING_MODEL: 's3:bucket/x': unknown source scheme" in p for p in problems)
    assert any("CHAT_MODEL=not-a-catalog-id is not a catalog id" in p for p in problems)

    tiny = platform_for(root, CapabilityVector(system_memory_gb=1.0, cpu_cores=2), "cpu-low")
    auto = seeds_with(reasoning="auto", coding=f"gguf:{root}/w/b.gguf",
                      chat=f"gguf:{root}/w/c.gguf")
    problems = validate_seeds(auto, DESCRIPTORS, CATALOG, tiny)
    assert any(p.startswith("REASONING_MODEL=auto: no catalog model fits") for p in problems)

    pinned = seeds_with(reasoning=f"gguf:{root}/w/a.gguf", coding=f"gguf:{root}/w/b.gguf",
                        chat=f"gguf:{root}/w/c.gguf")
    pinned.pins = {"nope": "a"}
    assert "unknown role 'nope'" in validate_seeds(pinned, DESCRIPTORS, CATALOG, platform)[0]


# ── planning ─────────────────────────────────────────────────────────────────


def test_plan_generation_uses_reference_roles_without_reference_pins(root):
    seeds = read_seeds(parse_env(NEW_ENV.format(root=root)))
    installed = RegistryV2(providers=["llamacpp", "vllm"],
                           models={n: {"provider": "llamacpp", "state": "benchmarked",
                                       "source": {"gguf": f"{root}/w/{n}.gguf"}}
                                   for n in ("reasoner", "coder", "chatter")},
                           groups={"reasoning": [], "coding": [], "chat": []})
    planned = plan_generation(seeds, installed, {"reasoning": "reasoner", "coding": "coder",
                                                 "chat": "chatter"})
    assert planned.groups == {"reasoning": ["reasoner"], "coding": ["coder"],
                              "chat": ["chatter"]}
    assert planned.roles["reviewer"].pin == ["coder"]           # the seed pin
    assert planned.roles["debugger"].pin == []                   # no reference pins carried
    assert planned.roles["coder"].requires.tool_calling is True  # contracts carried
    assert set(planned.roles) >= {"orchestrator", "planner", "coder", "chat"}
    seeds.pins = {"reviewer": "ghost"}
    with pytest.raises(BootstrapError, match="names no seeded model"):
        plan_generation(seeds, installed, {"reasoning": "reasoner", "coding": "coder",
                                           "chat": "chatter"})


# ── the bootstrap ────────────────────────────────────────────────────────────


def test_bootstrap_end_to_end_creates_generation_one(root):
    result, seeds = run_bootstrap(root, NEW_ENV.format(root=root))
    assert result.generation == 1 and result.request_id == "cr-0001"
    assert result.models == {"reasoning": "reasoner", "coding": "coder", "chat": "chatter"}
    assert "generation 1 bootstrapped" in result.message and result.stamped
    registry = load_registry(root / "config")
    assert registry.generation == 1 and registry.note.startswith("bootstrap cr-0001")
    assert all(registry.models[n].state == "active" for n in result.models.values())
    assert registry.resolve("coder").primary == "coder"
    assert registry.resolve("reviewer").primary == "coder"  # the .env pin
    assert registry.resolve("chat").primary == "chatter"
    assert registry.models["coder"].benchmarks["tokens_per_s"] == 9.0
    # rendered: three served models → router preset; every role alias present
    rendered = root / "config" / "rendered"
    assert (rendered / PRESET_FILENAME).is_file() and (rendered / ENGINE_COMPOSE_FILENAME).is_file()
    litellm = yaml.safe_load((rendered / LITELLM_FILENAME).read_text())
    aliases = {e["model_name"] for e in litellm["model_list"]}
    assert {"reasoner", "coder", "chatter", "role-planner", "role-coder", "role-chat"} <= aliases
    # the one implicit approval, audited as such
    request = GovernanceQueue(root / "config").get("cr-0001")
    assert request.status == "applied" and request.kind == "bootstrap"
    assert request.decision.by == "policy" and "implicit approval" in request.decision.reason
    assert request.evidence["seeds"]["coding"] == f"gguf:{root}/w/coder.gguf"
    # F10: the generation-1 diff is exactly the seeds
    added = {line.split(": ")[1].split(" (")[0] for line in result.diff
             if line.startswith("model added: ")}
    assert added == {"reasoner", "coder", "chatter"}
    assert "group coding: [] → [coder]" in result.diff
    # read once: the stamp is in .env and blocks a re-run
    env_text = (root / ".env").read_text()
    assert f"{CONSUMED_KEY}=1@" in env_text and "read ONCE" in env_text
    with pytest.raises(BootstrapError, match="already exists"):
        run_bootstrap(root, env_text)


def test_bootstrap_refuses_consumed_seeds_and_invalid_seeds(root):
    stamped = NEW_ENV.format(root=root) + f"\n{CONSUMED_KEY}=1@2026-09-08T00:00:00\n"
    with pytest.raises(BootstrapError, match="already consumed"):
        run_bootstrap(root, stamped)
    with pytest.raises(BootstrapError) as err:
        run_bootstrap(root, "AI_RUNTIME=vllm\nREASONING_MODEL=gguf:https://x.invalid/a.gguf\n"
                            "CODING_MODEL=hf:org/a\nCODING_MODEL_TOOL_FORMAT=hermes\n"
                            "CHAT_MODEL=hf:org/a\n",
                      platform=platform_for(root, ACCEL_LARGE, "accel-large"))
    assert "cannot be bootstrapped" in str(err.value)
    assert "REASONING_MODEL is gguf but AI_RUNTIME=vllm" in str(err.value)
    assert list_generations(root / "config") == []  # nothing downloaded or written


def test_declared_tool_format_and_context_reach_the_generation(root):
    env = (NEW_ENV.format(root=root)
           + "REASONING_MODEL_TOOL_FORMAT=llama3\nCODING_MODEL_CONTEXT=16384\n")
    result, seeds = run_bootstrap(root, env)
    registry = load_registry(root / "config")
    assert seeds.models["reasoning"].tool_call_format == "llama3"
    assert registry.models["reasoner"].tool_call_format == "llama3"   # declared
    assert registry.models["chatter"].tool_call_format == "generic"   # llamacpp default
    assert registry.models["coder"].context == 16384
    # a declared context below a role contract is refused before any download
    too_small = NEW_ENV.format(root=root) + "CODING_MODEL_CONTEXT=4096\n"
    with pytest.raises(BootstrapError) as err:
        run_bootstrap(root, too_small, force=True)
    assert "CODING_MODEL_CONTEXT=4096 is below the 8192-token contract of role coder" in str(
        err.value)
    assert len(list_generations(root / "config")) == 1  # F8 fired first: nothing new saved


def test_forced_rebootstrap_is_a_governed_request_not_a_second_implicit_approval(root):
    """Generation 1 gets the one implicit approval; a forced re-run on a
    platform with history files a pending change request (MODEL_GOVERNANCE_V2
    §2) that a human approves and applies like any other."""
    run_bootstrap(root, NEW_ENV.format(root=root))
    # the human edits the stamped .env in place (the stamp stays) and forces
    changed = (root / ".env").read_text().replace(
        f"CHAT_MODEL=\"gguf:{root}/w/chatter.gguf\"", f"CHAT_MODEL=gguf:{root}/w/reasoner.gguf")
    assert CONSUMED_KEY in changed
    result, _ = run_bootstrap(root, changed, force=True)
    assert result.generation is None and result.request_id == "cr-0002"
    assert "awaiting approval" in result.message
    assert "model removed: chatter" in result.diff          # diff against generation 1
    queue = GovernanceQueue(root / "config")
    request = queue.get("cr-0002")
    assert request.status == "pending" and request.kind == "bootstrap"
    assert len(list_generations(root / "config")) == 1     # nothing applied yet
    assert (root / ".env").read_text() == changed           # no second stamp
    # a human approves → the request applies as generation 2 through PR-5
    queue.approve("cr-0002", by="nita", reason="re-seed")
    applied = apply(platform_for(root), queue, "cr-0002", actor="nita")
    assert applied.ok and applied.generation == 2
    assert "chatter" not in load_registry(root / "config").models


def test_bootstrap_dry_run_plans_without_writing(root):
    result, _ = run_bootstrap(root, NEW_ENV.format(root=root), dry_run=True)
    assert result.dry_run and result.generation is None
    assert "model added: reasoner (llamacpp, state active)" in result.diff
    assert list_generations(root / "config") == []
    assert not (root / "config" / "rendered").exists()
    assert CONSUMED_KEY not in (root / ".env").read_text()


def test_bootstrap_migrated_single_model_keeps_the_served_name(root):
    """F11 end to end: a legacy accelerator .env → one model active in all
    three groups, served under its legacy name AND every role alias — the
    rendered LiteLLM config keeps existing chats working."""
    legacy = "CHAT_MODEL=Org/Chat-7B-Instruct\nCHAT_MODEL_NAME=chat-7b\nMAX_MODEL_LEN=4096\n"
    result, seeds = run_bootstrap(root, legacy,
                                  platform=platform_for(root, ACCEL_LARGE, "accel-large"))
    assert seeds.migrated_from == "accelerator-hub"
    assert result.models == {"reasoning": "chat-7b", "coding": "chat-7b", "chat": "chat-7b"}
    registry = load_registry(root / "config")
    assert list(registry.models) == ["chat-7b"] and registry.models["chat-7b"].provider == "vllm"
    assert registry.groups == {"reasoning": ["chat-7b"], "coding": ["chat-7b"], "chat": ["chat-7b"]}
    litellm = yaml.safe_load((root / "config" / "rendered" / LITELLM_FILENAME).read_text())
    entries = {e["model_name"]: e["litellm_params"]["model"] for e in litellm["model_list"]}
    assert entries["chat-7b"] == "openai/chat-7b" and entries["role-chat"] == "openai/chat-7b"
    compose = (root / "config" / "rendered" / ENGINE_COMPOSE_FILENAME).read_text()
    assert "--served-model-name\n    - chat-7b" in compose
    assert "migrated from legacy accelerator-hub" in GovernanceQueue(root / "config") \
        .get(result.request_id).title


def test_bootstrap_skip_benchmark_is_explicit_and_audited(root):
    result, _ = run_bootstrap(root, NEW_ENV.format(root=root), skip_benchmark=True,
                              benchmark_fn=None)
    registry = load_registry(root / "config")
    assert registry.models["coder"].state == "active" and not registry.models["coder"].benchmarks
    assert "benchmark skipped" in GovernanceQueue(root / "config").get(result.request_id).title


def test_bootstrap_without_benchmark_function_is_refused(root):
    with pytest.raises(BootstrapError, match="benchmarking needs a benchmark function"):
        run_bootstrap(root, NEW_ENV.format(root=root), benchmark_fn=None)


def test_implicit_approval_is_reserved_for_generation_one(root):
    platform = platform_for(root)
    from tests.unit.test_activation import seed_registry

    current = save_generation(seed_registry(), root / "config", note="gen1")
    with pytest.raises(LifecycleError, match="reserved for generation 1"):
        propose(platform, GovernanceQueue(root / "config"), current, current, kind="bootstrap",
                title="again", requested_by="x", implicit_approval=True)


def test_stamp_env_is_idempotent(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    stamp_env(env, 1, now="T1")
    stamp_env(env, 2, now="T2")
    text = env.read_text()
    assert text.count(CONSUMED_KEY + "=") == 1 and f"{CONSUMED_KEY}=2@T2" in text
    assert text.startswith("A=1\n")
