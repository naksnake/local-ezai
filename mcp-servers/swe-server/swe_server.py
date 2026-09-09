#!/usr/bin/env python3
"""Local-EZAI SWE Tool Server — an MCP server behind mcpo (V1 P3, PR-13,
ADR-029; docs/OPENWEBUI_INTEGRATION.md §2 tool catalog, §4 run artifacts in
chat, §5 security at the chat boundary).

A THIN adapter over the ``ezaid`` control plane: every tool is one or two
HTTP calls to the frozen 1.0.0 contract, carrying the service token and the
tool server's identity (``X-EZAI-Client: swe-server``; the control plane
audits every call). No pipeline logic, no repository access, no secret is
ever shown to the model.

**Start + inspect only.** The catalog can start work (plan / run / sprint /
fix / evolve) on a *registered* project and read results (status / report /
journal / models / governance queue). Approve, reject, activate, rollback,
upgrade, retire, uninstall, install, merge, push and cancel do not exist
here — by construction, not by prompt. A hostile chat can waste a run; it
cannot govern the platform.

Runs inside the mcpo image (its Python venv already has ``mcp`` and
``httpx``); imports nothing from agentd so the image stays small.

Environment
    EZAI_CONTROL_URL     where ezaid answers (default http://ezaid:8010 — the
                         compose overlay; http://host.docker.internal:8010 for
                         a host daemon started with `make control-serve`)
    EZAI_CONTROL_TOKEN   the service token (required)
    EZAI_ADMIN_URL       base of the Admin Center deep links
                         (default http://localhost:8888)
    EZAI_PLAN_WAIT_S     how long swe_plan waits for the plan (default 180)
"""

from __future__ import annotations

import functools
import os
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

CLIENT_NAME = "swe-server"
API = "/v1"
DEFAULT_CONTROL_URL = "http://ezaid:8010"
DEFAULT_ADMIN_URL = "http://localhost:8888"
DEFAULT_PLAN_WAIT_S = 180.0
TERMINAL = ("completed", "failed", "cancelled")
FOLLOW_HINT = ("Follow it with `swe_status(run_id)`; when it has finished, "
               "`swe_report(run_id)` renders the result and `swe_journal(run_id)` the trail.")
GOVERNANCE_FOOTER = ("Decisions are made in the Admin Center or with the CLI "
                     "(`local-ezai governance approve|reject <id>`) — never from chat.")

#: The catalog (OPENWEBUI_INTEGRATION §2, v1). Order = presentation order.
TOOLS = ("swe_projects", "swe_plan", "swe_run", "swe_sprint", "swe_fix", "swe_evolve",
         "swe_status", "swe_report", "swe_journal", "model_list", "model_explain",
         "governance_queue")
#: Verbs that must never appear as tools (the chat-ops ceiling, §5).
FORBIDDEN_VERBS = ("approve", "reject", "activate", "rollback", "upgrade", "retire",
                   "uninstall", "install", "merge", "push", "cancel")


class ToolError(Exception):
    """A refusal or failure the control plane reported — rendered for the
    model as text (never raised at it), fix included."""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix

    def render(self) -> str:
        return f"⚠️ **{self.code}**: {self.message}" + (f"\n\nFix: {self.fix}" if self.fix else "")


class ControlPlane:
    """The handful of HTTP calls the tools need."""

    def __init__(self, client: httpx.Client, token: str, *, url: str = "") -> None:
        self._client, self._token, self.url = client, token, url or str(client.base_url)

    def _headers(self, mutating: bool) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}", "X-EZAI-Client": CLIENT_NAME}
        if mutating:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        return headers

    def call(self, method: str, path: str, *, json: dict[str, Any] | None = None,
             params: dict[str, Any] | None = None) -> dict[str, Any]:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            response = self._client.request(method, API + path, json=json, params=clean or None,
                                            headers=self._headers(method != "GET"))
        except httpx.HTTPError as exc:
            raise ToolError("control_unreachable",
                            f"the control plane at {self.url} did not answer "
                            f"({type(exc).__name__})",
                            "ask the operator to start it: `make control-up` (container) or "
                            "`make control-serve` (host)") from exc
        if response.status_code >= 400:
            try:
                error = response.json().get("error", {})
            except ValueError:
                error = {}
            raise ToolError(error.get("code") or f"http_{response.status_code}",
                            error.get("message") or response.text[:300], error.get("fix", ""))
        return response.json()

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        return self.call("GET", path, params=params)

    def post(self, path: str, **body: Any) -> dict[str, Any]:
        return self.call("POST", path, json={k: v for k, v in body.items() if v is not None})


def tool(fn: Callable[..., str]) -> Callable[..., str]:
    """Tools answer text, always — a refusal is an answer the model can act on."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            return exc.render()

    return wrapper


# ── markdown renderers (§4: never raw JSON dumps into chat) ──────────────────


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    cell = lambda value: str(value if value is not None else "").replace("|", "\\|")  # noqa: E731
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def render_plan(plan: dict[str, Any]) -> str:
    """A run plan (`PlanTask`: id, kind, intent, check) or a sprint plan
    (`SprintTaskSpec`: id, title, description, depends_on) as one table."""
    rows = [[t.get("id", i + 1), t.get("kind", ""),
             (t.get("title") or t.get("intent") or t.get("description") or "")[:90],
             (t.get("check") or ", ".join(t.get("depends_on", []) or []) or "—")[:60]]
            for i, t in enumerate(plan.get("tasks", []))]
    return "\n".join([f"**Goal:** {plan.get('goal', '')}", "",
                      _table(["task", "kind", "what", "check / depends on"], rows) if rows
                      else "_(no tasks)_"])


def _validation_lines(validation: dict[str, Any] | None) -> list[str]:
    if not validation:
        return ["- validation: _not reached_"]
    marks = [f"{'✅' if c.get('ok') else '❌'} {c.get('name', '')}"
             for c in validation.get("checks", [])]
    lines = [f"- validation: **{'passed' if validation.get('passed') else 'FAILED'}** — "
             + (", ".join(marks) or validation.get("summary", ""))]
    browser = validation.get("browser")
    if browser:
        lines.append(f"- Browser QA: **{'passed' if browser.get('passed') else 'FAILED'}**"
                     + (f" — {browser.get('summary', '')}" if browser.get("summary") else ""))
    return lines


def _review_lines(review: dict[str, Any] | None) -> list[str]:
    if not review:
        return ["- review: _not reached_"]
    lines = [f"- review: **{review.get('verdict', '')}** — {review.get('summary', '')}"]
    for finding in review.get("findings", [])[:10]:
        where = finding.get("file", "") + (f":{finding['line']}" if finding.get("line") else "")
        lines.append(f"  - [{finding.get('severity', '')}] {where}: {finding.get('issue', '')}")
    return lines


def render_run_report(run_id: str, report: dict[str, Any], admin_url: str) -> str:
    lines = [f"## Run `{run_id}` — **{report.get('status', '').upper()}**",
             f"**Task:** {report.get('request', '')}", ""]
    if report.get("plan"):
        lines += [render_plan(report["plan"]), ""]
    lines += _validation_lines(report.get("validation"))
    lines += _review_lines(report.get("review"))
    commit = report.get("commit") or {}
    if commit.get("sha"):
        lines.append(f"- commit: `{commit['sha'][:12]}` on branch `{report.get('branch', '')}`"
                     + (" (pushed)" if commit.get("pushed") else " (not pushed)"))
    else:
        lines.append(f"- branch: `{report.get('branch', '')}` — no commit")
    if report.get("iterations_used"):
        lines.append(f"- self-healing iterations: {report['iterations_used']}")
    if report.get("error"):
        lines.append(f"- error: {report['error']}")
    models = report.get("models_used") or {}
    if models:
        lines += ["", "**Models used:** " + ", ".join(f"{role} → `{model}`"
                                                        for role, model in models.items())]
    lines += ["", f"Admin Center: {admin_url}/runs/{run_id}"]
    return "\n".join(lines)


def render_sprint_report(run_id: str, report: dict[str, Any], admin_url: str) -> str:
    rows = [[t.get("task_id") or t.get("index"), t.get("status"), (t.get("task") or "")[:70],
             (t.get("commit_sha") or "")[:10], (t.get("error") or "")[:60]]
            for t in report.get("tasks", [])]
    lines = [f"## Sprint `{run_id}` — **{report.get('status', '').upper()}**"]
    if report.get("plan"):
        lines.append(f"**Goal:** {report['plan'].get('goal', '')}")
    lines += ["", _table(["task", "status", "what", "commit", "error"], rows) if rows
              else "_(no tasks)_",
              "", f"- branch: `{report.get('branch', '')}` · waves: {report.get('waves', 0)}"]
    if report.get("report_doc"):
        lines.append(f"- sprint report: `{report['report_doc']}`")
    lines += ["", f"Admin Center: {admin_url}/runs/{run_id}"]
    return "\n".join(lines)


def render_evolution_report(run_id: str, report: dict[str, Any], admin_url: str) -> str:
    lines = [f"## Evolution `{run_id}` — **{report.get('status', '').upper()}**"]
    proposal = report.get("proposal") or {}
    if proposal:
        lines.append(f"**Proposal:** {proposal.get('title', '')}")
        lines += [f"- pattern: {p}" for p in proposal.get("failure_patterns", [])[:5]]
        lines += [f"- bottleneck: {b}" for b in proposal.get("bottlenecks", [])[:5]]
    rows = [[t.get("task_id") or t.get("index"), t.get("status"), (t.get("task") or "")[:70],
             (t.get("commit_sha") or "")[:10]] for t in report.get("tasks", [])]
    if rows:
        lines += ["", _table(["task", "status", "what", "commit"], rows)]
    before, after = report.get("benchmark_before"), report.get("benchmark_after")
    if before and after:
        lines.append(f"- benchmark: {'PASS' if before.get('passed') else 'FAIL'} "
                     f"({before.get('duration_seconds', 0)}s) → "
                     f"{'PASS' if after.get('passed') else 'FAIL'} "
                     f"({after.get('duration_seconds', 0)}s)")
    pull_request = report.get("pull_request") or {}
    if pull_request.get("url") or pull_request.get("bundle_path"):
        lines.append(f"- pull request: {pull_request.get('url') or pull_request.get('bundle_path')}"
                     + (f" — {pull_request['note']}" if pull_request.get("note") else ""))
    lines += ["", f"- branch: `{report.get('branch', '')}` — **awaiting human review**",
              f"Admin Center: {admin_url}/runs/{run_id}"]
    return "\n".join(lines)


def render_report(record: dict[str, Any], report: dict[str, Any], admin_url: str) -> str:
    run_id, kind = record["run_id"], record["kind"]
    if kind == "plan":
        return "\n".join([f"## Plan `{run_id}`", render_plan(report), "",
                          "This plan executed nothing (A0 dry-run). To implement it: "
                          f"`swe_run(project=\"{record.get('project_name', '')}\", "
                          f"task=\"…\")`."])
    if kind == "sprint":
        return render_sprint_report(run_id, report, admin_url)
    if kind == "evolve":
        return render_evolution_report(run_id, report, admin_url)
    return render_run_report(run_id, report, admin_url)


def render_status(record: dict[str, Any], admin_url: str) -> str:
    progress = record.get("progress") or {}
    lines = [f"**{record['kind']}** `{record['run_id']}` on **{record.get('project_name', '')}** — "
             f"status **{record['status']}**",
             f"- request: {record.get('request', '')[:200]}",
             f"- submitted {record.get('submitted_at', '')}"
             + (f" · started {record['started_at']}" if record.get("started_at") else "")
             + (f" · finished {record['finished_at']}" if record.get("finished_at") else "")]
    if progress.get("events"):
        lines.append(f"- progress: {progress['events']} journal events, last "
                     f"`{progress.get('last_event')}` at {progress.get('last_ts')}")
    if record.get("cancel_requested"):
        lines.append("- cancellation requested (stops at the next model call)")
    if record.get("error"):
        lines.append(f"- error: {record['error']}")
    if record["status"] in TERMINAL:
        lines.append(f"- next: `swe_report(\"{record['run_id']}\")`"
                     if record["status"] == "completed" else
                     f"- next: `swe_journal(\"{record['run_id']}\")` shows how far it got")
    else:
        lines.append("- next: call `swe_status` again in a while")
    lines.append(f"Admin Center: {admin_url}/runs/{record['run_id']}")
    return "\n".join(lines)


# ── the tools ────────────────────────────────────────────────────────────────


class SweTools:
    """The catalog, as plain methods (registered with FastMCP by
    ``build_server``; exercised directly by the tests)."""

    def __init__(self, plane: ControlPlane, *, admin_url: str = DEFAULT_ADMIN_URL,
                 plan_wait_s: float = DEFAULT_PLAN_WAIT_S,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._plane, self.admin_url = plane, admin_url.rstrip("/")
        self._plan_wait_s, self._sleep, self._clock = plan_wait_s, sleep, clock

    # ── helpers ──────────────────────────────────────────────────────────

    def _start(self, kind: str, project: str, **fields: Any) -> dict[str, Any]:
        return self._plane.post("/runs", kind=kind, project=project, **fields)

    def _started(self, record: dict[str, Any], note: str = "") -> str:
        return "\n".join(filter(None, [
            f"Started **{record['kind']}** `{record['run_id']}` on project "
            f"**{record.get('project_name', '')}** (status: {record['status']}).",
            note, "", FOLLOW_HINT, "",
            f"Admin Center: {self.admin_url}/runs/{record['run_id']}"]))

    def _wait(self, run_id: str, budget_s: float) -> dict[str, Any]:
        deadline = self._clock() + budget_s
        record = self._plane.get(f"/runs/{run_id}")
        while record["status"] not in TERMINAL and self._clock() < deadline:
            self._sleep(0.5)
            record = self._plane.get(f"/runs/{run_id}")
        return record

    # ── projects · start work ────────────────────────────────────────────

    @tool
    def swe_projects(self) -> str:
        """List the repositories the SWE tools may work on (registered by the
        operator with `local-ezai project add`). Use a project's name in the
        other tools."""
        projects = self._plane.get("/projects")["projects"]
        if not projects:
            return ("No projects are registered — the operator registers one with "
                    "`local-ezai project add <path>`.")
        rows = [[p["name"], f"`{p['path']}`", p.get("added_by", "")] for p in projects]
        return "\n".join(["**Registered projects** (the only repositories SWE tools may touch):",
                          "", _table(["project", "path", "registered by"], rows)])

    @tool
    def swe_plan(self, project: str, task: str) -> str:
        """Plan a task on a registered project WITHOUT changing anything (an A0
        dry-run): returns the plan as markdown. Show it to the user and ask
        for confirmation before calling swe_run."""
        record = self._start("plan", project, task=task)
        record = self._wait(record["run_id"], self._plan_wait_s)
        if record["status"] not in TERMINAL:
            return (f"Planning `{record['run_id']}` is still {record['status']} after "
                    f"{self._plan_wait_s:.0f}s — call `swe_report(\"{record['run_id']}\")` "
                    "in a while.")
        if record["status"] != "completed":
            return f"⚠️ planning **{record['status']}**: {record.get('error') or 'no detail'}"
        report = self._plane.get(f"/runs/{record['run_id']}/report")["report"]
        return render_report(record, report, self.admin_url)

    @tool
    def swe_run(self, project: str, task: str) -> str:
        """Start the full autonomous pipeline on a registered project: plan →
        code → validate (tests, lint, Browser QA) → self-heal → review → commit
        on a `swe/<id>` branch. Returns the run id immediately; the run is
        never pushed."""
        return self._started(self._start("run", project, task=task))

    @tool
    def swe_sprint(self, project: str, spec_md: str) -> str:
        """Start an autonomous sprint from a markdown specification: requirement
        analysis → dependency waves → parallel task pipelines → merged commits
        on a `sprint/<id>` branch. Returns the sprint id immediately."""
        return self._started(self._start("sprint", project, spec=spec_md))

    @tool
    def swe_fix(self, project: str, goal: str = "") -> str:
        """Start the self-healing loop in place on a registered project:
        validate → debug → fix → re-validate until green, then commit. Returns
        the run id immediately."""
        return self._started(self._start("fix", project, goal=goal or None))

    @tool
    def swe_evolve(self, project: str, focus: str = "") -> str:
        """Start an evolution cycle on a registered project: analyze history,
        failures and bottlenecks → propose → implement → validate → benchmark
        → a pull request / proposal bundle on an `evolve/<id>` branch. Always
        ends awaiting human approval."""
        return self._started(self._start("evolve", project, focus=focus or None),
                             note="It ends awaiting human review — nothing merges from chat.")

    # ── inspect ──────────────────────────────────────────────────────────

    @tool
    def swe_status(self, run_id: str) -> str:
        """Status of a run / sprint / fix / evolve / plan job: state, timing,
        journal progress, next step."""
        return render_status(self._plane.get(f"/runs/{run_id}"), self.admin_url)

    @tool
    def swe_report(self, run_id: str) -> str:
        """The finished job's report as markdown: goal, plan, validation (incl.
        Browser QA), review verdict and findings, commit and branch, models
        used per stage, Admin Center link."""
        record = self._plane.get(f"/runs/{run_id}")
        try:
            report = self._plane.get(f"/runs/{run_id}/report")["report"]
        except ToolError as exc:
            if exc.code == "report_pending":
                return (f"Run `{run_id}` is still **{record['status']}** — no report yet. "
                        f"`swe_status(\"{run_id}\")` shows its progress.")
            raise
        return render_report(record, report, self.admin_url)

    @tool
    def swe_journal(self, run_id: str, tail: int = 30) -> str:
        """A bounded excerpt of the job's event journal (the last `tail`
        events): what the agents did, in order."""
        tail = max(1, min(int(tail), 200))
        page = self._plane.get(f"/runs/{run_id}/journal", tail=tail)
        if not page["events"]:
            return f"Run `{run_id}` has no journal events yet."
        lines = [f"**Journal of `{run_id}`** — last {len(page['events'])} of {page['total']} "
                 "events:", "```"]
        for event in page["events"]:
            payload = event.get("payload") or {}
            detail = ", ".join(f"{k}={str(v)[:60]}" for k, v in payload.items()
                               if v not in (None, [], ""))
            lines.append(f"{event.get('seq', ''):>4}  {event.get('type', ''):24} {detail[:160]}")
        lines.append("```")
        return "\n".join(lines)

    # ── models · governance (read-only) ──────────────────────────────────

    @tool
    def model_list(self) -> str:
        """The platform's models and their lifecycle states (registry
        generation, runtime, benchmark)."""
        data = self._plane.get("/models")
        rows = [[name, m.get("state"), m.get("runtime"),
                 f"{m['tokens_per_s']:.1f}" if m.get("tokens_per_s") else "—"]
                for name, m in sorted(data.get("models", {}).items())]
        return "\n".join([f"**Models — registry generation {data.get('generation')}**", "",
                          _table(["model", "state", "runtime", "tok/s"], rows) if rows
                          else "_(no models yet)_"])

    @tool
    def model_explain(self, role: str) -> str:
        """Which model serves an agent role (planner, coder, debugger, reviewer,
        …), its fallbacks, why, and whether it meets the role's contract."""
        data = self._plane.get(f"/roles/{role}")
        source = data.get("source") or {}
        origin = ("pin " + ", ".join(source.get("pin", []))) if source.get("pin") \
            else "group " + str(source.get("group", ""))
        fallbacks = ", ".join(f"`{f}`" for f in data.get("fallbacks", [])) or "(none)"
        lines = [f"**Role `{role}`** — generation {data.get('generation')} · {origin}",
                 f"- primary: `{data.get('primary')}`",
                 f"- fallbacks: {fallbacks}"]
        lines += [f"- why: {reason}" for reason in data.get("reason", [])]
        for name, check in (data.get("checks") or {}).items():
            status = "meets the contract" if check.get("ok") else "FAILS the contract"
            detail = "; ".join(check.get("failures", [])) or ", ".join(
                k for k, v in (check.get("checks") or {}).items() if v) or "no requirements"
            lines.append(f"- `{name}` {status} ({detail})")
        return "\n".join(lines)

    @tool
    def governance_queue(self) -> str:
        """The approval queue — pending and decided change requests (model
        activations, upgrades, evolution PRs) with Admin Center links. Read
        only: chat cannot approve or reject."""
        requests = self._plane.get("/governance")["requests"]
        if not requests:
            return "The governance queue is empty.\n\n" + GOVERNANCE_FOOTER
        rows = [[r["id"], r["status"], r["kind"], (r.get("title") or "")[:60],
                 r.get("requested_by", ""), f"{self.admin_url}/governance/{r['id']}"]
                for r in requests]
        pending = sum(1 for r in requests if r["status"] == "pending")
        return "\n".join([f"**Governance queue** — {pending} pending, {len(requests)} total", "",
                          _table(["id", "status", "kind", "title", "by", "Admin Center"], rows),
                          "", GOVERNANCE_FOOTER])


# ── MCP wiring ───────────────────────────────────────────────────────────────


def build_server(tools: SweTools):
    """Register the catalog with FastMCP (stdio server for mcpo)."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(
        "local-ezai-swe",
        instructions=("Local-EZAI autonomous software engineering. You can plan and start "
                      "work on REGISTERED projects and inspect results; you cannot approve, "
                      "merge, activate or roll back anything — direct the human to the Admin "
                      "Center or the CLI for governance. Always show swe_plan's plan and get "
                      "the user's confirmation before swe_run."))
    for name in TOOLS:
        server.tool(name=name)(getattr(tools, name))
    return server


def main() -> int:
    token = os.environ.get("EZAI_CONTROL_TOKEN", "")
    if not token:
        print("swe-server: EZAI_CONTROL_TOKEN is not set — the control plane requires it",
              file=sys.stderr)
        return 2
    url = os.environ.get("EZAI_CONTROL_URL", DEFAULT_CONTROL_URL).rstrip("/")
    plane = ControlPlane(httpx.Client(base_url=url, timeout=httpx.Timeout(None, connect=5.0)),
                         token, url=url)
    tools = SweTools(plane, admin_url=os.environ.get("EZAI_ADMIN_URL", DEFAULT_ADMIN_URL),
                     plan_wait_s=float(os.environ.get("EZAI_PLAN_WAIT_S", DEFAULT_PLAN_WAIT_S)))
    build_server(tools).run()  # stdio, as mcpo expects
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
