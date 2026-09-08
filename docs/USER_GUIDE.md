# Local-EZAI — User Guide

Local-EZAI is a self-hosted AI platform: **Chat · Agents · Models · MCP ·
Knowledge · Tools · Autonomous SWE** — running entirely on your own
hardware. This guide covers using it; running it is in
[OPERATION_MANUAL.md](OPERATION_MANUAL.md).

## 1. Chat (the platform)

1. Start the stack (`make setup-n97` / `setup-cpu` / `setup-gpu`) and open
   `http://<host>:3000` — the first account becomes admin.
2. Pick your model in the selector and talk to it.
3. **Knowledge (RAG):** upload PDFs/MD/TXT in the monitor's Knowledge Base
   bar (`http://<host>:8888`) or run `make embed`; answers cite sources
   automatically — retrieval is injected at the LiteLLM proxy, no per-chat
   setup.
4. **Web search:** toggle the globe icon (always-on retrieval) or install
   the `tools/web-search.py` tool for model-driven search via the bundled
   private SearXNG.
5. **Agent tools (MCP):** wrench icon → filesystem (`./documents`),
   persistent memory graph, web fetch, knowledge-base search.

Full platform details: the root [README.md](../README.md).

## 2. Autonomous SWE (the feature)

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
(`docs/sprints/sprint-<id>.md` with the dependency diagram).

### Teaching the agents your project

```bash
local-ezai memory --add "all endpoints must have integration tests" \
    --kind project_rule
```

Rules, coding styles, and architecture decisions are injected into
planning; failed fix approaches are never repeated for the same error
(the platform warns and records `MEMORY_REPEAT_WARNING`). Everything the
agents learn lives in your repo's `.agent/memory.db` +
`lessons_learned.json`.

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

## 3. Where things live

| Artifact | Location |
|---|---|
| Run branches | `swe/<id>`, `sprint/<id>`, `evolve/<id>` in your repo |
| Run journals & reports | `~/.agentd/runs/<id>/` (`journal.jsonl`, `report.json`, screenshots, PR bundles) |
| Project memory | `<repo>/.agent/memory.db` + `lessons_learned.json` |
| Model routing | `<repo>/.agent/model_registry.yaml` + `model_benchmarks.json` |

Problems? [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
