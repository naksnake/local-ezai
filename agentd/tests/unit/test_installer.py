"""The installer — steps 1–3 of the first run (PR-21, ADR-031 Proposed;
docs/FINAL_FIRST_RUN_EXPERIENCE.md §3, docs/FIRST_RUN_EXPERIENCE.md §6 F2/F5/F7/F8).

Offline: the host vector is injected, editors are fake scripts, nothing is
fetched. The shipped ``.env.example`` and descriptors are the subject where
the test says so; model names are fixtures otherwise."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from agentd import installer, platform_cli
from agentd.bootstrap import CONSUMED_KEY, RUNTIME_KEY, parse_env
from agentd.capability import PROFILE_PRESETS, CapabilityVector
from agentd.config import AgentdConfig
from agentd.installer import (
    CLASS_KEY,
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_REVIEW,
    EXIT_USAGE,
    HARDWARE_BEGIN,
    HARDWARE_END,
    LITELLM_KEY,
    LITELLM_PREFIX,
    PASSWORD_KEYS,
    PROFILE_TARGETS,
    SECRET_KEYS,
    EnvText,
    InstallError,
    Options,
    choose_runtime,
    mint,
    resolve_class,
    run_install,
    suggest_profile,
)
from agentd.platform_errors import PlatformError
from agentd.runtime_descriptor import (
    CAPABILITY_CLASSES,
    DescriptorError,
    load_descriptors,
    parse_descriptor,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_TEXT = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
EXAMPLE = parse_env(EXAMPLE_TEXT)
DESCRIPTORS = load_descriptors(REPO_ROOT / "config")
CPU_LOW = CapabilityVector(system_memory_gb=15.5, cpu_cores=4, cpu_flags=["avx2"])
ACCEL = CapabilityVector(accelerator="cuda", accel_memory_gb=24, system_memory_gb=64,
                         cpu_cores=16, cpu_flags=["avx2"])
IGPU = CapabilityVector(accelerator="igpu", system_memory_gb=15.5, cpu_cores=4, cpu_flags=["avx2"])
NOW = "2026-09-09T10:00:00"


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """The parts of a clone the installer needs: the example, the shipped
    descriptors, the compose marker, three tiny GGUF files to seed with."""
    root = tmp_path / "local-ezai"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config" / "providers", root / "config" / "providers")
    shutil.copy(REPO_ROOT / ".env.example", root / ".env.example")
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / "w").mkdir()
    for name in ("reasoner", "coder", "chatter"):
        (root / "w" / f"{name}.gguf").write_bytes(name.encode() * 512)
    return root


def seeds_for(root: Path) -> str:
    return (f"{RUNTIME_KEY}=llamacpp\nREASONING_MODEL=gguf:{root}/w/reasoner.gguf\n"
            f"CODING_MODEL=gguf:{root}/w/coder.gguf\nCHAT_MODEL=gguf:{root}/w/chatter.gguf\n")


def install(root: Path, vector: CapabilityVector = CPU_LOW, **kwargs):
    options = Options(root=root, interactive=kwargs.pop("interactive", False), **kwargs)
    return run_install(options, vector_fn=lambda: vector, now=NOW, say=lambda text: None)


def env_of(root: Path) -> dict[str, str]:
    return parse_env((root / ".env").read_text(encoding="utf-8"))


def default_runtime_for(klass: str) -> str:
    [runtime] = [rt for rt, d in DESCRIPTORS.items() if klass in d.default_for_classes]
    return runtime


# ── a fresh .env from the shipped example ────────────────────────────────────


def test_fresh_env_mints_every_placeholder_secret_and_keeps_the_rest_of_the_example(checkout):
    report = install(checkout, assume_yes=True)
    assert report.created and report.changed and report.backup is None
    text = (checkout / ".env").read_text(encoding="utf-8")
    env = parse_env(text)
    assert report.minted == list(SECRET_KEYS)
    for key in SECRET_KEYS:
        assert env[key] and env[key] != EXAMPLE[key], key
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", env[key]), key  # safe in .env and compose
    assert env[LITELLM_KEY].startswith(LITELLM_PREFIX)
    for key, value in EXAMPLE.items():  # everything else is the example, byte for byte
        if key not in SECRET_KEYS:
            assert env[key] == value, key
    # the runtime is the descriptors' default for the class, uncommented IN the
    # seed section (not appended at the end); the class is detected, not asserted
    assert env[RUNTIME_KEY] == default_runtime_for("cpu-low")
    assert "none" in DESCRIPTORS[env[RUNTIME_KEY]].accelerators
    assert text.index(f"{RUNTIME_KEY}=") < text.index("── Model Configuration")
    assert CLASS_KEY not in env
    assert HARDWARE_BEGIN in text and "capability class cpu-low (detected)" in text
    assert "15.5 GB · 4 cores · avx2" in text
    assert report.capability_class == "cpu-low" and report.asserted is None
    # only .env appeared on disk
    assert sorted(p.name for p in checkout.iterdir()) == sorted(
        [".env", ".env.example", "config", "docker-compose.yml", "w"])


def test_fresh_env_on_a_low_power_host_flags_the_examples_default_model_with_a_fix(checkout):
    """F8: the example's legacy chat default is accelerator-sized and HF-format;
    on a CPU class the descriptors pick a GGUF runtime — the mismatch is
    printed with its fix and a class-aware hint, nothing is downloaded."""
    report = install(checkout, assume_yes=True)
    assert report.exit_code == EXIT_PROBLEMS and report.next_steps == []
    assert report.migrated_from and any("serves gguf" in p for p in report.problems)
    assert any("default chat model" in h and "cpu-low" in h and "auto" in h for h in report.hints)
    assert not (checkout / "config" / "models").exists() and not list(checkout.glob(".env.bak*"))
    assert len(report.problems) == 3  # one per seeded group, each naming the fix
    lines = "\n".join(report.lines())
    assert "problems  3 problem(s)" in lines and "nothing was downloaded" in lines
    assert "login     Admin Center" in lines


def test_fresh_env_on_an_accelerator_host_is_ready_with_the_examples_default(checkout):
    report = install(checkout, ACCEL, assume_yes=True)
    assert report.exit_code == EXIT_OK, report.problems
    assert report.capability_class == "accel-large"
    assert env_of(checkout)[RUNTIME_KEY] == default_runtime_for("accel-large")
    assert report.migrated_from and len(report.seeds) == 3
    assert report.next_steps[0].startswith("make bootstrap")
    assert "make up " in report.next_steps[1] and "make setup-gpu" in report.next_steps[2]


# ── step 1: detection, assertions, the runtime default (all data) ────────────


def test_resolve_class_precedence_and_profile_assertions():
    assert resolve_class(CPU_LOW) == ("cpu-low", None, None)
    assert resolve_class(ACCEL) == ("accel-large", None, None)
    assert resolve_class(ACCEL, capability_class="cpu-low") == ("cpu-low", "--class cpu-low",
                                                                "cpu-low")
    assert resolve_class(ACCEL, profile="n97") == ("cpu-low", "--profile n97", "cpu-low")
    assert resolve_class(ACCEL, profile="cpu") == ("cpu-standard", "--profile cpu",
                                                   "cpu-standard")
    # the detect-marked profile checks the host and writes nothing
    assert resolve_class(ACCEL, profile="gpu") == ("accel-large", "--profile gpu", None)
    with pytest.raises(InstallError, match="asserts an accelerator but none was detected"):
        resolve_class(CPU_LOW, profile="gpu")
    # an assertion already in .env is honored when no flag says otherwise
    assert resolve_class(ACCEL, env_class="cpu-standard") == ("cpu-standard",
                                                              f"{CLASS_KEY} in .env", None)
    assert resolve_class(ACCEL, env_class="cpu-standard", profile="n97")[0] == "cpu-low"
    with pytest.raises(InstallError, match="not a capability class"):
        resolve_class(CPU_LOW, capability_class="huge")
    with pytest.raises(InstallError, match="not a known profile"):
        resolve_class(CPU_LOW, profile="mainframe")
    with pytest.raises(InstallError, match=f"{CLASS_KEY}=weird in .env"):
        resolve_class(CPU_LOW, env_class="weird")


def test_choose_runtime_is_data_driven_over_the_shipped_descriptors():
    for klass in CAPABILITY_CLASSES:
        accelerator = "cuda" if klass.startswith("accel") else "none"
        runtime, reason, problem = choose_runtime(DESCRIPTORS, klass, accelerator)
        assert runtime == default_runtime_for(klass) and problem is None
        assert f"default for class {klass}" in reason
    # an iGPU host: the class default has no image for the kind → the runtime
    # that does, with the honest reason
    runtime, reason, problem = choose_runtime(DESCRIPTORS, "accel-small", "igpu")
    assert "igpu" in DESCRIPTORS[runtime].accelerators and problem is None
    assert runtime != default_runtime_for("accel-small") and "first shipped runtime" in reason
    # explicit requests: unknown → usage error; no image for the kind → a problem
    with pytest.raises(InstallError, match="not a shipped runtime"):
        choose_runtime(DESCRIPTORS, "cpu-low", "none", requested="mockengine")
    no_igpu = default_runtime_for("accel-large")
    runtime, reason, problem = choose_runtime(DESCRIPTORS, "accel-small", "igpu",
                                              requested=no_igpu)
    assert runtime == no_igpu and reason == "requested with --runtime"
    assert problem and "has no image for accelerator kind 'igpu'" in problem
    # no descriptor serves the kind at all
    only = {no_igpu: DESCRIPTORS[no_igpu]}
    assert choose_runtime(only, "accel-small", "igpu")[0] is None
    assert "no shipped runtime has an image" in choose_runtime(only, "accel-small", "igpu")[2]


def test_suggest_profile_and_the_make_targets_it_names():
    assert suggest_profile("cpu-low", "none") == "n97"
    assert suggest_profile("cpu-standard", "none") == "cpu"
    assert suggest_profile("accel-large", "cuda") == "gpu"
    assert suggest_profile("accel-small", "rocm") == "gpu"
    assert suggest_profile("accel-small", "igpu") == "n97-igpu"
    assert set(PROFILE_TARGETS) == set(PROFILE_PRESETS)
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    for setup_target, up_target in PROFILE_TARGETS.values():
        assert re.search(rf"^{re.escape(setup_target)}:", makefile, re.M), setup_target
        assert re.search(rf"^{re.escape(up_target)}:", makefile, re.M), up_target
    assert re.search(r"^install:", makefile, re.M)


def test_shipped_descriptors_declare_an_installer_default_for_every_class():
    covered = [klass for d in DESCRIPTORS.values() for klass in d.default_for_classes]
    assert sorted(covered) == sorted(CAPABILITY_CLASSES)  # each class exactly once
    text = (REPO_ROOT / "config" / "providers" / "llamacpp.yaml").read_text(encoding="utf-8")
    broken = text.replace("default_for_classes: [cpu-standard, cpu-low]",
                          "default_for_classes: [cpu-huge]")
    with pytest.raises(DescriptorError, match="unknown capability class 'cpu-huge'"):
        parse_descriptor(broken, "llamacpp.yaml")
    # the field is optional: a descriptor without it still parses
    assert parse_descriptor(text.replace("default_for_classes: [cpu-standard, cpu-low]", ""),
                            "llamacpp.yaml").default_for_classes == []


def test_a_profile_assertion_is_written_to_env_and_honored_by_the_cli(checkout, monkeypatch):
    (checkout / ".env").write_text(seeds_for(checkout), encoding="utf-8")
    report = install(checkout, ACCEL, profile="n97")
    assert report.exit_code == EXIT_OK and report.capability_class == "cpu-low"
    assert report.asserted == "--profile n97"
    text = (checkout / ".env").read_text(encoding="utf-8")
    assert parse_env(text)[CLASS_KEY] == "cpu-low" and "asserted via --profile n97" in text
    # a re-run without the flag keeps the assertion (it lives in .env now)
    again = install(checkout, ACCEL)
    assert again.capability_class == "cpu-low" and again.asserted == f"{CLASS_KEY} in .env"
    assert not again.changed
    # the CLI's platform context sees the same class: from the environment
    # (`make` exports .env) or, in a plain shell, from the platform's .env (PR-22)
    monkeypatch.setattr(platform_cli, "detect_vector", lambda: ACCEL)
    config = AgentdConfig()
    config.platform.config_dir = checkout / "config"
    monkeypatch.delenv(CLASS_KEY, raising=False)
    ctx = platform_cli.build_context(config, checkout)
    assert ctx.platform.klass == "cpu-low" and ctx.platform.accel == "cuda"
    monkeypatch.setenv(CLASS_KEY, "accel-small")   # the environment wins over .env
    assert platform_cli.build_context(config, checkout).platform.klass == "accel-small"
    monkeypatch.setenv(CLASS_KEY, "cpu-huge")
    with pytest.raises(PlatformError, match="EZAI_CAPABILITY_CLASS=cpu-huge is not a capability"):
        platform_cli.build_context(config, checkout)
    # no assertion anywhere → detection
    monkeypatch.delenv(CLASS_KEY, raising=False)
    (checkout / ".env").write_text(seeds_for(checkout), encoding="utf-8")
    assert platform_cli.build_context(config, checkout).platform.klass == "accel-large"


# ── F5: repair mode never wipes anything ─────────────────────────────────────


def test_repair_keeps_every_user_value_fills_gaps_backs_up_and_is_idempotent(checkout):
    original = ("# my notes — keep them\n"
                f"{LITELLM_KEY}=sk-mine\n"
                f"WEBUI_SECRET_KEY={EXAMPLE['WEBUI_SECRET_KEY']}\n"   # the placeholder
                "MCP_API_KEY=\n"                                     # empty = placeholder
                "OPENWEBUI_PORT=3100\n"
                "LAN_HOST=192.168.1.5\n"
                f"{seeds_for(checkout)}"
                "EZAI_ROLE_PIN_reviewer=coder\n")
    (checkout / ".env").write_text(original, encoding="utf-8")
    markers = {checkout / "config" / "models" / "registry.yaml": "generation: 3\n",
               checkout / "config" / "rendered" / "litellm-config.yaml": "model_list: []\n",
               checkout / "models" / "gguf" / "a.gguf": "weights\n"}
    for path, content in markers.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    report = install(checkout)
    assert report.exit_code == EXIT_OK, report.problems
    assert not report.created and report.changed and report.backup
    assert Path(report.backup).read_text(encoding="utf-8") == original   # byte-identical copy
    text = (checkout / ".env").read_text(encoding="utf-8")
    env = parse_env(text)
    # user values: untouched, in place, in order
    assert text.startswith("# my notes — keep them\n" f"{LITELLM_KEY}=sk-mine\n")
    for key, value in (("OPENWEBUI_PORT", "3100"), ("LAN_HOST", "192.168.1.5"),
                       (RUNTIME_KEY, "llamacpp"), ("EZAI_ROLE_PIN_reviewer", "coder"),
                       ("REASONING_MODEL", f"gguf:{checkout}/w/reasoner.gguf")):
        assert env[key] == value, key
    assert report.runtime == "llamacpp" and report.runtime_reason == "already set in .env"
    # placeholders minted, gaps filled from the example, secrets among the gaps minted too
    assert env["WEBUI_SECRET_KEY"] != EXAMPLE["WEBUI_SECRET_KEY"] and env["MCP_API_KEY"]
    assert report.minted == [k for k in SECRET_KEYS if k != LITELLM_KEY]
    for key in ("EZAI_CONTROL_TOKEN", "SEARXNG_SECRET", *PASSWORD_KEYS):
        assert env[key] and env[key] != EXAMPLE[key], key
    # only secrets are filled in — never the example's model defaults or settings
    assert "RAG_COLLECTION" not in env and "CHAT_MODEL_NAME" not in env
    assert env["CHAT_MODEL"] == f"gguf:{checkout}/w/chatter.gguf"   # the user's seed
    assert "added EZAI_CONTROL_TOKEN (missing; minted)" in report.changes
    assert not any(RUNTIME_KEY in c for c in report.changes)
    # nothing else on disk moved
    for path, content in markers.items():
        assert path.read_text(encoding="utf-8") == content
    assert (checkout / ".env.example").read_text(encoding="utf-8") == EXAMPLE_TEXT
    assert "make bootstrap" in report.next_steps[0] and "make up-n97" in report.next_steps[1]

    # the second run is a no-op: same text, no backup, no changes
    again = install(checkout)
    assert again.exit_code == EXIT_OK and not again.changed and again.changes == []
    assert again.backup is None and again.minted == []
    assert (checkout / ".env").read_text(encoding="utf-8") == text
    assert len(list(checkout.glob(".env.bak.*"))) == 1
    assert "unchanged — nothing to repair" in "\n".join(again.lines())


def test_repair_leaves_consumed_seeds_alone_and_reports_the_generation(checkout):
    (checkout / ".env").write_text(
        f"{LITELLM_KEY}=sk-mine\n{CLASS_KEY}=cpu-standard\n"
        f"{CONSUMED_KEY}=1@2026-09-08T00:00:00\n", encoding="utf-8")
    report = install(checkout, ACCEL)
    assert report.exit_code == EXIT_OK and report.problems == []
    assert report.consumed == "1@2026-09-08T00:00:00" and report.seeds == {}
    assert report.capability_class == "cpu-standard" and report.asserted == f"{CLASS_KEY} in .env"
    env = env_of(checkout)
    assert RUNTIME_KEY not in env           # seeds are history — no runtime is proposed
    assert report.runtime is None
    assert "consumed into generation 1@2026-09-08T00:00:00" in "\n".join(report.lines())
    assert len(report.next_steps) == 1 and "start the stack" in report.next_steps[0]


def test_an_explicit_runtime_request_replaces_the_value_and_is_checked_against_the_host(checkout):
    (checkout / ".env").write_text(seeds_for(checkout), encoding="utf-8")
    other = default_runtime_for("accel-large")
    report = install(checkout, runtime=other)
    assert env_of(checkout)[RUNTIME_KEY] == other
    assert report.runtime_reason == "requested with --runtime"
    assert report.exit_code == EXIT_PROBLEMS
    assert any("is gguf but" in p and f"{RUNTIME_KEY}={other}" in p for p in report.problems)
    with pytest.raises(InstallError, match="not a shipped runtime"):
        install(checkout, runtime="mockengine")


# ── the one review-edit stop (F2 / F7) ───────────────────────────────────────


def test_review_stop_without_a_terminal_exits_3_with_the_instruction(checkout):
    report = install(checkout)  # fresh, not --yes, not interactive
    assert report.exit_code == EXIT_REVIEW and (checkout / ".env").is_file()
    assert report.next_steps[0].startswith("edit ")
    assert "then re-run ./install.sh" in report.next_steps[0]
    assert "./install.sh --yes" in report.next_steps[1]
    assert report.problems and not report.editor_opened   # what the edit must fix is listed


def test_review_stop_on_a_terminal_opens_the_editor_once_and_validates_the_edit(checkout, tmp_path):
    fake_editor = tmp_path / "editor.py"
    fake_editor.write_text(
        "import sys, pathlib\n"
        "p = pathlib.Path(sys.argv[1])\n"
        f"p.write_text(p.read_text() + {seeds_for(checkout)!r})\n"
        "(p.parent / 'editor-ran').write_text('1')\n", encoding="utf-8")
    report = install(checkout, interactive=True, editor=f"{sys.executable} {fake_editor}")
    assert report.editor_opened and (checkout / "editor-ran").is_file()
    assert report.exit_code == EXIT_OK, report.problems
    assert report.seeds["coding"] == f"gguf:{checkout}/w/coder.gguf"
    # --yes never opens an editor; --no-editor on a terminal falls back to the instruction
    (checkout / ".env").unlink()
    boom = install(checkout, assume_yes=True, interactive=True, editor="false")
    assert not boom.editor_opened and boom.exit_code == EXIT_PROBLEMS
    (checkout / ".env").unlink()
    assert install(checkout, interactive=True, open_editor=False).exit_code == EXIT_REVIEW


def test_check_mode_writes_nothing(checkout):
    report = install(checkout, check=True)
    assert report.dry_run and report.created and not report.changed
    assert not (checkout / ".env").exists() and report.minted == list(SECRET_KEYS)
    assert "would be created" in "\n".join(report.lines()) and report.exit_code == EXIT_PROBLEMS
    original = seeds_for(checkout) + f"WEBUI_SECRET_KEY={EXAMPLE['WEBUI_SECRET_KEY']}\n"
    (checkout / ".env").write_text(original, encoding="utf-8")
    report = install(checkout, check=True)
    assert report.dry_run and report.exit_code == EXIT_OK and "WEBUI_SECRET_KEY" in report.minted
    assert (checkout / ".env").read_text(encoding="utf-8") == original
    assert not list(checkout.glob(".env.bak.*"))
    assert "would be repaired" in "\n".join(report.lines())


# ── the pieces ───────────────────────────────────────────────────────────────


def test_env_text_replaces_uncomments_or_appends_and_strips_a_block():
    env = EnvText(f"A=1\n# B=old\n\n{HARDWARE_BEGIN} ──\n# stale\n{HARDWARE_END}\nC=3   # keep\n")
    env.strip_block(HARDWARE_BEGIN, HARDWARE_END)
    assert env.lines == ["A=1", "# B=old", "C=3   # keep"]
    assert env.set("A", "2") == "replaced"
    assert env.set("B", "new", comment="why") == "uncommented"
    assert env.set("D", "4") == "added"
    text = env.text("2026-09-09T10:00:00", trailer=["# hw"])
    assert text == ("A=2\nB=new   # why\nC=3   # keep\n\n# ── added by install.sh · "
                    "2026-09-09T10:00:00 ──\nD=4\n\n# hw\n")
    assert parse_env(text) == {"A": "2", "B": "new", "C": "3", "D": "4"}


def test_mint_shapes():
    assert mint(LITELLM_KEY).startswith(LITELLM_PREFIX) and len(mint(LITELLM_KEY)) == 51
    for key in PASSWORD_KEYS:
        assert len(mint(key)) == 12
    assert len(mint("WEBUI_SECRET_KEY")) >= 40 and mint("X") != mint("X")
    for key in SECRET_KEYS:
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", mint(key))


def test_main_json_and_usage_errors(checkout, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(installer, "detect_vector", lambda: CPU_LOW)
    code = installer.main(["--root", str(checkout), "--yes", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == EXIT_PROBLEMS and data["ok"] is False and data["capability_class"] == "cpu-low"
    assert data["problems"] and data["minted"] == list(SECRET_KEYS)
    (checkout / ".env").write_text(seeds_for(checkout), encoding="utf-8")
    assert installer.main(["--root", str(checkout)]) == EXIT_OK
    assert "next      make bootstrap" in capsys.readouterr().out
    assert installer.main(["--root", str(tmp_path)]) == EXIT_USAGE
    assert "not a Local-EZAI checkout" in capsys.readouterr().err
    assert installer.main(["--root", str(checkout), "--class", "nope", "--json"]) == EXIT_USAGE
    assert json.loads(capsys.readouterr().out)["exit_code"] == EXIT_USAGE
    with pytest.raises(SystemExit):
        installer.main(["--root", str(checkout), "--profile", "cpu", "--class", "cpu-low"])
