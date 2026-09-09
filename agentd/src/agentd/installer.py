"""Installer — the first three steps of the first run (PR-21, ADR-031;
docs/FINAL_FIRST_RUN_EXPERIENCE.md §3, docs/FIRST_RUN_EXPERIENCE.md §1).

``install.sh`` at the repository root runs this module::

    python -m agentd.installer --root <checkout> [--yes] [--check]
                               [--profile gpu|cpu|n97|n97-igpu | --class <class>]
                               [--runtime <id>] [--no-editor] [--json]

    1 detect    the host's capability vector → class (``capability.py``), or the
                class a legacy profile / ``--class`` asserts (recorded in .env as
                ``EZAI_CAPABILITY_CLASS`` so ``make bootstrap`` agrees)
    2 .env      FRESH: ``.env.example`` → ``.env`` with ``AI_RUNTIME`` chosen from
                the runtime descriptors for the class (data: ``default_for_classes``)
                REPAIR (F5): an existing ``.env`` keeps every user value byte for
                byte — only missing keys are added, only placeholder secrets are
                minted, a timestamped backup is written first, nothing else on
                disk is touched
                then the model seeds are validated with the bootstrap's own F8
                rules: every problem with its fix, nothing downloaded
    3 secrets   the example's placeholder keys, tokens and passwords are minted

The one review-edit stop: a freshly created ``.env`` opens once in ``$EDITOR``
on a terminal; without a terminal the installer prints what to edit and exits
3; ``--yes`` accepts the file as generated (F7: zero prompts when ``.env`` is
fully specified). ``--check`` writes nothing.

The installer never picks models (ADR-026: users select models; the platform
adapts) and names no runtime, model or vendor — the runtime default and the
legacy profile names are data (descriptors, ``capability.PROFILE_PRESETS``).
Steps 4–8 (fetch · render · up · verify · report) stay with ``make bootstrap``
and ``make up-*`` until PR-22 folds them into ``make setup``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentd.activation import Platform
from agentd.bootstrap import (
    CONSUMED_KEY,
    RUNTIME_KEY,
    SEED_GROUPS,
    Seeds,
    parse_env,
    read_seeds,
    validate_seeds,
)
from agentd.capability import (
    PROFILE_PRESETS,
    CapabilityVector,
    class_for_profile,
    classify,
    detect_vector,
)
from agentd.catalog import load_catalog
from agentd.logging_setup import get_logger
from agentd.runtime_descriptor import CAPABILITY_CLASSES, RuntimeDescriptor, load_descriptors

log = get_logger("installer")

ENV_FILENAME = ".env"
EXAMPLE_FILENAME = ".env.example"
#: Written to .env only when a class is ASSERTED (``--profile cpu|n97…`` /
#: ``--class``); honored by ``platform_cli.build_context`` and by re-runs.
CLASS_KEY = "EZAI_CAPABILITY_CLASS"
#: The example's placeholder secrets. A value equal to the example's, or empty,
#: is a placeholder and gets minted; anything else is the user's and is kept.
SECRET_KEYS = ("LITELLM_MASTER_KEY", "WEBUI_SECRET_KEY", "MCP_API_KEY", "EZAI_CONTROL_TOKEN",
               "SEARXNG_SECRET", "MONITOR_ADMIN_PASSWORD", "MONITOR_VIEWER_PASSWORD")
#: LiteLLM accepts master keys with this prefix only.
LITELLM_KEY = "LITELLM_MASTER_KEY"
LITELLM_PREFIX = "sk-"
#: Typed by humans at the Admin Center login — shorter.
PASSWORD_KEYS = ("MONITOR_ADMIN_PASSWORD", "MONITOR_VIEWER_PASSWORD")
HARDWARE_BEGIN = "# ── local-ezai install.sh · detected hardware"
HARDWARE_END = "# ── end of detected hardware ──"
ADDED_HEADER = "# ── added by install.sh"
#: Exit codes: ready · problems printed (fix .env, re-run) · usage or preflight ·
#: review stop (a fresh .env awaits its one edit, then re-run).
EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, EXIT_REVIEW = 0, 1, 2, 3
#: The legacy make entry points per profile — (one-shot setup target, start
#: target). Mirrors the Makefile; ``tests/unit/test_installer.py`` pins it.
PROFILE_TARGETS: dict[str, tuple[str, str]] = {
    "gpu": ("setup-gpu", "up"), "cpu": ("setup-cpu", "up-cpu"),
    "n97": ("setup-n97", "up-n97"), "n97-igpu": ("setup-n97", "up-n97-igpu"),
}
_STAMP = "%Y-%m-%dT%H:%M:%S"


class InstallError(ValueError):
    """A usage or preflight error (exit 2) — the message names the fix."""


# ── options and report ───────────────────────────────────────────────────────


@dataclass
class Options:
    root: Path
    env_path: Path | None = None
    example_path: Path | None = None
    assume_yes: bool = False
    check: bool = False
    profile: str | None = None
    capability_class: str | None = None
    runtime: str | None = None
    open_editor: bool = True
    #: None → both stdin and stdout are terminals.
    interactive: bool | None = None
    #: Editor command (shell-split); None → $VISUAL, $EDITOR, nano, vi.
    editor: str | None = None
    #: An offline bundle to consume after .env is written (PR-23): images
    #: loaded, weights placed, the bundle's seeds written — no review stop.
    offline_bundle: Path | None = None
    as_json: bool = False


@dataclass
class Report:
    root: str
    env: str
    created: bool = False
    changed: bool = False
    backup: str | None = None
    vector: dict = field(default_factory=dict)
    capability_class: str = ""
    #: How the class was asserted ("--profile n97", "--class …", ".env"), or None.
    asserted: str | None = None
    runtime: str | None = None
    runtime_reason: str = ""
    minted: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    seeds: dict[str, str] = field(default_factory=dict)
    migrated_from: str | None = None
    consumed: str | None = None
    problems: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    editor_opened: bool = False
    #: ``--check``: nothing was written; ``changes`` says what would be.
    dry_run: bool = False
    #: The consumed offline bundle's summary (PR-23), or "".
    bundle: str = ""
    exit_code: int = EXIT_OK

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK

    def lines(self) -> list[str]:
        v = self.vector
        accel = v.get("accelerator", "none")
        if accel != "none" and v.get("accel_memory_gb"):
            accel += f" ({v['accel_memory_gb']:g} GB)"
        flags = ", ".join(v.get("cpu_flags") or []) or "no SIMD flags detected"
        how = f"asserted via {self.asserted}" if self.asserted else "detected"
        out = [f"local-ezai installer — {self.root}",
               f"  hardware  accelerator {accel} · {v.get('system_memory_gb', 0):g} GB RAM · "
               f"{v.get('cpu_cores', 0)} cores · {flags} → class {self.capability_class} ({how})"]
        if self.runtime:
            out.append(f"  runtime   {RUNTIME_KEY}={self.runtime} — {self.runtime_reason}")
        if self.dry_run:
            state = (f"would be created from {EXAMPLE_FILENAME}" if self.created
                     else "would be repaired" if self.changes else "unchanged — nothing to repair"
                     ) + " (--check: nothing written)"
        elif self.created:
            state = f"created from {EXAMPLE_FILENAME}"
        elif self.changed:
            state = "repaired" + (f" (backup: {self.backup})" if self.backup else "")
        else:
            state = "unchanged — nothing to repair"
        out.append(f"  .env      {state} → {self.env}")
        out += [f"            {change}" for change in self.changes]
        if self.bundle:
            out.append(f"  bundle    {self.bundle}")
        if self.consumed:
            out.append(f"  seeds     consumed into generation {self.consumed} — models are managed "
                       "with `local-ezai model …` or the Admin Center; the seeds are history")
        elif self.seeds:
            out.append("  seeds     " + " · ".join(f"{g} ← {r}" for g, r in self.seeds.items())
                       + (f" (migrated from legacy .env family {self.migrated_from})"
                          if self.migrated_from else ""))
        if self.problems:
            out.append(f"  problems  {len(self.problems)} problem(s) with the model seeds — fix "
                       f"them in {self.env}, then re-run (nothing was downloaded):")
            out += [f"            - {p}" for p in self.problems]
        out += [f"            hint: {h}" for h in self.hints]
        if any(k in self.minted for k in PASSWORD_KEYS):
            out.append("  login     Admin Center (monitor :8888): admin / viewer — passwords are "
                       f"the minted {' and '.join(k for k in PASSWORD_KEYS if k in self.minted)}"
                       " in .env")
        if self.next_steps:
            out.append("  next      " + self.next_steps[0])
            out += [f"            {step}" for step in self.next_steps[1:]]
        return out


# ── step 1: detect / assert the class ────────────────────────────────────────


def resolve_class(vector: CapabilityVector, *, profile: str | None = None,
                  capability_class: str | None = None,
                  env_class: str | None = None) -> tuple[str, str | None, str | None]:
    """→ (class, assertion note, class to WRITE into .env or None).

    Precedence: ``--class`` > ``--profile`` > ``EZAI_CAPABILITY_CLASS`` already in
    .env > detection. The detect-marked profile (an accelerator is expected)
    checks the host and writes nothing.
    """
    if capability_class:
        if capability_class not in CAPABILITY_CLASSES:
            raise InstallError(f"--class {capability_class} is not a capability class — known: "
                               + ", ".join(CAPABILITY_CLASSES))
        return capability_class, f"--class {capability_class}", capability_class
    if profile:
        if profile not in PROFILE_PRESETS:
            raise InstallError(f"--profile {profile} is not a known profile — known: "
                               + ", ".join(sorted(PROFILE_PRESETS)))
        if PROFILE_PRESETS[profile] is None and vector.accelerator == "none":
            raise InstallError(
                f"--profile {profile} asserts an accelerator but none was detected — install "
                "its driver and re-run, or run without --profile (detected class "
                f"{classify(vector)})")
        klass = class_for_profile(profile, vector)
        return klass, f"--profile {profile}", (klass if PROFILE_PRESETS[profile] else None)
    if env_class:
        if env_class not in CAPABILITY_CLASSES:
            raise InstallError(f"{CLASS_KEY}={env_class} in .env is not a capability class — "
                               "known: " + ", ".join(CAPABILITY_CLASSES) + "; fix or remove it")
        return env_class, f"{CLASS_KEY} in .env", None
    return classify(vector), None, None


def choose_runtime(descriptors: Mapping[str, RuntimeDescriptor], klass: str, accelerator: str,
                   requested: str | None = None) -> tuple[str | None, str, str | None]:
    """→ (runtime id, reason, problem). Data-driven: the descriptors with an
    image for the host's accelerator kind are the candidates; the one that
    lists the class under ``default_for_classes`` wins, ties by name."""
    if requested:
        if requested not in descriptors:
            raise InstallError(f"--runtime {requested} is not a shipped runtime — known: "
                               + ", ".join(sorted(descriptors)))
        problem = None
        if accelerator not in descriptors[requested].accelerators:
            problem = (f"{RUNTIME_KEY}={requested} has no image for accelerator kind "
                       f"'{accelerator}' (config/providers/{requested}.yaml lists: "
                       f"{', '.join(descriptors[requested].accelerators)}) — pick a runtime "
                       "that serves this host, or add the accelerator to its descriptor")
        return requested, "requested with --runtime", problem
    candidates = sorted(rt for rt, d in descriptors.items() if accelerator in d.accelerators)
    if not candidates:
        return None, "", (f"no shipped runtime has an image for accelerator kind '{accelerator}' "
                          f"(descriptors: {', '.join(sorted(descriptors))}) — add one under "
                          f"config/providers/ and set {RUNTIME_KEY} in .env")
    preferred = [rt for rt in candidates if klass in descriptors[rt].default_for_classes]
    if preferred:
        return preferred[0], (f"default for class {klass} on accelerator {accelerator} "
                              f"(config/providers/{preferred[0]}.yaml)"), None
    return candidates[0], (f"the first shipped runtime with an image for accelerator "
                           f"{accelerator} (none declares class {klass} as its default)"), None


def suggest_profile(klass: str, accelerator: str) -> str:
    """The legacy make profile for this host, from the preset table (data)."""
    if accelerator == "igpu":
        return "n97-igpu"
    if accelerator != "none":
        return "gpu"
    for profile, preset in PROFILE_PRESETS.items():
        if preset == klass and "-" not in profile:
            return profile
    return "gpu"


# ── step 2/3: .env text (line-preserving) ────────────────────────────────────


def mint(key: str) -> str:
    if key == LITELLM_KEY:
        return LITELLM_PREFIX + secrets.token_hex(24)
    if key in PASSWORD_KEYS:
        return secrets.token_urlsafe(9)
    return secrets.token_urlsafe(32)


def _active(key: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")


def _commented(key: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*#\s*{re.escape(key)}\s*=")


class EnvText:
    """Edit ``.env`` text line by line: replace an active key, else uncomment
    the example's commented line for it, else append under one header — so
    a repaired file stays the user's file plus the minimum."""

    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()
        self.added: list[str] = []

    def set(self, key: str, value: str, comment: str | None = None) -> str:
        line = f"{key}={value}" + (f"   # {comment}" if comment else "")
        for i, raw in enumerate(self.lines):
            if _active(key).match(raw):
                self.lines[i] = line
                return "replaced"
        for i, raw in enumerate(self.lines):
            if _commented(key).match(raw):
                self.lines[i] = line
                return "uncommented"
        self.added.append(line)
        return "added"

    def strip_block(self, begin: str, end: str) -> None:
        out: list[str] = []
        skipping = False
        for raw in self.lines:
            if raw.startswith(begin):
                skipping = True
                if out and out[-1] == "":
                    out.pop()
                continue
            if skipping:
                if raw.startswith(end):
                    skipping = False
                continue
            out.append(raw)
        self.lines = out

    def text(self, stamp: str, trailer: list[str] | None = None) -> str:
        body = list(self.lines)
        if self.added:
            body += ["", f"{ADDED_HEADER} · {stamp} ──", *self.added]
        if trailer:
            body += ["", *trailer]
        return "\n".join(body).rstrip("\n") + "\n"


def hardware_block(vector: CapabilityVector, klass: str, note: str | None) -> list[str]:
    """Comment-only record of the detection (no timestamp, no volatile
    numbers, so an unchanged host re-renders byte-identical text)."""
    accel = vector.accelerator
    if accel != "none" and vector.accel_memory_gb:
        accel += f" ({vector.accel_memory_gb:g} GB)"
    flags = ", ".join(vector.cpu_flags) or "no SIMD flags detected"
    return [f"{HARDWARE_BEGIN} ──",
            f"# accelerator {accel} · system memory {vector.system_memory_gb:g} GB · "
            f"{vector.cpu_cores} cores · {flags}",
            f"# capability class {klass}" + (" (asserted)" if note else " (detected)")
            + " — classes: " + " · ".join(CAPABILITY_CLASSES),
            HARDWARE_END]


def render_env(example_text: str, existing_text: str | None, *, vector: CapabilityVector,
               klass: str, class_note: str | None, write_class: str | None,
               runtime: str | None, runtime_reason: str, runtime_requested: bool,
               stamp: str, mint_fn: Callable[[str], str] = mint
               ) -> tuple[str, list[str], list[str]]:
    """→ (text, changes, minted).

    Fresh (``existing_text`` None): the example with its placeholder secrets
    minted, ``AI_RUNTIME`` set and the hardware recorded. Repair: only the
    SECRETS the example has and the file lacks are added (installation
    parameters are the installer's; model settings never are — the example's
    legacy model default is not copied into a user's file), only placeholder
    secrets are minted, ``AI_RUNTIME`` is set only when unset and the seeds
    were not consumed; every other line is the user's, untouched.
    """
    example = parse_env(example_text)
    env = EnvText(existing_text if existing_text is not None else example_text)
    env.strip_block(HARDWARE_BEGIN, HARDWARE_END)
    current = parse_env("\n".join(env.lines))
    changes: list[str] = []
    minted: list[str] = []
    if existing_text is not None:
        for key in SECRET_KEYS:
            if key in example and key not in current:
                how = env.set(key, example[key])
                changes.append(f"{how} {key} (missing; minted)")
                current[key] = example[key]
    for key in SECRET_KEYS:
        if key in current and (current[key] == "" or current[key] == example.get(key)):
            value = mint_fn(key)
            env.set(key, value)
            current[key] = value
            minted.append(key)
    if runtime and (runtime_requested or (not current.get(RUNTIME_KEY)
                                          and not current.get(CONSUMED_KEY))):
        if current.get(RUNTIME_KEY) != runtime:
            how = env.set(RUNTIME_KEY, runtime,
                          comment=f"set by install.sh — {runtime_reason}; edit to switch")
            changes.append(f"{how} {RUNTIME_KEY}={runtime} — {runtime_reason}")
            current[RUNTIME_KEY] = runtime
    if write_class and current.get(CLASS_KEY) != write_class:
        how = env.set(CLASS_KEY, write_class,
                      comment=f"asserted via {class_note}; remove this line to detect")
        changes.append(f"{how} {CLASS_KEY}={write_class} (asserted via {class_note})")
    if minted:
        changes.insert(0, "minted " + ", ".join(minted))
    return env.text(stamp, hardware_block(vector, klass, class_note)), changes, minted


# ── validation (F8, through the bootstrap's own rules) ───────────────────────


def validate(text: str, *, root: Path, config_dir: Path, vector: CapabilityVector, klass: str,
             example_text: str) -> tuple[Seeds, list[str], list[str]]:
    """→ (seeds, problems, hints). Consumed seeds are history: nothing to
    validate. Pure — nothing is fetched or written."""
    env = parse_env(text)
    seeds = read_seeds(env)
    if seeds.consumed:
        return seeds, [], []
    descriptors = load_descriptors(config_dir)
    platform = Platform(config_dir=config_dir, platform_root=root, descriptors=descriptors,
                        vector=vector, capability_class=klass, accelerator=vector.accelerator)
    problems = validate_seeds(seeds, descriptors, load_catalog(config_dir), platform)
    hints: list[str] = []
    example_chat = (parse_env(example_text).get("CHAT_MODEL") or "").strip()
    example_refs = {example_chat, f"hf:{example_chat}"} if example_chat else set()
    if problems and seeds.migrated_from and \
            any(seed.ref in example_refs for seed in seeds.models.values()):
        hints.append(
            f"this .env still carries {EXAMPLE_FILENAME}'s default chat model (the legacy "
            f"accelerator-sized checkpoint) — on class {klass} set REASONING_MODEL / "
            "CODING_MODEL / CHAT_MODEL yourself: `auto` lets the recommender pick models that "
            "fit this host, or name a source the runtime serves (hf:… · gguf:… · catalog id)")
    return seeds, problems, hints


# ── the review-edit stop ─────────────────────────────────────────────────────


def default_editor() -> list[str] | None:
    for var in ("VISUAL", "EDITOR"):
        if os.environ.get(var):
            return shlex.split(os.environ[var])
    for name in ("nano", "vi"):
        if shutil.which(name):
            return [name]
    return None


def open_in_editor(path: Path, editor: list[str]) -> None:
    subprocess.call([*editor, str(path)])


# ── the run ──────────────────────────────────────────────────────────────────


def run_install(options: Options, *, vector_fn: Callable[[], CapabilityVector] = detect_vector,
                mint_fn: Callable[[str], str] = mint,
                editor_fn: Callable[[Path, list[str]], None] = open_in_editor,
                now: str | None = None, say: Callable[[str], None] = print,
                runner: Callable[[list[str]], Any] | None = None) -> Report:
    root = Path(options.root).expanduser().resolve()
    env_path = (options.env_path or root / ENV_FILENAME).resolve()
    example_path = (options.example_path or root / EXAMPLE_FILENAME).resolve()
    config_dir = root / "config"
    if not example_path.is_file() or not (config_dir / "providers").is_dir():
        raise InstallError(f"{root} is not a Local-EZAI checkout (needs {EXAMPLE_FILENAME} and "
                           "config/providers/) — run install.sh from the clone")
    stamp = now or time.strftime(_STAMP)
    example_text = example_path.read_text(encoding="utf-8")
    existing = env_path.read_text(encoding="utf-8") if env_path.is_file() else None
    report = Report(root=str(root), env=str(env_path), created=existing is None)

    vector = vector_fn()
    env_class = (parse_env(existing).get(CLASS_KEY) or "").strip() if existing else ""
    klass, note, write_class = resolve_class(vector, profile=options.profile,
                                             capability_class=options.capability_class,
                                             env_class=env_class or None)
    report.vector = vector.model_dump()
    report.capability_class, report.asserted = klass, note
    descriptors = load_descriptors(config_dir)
    runtime, reason, runtime_problem = choose_runtime(descriptors, klass, vector.accelerator,
                                                      options.runtime)
    text, changes, minted = render_env(
        example_text, existing, vector=vector, klass=klass, class_note=note,
        write_class=write_class, runtime=runtime, runtime_reason=reason,
        runtime_requested=bool(options.runtime), stamp=stamp, mint_fn=mint_fn)
    current_runtime = parse_env(text).get(RUNTIME_KEY) or None
    report.runtime = current_runtime
    report.runtime_reason = (reason if current_runtime == runtime and any(
        RUNTIME_KEY in c for c in changes) else "already set in .env")
    report.changes, report.minted = changes, minted
    report.changed = text != (existing or "")
    if report.changed and not report.created and not changes:
        report.changes.append("recorded the detected hardware")
    if report.changed and not options.check:
        if existing is not None:
            backup = env_path.with_name(f"{ENV_FILENAME}.bak.{stamp.replace(':', '')}")
            shutil.copy2(env_path, backup)
            report.backup = str(backup)
        env_path.write_text(text, encoding="utf-8")
        log.info("%s %s", "created" if report.created else "repaired", env_path)
    if options.check:
        report.changed, report.dry_run = False, True  # nothing written; `changes` = would be

    consumed = False
    if options.offline_bundle is not None:
        bundle_path = Path(options.offline_bundle).expanduser().resolve()
        if options.check:
            report.bundle = f"would consume {bundle_path} (--check: nothing written)"
        else:
            from agentd.bundle import BundleError, consume_bundle

            try:
                outcome = consume_bundle(bundle_path, root, env_path=env_path,
                                         descriptors=descriptors, runner=runner, now=stamp)
            except BundleError as exc:
                raise InstallError(str(exc)) from exc
            report.bundle = outcome.summary()
            report.changes += [f"bundle: {change}" for change in outcome.env_changes]
            text = env_path.read_text(encoding="utf-8")
            consumed = True  # the bundle's seeds ARE the decision — no review stop

    if report.created and not options.assume_yes and not options.check and not consumed:
        interactive = options.interactive
        if interactive is None:
            interactive = sys.stdin.isatty() and sys.stdout.isatty()
        editor = shlex.split(options.editor) if options.editor else default_editor()
        if interactive and options.open_editor and editor:
            say(f"{env_path} created — review it once: the model seeds "
                f"({RUNTIME_KEY}, {', '.join(SEED_GROUPS)}) and LAN_HOST if other devices use "
                f"the UI. Opening {' '.join(editor)} …")
            editor_fn(env_path, editor)
            report.editor_opened = True
            text = env_path.read_text(encoding="utf-8")
        else:
            report.exit_code = EXIT_REVIEW
            report.next_steps = [
                f"edit {env_path} once — the model seeds ({RUNTIME_KEY} is set; "
                f"{' / '.join(SEED_GROUPS)}: hf:<org/repo> · gguf:<url|path> · a catalog id "
                "· auto) and LAN_HOST if other devices use the UI — then re-run ./install.sh",
                "or accept the file as generated: ./install.sh --yes"]

    seeds, problems, hints = validate(text, root=root, config_dir=config_dir, vector=vector,
                                      klass=klass, example_text=example_text)
    if runtime_problem:
        problems.insert(0, runtime_problem)
    report.seeds = {group: seed.ref for group, seed in seeds.models.items()}
    report.migrated_from, report.consumed = seeds.migrated_from, seeds.consumed
    report.problems, report.hints = problems, hints
    if report.exit_code == EXIT_REVIEW:
        return report
    if problems:
        report.exit_code = EXIT_PROBLEMS
        return report
    setup_target, up_target = PROFILE_TARGETS[suggest_profile(klass, vector.accelerator)]
    if seeds.consumed:
        report.next_steps = [f"make {up_target}   start the stack (rendered engine) · then "
                             "make wait-ready · local-ezai status"]
    else:
        report.next_steps = [
            "make bootstrap   consume the seeds into generation 1 (downloads + benchmarks "
            "the models, renders LiteLLM + the engine slot)",
            f"make {up_target}   start the stack with the rendered engine, then make wait-ready",
            f"or in one go: make {setup_target}   (pull · build · download · bootstrap · up · "
            "wait-ready)"]
    return report


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agentd.installer",
        description="Local-EZAI first run, steps 1–3: detect hardware, generate or repair "
                    ".env (secrets minted, model seeds validated before any download).")
    parser.add_argument("--root", default=".", help="The local-ezai checkout (default: cwd)")
    parser.add_argument("--env", default=None, help="Path of .env (default: <root>/.env)")
    parser.add_argument("-y", "--yes", action="store_true", dest="assume_yes",
                        help="No review stop: accept a freshly generated .env as is")
    parser.add_argument("--check", action="store_true",
                        help="Report what would change and validate; write nothing")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--profile", default=None,
                       help="Assert a legacy profile: " + ", ".join(sorted(PROFILE_PRESETS)))
    group.add_argument("--class", dest="capability_class", default=None,
                       help="Assert a capability class: " + ", ".join(CAPABILITY_CLASSES))
    parser.add_argument("--runtime", default=None,
                        help=f"Set {RUNTIME_KEY} explicitly (default: from the descriptors)")
    parser.add_argument("--no-editor", action="store_true", dest="no_editor",
                        help="Never open an editor (print the instruction instead)")
    parser.add_argument("--offline", default=None, metavar="BUNDLE",
                        help="Consume an offline bundle (local-ezai bundle create) after .env is "
                             "written: images loaded, weights placed, seeds set, no egress")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    options = Options(root=Path(args.root), env_path=Path(args.env) if args.env else None,
                      assume_yes=args.assume_yes, check=args.check, profile=args.profile,
                      capability_class=args.capability_class, runtime=args.runtime,
                      open_editor=not args.no_editor,
                      offline_bundle=Path(args.offline) if args.offline else None,
                      as_json=args.as_json)
    try:  # the detector is looked up here so tests can seam it on the module
        report = run_install(options, vector_fn=detect_vector,
                             say=(lambda text: None) if args.as_json else print)
    except InstallError as exc:
        if args.as_json:
            print(json.dumps({"ok": False, "exit_code": EXIT_USAGE, "error": str(exc)}))
        else:
            print(f"install: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.as_json:
        print(json.dumps({**asdict(report), "ok": report.ok}, indent=2, default=str))
    else:
        print("\n".join(report.lines()))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
