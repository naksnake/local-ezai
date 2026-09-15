# Autonomous SWE — the offline demo, then the real thing

> For someone who has the repository and wants to *see* the autonomous
> software-engineering pipeline work before installing models, and then run
> it for real on their own hardware and repositories. Everything below runs
> on Linux or macOS with `python3` (≥ 3.10) and `git`; §3 also needs Docker.

**What the SWE function is.** You hand Local-EZAI a software task in plain
language. The platform plans it (Planner), edits a git worktree on its own
branch (Coder), runs the project's own checks (Validator; Browser QA when
configured), diagnoses and repairs a red suite in a bounded loop (Debug
Agent), has the change reviewed before anything is committed (Reviewer), and
delivers one commit on `swe/<run-id>` for you to review and merge. Nothing
ships itself: pushes, approvals and merges are yours
([TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md), [AGENT_DESIGN.md](AGENT_DESIGN.md)).

```
plan ──► code ──► validate ──► review ──► commit on swe/<run-id>
                     │ red                                    (never pushed unless you say so)
                     └──► debug ──► fix ──► revalidate (bounded, stall-detected)
```

## 1. The one-minute offline demo

No GPU, no model download, no running stack. A *scripted* model — agentd's
`scripted` provider, the same one the 775-test suite uses — plays the
Planner, the Coder and the Reviewer with the canned answers in
[`examples/swe-demo/responses.json`](../examples/swe-demo/responses.json).
**Everything else is the real platform:** the worktree and branch, the file
edits, the project's own `unittest` suite as the validation gate, the
review gate, the commit, the journal, the report, the project memory.

```bash
git clone https://github.com/naksnake/local-ezai.git && cd local-ezai
make swe-demo                       # first time: installs the agentd venv (~1 min), then runs
make swe-demo DEMO_DIR=/tmp/demo    # same, but keeps the repository, report and journal
```

(Without `make`: `bash scripts/swe-demo.sh --keep`; the CLI is taken from
`$EZAI_CLI`, else `.venv-agentd/bin/local-ezai`, else `local-ezai` on PATH.)

### What you will see, step by step

1. **A scratch project.** `examples/sample-project` — a tiny stdlib HTTP
   service with two tests and *deliberately* no `/health` endpoint — is
   copied to `<demo>/project`, made a git repository (branch `main`), and its
   check from `.agentd.yaml` is run once so you see it is green before the
   agents touch it. A config file with one non-default line,
   `llm.provider: scripted`, is written next to it.
2. **`local-ezai <project> plan "add a GET /health endpoint …"`** — the
   Planner reads the repository (a symbol index is built first) and returns a
   plan: goal, assumptions, one task with its check, risks. Plans are
   traceless: nothing is written to the project.
3. **`local-ezai <project> run "…the same task…"`** — the full pipeline. Watch
   the log: worktree created on `swe/<run-id>` → plan ready → coding task T1
   (the coder reads `app.py`, edits the route table, writes
   `tests/test_health.py`) → `validate: all 1 check(s) passed` → `review:
   approve` → `delivered: <sha> on swe/<run-id> (pushed=False)` → memory
   recorded. Then the report: run id, branch, workspace, goal, task outcome,
   validation, commit, journal path.
4. **The delivery.** `git log main..swe/<id>` shows exactly one commit; the
   diff touches `app.py` (one line) and adds a test; `main` is untouched.
   The project's own tests run on the branch: **3 tests, OK** — the third is
   the one the coder wrote.
5. **The evidence.** The journal (`journal.jsonl`, ~40 events: state
   transitions, agent spawns, model calls, tool calls, plan, validation,
   review gate, commit) and `explain-run`, which names the model behind each
   stage — here `scripted` for Planner, Coder and Reviewer, and *"deterministic
   harness (no LLM)"* for Validation, because verdicts never come from a model.

### Reading the artifacts (with `DEMO_DIR`)

| What | Where |
|---|---|
| the delivered branch | `git -C <demo>/project log -p main..swe/<id>` |
| the run report (machine-readable) | `<demo>/state/runs/<id>/report.json` — status, plan, task results, validation, review, commit, `models_used` |
| the journal | `<demo>/state/runs/<id>/journal.jsonl` — one JSON event per line, `type` + `payload` |
| the worktree the run used | `<demo>/state/workspaces/<id>` |
| what the project remembers | `<demo>/project/.agent/memory.db` (+ `lessons_learned.json`) — the implementation history the Planner and Debug Agent read next time |
| the sandbox audit | `<demo>/state/runs/<id>/exec_audit.jsonl` — every command the validator ran |

### How the scripted model works, and how to play with it

`responses.json` is a list; each entry answers **one model call, in order**,
whatever the role:

```json
[
  {"content": "{\"goal\": …, \"tasks\": [{\"id\": \"T1\", …}], …}"},   // 1 Planner → a Plan (JSON in a string)
  {"tool_calls": [{"name": "fs_read", "arguments": {"path": "app.py"}}]}, // 2 Coder: read before writing
  {"tool_calls": [{"name": "fs_edit", …}, {"name": "fs_write", …}]},     // 3 Coder: edit + new test, one turn
  {"content": "Added the /health route …"},                              // 4 Coder: no tool calls = task done
  {"content": "{\"verdict\": \"approve\", \"summary\": …, \"findings\": []}"} // 5 Reviewer → the gate
]
```

Validation, the Git Agent and the Memory Agent consume no model calls: they
are deterministic. Things worth trying in a copy of the file (point
`llm.script_path` at it in `<demo>/state/config.yaml` and re-run step 3 on a
fresh `--dir`):

- change entry 5 to `{"verdict": "request_changes", …, "findings": [{"severity": "high", …}]}`
  — the **review gate blocks the commit**, the run fails with the structured
  report, and nothing lands on the branch;
- break the edit (a wrong `old_string`) — the coder's tool call fails, the
  task fails, validation never runs;
- delete the test entry and let the plan's check fail — the run enters the
  **debug → fix → revalidate** loop, which needs Debug Agent answers of its
  own (see `debug_response()` in `agentd/tests/conftest.py` for the shape).

## 2. What changes with a real model

Nothing in the pipeline. The one line `llm.provider: scripted` goes away and
the agents bind to **role aliases** (`role-planner`, `role-coder`,
`role-reviewer`, …) that LiteLLM serves from the models you activated. The
same commands, the same journal, the same gates — the answers come from your
hardware. Which model serves which role is data you govern (`local-ezai
model explain coder`, the Routing page), never code.

## 3. The real first run (models, stack, WebUI) — five steps

Prerequisites: a Linux host with Docker ≥ 24 and Compose ≥ 2.24.4, `python3`
≥ 3.10, disk for the models (a few GB for a CPU-class set, tens of GB with an
accelerator). A GPU is optional: the installer detects a *capability class*
(`accel-large` · `accel-small` · `cpu-standard` · `cpu-low`) and adapts.
Full detail: [FINAL_FIRST_RUN_EXPERIENCE.md](FINAL_FIRST_RUN_EXPERIENCE.md);
the user-level walkthrough: [USER_GUIDE.md](USER_GUIDE.md) §1.

1. **Clone.** `git clone https://github.com/naksnake/local-ezai.git && cd local-ezai`
2. **`./install.sh`** (or `make install`) — detects your hardware, records the
   class, creates `.env` with secrets minted and the runtime proposed for your
   class, validates the model seeds *before anything downloads*, and opens
   `.env` **once**. The only edit you make, in the seed block:

   ```dotenv
   AI_RUNTIME=…            # proposed for your class; leave it
   REASONING_MODEL=auto    # or a catalog id, hf:<org/repo>, gguf:<url|path>
   CODING_MODEL=auto       # `auto` = the catalog's recommendation for your class
   CHAT_MODEL=auto
   ```

   `local-ezai init` is the interactive fallback: it proposes the recommended
   set per group with fit verdicts and writes the seeds for you. Air-gapped
   hosts: `install.sh --offline <bundle>` ([USER_GUIDE.md](USER_GUIDE.md) §1).
3. **`make setup`** — bootstrap (fetch, validate and benchmark the seeds →
   generation 1, rendered LiteLLM config), pull the images, download the
   embedding model, `up`, wait-ready, smoke (a chat turn, a RAG answer, a plan
   on the sample project), then the **Platform ready** card with the URLs.
   `make setup-cpu` / `make setup-gpu` assert a class instead of detecting.
   Every step is idempotent: re-run it after an interruption.
4. **Open the WebUI** at `http://<host>:3000` (`OPENWEBUI_PORT`) and sign up —
   the first account is the admin. The Admin Center is the monitor at
   `http://<host>:8888` (`/overview`), the control plane at `:8010`.
5. **First SWE task, from chat.** `make orchestrator` installs the
   *Local-EZAI Orchestrator* persona (needs that first account). In a chat with
   it: *"Plan: add a GET /health endpoint to sample-project"* → it shows the
   plan and asks you to confirm → *"go"* → a run id within seconds → *"status"*
   / *"report"* as it works. Chat can start and inspect work; approvals,
   rollbacks and merges are deliberately not reachable from it
   ([OPENWEBUI_INTEGRATION.md](OPENWEBUI_INTEGRATION.md)). The Runs page of the
   Admin Center shows the same run, its journal and report, with *cancel*.

The sample project was copied to `<checkout>/sample-project` and registered
by `make setup`, so your first autonomous task never touches a repository of
yours — exactly what the demo did by hand.

## 4. Running tasks from the CLI, on your repositories

The CLI is installed by `make swe-install` (`.venv-agentd/bin/local-ezai`;
`. .venv-agentd/bin/activate` puts it on PATH) or with pipx
([agentd/INSTALL.md](../agentd/INSTALL.md)). It is path-first: a directory
argument or `-C` selects the project, otherwise the current directory.

```bash
cd ~/code/myapp
local-ezai plan "add JWT authentication"       # see the plan first — traceless
local-ezai run  "add JWT authentication"       # full pipeline → branch swe/<id>, never pushed
git diff main...swe/<id>                       # review; merge when happy
local-ezai run "…" --push                      # only if you want the branch pushed (T3, explicit)

local-ezai test                                # the project's checks (+ Browser QA if configured)
local-ezai fix                                 # self-heal a red suite, in place, bounded
local-ezai review                              # adversarial review of your working-tree diff
local-ezai commit -m "polish the API"          # gated: green validation + review approval
local-ezai sprint sprint.md                    # a whole markdown spec: DAG → parallel waves → one commit per task
local-ezai memory                              # what the platform learned about this repo
local-ezai explain-run                         # which model served which stage of the last run
```

Teach the agents your project with a `.agentd.yaml` (validation commands,
Browser QA workflows) and a `CLAUDE.md`/`AGENT.md` (conventions) in the
repository — [USER_GUIDE.md](USER_GUIDE.md) §4, [CLI_REFERENCE.md](CLI_REFERENCE.md).

**Where the daemon fits.** Repository verbs (`plan`, `run`, `fix`, …) always
run in-process; they never need the control plane. Management verbs (`model`,
`governance`, `project`, `status`) go through the daemon when it answers,
in-process otherwise — same output either way. For runs started *from chat
or the Admin Center*, the daemon must see your repositories: serve it on the
host (`make control-serve`) and register each repository once
(`local-ezai project add ~/code/myapp`). The container overlay (`make
control-up`) is for model and governance operations over `config/` only; it
refuses model install/benchmark, which need the host's docker, with the fix
named.

## 5. If something goes wrong

- The demo: a failed run keeps its directory and prints the report and
  journal paths; `local-ezai <project> explain-run` and the journal's last
  `RUN_TERMINAL` event say where it stopped.
- The stack: `make health`, `local-ezai status`, then
  [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — first run (§2), control plane
  and connected mode (§3), Admin Center (§4), governance and lifecycle (§5),
  runs and self-healing (§8).
- Before changing the platform itself: `make swe-test` (the offline suite,
  the demo included) and `make release-gate`.
