# Local-EZAI — User Guide

Local-EZAI is a self-hosted AI platform: **Chat · Agents · Models · MCP ·
Knowledge · Tools · Autonomous SWE** — running entirely on your own
hardware, governed by you. This guide covers using it; running it is in
[OPERATION_MANUAL.md](OPERATION_MANUAL.md), fixing it in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## 1. First run — five steps, one file to edit

```
1  git clone …/local-ezai && cd local-ezai
2  edit .env once           AI_RUNTIME + REASONING_MODEL / CODING_MODEL / CHAT_MODEL
                            (hf:<org/repo> · gguf:<url|path> · a catalog id · auto)
3  make setup               detect the hardware → mint the secrets → validate the seeds
                            → models → images → start → smoke → "Platform ready"
4  open http://<host>:3000  the first account becomes the admin
5  use everything           chat · knowledge · tools · the Orchestrator · the Admin Center
```

- `make setup` stops exactly once, for your edit of `.env` (a fresh install
  opens it in `$EDITOR`; `make setup-gpu` / `setup-cpu` / `setup-n97` assert
  a hardware class instead of detecting it). Every problem with the seeds is
  printed with its fix **before anything downloads**. Re-running is safe:
  every step is idempotent.
- No model seeds yet? `local-ezai init` proposes the catalog's recommended
  set for your hardware (accept with Enter, or type a catalog id, `hf:` or
  `gguf:` reference per group), then runs the setup.
- Air-gapped host: on a connected machine that finished `make setup`, run
  `make bundle BUNDLE=/media/usb/local-ezai-bundle`; on the target,
  `./install.sh --offline /media/usb/local-ezai-bundle && make setup-offline
  BUNDLE=/media/usb/local-ezai-bundle` — the same steps, nothing downloaded.
- The **"Platform ready" card** (a WebUI banner, `config/first-run/report.md`,
  and the Admin Center Overview) shows which model serves each group, the
  runtime and hardware class, and the smoke results — confirmation, not
  configuration. After the first login, `make orchestrator` installs the
  *Local-EZAI Orchestrator* persona (§3).

Details: [FINAL_FIRST_RUN_EXPERIENCE.md](FINAL_FIRST_RUN_EXPERIENCE.md).

## 2. Chat (the platform)

1. Open `http://<host>:3000` and pick a model in the selector — the role
   entries (`role-chat`, …) are what the platform serves for each purpose;
   raw model names are behind them.
2. **Knowledge (RAG):** upload PDFs/MD/TXT in the monitor's Knowledge Base
   bar (`http://<host>:8888`) or run `make embed`; answers cite sources
   automatically — retrieval is injected at the LiteLLM proxy, no per-chat
   setup.
3. **Web search:** toggle the globe icon (always-on retrieval) or install
   the `tools/web-search.py` tool for model-driven search via the bundled
   private SearXNG.
4. **Agent tools (MCP):** wrench icon → filesystem (`./documents`),
   persistent memory graph, web fetch, knowledge-base search — and the
   **Local-EZAI SWE** tools (§3), enabled for the Orchestrator persona only.

Full platform details: the root [README.md](../README.md).

## 3. Autonomous SWE from chat — the Orchestrator

Pick the **Local-EZAI Orchestrator** in the model selector (installed by
`make orchestrator`). It can plan and run software tasks on **registered
projects** — an admin registers a repository once with `local-ezai project
add <path>` or on the Admin Center's Projects page.

```
you:          plan "add rate limiting to the login endpoint" on my-api
orchestrator: <the plan, task by task>  — start it?
you:          yes
orchestrator: run 20260909-… started on branch swe/20260909-… — report: http://<host>:8888/runs/20260909-…
```

From chat you can **start and inspect** (plan, run, sprint, fix, evolve,
status, report, journal, models, the governance queue). You cannot approve,
activate or roll back from chat — by design, so that nothing a web page or a
document injects into a conversation can reach the governance boundary
([OPENWEBUI_INTEGRATION.md](OPENWEBUI_INTEGRATION.md) §5). Runs started
from chat never push. Their branches and reports are the same ones the CLI
produces (§4, §7).

## 4. Autonomous SWE from the CLI

Point the `local-ezai` CLI at any git repository and let the agents work.
Install: [agentd/INSTALL.md](../agentd/INSTALL.md); all commands:
[CLI_REFERENCE.md](CLI_REFERENCE.md).

### Everyday flows

```bash
cd ~/code/myapp

local-ezai plan "add JWT authentication"     # see the plan first (traceless)
local-ezai run  "add JWT authentication"     # full pipeline → branch swe/<id>
git diff main...swe/<id>                     # review, merge when happy

local-ezai test                              # lint/type/build/test + Browser QA
local-ezai fix                               # self-heal a red suite, in place
local-ezai review                            # adversarial review of your diff
local-ezai commit -m "polish the API"        # gated: green validation + review approval
```

### Sprints (multi-task, parallel)

Write `sprint.md` with your goals (checklists, bullets, or numbered items):

```markdown
# Sprint 28
- [ ] add JWT authentication to the API
- [ ] add customer CRUD endpoints
- [ ] protect the CRUD endpoints with JWT
```

```bash
local-ezai sprint sprint.md
```

The Sprint Agent analyzes requirements, builds a dependency graph,
runs independent tasks **in parallel**, validates everything (including
Browser QA when configured), and delivers one `sprint/<id>` branch with a
commit per task plus a committed sprint report
(`docs/sprints/sprint-<id>.md` with the dependency diagram). An admin can
also start sprints and evolution cycles from the Admin Center (§6).

### Teaching the agents your project

```bash
local-ezai memory --add "all endpoints must have integration tests" \
    --kind project_rule
```

Rules, coding styles, and architecture decisions are injected into
planning; failed fix approaches are never repeated for the same error
(the platform warns and records `MEMORY_REPEAT_WARNING`). Everything the
agents learn lives in your repo's `.agent/memory.db` +
`lessons_learned.json`; the Admin Center's Memory page browses the same
store for registered projects.

Per-repo configuration (`.agentd.yaml` at the repo root):

```yaml
validation:
  commands:
    test: ["python -m pytest -q"]
    lint: ["ruff check ."]
    type: ["mypy src"]
browser_qa:                       # optional — see the tested example at
  enabled: true                   # agentd/examples/browser-qa.customer-crud.yaml
  app: {start: "python app.py"}
  workflows: [...]
```

### Documentation & evolution

```bash
local-ezai docs                 # generate/refresh the four repo guides
local-ezai evolve               # autonomous improvement cycle → PR proposal
local-ezai roadmap              # show the project roadmap
local-ezai models               # live model routing (primary + fallback per role)
local-ezai evaluate-models      # verify every role + quality metrics (--report)
local-ezai explain-run          # which model handled each stage of a run
```

Every delivering pipeline passes the **mandatory reviewer gate** before
committing — critical security/architecture/maintainability findings block
the commit ([REVIEW_PROCESS.md](REVIEW_PROCESS.md)) — and agent commands
execute inside the sandbox ([SANDBOX_GUIDE.md](SANDBOX_GUIDE.md)). Plans
draw on a semantic index of your code
([CODE_INTELLIGENCE.md](CODE_INTELLIGENCE.md)).

`evolve` never merges anything: it ends at a pull request (or a local
PR proposal bundle) **awaiting your approval** — see
[SELF_EVOLUTION_GUIDE.md](SELF_EVOLUTION_GUIDE.md) and
[GOVERNANCE.md](GOVERNANCE.md).

## 5. Models and approvals

The platform's models are **state, not files**: three logical groups
(reasoning · coding · chat) served by named models, resolved into roles
(`role-planner`, `role-coder`, `role-chat`, …), recorded as numbered
generations. You never edit a config file to change them.

```bash
local-ezai status                       # class, generation, slot runtime, model states, approvals pending
local-ezai model explain coder          # what serves a role, why, and the contract check
local-ezai model catalog --group chat   # what would fit this hardware (fit verdicts)

local-ezai model install hf:Org/Model   # or gguf:<url|path>, or a catalog id → validated
local-ezai model benchmark my-model     # tokens/s on this host
local-ezai model activate my-model --group chat      # → a change request …
local-ezai governance list              # … waiting for a human when a serving role changes
local-ezai governance approve cr-0001 --reason "faster, same quality"   # applied as generation N+1
local-ezai model rollback --reason "regression"                          # previous generation, audited
```

The same actions live on the Admin Center's **Models**, **Routing** and
**Governance** pages (§6), with the evidence beside the buttons: the diff,
the affected roles, benchmarks measured on this host, the fit verdict, who
proposed it and what a rollback would restore. Approvals are yours;
evolution proposals enter the same queue and wait the same way. Every
change is in the audit log (`config/governance/log.jsonl`) under your name,
whichever surface you used.

## 6. The Admin Center (`http://<host>:8888`)

| Page | What you do there |
|---|---|
| Overview | health as the control plane sees it, the generation, roles → models, the pending queue, recent runs, the Platform-ready card, a one-click rollback of the last change |
| Runs | every run with plan, validation, review, healing, delivery and journal; cancel (admin) |
| Models · Routing · Runtime | add / benchmark / activate / upgrade / retire / uninstall / roll back; why each role resolves as it does; the engine slot and a pre-check for another runtime |
| Governance | the approval queue; approve & apply or reject with a reason, with the evidence |
| Projects · Sprints · Evolution · Memory | the allowlist chat may act on; start sprints and evolution cycles; browse and curate a project's memory |

Sign in with the monitor login (`admin` acts, `viewer` reads); when the
operator configured it, your OpenWebUI session or a proxy identity header
signs you in. The console does exactly what `local-ezai` does — same
refusals, same audit trail. With the control plane down, the pages say so
and the health and knowledge views keep working.

## 7. Where things live

| Artifact | Location |
|---|---|
| Run branches | `swe/<id>`, `sprint/<id>`, `evolve/<id>` in your repo |
| Run journals & reports | `~/.agentd/runs/<id>/` (`journal.jsonl`, `report.json`, screenshots, PR bundles) |
| Project memory | `<repo>/.agent/memory.db` + `lessons_learned.json` |
| Per-repo model routing (optional override) | `<repo>/.agent/model_registry.yaml` + `model_benchmarks.json` |
| Platform model state | `config/models/` (registry + generations), `config/providers/` (runtime descriptors), `config/catalog/` (your catalog additions) |
| Audit log · queue · allowlist | `config/governance/log.jsonl` · `config/governance/` · `config/projects.yaml` |
| First-run report | `config/first-run/report.{json,md}` |
| Rendered outputs (never edit) | `config/rendered/` — LiteLLM config, engine compose override, role map, capability report |

Problems? [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
