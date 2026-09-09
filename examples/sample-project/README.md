# Local-EZAI sample project

A tiny, dependency-free HTTP service the first run uses so your first
autonomous engineering task never touches a repository of yours
(docs/FIRST_RUN_EXPERIENCE.md §4).

- `app.py` — answers `GET /` with a JSON greeting. There is **deliberately no
  `/health` endpoint**: the first-run smoke asks the Planner to add one, and
  the Orchestrator's suggested task does the same.
- `tests/test_app.py` — standard-library `unittest`; the validation command
  in `.agentd.yaml` runs it.

`local-ezai setup` copies this folder to `<checkout>/sample-project`, makes it
a git repository and registers it as project `sample-project`. Try:

```
local-ezai sample-project plan "add a GET /health endpoint that returns {\"status\": \"ok\"} and a test for it"
local-ezai sample-project run  "…the same task…"
```

or say it to the Orchestrator in the WebUI. The run ends on a branch you
review and merge yourself — nothing ships itself.
