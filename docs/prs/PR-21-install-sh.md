# PR-21 — `install.sh`: the first run, steps 1–3 (phase P5 opens)

**Phase:** P5 (opens) · **ADR:** ADR-031 (**Proposed**, entered with this PR) ·
**Size:** M · **Plan entry:** [V1_PR_PLAN.md](../V1_PR_PLAN.md) §3/PR-21 ·
**Depends on:** PR-2 (vector, classes, preset table), PR-3 (runtime
descriptors), PR-4/PR-7 (`parse_source`, `read_seeds`, `validate_seeds`, the
consumption stamp), PR-12 (frozen contract, untouched), PR-15 (chat-stack
baseline, unchanged) · **Status:** implemented on
`claude/next-ready-pr-bnq7r3` as the nineteenth stacked commit (PR-3..20
unmerged; each commit reverts independently)

## Scope (as delivered)

The plan entry: capability detect, `.env` generation / validation with
printed fixes **before any download** (F8), secrets minting, the single
review-edit stop, re-run repair mode (F5) — steps 1–3 of
[FINAL_FIRST_RUN_EXPERIENCE.md](../FINAL_FIRST_RUN_EXPERIENCE.md) §3.

- **`install.sh`** (repository root; also `make install`). Preflight:
  python3 ≥ 3.10 (fix printed), the agentd venv created on demand through
  `make swe-install` (or the same pip commands without make), Docker with
  the compose plugin present or the fix printed as a warning — the script
  starts nothing and downloads nothing, Docker is needed from `make
  bootstrap` on. Then `python -m agentd.installer --root <checkout> "$@"`;
  on success, without `--check`, the existing `scripts/check-ports.sh`
  relocates occupied host ports as `make up*` does. Exit codes: 0 ready ·
  1 problems printed · 2 usage / preflight · 3 review stop. `EZAI_PYTHON`
  names an interpreter with agentd importable (tests, CI).
- **`agentd/src/agentd/installer.py`** — every decision, offline-testable.
  - **Step 1, detect / assert.** `capability.detect_vector()` → `classify`;
    `--profile gpu|cpu|n97|n97-igpu` resolves through `PROFILE_PRESETS`
    (`gpu` = an accelerator must exist, asserts nothing else; the preset
    profiles assert their class), `--class <class>` asserts directly. An
    asserted class is written to `.env` as `EZAI_CAPABILITY_CLASS`; re-runs
    honor it, and so does `platform_cli.build_context` (new: a validated read
    of that variable, which `make` exports from `.env`), so the installer and
    `make bootstrap` agree on the class. The detected vector is recorded in
    `.env` as a comment block (accelerator kind, memory, cores, SIMD flags →
    class) — stable text, no timestamps or volatile numbers.
  - **The runtime default is data.** New optional descriptor field
    `default_for_classes` (validated against the class names): candidates
    are the descriptors with an image for the detected accelerator kind; the
    one listing the class wins, ties by name; a host no default serves gets
    the first candidate with the reason printed; `--runtime` overrides and
    is checked against the host's accelerator (a problem, not a silent
    fallback). Shipped: the GGUF runtime for `cpu-standard` and `cpu-low`,
    the HF runtime for `accel-large` and `accel-small`; an iGPU host gets the
    runtime with an iGPU image.
  - **Step 2, `.env` fresh.** The shipped example with the seven placeholder
    secrets minted (values equal to the example's or empty are placeholders),
    `AI_RUNTIME` uncommented *in the seed section* (a line-preserving editor
    replaces an active key, else uncomments the example's commented line,
    else appends under one header) and the hardware block appended. **The
    installer never picks models**: the seeds are the human's edit.
  - **Step 2, `.env` repair (F5).** A timestamped backup first; user lines
    byte-identical and in place; only *secrets* that are missing, empty or
    still the example's placeholder are minted — the example's model
    defaults and settings are never copied into a user's file; `AI_RUNTIME`
    is set only when unset and the seeds not yet consumed; a consumed stamp
    means the seeds are history: reported, not validated; a second run
    changes nothing and writes no backup; `config/`, `models/`, volumes are
    never touched.
  - **Validation (F8).** `bootstrap.read_seeds` + `validate_seeds` against
    the chosen class and runtime — the same rules `local-ezai bootstrap`
    applies — plus a class-aware hint when a CPU-class host still carries
    the example's legacy accelerator-sized default. Pure: nothing fetched.
  - **Step 3, secrets.** `mint()`: the LiteLLM key keeps its `sk-` prefix,
    the two monitor passwords are 12 URL-safe characters (typed at the
    login), the rest 43; every value is safe in `.env` and compose
    interpolation (no `$`, quotes or spaces). The report names the keys,
    never the values.
  - **The one review-edit stop (F2 / F7).** A freshly created `.env` opens
    once in `$VISUAL` / `$EDITOR` (else nano, vi) when stdin and stdout are
    terminals, then is re-validated; without a terminal (or `--no-editor`)
    the installer prints what to edit and exits 3; `--yes` accepts the file
    as generated; `--check` writes nothing and reports what would change.
  - **Report.** Hardware and class (detected / asserted), the runtime and
    why, what changed in `.env`, the seeds (or the consumption stamp), the
    problems with their fixes, the Admin Center login note when passwords
    were minted, and the next commands: `make bootstrap`, then the profile's
    `make up-*` (from the preset table), or `make setup-*` in one go. `--json`
    prints the whole report.
- **Makefile:** `install` target (`INSTALL_ARGS` pass-through).
  `scripts/setup.sh` no longer copies `.env.example` and points its next
  steps at `./install.sh`. `.env.example` header explains the installer.
- **Excluded by design:** steps 4–8 (fetch, render, up, smoke, the "Platform
  ready" card, `local-ezai init`) — PR-22; the offline bundle and the F1–F11
  acceptance suite — PR-23; re-pointing `make setup` (system packages today)
  — PR-22; passing the class assertion from `make setup-*` — PR-22; any
  model choice by the installer; system-package installation beyond what
  `scripts/setup.sh` already does; `LAN_HOST` detection (the review stop
  names it).

## Design decisions made in this PR

1. **Thin bash, thick Python.** The shell does what only the shell can
   (preflight, venv, exec); every rule is a tested function that reuses the
   platform's own detector and validator — no second implementation of
   either, no drift between "what the installer accepts" and "what
   `bootstrap` accepts".
2. **Descriptor data decides the runtime.** `default_for_classes` is an
   optional field with a validated vocabulary; the module knows no runtime
   id (H1 discipline), and a host without a declared default still gets a
   working proposal with the reason printed.
3. **Assertions are explicit and shared.** A class is asserted only on the
   operator's word (`--profile` / `--class`) and then lives in `.env`, where
   both the installer and the CLI read it; detection is recorded as
   comments so a stale record cannot override a changed host.
4. **Repair edits secrets only.** Copying the example's `CHAT_MODEL` default
   into an existing `.env` would have made the installer pick a model; the
   gap-fill is therefore limited to the seven secrets. Other missing keys
   keep their compose defaults.
5. **Stable text, honest idempotence.** The hardware record carries no
   timestamp and no volatile number (disk free is omitted), so a re-run on
   an unchanged host produces byte-identical text and no backup — the F5
   proof is a real equality, not a tolerance.
6. **Validation happens even when the run stops for the edit.** Exit 3 still
   lists what the untouched file would fail on, so the human edits with the
   fixes in front of them.

## Tests (21 new; suite 646 → 667)

`tests/unit/test_installer.py` (17):

- **Fresh `.env` from the shipped example** (tripwire): all seven secrets
  minted, URL-safe, `sk-` prefix kept; every other example key byte-identical;
  `AI_RUNTIME` = the descriptors' default for the class, uncommented in the
  seed section; class detected, not asserted; hardware block present; only
  `.env` appeared on disk.
- **F8 on a low-power host**: the example's legacy default is three
  problems with fixes plus the class-aware hint, exit 1, nothing downloaded,
  no backup, the login note.
- **An accelerator host is ready** with the example's default: exit 0, the
  three seeds migrated, next steps name `make bootstrap`, `make up`,
  `make setup-gpu`.
- `resolve_class` precedence (`--class` > `--profile` > `.env` > detection),
  the detect-marked profile refusing a host without an accelerator, unknown
  class / profile / `.env` value → usage errors.
- `choose_runtime` over the shipped descriptors for every class; the iGPU
  host falls to the runtime with an iGPU image with the honest reason;
  unknown `--runtime` → usage error; a requested runtime without an image
  for the kind → a problem; no candidate at all → a problem.
- `suggest_profile` per class / accelerator and **the Makefile tripwire**:
  every target the installer names exists, `install:` included.
- **Descriptors declare exactly one installer default per class**; an
  unknown class in `default_for_classes` is a `DescriptorError`; the field
  is optional.
- **A profile assertion is written and honored**: `EZAI_CAPABILITY_CLASS`
  in `.env`, kept on re-runs (no rewrite), and `platform_cli.build_context`
  returns the asserted class over detection and refuses an unknown value.
- **F5 repair**: user lines byte-identical and in order, placeholders and
  empty secrets minted, missing secrets added, no model or setting copied
  from the example, the user's seeds and runtime untouched, backup equals
  the original, markers under `config/models`, `config/rendered`, `models/`
  unchanged, `.env.example` unchanged; **the second run is a no-op** (same
  text, no backup, no changes).
- Consumed seeds: reported, not validated; no runtime proposed; the asserted
  class from `.env` honored.
- `--runtime` replaces the value and is checked (format mismatch reported);
  unknown → usage error.
- **Review stop**: non-interactive → exit 3 with the instruction and the
  problems listed; interactive → a fake editor script runs once, its edit
  is validated → exit 0; `--yes` never opens an editor; `--no-editor` on a
  terminal → exit 3.
- `--check` writes nothing (fresh and repair), reports what would change.
- `EnvText` replace / uncomment / append / strip; `mint` shapes; `main`
  JSON output, usage errors (not a checkout, bad class, exclusive flags).

`tests/integration/test_install_sh.py` (4) — the bash entry against a
temporary checkout with `EZAI_PYTHON` = the test interpreter, the host's
real hardware detected, assertions independent of it: fresh run without a
terminal → exit 3, `.env` with minted secrets and a runtime, no backup;
repair run → exit 0, `make bootstrap` named, user values kept, backup equals
the original, second run a no-op; `--check` writes nothing and `--yes
--json` creates; `--help`, bad class (2), unknown flag (2), not a checkout
(2, nothing written).

Updated: none. Every pre-existing test passes unmodified (the descriptor
field is optional; `build_context` reads an unset variable); goldens and
the chat-stack baseline unchanged.

## Behavior notes

- **New:** `install.sh`, `make install`, `python -m agentd.installer`,
  descriptor field `default_for_classes` (optional), `.env` keys the
  installer may write: `EZAI_CAPABILITY_CLASS` (asserted only),
  `AI_RUNTIME` (when unset), the seven secrets (placeholders only), a
  comment block.
- **`platform_cli.build_context`** honors `EZAI_CAPABILITY_CLASS` from the
  environment (validated; unknown → `PlatformError` with the fix). Unset —
  the case for every existing install — detection is unchanged.
- **`scripts/setup.sh`** no longer copies `.env.example` to `.env` (the
  installer does, with secrets) and its next-steps text names `./install.sh`
  and `make setup-gpu|n97`.
- **`.env.example`** header text changed (comments only; the PR-7 tripwire
  that the shipped example migrates and validates still passes).
- A fresh `.env` on a CPU-class host with the example's untouched seeds
  fails validation with three fixes and a hint (the example's chat default
  is an HF checkpoint sized for an accelerator, the runtime chosen for the
  class serves GGUF). This is F8 by design; the review-edit stop is where
  the seeds are set. Monitor passwords are minted on fresh installs — the
  report says where they are.
- No contract, daemon, compose or chat-stack change.

## Verification boundary

The host vector is injected in the unit tests; the bash tests detect the
real (CPU-only) CI host and assert nothing that depends on it. Editors are
fake scripts. Docker is absent in CI, so the warning path of the preflight
ran and `check-ports.sh` was skipped; the port relocation itself is the
pre-existing script. `make swe-install` was not exercised (the venv step is
bypassed with `EZAI_PYTHON`).

## Rollback note

`git revert <commit>` removes `install.sh`, the module, the tests, the
Makefile target, the descriptor field (optional — old descriptors parse
either way) and the `build_context` environment read. A `.env` the
installer generated is an ordinary `.env` the previous code reads
unchanged; an `EZAI_CAPABILITY_CLASS` line becomes inert; `.env.bak.*`
files are the operator's to delete. No data migration.

## Self-review

- `local-ezai . review` (live Reviewer Agent) requires the model plane —
  unavailable here; to be attached on a stack-connected re-run.
  Checklist review performed:
- [x] Scope matches the plan entry; additions listed and justified above:
      the `default_for_classes` descriptor field (the runtime default as
      data), the `build_context` read of the asserted class (one truth for
      installer and bootstrap), `make install`
- [x] New tests cover the scope incl. the F5 repair proof named in the
      plan's risk register; full suite green (667); ruff clean
- [x] Pre-existing tests unmodified; goldens passing; chat-stack baseline
      unchanged
- [x] Architecture docs updated to as-built (FINAL_FIRST_RUN_EXPERIENCE §3,
      FIRST_RUN_EXPERIENCE §1, HARDWARE_AGNOSTIC §1, RUNTIME_ABSTRACTION §2,
      CURRENT_ARCHITECTURE, CLI_REFERENCE, OPERATION_MANUAL, README);
      ADR-031 Proposed
- [x] Rollback note present
- [x] No model/vendor/runtime names in code: the runtime default is
      descriptor data, profiles come from `PROFILE_PRESETS`, tests derive
      the expected runtime from the descriptors
