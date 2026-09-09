"""H1 — the word audit (PR-25, P6; HARDWARE_AGNOSTIC_ARCHITECTURE §2/§6,
ADR-026 (2)): no GPU vendor, brand or SKU string outside runtime-descriptor
data and the places this file names, with a reason each.

Accelerator *kinds* (cuda / rocm / igpu / none) are API families and allowed
everywhere; brands and SKUs are not. The scan covers the platform's code,
packaged data, prompts, the console, the tool server and the continuity
layer (the frozen chat stack's compose/env/scripts, the Makefile, the
installer). Documentation (`docs/`, READMEs, `.agent/`) and tests are
history and fixtures, out of scope by the gate's definition. Every allowance
must stay in use — a stale one fails too, so the list never grows quietly."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The sanctioned home of hardware knowledge (RUNTIME_ABSTRACTION §2).
SANCTIONED = ("config/providers/*.yaml",)

#: What is scanned: (directory, glob).
SCOPE: tuple[tuple[str, str], ...] = (
    ("agentd/src", "**/*.py"), ("agentd/src", "**/*.yaml"),
    ("monitor", "**/*.py"), ("mcp-servers", "**/*.py"),
    ("config/prompts", "**/*.md"), ("config", "*.yaml"), ("config", "*.py"),
    ("k8s", "**/*.yaml"), ("scripts", "**/*.sh"),
    (".", "Makefile"), (".", "install.sh"), (".", "docker-compose*.yml"), (".", ".env.example"),
)

#: Forbidden tokens: vendors and brands, then SKU strings. Word-bounded,
#: case-insensitive; the vendor word that is also an English word (and the
#: name of this platform's code-intelligence module) counts only next to a
#: product word; `amd64` is an architecture, `-apple-system` a font stack.
TOKENS: dict[str, re.Pattern[str]] = {
    "nvidia": re.compile(r"\bnvidia\b", re.I),
    "geforce": re.compile(r"\bgeforce\b", re.I),
    "rtx": re.compile(r"\brtx\b", re.I),
    "gtx": re.compile(r"\bgtx\b", re.I),
    "quadro": re.compile(r"\bquadro\b", re.I),
    "radeon": re.compile(r"\bradeon\b", re.I),
    "ryzen": re.compile(r"\bryzen\b", re.I),
    "epyc": re.compile(r"\bepyc\b", re.I),
    "threadripper": re.compile(r"\bthreadripper\b", re.I),
    "xeon": re.compile(r"\bxeon\b", re.I),
    "intel": re.compile(r"\bintel\b(?=[\s(®]+(?:n\d|core|xeon|arc\b|iris|uhd|gpu|cpu|celeron|"
                        r"pentium|alder|raptor|meteor|lunar|ultra))", re.I),
    "amd": re.compile(r"\bamd\b", re.I),
    "apple": re.compile(r"(?<!-)\bapple\b(?!-system)", re.I),
    "jetson": re.compile(r"\bjetson\b", re.I),
    "orin": re.compile(r"\borin\b", re.I),
    "tegra": re.compile(r"\btegra\b", re.I),
    "snapdragon": re.compile(r"\bsnapdragon\b", re.I),
    "adreno": re.compile(r"\badreno\b", re.I),
    "datacenter-sku": re.compile(r"\b(?:[ah]100|h200|l40s?|a10g?|v100)\b", re.I),
    "consumer-sku": re.compile(r"\b[1-4]0[5-9]0(?:\s?(?:ti|super))?\b", re.I),
    "n97": re.compile(r"\bn97\b", re.I),
    "n100": re.compile(r"\bn100\b", re.I),
    "n305": re.compile(r"\bn305\b", re.I),
    "lake": re.compile(r"\b(?:alder|raptor|meteor|lunar|arrow|tiger|ice)\s+lake\b", re.I),
}

#: (path glob, allowed tokens, why). The continuity layer is the frozen chat
#: stack (ADR-002): its legacy profile names and historical comments are
#: pinned here, not edited.
ALLOWED: tuple[tuple[str, frozenset[str], str], ...] = (
    ("agentd/src/agentd/capability.py", frozenset({"nvidia", "n97"}),
     "ACCELERATOR_PROBES names each kind's driver interface; PROFILE_PRESETS keeps the legacy "
     "alias (HARDWARE_AGNOSTIC §2 as-built, H4)"),
    ("agentd/src/agentd/installer.py", frozenset({"n97"}),
     "legacy profile names: --profile help and PROFILE_TARGETS (H4 continuity)"),
    ("agentd/src/agentd/platform_cli.py", frozenset({"n97"}),
     "legacy profile names in the --profile help text (H4 continuity)"),
    ("scripts/setup.sh", frozenset({"nvidia"}),
     "host provisioning: the accelerator driver and container-toolkit packages (system layer; "
     "the platform's behavior does not depend on it)"),
    ("scripts/*.sh", frozenset({"n97"}), "legacy profile names (make up-n97 …)"),
    ("Makefile", frozenset({"nvidia", "n97"}),
     "continuity layer: legacy profile targets; setup-system names the toolkit it installs"),
    ("install.sh", frozenset({"n97"}), "--profile help (legacy profile names)"),
    ("docker-compose.yml", frozenset({"nvidia"}),
     "the frozen chat stack's accelerator reservation (ADR-002; the renderer overrides it)"),
    ("docker-compose.cpu.yml", frozenset({"nvidia", "intel", "n97", "n100"}),
     "frozen chat-stack profile file: historical comments (ADR-002)"),
    ("docker-compose.n97*.yml", frozenset({"n97", "n100", "intel", "nvidia", "lake"}),
     "the legacy profile files themselves: frozen chat stack, historical comments (ADR-002)"),
    (".env.example", frozenset({"n97", "n100"}),
     "the legacy variable family (F11 reads it) and its historical comments"),
)


def scanned_files() -> list[Path]:
    files: list[Path] = []
    for directory, pattern in SCOPE:
        base = REPO_ROOT / directory
        if not base.exists():
            continue
        files += [p for p in base.glob(pattern) if p.is_file()]
    seen: set[Path] = set()
    unique = []
    for path in files:
        rel = str(path.relative_to(REPO_ROOT))
        if path in seen or any(fnmatch.fnmatch(rel, s) for s in SANCTIONED):
            continue
        seen.add(path)
        unique.append(path)
    return sorted(unique)


def allowed_tokens(rel: str) -> set[str]:
    allowed: set[str] = set()
    for pattern, tokens, _reason in ALLOWED:
        if fnmatch.fnmatch(rel, pattern):
            allowed |= tokens
    return allowed


def hits() -> list[tuple[str, int, str, str]]:
    """(relative path, line number, token, line) for every occurrence in scope."""
    found: list[tuple[str, int, str, str]] = []
    for path in scanned_files():
        rel = str(path.relative_to(REPO_ROOT))
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore")
                                      .splitlines(), start=1):
            for token, pattern in TOKENS.items():
                if pattern.search(line):
                    found.append((rel, number, token, line.strip()))
    return found


def test_h1_no_vendor_brand_or_sku_string_outside_the_sanctioned_places():
    violations = [(rel, number, token, line) for rel, number, token, line in hits()
                  if token not in allowed_tokens(rel)]
    assert violations == [], (
        "H1: hardware vendor/brand/SKU strings outside runtime-descriptor data —\n"
        + "\n".join(f"  {rel}:{number}: [{token}] {line[:100]}"
                    for rel, number, token, line in violations)
        + "\nFix: express the fact as descriptor data (config/providers/*.yaml) or a capability "
          "class/kind; a legitimate exception is an ALLOWED entry with its reason.")


def test_h1_every_allowance_is_still_in_use():
    used = {(rel, token) for rel, _number, token, _line in hits()}
    stale = []
    for pattern, tokens, _reason in ALLOWED:
        for token in sorted(tokens):
            if not any(fnmatch.fnmatch(rel, pattern) and t == token for rel, t in used):
                stale.append((pattern, token))
    assert stale == [], f"stale H1 allowances — remove them: {stale}"


def test_h1_the_sanctioned_home_is_where_the_knowledge_lives():
    """The exemption is not a loophole: the shipped descriptors are the one
    place a device reservation driver is spelled."""
    descriptors = "\n".join(p.read_text(encoding="utf-8")
                            for p in (REPO_ROOT / "config" / "providers").glob("*.yaml"))
    assert TOKENS["nvidia"].search(descriptors)
    assert not any(fnmatch.fnmatch("config/providers/vllm.yaml", s) is False for s in SANCTIONED)
    assert "config/providers/vllm.yaml" not in {str(p.relative_to(REPO_ROOT))
                                                 for p in scanned_files()}


def test_h1_tokens_are_word_bounded_and_context_aware():
    assert not TOKENS["amd"].search("linux/amd64 image")
    assert TOKENS["amd"].search("an AMD card")
    assert not TOKENS["apple"].search("font-family: -apple-system, sans-serif")
    assert TOKENS["apple"].search("Apple silicon")
    assert not TOKENS["intel"].search("code intel must never fail a run")
    assert TOKENS["intel"].search("Intel N97 boxes") and TOKENS["intel"].search("Intel Arc")
    assert TOKENS["consumer-sku"].search("an RTX 4090")
    assert not TOKENS["consumer-sku"].search("port 8080")
    assert TOKENS["n97"].search("make up-n97") and not TOKENS["n97"].search("n975")
