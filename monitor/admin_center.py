"""
monitor/admin_center.py
──────────────────────────────────────────────────────────────────────────────
Admin Center pages of the monitor (V1 P4 · PR-16 · ADR-030;
docs/WEBUI_ADMIN_CENTER.md §1–§2, docs/CLI_AND_WEBUI_STRATEGY.md §3).

The monitor renders ONLY what the ezaid control plane (:8010) serves. This
module is a thin client over the frozen 1.0.0 contract plus the pages —
Overview and Runs with a deep-linkable run detail (PR-16), Models / Routing /
Runtime (PR-17). The service token never reaches the browser: the page's
JavaScript calls this service under /api/ezai/…, which calls the daemon with
the token and forwards the monitor login as the human identity (audit actor
"<user> via admin-center"). Nothing here touches files or docker; anything
the pages show, `local-ezai` shows too (parity, not dependence); every
mutation is the CLI's verb, admin only, same-origin guarded.

  GET  /overview · /models · /routing · /runtime · /runs · /runs/{run_id} · /sprints
       · /evolution · /governance · /governance/{request_id} · /memory · /projects   pages
  GET  /api/ezai/overview               health · platform · roles · queue · recent runs
  GET  /api/ezai/runs                   run list (kind / status / project / limit)
  GET  /api/ezai/runs/{run_id}          record + report (once written) + journal tail
  POST /api/ezai/runs/{run_id}/cancel   admin
  GET  /api/ezai/models                 groups in resolution order · fit verdicts · catalog
                                        · generations · queue
  POST /api/ezai/models                 install (catalog id · hf: · gguf:)        admin
  POST /api/ezai/models/upgrade                                                  admin
  POST /api/ezai/models/{name}/benchmark | activate | retire                     admin
  DELETE /api/ezai/models/{name}[?force=true]                                    admin
  POST /api/ezai/generations/rollback                                            admin
  GET  /api/ezai/routing                the explain view (every defined role)
  GET  /api/ezai/runtime                engine slot + per-runtime switch pre-check
  GET  /api/ezai/governance[?status=]   the queue + history with counts
  GET  /api/ezai/governance/{id}        one request: diff · affected roles · evidence · decision
  POST /api/ezai/governance/{id}/approve | reject                                admin
  GET  /api/ezai/projects               the allowlist with each project's work
  POST /api/ezai/projects · DELETE /api/ezai/projects?target=                    admin
  GET  /api/ezai/sprints                sprint runs + reports + dependency graph source
  GET  /api/ezai/evolution              evolution cycles + reports + queued proposals
  POST /api/ezai/runs                   start a sprint or an evolution cycle           admin
  GET  /api/ezai/memory                 a registered project's memory (kind / search)
  POST /api/ezai/memory                 remember a curated rule / style / decision      admin
"""
# ruff: noqa: E501  — the embedded page template carries long markup lines
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Annotated, Any

import httpx
from fastapi import Body, Depends, FastAPI, Header, Query
from fastapi.responses import HTMLResponse, JSONResponse

CLIENT_NAME = "admin-center"
API = "/v1"
DEFAULT_CONTROL_URL = "http://ezaid:8010"
TERMINAL = ("completed", "failed", "cancelled")
#: Roles the Overview explains (ADR-026: roles are the interface, models are
#: evidence). A role the registry does not define is simply not shown.
OVERVIEW_ROLES = ("orchestrator", "planner", "coder", "debugger", "reviewer", "chat")
#: The platform's logical LLM roles (MODEL_ROUTING_DESIGN §3) — what the
#: Routing page explains; a role the registry does not define is reported as such.
ROUTING_ROLES = ("orchestrator", "planner", "coder", "debugger", "reviewer", "memory", "chat",
                 "documentation", "evolution", "sprint")
SAME_ORIGIN_MESSAGE = ("mutations need the X-Requested-With header the Admin Center's own page "
                       "sends")
#: The queue's statuses (governance.RequestStatus), in the order the page counts them.
GOVERNANCE_STATUSES = ("pending", "approved", "applied", "rejected", "failed", "superseded")
#: Change-request fields the queue listing shows; the detail view adds diff,
#: affected roles and evidence (the full proposed registry stays server-side).
REQUEST_SUMMARY_FIELDS = ("id", "kind", "title", "status", "requested_by", "proposed_by",
                          "created_at", "base_generation", "requires_approval", "decision",
                          "result")
UNREACHABLE_FIX = ("start it with `make control-up` (container) or `make control-serve` (host); "
                   "when it runs elsewhere set EZAI_CONTROL_URL_MONITOR in .env")
TOKEN_FIX = ("set EZAI_CONTROL_TOKEN in .env (the value the control plane uses) and "
             "restart the monitor")


class ControlPlaneError(Exception):
    """The daemon's error object — or this service's own for a daemon that
    cannot be reached — kept in the shared ``{"error": {code, message, fix}}``
    shape so the page shows the same text the CLI prints."""

    def __init__(self, status: int, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.fix = status, code, message, fix

    def envelope(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "fix": self.fix}}

    def response(self) -> JSONResponse:
        return JSONResponse(self.envelope(), status_code=self.status)


class ControlPlane:
    """The handful of HTTP calls the pages need — every one authenticated
    with the service token and attributed to the monitor login."""

    def __init__(self, url: str, token: str, *, timeout: float = 20.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = (url or DEFAULT_CONTROL_URL).rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        #: Test seam: an ASGI transport reaches an in-process daemon.
        self.transport = transport

    def headers(self, user: str, mutating: bool) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}", "X-EZAI-Client": CLIENT_NAME,
                   "X-EZAI-User": user}
        if mutating:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        return headers

    async def call(self, method: str, path: str, *, user: str,
                   params: dict[str, Any] | None = None,
                   json: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.token:
            raise ControlPlaneError(503, "control_token_missing",
                                    "the monitor has no EZAI_CONTROL_TOKEN to present to the "
                                    "control plane", TOKEN_FIX)
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        try:
            async with httpx.AsyncClient(base_url=self.url, timeout=self.timeout,
                                         transport=self.transport) as client:
                response = await client.request(method, API + path, params=clean or None,
                                                json=json, headers=self.headers(user, method != "GET"))
        except httpx.HTTPError as exc:
            raise ControlPlaneError(503, "control_unreachable",
                                    f"the control plane at {self.url} did not answer "
                                    f"({type(exc).__name__})", UNREACHABLE_FIX) from exc
        if response.status_code >= 400:
            try:
                error = response.json().get("error", {})
            except ValueError:
                error = {}
            raise ControlPlaneError(response.status_code,
                                    error.get("code") or f"http_{response.status_code}",
                                    error.get("message") or response.text[:300],
                                    error.get("fix", ""))
        return response.json()

    async def get(self, path: str, user: str, **params: Any) -> dict[str, Any]:
        return await self.call("GET", path, user=user, params=params)

    async def post(self, path: str, user: str, **body: Any) -> dict[str, Any]:
        payload = {k: v for k, v in body.items() if v is not None}
        return await self.call("POST", path, user=user, json=payload or None)


# ── page data (aggregated server-side so one request renders one page) ────────


async def overview(plane: ControlPlane, user: str) -> dict[str, Any]:
    """Everything the Overview shows. A daemon that cannot be reached is a
    *state* of the page (``connected: false`` + the fix), not an error."""
    try:
        health = await plane.get("/health", user)
    except ControlPlaneError as exc:
        return {"connected": False, "control_url": plane.url, "error": exc.envelope()["error"]}
    roles = []
    for result in await explain_roles(plane, user, OVERVIEW_ROLES):
        source = result.get("source") or {}
        roles.append({"role": result["role"], "group": source.get("group") or "",
                      "pinned": bool(source.get("pin")), "primary": result.get("primary") or "",
                      "fallbacks": list(result.get("fallbacks") or []),
                      "ok": bool(result.get("ok")), "reason": list(result.get("reason") or [])})
    pending = (await plane.get("/governance", user, status="pending")).get("requests", [])
    runs = await plane.get("/runs", user, limit=8)
    return {
        "connected": True, "control_url": plane.url, "ok": bool(health.get("ok")),
        "control": health.get("control") or {}, "platform": health.get("platform") or {},
        "services": health.get("services") or [], "roles": roles,
        "pending": [{"id": r.get("id"), "kind": r.get("kind"), "title": r.get("title"),
                     "requested_by": r.get("requested_by"), "created_at": r.get("created_at")}
                    for r in pending],
        "runs": runs.get("runs") or [], "active_runs": runs.get("active", 0),
        "limits": {"max_concurrent": runs.get("max_concurrent"),
                   "max_queued": runs.get("max_queued")},
    }


async def run_detail(plane: ControlPlane, user: str, run_id: str, tail: int) -> dict[str, Any]:
    """The record, its report when one has been written, and the journal tail."""
    record = await plane.get(f"/runs/{run_id}", user)
    report = None
    try:
        report = (await plane.get(f"/runs/{run_id}/report", user)).get("report")
    except ControlPlaneError as exc:
        if exc.status not in (404, 409):  # no report (yet) is a normal state
            raise
    journal: dict[str, Any] = {"events": [], "total": 0}
    try:
        journal = await plane.get(f"/runs/{run_id}/journal", user, tail=tail)
    except ControlPlaneError as exc:
        if exc.status != 404:
            raise
    return {"run": record, "report": report, "journal": journal.get("events") or [],
            "journal_total": journal.get("total", 0)}


# ── models · routing · runtime (PR-17) ───────────────────────────────────────


async def explain_roles(plane: ControlPlane, user: str,
                        roles: tuple[str, ...]) -> list[dict[str, Any]]:
    """`GET /v1/roles/{role}` for every role, in one round; a role the
    registry does not define (404) is simply absent from the answer."""
    explained = await asyncio.gather(*(plane.get(f"/roles/{role}", user) for role in roles),
                                     return_exceptions=True)
    out: list[dict[str, Any]] = []
    for role, result in zip(roles, explained, strict=True):
        if isinstance(result, ControlPlaneError):
            continue
        if isinstance(result, BaseException):
            raise result
        out.append({**result, "role": result.get("role") or role})
    return out


async def recommendations(plane: ControlPlane, user: str, groups: list[str],
                          runtime: str | None = None) -> dict[str, dict[str, Any]]:
    """The platform's recommender per group — every catalog candidate with
    its fit verdict on this host (the only source of fit badges)."""
    results = await asyncio.gather(*(plane.get("/catalog/recommendations", user, group=group,
                                               runtime=runtime) for group in groups),
                                   return_exceptions=True)
    out: dict[str, dict[str, Any]] = {}
    for group, result in zip(groups, results, strict=True):
        if isinstance(result, ControlPlaneError):
            out[group] = {"candidates": [], "error": result.envelope()["error"]}
            continue
        if isinstance(result, BaseException):
            raise result
        out[group] = result
    return out


def active_runtime(models: dict[str, Any]) -> str | list[str] | None:
    """The engine slot's runtime: the one runtime the active set uses (a
    list when mixed — the renderer refuses that; None when nothing serves)."""
    runtimes = sorted({m.get("runtime") for m in models.values()
                       if m.get("state") == "active" and m.get("runtime")})
    if len(runtimes) == 1:
        return runtimes[0]
    return runtimes or None


def group_names(models: dict[str, Any], roles: list[dict[str, Any]]) -> list[str]:
    names = {g for m in models.values() for g in (m.get("groups") or [])}
    names |= {(r.get("source") or {}).get("group") for r in roles}
    return sorted(n for n in names if n)


def source_group(role: dict[str, Any]) -> str:
    return (role.get("source") or {}).get("group") or ""


def is_pinned(role: dict[str, Any]) -> bool:
    return bool((role.get("source") or {}).get("pin"))


async def models_page(plane: ControlPlane, user: str) -> dict[str, Any]:
    """Everything the Models page shows: group panels with the serving chain
    (resolution order) and every member, fit badges from the recommender,
    roles, the catalog with per-variant verdicts, generations, the queue."""
    listing = await plane.get("/models", user)
    models: dict[str, Any] = listing.get("models") or {}
    roles = await explain_roles(plane, user, ROUTING_ROLES)
    names = group_names(models, roles)
    runtime = active_runtime(models)
    recs = await recommendations(plane, user, names)  # every runtime; filtered below
    catalog = await plane.get("/catalog", user)
    history = await plane.get("/generations", user, limit=10)
    pending = (await plane.get("/governance", user, status="pending")).get("requests", [])
    candidates = [c for rec in recs.values() for c in rec.get("candidates") or []]
    by_id_provider = {(c["id"], c["provider"]): c for c in candidates}
    runtimes = sorted({c["provider"] for c in candidates}
                      | {m["runtime"] for m in models.values() if m.get("runtime")})
    verdicts: dict[str, dict[str, Any]] = {}
    for c in candidates:
        if runtime is None or c["provider"] == runtime:
            verdicts.setdefault(c["id"], {})[c["format"]] = c

    def card(name: str, serving: bool) -> dict[str, Any]:
        info = models[name]
        match = by_id_provider.get((name, info.get("runtime")))
        return {"name": name, **info, "serving": serving,
                "fit": match["verdict"] if match else None,
                "fit_explain": match["explain"] if match else ""}

    groups = []
    for name in names:
        chain = next(([r["primary"], *(r.get("fallbacks") or [])] for r in roles
                      if source_group(r) == name and not is_pinned(r)), None)
        members = sorted(m for m, info in models.items() if name in (info.get("groups") or []))
        if chain is None:  # no unpinned role resolves through this group: active members
            chain = [m for m in members if models[m].get("state") == "active"]
        chain = [m for m in chain if m in models]
        rec = recs.get(name) or {}
        groups.append({"group": name,
                       "roles": [r["role"] for r in roles if source_group(r) == name],
                       "serving": [card(m, True) for m in chain],
                       "others": [card(m, False) for m in members if m not in chain],
                       "candidates": [c for c in rec.get("candidates") or []
                                      if runtime is None or c["provider"] == runtime]})
    class_ = next((rec.get("class") for rec in recs.values() if rec.get("class")), None)
    return {"generation": listing.get("generation"), "runtime": runtime, "runtimes": runtimes,
            "class": class_, "models": models, "groups": groups,
            "orphans": [card(m, False) for m, info in sorted(models.items())
                        if not info.get("groups")],
            "roles": roles, "catalog": catalog, "verdicts": verdicts,
            "history": history.get("generations") or [],
            "pending": [{"id": r.get("id"), "kind": r.get("kind"), "title": r.get("title"),
                         "requested_by": r.get("requested_by")} for r in pending]}


async def routing_page(plane: ControlPlane, user: str) -> dict[str, Any]:
    """The explain view (MODEL_ROUTING_DESIGN §7): every defined role's
    resolution with its reasons and contract checks, plus the generations."""
    listing = await plane.get("/models", user)
    roles = await explain_roles(plane, user, ROUTING_ROLES)
    history = await plane.get("/generations", user, limit=20)
    return {"generation": listing.get("generation"), "models": listing.get("models") or {},
            "roles": roles, "known_roles": list(ROUTING_ROLES),
            "history": history.get("generations") or []}


async def runtime_page(plane: ControlPlane, user: str) -> dict[str, Any]:
    """The engine slot and, for every other runtime the descriptors serve, a
    switch pre-check (RUNTIME_ABSTRACTION §5): which active models lack a
    variant for it and which catalog candidates fit per group."""
    health = await plane.get("/health", user)
    platform = health.get("platform") or {}
    models: dict[str, Any] = platform.get("models") or {}
    roles = await explain_roles(plane, user, ROUTING_ROLES)
    names = group_names(models, roles)
    recs = await recommendations(plane, user, names)
    candidates = [c for rec in recs.values() for c in rec.get("candidates") or []]
    runtimes = sorted({c["provider"] for c in candidates}
                      | {m["runtime"] for m in models.values() if m.get("runtime")})
    active = active_runtime(models)
    active_models = [{"name": n, **m} for n, m in sorted(models.items())
                     if m.get("state") == "active"]
    serving_groups = {source_group(r) for r in roles}
    prechecks = []
    for target in runtimes:
        if target == active:
            continue
        per_group = []
        for name in names:
            cands = [c for c in (recs.get(name) or {}).get("candidates") or []
                     if c["provider"] == target]
            per_group.append({"group": name, "candidates": cands,
                              "eligible": sum(1 for c in cands if c.get("eligible"))})
        variants = {c["id"] for c in candidates if c["provider"] == target}
        blockers = [{"name": m["name"], "runtime": m.get("runtime"), "format": m.get("format"),
                     "variant_in_catalog": m["name"] in variants}
                    for m in active_models if m.get("runtime") != target]
        ready = all(b["variant_in_catalog"] for b in blockers) and all(
            g["eligible"] for g in per_group if g["group"] in serving_groups)
        prechecks.append({"runtime": target, "groups": per_group, "active_models": blockers,
                          "ready": ready})
    return {"platform": platform, "ok": bool(health.get("ok")),
            "services": [s for s in health.get("services") or []
                         if s.get("id") in ("engine", "router")],
            "active": active, "runtimes": runtimes, "active_models": active_models,
            "prechecks": prechecks, "generation": platform.get("generation")}


# ── governance (PR-18) ───────────────────────────────────────────────────────


def request_summary(request: dict[str, Any]) -> dict[str, Any]:
    evidence = request.get("evidence") or {}
    return {**{k: request.get(k) for k in REQUEST_SUMMARY_FIELDS},
            "affected_roles": sorted(request.get("affected_roles") or {}),
            "runtime_switch": bool((evidence.get("runtime") or {}).get("switch"))}


async def governance_page(plane: ControlPlane, user: str,
                          status: str | None = None) -> dict[str, Any]:
    """The queue (pending first) and the history, with counts per status."""
    listing = await plane.get("/models", user)
    everything = (await plane.get("/governance", user)).get("requests") or []
    requests = [r for r in everything if status is None or r.get("status") == status]
    counts = {s: sum(1 for r in everything if r.get("status") == s) for s in GOVERNANCE_STATUSES}
    return {"generation": listing.get("generation"), "status": status,
            "requests": [request_summary(r) for r in requests], "counts": counts,
            "pending": counts["pending"]}


async def governance_detail(plane: ControlPlane, user: str, request_id: str) -> dict[str, Any]:
    """One change request with everything the approval view shows. The
    proposed registry dump is not human evidence — the diff, the affected
    roles and the evidence block are — so it stays on the daemon."""
    listing = await plane.get("/models", user)
    request = dict((await plane.get(f"/governance/{request_id}", user)).get("request") or {})
    request.pop("proposed", None)
    return {"generation": listing.get("generation"), "request": request,
            "reversible_to": request.get("base_generation")}


# ── projects · sprints · evolution · memory (PR-19) ─────────────────────────

#: Kinds the console starts (WEBUI_PRODUCT_STRATEGY §3.5 / §3.6, zero-CLI
#: journeys 3 and 4); plain runs, fixes and plans start from the CLI or chat.
CONSOLE_STARTS = ("sprint", "evolve")
#: Memory kinds a human may add by hand (agentd.memory.CURATED_KINDS) and all
#: kinds the store records (agentd.memory.ALL_KINDS).
CURATED_MEMORY_KINDS = ("project_rule", "coding_style", "architecture_decision")
MEMORY_KINDS = ("project_rule", "coding_style", "architecture_decision", "failed_fix",
                "successful_fix", "implementation")


async def reports_for(plane: ControlPlane, user: str,
                      runs: list[dict[str, Any]]) -> dict[str, Any]:
    """The reports of the finished runs, in one round; a run that ended
    without one is simply absent (its journal tells the story)."""
    terminal = [r for r in runs if r.get("status") in TERMINAL]
    results = await asyncio.gather(*(plane.get(f"/runs/{r['run_id']}/report", user)
                                     for r in terminal), return_exceptions=True)
    out: dict[str, Any] = {}
    for run, result in zip(terminal, results, strict=True):
        if isinstance(result, ControlPlaneError):
            continue
        if isinstance(result, BaseException):
            raise result
        out[run["run_id"]] = result.get("report")
    return out


def mermaid_of(plan: dict[str, Any]) -> str:
    """The sprint's dependency graph as mermaid source — the same graph the
    runtime writes into the sprint report document inside the repository."""
    lines = ["graph LR"]
    for task in plan.get("tasks") or []:
        task_id = str(task.get("id") or "")
        title = str(task.get("title") or "")[:40].replace('"', "'")
        lines.append(f'  {task_id}["{task_id}: {title}"]')
        lines.extend(f"  {dep} --> {task_id}" for dep in task.get("depends_on") or [])
    return "\n".join(lines)


async def projects_page(plane: ControlPlane, user: str) -> dict[str, Any]:
    projects = (await plane.get("/projects", user)).get("projects") or []
    runs = (await plane.get("/runs", user, limit=500)).get("runs") or []  # newest first
    work: dict[str, dict[str, Any]] = {}
    for run in runs:
        entry = work.setdefault(run.get("project_name"), {"runs": 0, "active": 0, "last": None})
        entry["runs"] += 1
        if run.get("status") not in TERMINAL:
            entry["active"] += 1
        if entry["last"] is None:
            entry["last"] = {k: run.get(k) for k in ("run_id", "kind", "status", "submitted_at")}
    return {"projects": [{**p, **work.get(p.get("name"), {"runs": 0, "active": 0, "last": None})}
                         for p in projects]}


async def sprints_page(plane: ControlPlane, user: str, limit: int = 20) -> dict[str, Any]:
    listing = await plane.get("/runs", user, kind="sprint", limit=limit)
    runs = listing.get("runs") or []
    reports = await reports_for(plane, user, runs)
    projects = (await plane.get("/projects", user)).get("projects") or []
    items = []
    for run in runs:
        report = reports.get(run["run_id"])
        plan = (report or {}).get("plan") or {}
        items.append({"run": run, "report": report,
                      "mermaid": mermaid_of(plan) if plan.get("tasks") else ""})
    return {"sprints": items, "active": listing.get("active", 0),
            "projects": [p["name"] for p in projects]}


async def evolution_page(plane: ControlPlane, user: str, limit: int = 20) -> dict[str, Any]:
    listing = await plane.get("/runs", user, kind="evolve", limit=limit)
    runs = listing.get("runs") or []
    reports = await reports_for(plane, user, runs)
    projects = (await plane.get("/projects", user)).get("projects") or []
    pending = (await plane.get("/governance", user, status="pending")).get("requests") or []
    proposals = [request_summary(r) for r in pending
                 if r.get("proposed_by") == "evolution" or r.get("kind") == "evolution_pr"]
    return {"cycles": [{"run": run, "report": reports.get(run["run_id"])} for run in runs],
            "active": listing.get("active", 0), "projects": [p["name"] for p in projects],
            "queue": proposals}


async def memory_page(plane: ControlPlane, user: str, *, project: str | None = None,
                      kind: str | None = None, search: str | None = None,
                      limit: int = 100) -> dict[str, Any]:
    projects = [p["name"] for p in (await plane.get("/projects", user)).get("projects") or []]
    project = project or (projects[0] if projects else None)
    memory = None
    if project:
        memory = await plane.get(f"/projects/{project}/memory", user, kind=kind, search=search,
                                 limit=limit)
    return {"projects": projects, "project": project, "kind": kind, "search": search,
            "memory": memory, "kinds": list(MEMORY_KINDS), "curated": list(CURATED_MEMORY_KINDS)}


#: A JSON object body, optional (module scope: annotations are postponed and
#: FastAPI resolves them against the module's globals).
Payload = Annotated[dict[str, Any] | None, Body()]


def same_origin(x_requested_with: str | None) -> JSONResponse | None:
    """The monitor login is ambient (HTTP Basic): a cross-site form could post
    with the browser's credentials. Forms cannot set custom headers — the
    page's JavaScript does, so every mutation requires one."""
    if x_requested_with == CLIENT_NAME:
        return None
    return ControlPlaneError(400, "same_origin_required", SAME_ORIGIN_MESSAGE,
                            "use the buttons on the page, or send "
                            f"'X-Requested-With: {CLIENT_NAME}' with the request").response()


# ── routes ────────────────────────────────────────────────────────────────────


def install(app: FastAPI, plane: ControlPlane, *, viewer: Callable[..., Any],
            admin: Callable[..., Any]) -> None:
    """Register the Admin Center routes on the monitor app. ``viewer`` /
    ``admin`` are the monitor's RBAC dependencies (they resolve to the login
    role, which is forwarded as the human identity)."""
    app.state.control_plane = plane

    @app.get("/api/ezai/overview")
    async def ezai_overview(role: str = Depends(viewer)):
        try:
            return await overview(plane, role)
        except ControlPlaneError as exc:
            return exc.response()

    @app.get("/api/ezai/runs")
    async def ezai_runs(kind: str | None = None, status: str | None = None,
                        project: str | None = None,
                        limit: int = Query(50, ge=1, le=500), role: str = Depends(viewer)):
        try:
            return await plane.get("/runs", role, kind=kind, status=status, project=project,
                                   limit=limit)
        except ControlPlaneError as exc:
            return exc.response()

    @app.get("/api/ezai/runs/{run_id}")
    async def ezai_run(run_id: str, tail: int = Query(60, ge=1, le=1000),
                       role: str = Depends(viewer)):
        try:
            return await run_detail(plane, role, run_id, tail)
        except ControlPlaneError as exc:
            return exc.response()

    async def mutate(x_requested_with: str | None, call: Callable[[], Any]):
        """Admin mutations: same-origin guard, then the daemon's answer or
        its error object, unchanged."""
        refused = same_origin(x_requested_with)
        if refused is not None:
            return refused
        try:
            return await call()
        except ControlPlaneError as exc:
            return exc.response()

    @app.post("/api/ezai/runs/{run_id}/cancel")
    async def ezai_cancel(run_id: str, role: str = Depends(admin),
                          x_requested_with: str | None = Header(None)):
        return await mutate(x_requested_with, lambda: plane.post(f"/runs/{run_id}/cancel", role))

    # ── models · routing · runtime (PR-17) ───────────────────────────────

    @app.get("/api/ezai/models")
    async def ezai_models(role: str = Depends(viewer)):
        try:
            return {**await models_page(plane, role), "role": role}
        except ControlPlaneError as exc:
            return exc.response()

    @app.get("/api/ezai/routing")
    async def ezai_routing(role: str = Depends(viewer)):
        try:
            return {**await routing_page(plane, role), "role": role}
        except ControlPlaneError as exc:
            return exc.response()

    @app.get("/api/ezai/runtime")
    async def ezai_runtime(role: str = Depends(viewer)):
        try:
            return {**await runtime_page(plane, role), "role": role}
        except ControlPlaneError as exc:
            return exc.response()

    @app.post("/api/ezai/models")
    async def ezai_install(body: Payload = None, role: str = Depends(admin),
                           x_requested_with: str | None = Header(None)):
        data = body or {}
        fields = {k: data.get(k) for k in ("ref", "name", "group", "runtime", "refetch")}
        return await mutate(x_requested_with, lambda: plane.post("/models", role, **fields))

    @app.post("/api/ezai/models/upgrade")
    async def ezai_upgrade(body: Payload = None, role: str = Depends(admin),
                           x_requested_with: str | None = Header(None)):
        data = body or {}
        return await mutate(x_requested_with, lambda: plane.post(
            "/models/upgrade", role, old=data.get("old"), new=data.get("new")))

    @app.post("/api/ezai/models/{name}/benchmark")
    async def ezai_benchmark(name: str, role: str = Depends(admin),
                             x_requested_with: str | None = Header(None)):
        return await mutate(x_requested_with,
                            lambda: plane.post(f"/models/{name}/benchmark", role))

    @app.post("/api/ezai/models/{name}/activate")
    async def ezai_activate(name: str, body: Payload = None, role: str = Depends(admin),
                            x_requested_with: str | None = Header(None)):
        data = body or {}
        return await mutate(x_requested_with, lambda: plane.post(
            f"/models/{name}/activate", role, group=data.get("group"), role=data.get("role"),
            position=data.get("position")))

    @app.post("/api/ezai/models/{name}/retire")
    async def ezai_retire(name: str, role: str = Depends(admin),
                          x_requested_with: str | None = Header(None)):
        return await mutate(x_requested_with, lambda: plane.post(f"/models/{name}/retire", role))

    @app.delete("/api/ezai/models/{name}")
    async def ezai_uninstall(name: str, force: bool = False, role: str = Depends(admin),
                             x_requested_with: str | None = Header(None)):
        return await mutate(x_requested_with, lambda: plane.call(
            "DELETE", f"/models/{name}", user=role, params={"force": "true"} if force else None))

    @app.post("/api/ezai/generations/rollback")
    async def ezai_rollback(body: Payload = None, role: str = Depends(admin),
                            x_requested_with: str | None = Header(None)):
        data = body or {}
        return await mutate(x_requested_with, lambda: plane.post(
            "/generations/rollback", role, to_generation=data.get("to_generation"),
            reason=data.get("reason") or ""))

    # ── governance (PR-18): the queue, the approval view, the two decisions ──

    @app.get("/api/ezai/governance")
    async def ezai_governance(status: str | None = None, role: str = Depends(viewer)):
        try:
            return {**await governance_page(plane, role, status), "role": role}
        except ControlPlaneError as exc:
            return exc.response()

    @app.get("/api/ezai/governance/{request_id}")
    async def ezai_governance_detail(request_id: str, role: str = Depends(viewer)):
        try:
            return {**await governance_detail(plane, role, request_id), "role": role}
        except ControlPlaneError as exc:
            return exc.response()

    @app.post("/api/ezai/governance/{request_id}/approve")
    async def ezai_approve(request_id: str, body: Payload = None, role: str = Depends(admin),
                           x_requested_with: str | None = Header(None)):
        data = body or {}
        return await mutate(x_requested_with, lambda: plane.post(
            f"/governance/{request_id}/approve", role, reason=data.get("reason") or ""))

    @app.post("/api/ezai/governance/{request_id}/reject")
    async def ezai_reject(request_id: str, body: Payload = None, role: str = Depends(admin),
                          x_requested_with: str | None = Header(None)):
        data = body or {}
        return await mutate(x_requested_with, lambda: plane.post(
            f"/governance/{request_id}/reject", role, reason=data.get("reason") or ""))

    # ── projects · sprints · evolution · memory (PR-19) ──────────────────

    @app.get("/api/ezai/projects")
    async def ezai_projects(role: str = Depends(viewer)):
        try:
            return {**await projects_page(plane, role), "role": role}
        except ControlPlaneError as exc:
            return exc.response()

    @app.post("/api/ezai/projects")
    async def ezai_project_add(body: Payload = None, role: str = Depends(admin),
                               x_requested_with: str | None = Header(None)):
        data = body or {}
        payload = {k: data.get(k) for k in ("path", "name") if data.get(k) is not None}
        return await mutate(x_requested_with, lambda: plane.call(
            "POST", "/projects", user=role, json=payload))

    @app.delete("/api/ezai/projects")
    async def ezai_project_remove(target: str, role: str = Depends(admin),
                                  x_requested_with: str | None = Header(None)):
        return await mutate(x_requested_with, lambda: plane.call(
            "DELETE", "/projects", user=role, params={"target": target}))

    @app.get("/api/ezai/sprints")
    async def ezai_sprints(limit: int = Query(20, ge=1, le=100), role: str = Depends(viewer)):
        try:
            return {**await sprints_page(plane, role, limit), "role": role}
        except ControlPlaneError as exc:
            return exc.response()

    @app.get("/api/ezai/evolution")
    async def ezai_evolution(limit: int = Query(20, ge=1, le=100), role: str = Depends(viewer)):
        try:
            return {**await evolution_page(plane, role, limit), "role": role}
        except ControlPlaneError as exc:
            return exc.response()

    @app.post("/api/ezai/runs")
    async def ezai_start(body: Payload = None, role: str = Depends(admin),
                         x_requested_with: str | None = Header(None)):
        data = body or {}
        kind = data.get("kind")

        async def start():
            if kind not in CONSOLE_STARTS:
                raise ControlPlaneError(
                    400, "invalid_request",
                    f"the console starts {' and '.join(CONSOLE_STARTS)} jobs; a '{kind}' job "
                    "starts from the CLI or chat",
                    "local-ezai run|fix|plan on the host, or the Orchestrator persona in chat")
            fields = {k: data.get(k) for k in ("project", "spec", "focus", "simple",
                                                "keep_going", "max_parallel")}
            return await plane.post("/runs", role, kind=kind, **fields)

        return await mutate(x_requested_with, start)

    @app.get("/api/ezai/memory")
    async def ezai_memory(project: str | None = None, kind: str | None = None,
                          search: str | None = None, limit: int = Query(100, ge=1, le=500),
                          role: str = Depends(viewer)):
        try:
            return {**await memory_page(plane, role, project=project, kind=kind, search=search,
                                        limit=limit), "role": role}
        except ControlPlaneError as exc:
            return exc.response()

    @app.post("/api/ezai/memory")
    async def ezai_memory_add(body: Payload = None, role: str = Depends(admin),
                              x_requested_with: str | None = Header(None)):
        data = body or {}
        project = data.get("project") or ""
        return await mutate(x_requested_with, lambda: plane.post(
            f"/projects/{project}/memory", role, kind=data.get("kind"), text=data.get("text")))

    # ── pages: one template, the path picks the view ─────────────────────

    for path in ("/overview", "/models", "/routing", "/runtime", "/runs", "/governance",
                 "/projects", "/sprints", "/evolution", "/memory"):
        @app.get(path, response_class=HTMLResponse, name=f"page_{path.strip('/')}")
        async def admin_page(role: str = Depends(viewer)) -> str:
            return ADMIN_HTML

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(run_id: str, role: str = Depends(viewer)) -> str:
        return ADMIN_HTML

    @app.get("/governance/{request_id}", response_class=HTMLResponse)
    async def request_page(request_id: str, role: str = Depends(viewer)) -> str:
        return ADMIN_HTML


# ── the page (one template; the path decides the view) ───────────────────────
ADMIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Local-EZAI Admin Center</title>
<style>
  :root {
    --bg:    #0f1117;
    --card:  #1a1d27;
    --border:#2a2d3a;
    --text:  #e2e8f0;
    --muted: #6b7280;
    --green: #22c55e;
    --red:   #ef4444;
    --yellow:#eab308;
    --blue:  #3b82f6;
    --font: 'Inter', system-ui, -apple-system, sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--font);
         font-size: 14px; min-height: 100vh; display: flex; flex-direction: column; }
  header { background: var(--card); border-bottom: 1px solid var(--border);
           padding: 16px 24px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  header h1 { font-size: 18px; font-weight: 600; }
  header .subtitle { color: var(--muted); font-size: 13px; }
  nav.nav { display: flex; gap: 4px; margin-left: auto; }
  nav.nav a { color: var(--muted); text-decoration: none; padding: 6px 12px; border-radius: 6px;
              font-size: 13px; font-weight: 600; }
  nav.nav a.active, nav.nav a:hover { background: var(--bg); color: var(--text); }
  main { padding: 24px; flex: 1; width: 100%; max-width: 1240px; }
  section.card, .card { background: var(--card); border: 1px solid var(--border);
          border-radius: 10px; padding: 18px; }
  section + section { margin-top: 16px; }
  .card h2, h2.sec { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .5px;
                     color: var(--muted); margin-bottom: 12px; }
  .card-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .card-header .name { font-weight: 600; font-size: 15px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 12px; }
  .kv label { display: block; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .4px; }
  .kv .v { font-weight: 600; font-size: 13px; margin-top: 2px; overflow-wrap: anywhere; }
  .badge { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 12px;
           text-transform: uppercase; letter-spacing: .5px; white-space: nowrap; }
  .badge.ok, .badge.completed, .badge.passed, .badge.approve { background: rgba(34,197,94,.15); color: var(--green); }
  .badge.down, .badge.failed, .badge.block { background: rgba(239,68,68,.15); color: var(--red); }
  .badge.running { background: rgba(59,130,246,.15); color: var(--blue); }
  .badge.queued { background: rgba(234,179,8,.15); color: var(--yellow); }
  .badge.cancelled, .badge.unknown { background: rgba(107,114,128,.2); color: var(--muted); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--muted); font-weight: 600; font-size: 12px; padding: 6px 8px;
       border-bottom: 1px solid var(--border); }
  td { padding: 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
  tr:last-child td { border-bottom: 0; }
  a { color: var(--blue); }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
         background: var(--bg); padding: 1px 5px; border-radius: 4px; }
  .muted { color: var(--muted); }
  .banner { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px; }
  .banner.warn { border-left: 3px solid var(--yellow); }
  .banner.err { border-left: 3px solid var(--red); }
  .banner .muted { margin-top: 6px; font-size: 12px; }
  .toolbar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
  select { background: var(--bg); color: var(--text); border: 1px solid var(--border);
           border-radius: 6px; padding: 6px 10px; font-size: 13px; }
  button.btn { background: var(--blue); color: white; border: 0; border-radius: 6px; padding: 7px 14px;
               font-size: 13px; font-weight: 600; cursor: pointer; }
  button.btn.danger { background: var(--red); }
  button.btn.sm { padding: 3px 9px; font-size: 12px; background: var(--border); color: var(--text); margin: 1px 2px; }
  button.btn.sm.primary { background: var(--blue); color: white; }
  button.btn.sm.danger { background: rgba(239,68,68,.25); color: var(--red); }
  button.btn:disabled { opacity: .5; cursor: wait; }
  input { background: var(--bg); color: var(--text); border: 1px solid var(--border);
          border-radius: 6px; padding: 6px 10px; font-size: 13px; }
  .notice { padding: 0 24px; font-size: 13px; }
  .notice:not(:empty) { padding: 10px 24px; border-bottom: 1px solid var(--border); }
  .notice.ok { color: var(--green); }
  .notice.err { color: var(--red); }
  .badge.fit { text-transform: none; letter-spacing: 0; }
  pre.journal { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
                background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
                padding: 12px; overflow: auto; max-height: 420px; white-space: pre; }
  ul.checks, ol.heal { margin-left: 18px; line-height: 1.7; }
  .chip { display: inline-block; font-size: 12px; background: var(--bg); border: 1px solid var(--border);
          border-radius: 12px; padding: 3px 10px; margin: 2px 4px 2px 0; }
  footer { padding: 12px 24px; color: var(--muted); font-size: 12px; border-top: 1px solid var(--border); }
  @media (max-width: 640px) { nav.nav { margin-left: 0; } }
</style>
</head>
<body>
<header>
  <div>
    <h1>Local-EZAI Admin Center</h1>
    <div class="subtitle" id="subtitle">Loading…</div>
  </div>
  <nav class="nav" id="nav">
    <a href="/overview" data-nav="overview">Overview</a>
    <a href="/models" data-nav="models">Models</a>
    <a href="/routing" data-nav="routing">Routing</a>
    <a href="/runtime" data-nav="runtime">Runtime</a>
    <a href="/runs" data-nav="runs">Runs</a>
    <a href="/sprints" data-nav="sprints">Sprints</a>
    <a href="/evolution" data-nav="evolution">Evolution</a>
    <a href="/governance" data-nav="governance">Governance</a>
    <a href="/memory" data-nav="memory">Memory</a>
    <a href="/projects" data-nav="projects">Projects</a>
    <a href="/" data-nav="health">Health &amp; Knowledge</a>
  </nav>
</header>
<div id="notice" class="notice"></div>
<main id="app"><div class="muted">Loading…</div></main>
<footer id="footer"></footer>

<script>
const $ = id => document.getElementById(id);
const TERMINAL = ['completed', 'failed', 'cancelled'];
const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const short = (s, n) => { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
const when = ts => ts ? esc(String(ts).replace('T', ' ')) : '—';
const badge = s => `<span class="badge ${esc(s || 'unknown')}">${esc(s || 'unknown')}</span>`;
const card = (title, body) => `<section class="card"><h2>${title}</h2>${body}</section>`;
const runtimeName = p => Array.isArray(p.slot_runtime) ? (p.slot_runtime.join(' + ') || '—') : (p.slot_runtime || '—');

const route = (() => {
  const p = location.pathname.replace(/\\/+$/, '');
  const m = p.match(/^\\/runs\\/([^/]+)$/);
  if (m) return { view: 'run', id: decodeURIComponent(m[1]) };
  const g = p.match(/^\\/governance\\/([^/]+)$/);
  if (g) return { view: 'request', id: decodeURIComponent(g[1]) };
  if (p === '/runs') return { view: 'runs' };
  if (['/models', '/routing', '/runtime', '/governance', '/projects', '/sprints', '/evolution', '/memory'].includes(p)) return { view: p.slice(1) };
  return { view: 'overview' };
})();
const NAV_OF = { run: 'runs', request: 'governance' };
document.querySelectorAll('#nav a').forEach(a => {
  if (a.dataset.nav === (NAV_OF[route.view] || route.view)) a.classList.add('active');
});

async function api(path, opts) {
  const r = await fetch(path, opts);
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  if (!r.ok) {
    const e = (body && body.error) || { code: 'http_' + r.status,
      message: (body && body.detail) || r.statusText || ('HTTP ' + r.status), fix: '' };
    if (r.status === 403 && !(body && body.error)) e.code = 'forbidden';
    throw e;
  }
  return body;
}
const errorBanner = (e, title) => `<div class="banner err"><strong>${esc(title || e.code)}</strong> — ${esc(e.message)}` +
  (e.fix ? `<div class="muted">Fix: ${esc(e.fix)}</div>` : '') + `</div>`;
const footer = html => { $('footer').innerHTML = html; };
const fail = (e, title) => {
  $('app').innerHTML = errorBanner(e, title);
  footer(`error <code>${esc(e.code)}</code> · CLI: <code>local-ezai status</code>`);
};

// ── Overview ─────────────────────────────────────────────────────
async function renderOverview() {
  document.title = 'Overview · Local-EZAI Admin Center';
  let d;
  try { d = await api('/api/ezai/overview'); } catch (e) { fail(e); return; }
  if (!d.connected) {
    $('subtitle').textContent = 'Control plane unreachable';
    $('app').innerHTML = errorBanner(d.error, 'Control plane unreachable') +
      `<p class="muted" style="margin-top:12px">Service health and the knowledge base stay available under ` +
      `<a href="/">Health &amp; Knowledge</a>.</p>`;
    footer(`control plane: <code>${esc(d.control_url)}</code> · CLI: <code>local-ezai status</code>`);
    return;
  }
  const p = d.platform || {}, c = d.control || {};
  $('subtitle').textContent = `${d.ok ? 'Platform healthy' : 'Platform degraded'} · generation ${p.generation ?? '—'}` +
    ` · ${p.capability_class || '—'} · runtime ${runtimeName(p)}`;
  const services = (d.services || []).map(s => `<div class="kv"><label>${esc(s.id)}</label>` +
    `<div class="v">${badge(s.ok ? 'ok' : 'down')} <span class="muted">${s.latency_ms != null ? s.latency_ms + ' ms' : ''}` +
    `${s.ok ? '' : ' ' + esc(short(s.error, 70))}</span></div></div>`).join('');
  const roles = (d.roles || []).map(r => `<div class="card"><div class="card-header"><span class="name">${esc(r.role)}</span>` +
    `${badge(r.ok ? 'ok' : 'down')}</div><div class="muted" style="font-size:12px">${r.pinned ? '📌 pinned' : 'group ' + esc(r.group || '—')}</div>` +
    `<div style="margin-top:8px;font-weight:600">${esc(r.primary || '—')}</div>` +
    `<div class="muted" style="font-size:12px">${r.fallbacks.length ? '+' + r.fallbacks.length + ' fallback' + (r.fallbacks.length > 1 ? 's' : '') +
      ': ' + esc(r.fallbacks.join(', ')) : 'no fallback'}</div></div>`).join('');
  const pending = d.pending || [];
  const gov = pending.length
    ? `<div class="banner warn"><strong>⚠ ${pending.length} item${pending.length > 1 ? 's' : ''} await your approval</strong> <a href="/governance">Open Governance</a>` +
      `<table style="margin-top:8px">${pending.map(q => `<tr><td><a href="/governance/${encodeURIComponent(q.id)}"><code>${esc(q.id)}</code></a></td><td>${esc(q.kind)}</td>` +
      `<td>${esc(q.title)}</td><td class="muted">${esc(q.requested_by)} · ${when(q.created_at)}</td></tr>`).join('')}</table>` +
      `<div class="muted">Every item shows its evidence next to the decision · CLI twin: <code>local-ezai governance approve|reject &lt;id&gt;</code></div></div>`
    : `<div class="banner"><strong>Governance queue empty</strong> <span class="muted">— nothing awaits approval · <a href="/governance">history</a></span></div>`;
  const runs = d.runs || [];
  const rows = runs.length ? runs.map(runRow).join('')
    : `<tr><td colspan="7" class="muted">no runs yet — start one with <code>local-ezai run</code>, the Orchestrator persona in chat, or the SWE tools</td></tr>`;
  $('app').innerHTML =
    card('Stack health <span class="muted">· as the control plane sees it</span>', `<div class="grid">${services || '<div class="muted">no services probed</div>'}</div>`) +
    `<section><h2 class="sec">Roles → models</h2><div class="grid">${roles || '<div class="muted">no roles explained</div>'}</div></section>` +
    `<section>${gov}</section>` +
    card(`Recent runs <span class="muted">· ${d.active_runs} active · limits ${d.limits.max_concurrent} concurrent + ${d.limits.max_queued} queued · <a href="/runs">all runs</a></span>`,
         `<table><thead>${RUN_HEAD}</thead><tbody>${rows}</tbody></table>`);
  footer(`rendered from generation ${p.generation ?? '—'} (rendered artifacts: ${p.rendered_generation ?? '—'}) · registry <code>${esc(p.config_dir || '')}</code>` +
    ` · control plane <code>${esc(d.control_url)}</code> v${esc(c.version || '?')} (contract ${esc(c.contract || '?')}) · CLI: <code>local-ezai status</code>`);
}

// ── Runs ─────────────────────────────────────────────────────────
const RUN_HEAD = `<tr><th>run</th><th>kind</th><th>project</th><th>status</th><th>request</th><th>by</th><th>submitted</th></tr>`;
function runRow(r) {
  return `<tr><td><a href="/runs/${encodeURIComponent(r.run_id)}"><code>${esc(r.run_id)}</code></a></td><td>${esc(r.kind)}</td>` +
    `<td>${esc(r.project_name)}</td><td>${badge(r.status)}${r.cancel_requested ? ' <span class="muted">cancel requested</span>' : ''}</td>` +
    `<td>${esc(short(r.request, 90))}</td><td class="muted">${esc(r.actor)}</td><td class="muted">${when(r.submitted_at)}</td></tr>`;
}
let timer;
async function renderRuns() {
  document.title = 'Runs · Local-EZAI Admin Center';
  $('subtitle').textContent = 'Agents & Runs';
  const params = new URLSearchParams(location.search);
  const kind = params.get('kind') || '', status = params.get('status') || '', project = params.get('project') || '';
  const q = new URLSearchParams();
  if (kind) q.set('kind', kind);
  if (status) q.set('status', status);
  if (project) q.set('project', project);
  let d;
  try { d = await api('/api/ezai/runs' + (q.toString() ? '?' + q : '')); } catch (e) { fail(e); return; }
  const sel = (name, value, options) => `<select data-filter="${name}">${options.map(o =>
    `<option value="${o}"${o === value ? ' selected' : ''}>${o || 'all ' + name + 's'}</option>`).join('')}</select>`;
  $('app').innerHTML = `<section class="card"><div class="toolbar">` +
    sel('kind', kind, ['', 'run', 'fix', 'sprint', 'evolve', 'plan']) + sel('status', status, ['', 'queued', 'running', 'completed', 'failed', 'cancelled']) +
    (project ? `<span class="chip">project: ${esc(project)} <a href="/runs">×</a></span>` : '') +
    `<span class="muted">${d.active} active · limits ${d.max_concurrent} concurrent + ${d.max_queued} queued</span></div>` +
    `<table><thead>${RUN_HEAD}</thead><tbody>${d.runs.length ? d.runs.map(runRow).join('') : '<tr><td colspan="7" class="muted">no runs match</td></tr>'}</tbody></table></section>`;
  document.querySelectorAll('select[data-filter]').forEach(s => s.onchange = () => {
    const p = new URLSearchParams(location.search);
    if (s.value) p.set(s.dataset.filter, s.value); else p.delete(s.dataset.filter);
    location.search = p.toString();
  });
  footer(`${d.runs.length} run(s) shown · CLI: <code>ezai runs</code> · chat: <code>swe_status(run_id)</code>`);
  clearTimeout(timer);
  if (d.runs.some(r => !TERMINAL.includes(r.status))) timer = setTimeout(renderRuns, 4000);
}

// ── Run detail ───────────────────────────────────────────────────
const planTable = plan => {
  const rows = (plan && plan.tasks || []).map((t, i) => `<tr><td>${esc(t.id ?? i + 1)}</td><td>${esc(t.kind || '')}</td>` +
    `<td>${esc(short(t.title || t.intent || t.description || '', 120))}</td>` +
    `<td class="muted">${esc(short(t.check || (t.depends_on || []).join(', ') || '—', 80))}</td></tr>`).join('');
  return `<div><strong>Goal:</strong> ${esc(plan && plan.goal || '')}</div>` +
    (rows ? `<table style="margin-top:8px"><thead><tr><th>task</th><th>kind</th><th>what</th><th>check / depends on</th></tr></thead><tbody>${rows}</tbody></table>`
          : '<div class="muted">no tasks</div>');
};
const tasksTable = tasks => (tasks && tasks.length)
  ? `<table style="margin-top:8px"><thead><tr><th>task</th><th>status</th><th>what</th><th>commit</th><th>error</th></tr></thead><tbody>` +
    tasks.map(t => `<tr><td>${esc(t.task_id ?? t.index)}</td><td>${badge(t.status)}</td><td>${esc(short(t.task, 90))}</td>` +
    `<td><code>${esc((t.commit_sha || '').slice(0, 10))}</code></td><td class="muted">${esc(short(t.error, 80))}</td></tr>`).join('') + `</tbody></table>`
  : '';
const bench = (b, a) => (b && a)
  ? `<div>benchmark: ${badge(b.passed ? 'passed' : 'failed')} ${b.duration_seconds ?? 0}s → ${badge(a.passed ? 'passed' : 'failed')} ${a.duration_seconds ?? 0}s</div>` : '';
const checks = v => (v.checks || []).map(c => `<li>${c.ok ? '✅' : '❌'} ${esc(c.name)} <span class="muted">${esc(short(c.command, 90))}</span>` +
  (!c.ok && c.output_tail ? `<pre class="journal">${esc(short(c.output_tail, 2000))}</pre>` : '') + `</li>`).join('');

function reportSections(kind, rep, run) {
  const out = [];
  if (kind === 'plan') return card('Plan <span class="muted">· A0 dry-run — nothing executed</span>', planTable(rep));
  if (kind === 'sprint') {
    out.push(card('Sprint', `<div><strong>Goal:</strong> ${esc(rep.plan && rep.plan.goal || '')}</div>` +
      `<div class="muted">branch <code>${esc(rep.branch)}</code> · waves ${rep.waves ?? 0}${rep.report_doc ? ` · report <code>${esc(rep.report_doc)}</code>` : ''}</div>` + tasksTable(rep.tasks)));
  } else if (kind === 'evolve') {
    const pr = rep.pull_request || {}, prop = rep.proposal;
    out.push(card('Evolution proposal', prop ? `<div><strong>${esc(prop.title)}</strong></div>` +
      (prop.failure_patterns || []).map(x => `<div class="muted">pattern: ${esc(x)}</div>`).join('') +
      (prop.bottlenecks || []).map(x => `<div class="muted">bottleneck: ${esc(x)}</div>`).join('') : '<div class="muted">no proposal</div>'));
    out.push(card('Evidence', bench(rep.benchmark_before, rep.benchmark_after) +
      (pr.url || pr.bundle_path ? `<div>pull request: ${pr.url ? `<a href="${esc(pr.url)}">${esc(pr.url)}</a>` : `<code>${esc(pr.bundle_path)}</code>`} ${esc(pr.note || '')}</div>` : '') +
      `<div class="muted">branch <code>${esc(rep.branch)}</code> — <strong>awaiting human review</strong>; merging stays on the forge</div>` + tasksTable(rep.tasks)));
  } else {
    if (rep.plan) out.push(card('Plan', planTable(rep.plan)));
    const v = rep.validation;
    out.push(card('Validation', v ? `<div>${badge(v.passed ? 'passed' : 'failed')} ${esc(v.summary || '')}</div><ul class="checks">${checks(v)}</ul>` +
      (v.browser ? `<div style="margin-top:6px">Browser QA ${badge(v.browser.passed ? 'passed' : 'failed')} ${esc(v.browser.summary || '')}</div>` : '')
      : '<div class="muted">not reached</div>'));
    const rv = rep.review;
    out.push(card('Review', rv ? `<div>${badge(rv.verdict)} ${esc(rv.summary || '')}</div><ul class="checks">` +
      (rv.findings || []).map(f => `<li>[${esc(f.severity)}] <code>${esc(f.file)}${f.line ? ':' + f.line : ''}</code> ${esc(f.issue)}</li>`).join('') + `</ul>`
      : '<div class="muted">not reached</div>'));
    if (rep.healing && rep.healing.length) out.push(card(`Self-healing · ${rep.healing.length} iteration${rep.healing.length > 1 ? 's' : ''}`,
      `<ol class="heal">${rep.healing.map(h => `<li>${esc((h.categories || []).join(', '))}: ${esc(h.root_cause)} → ${esc(h.approach)} ${badge(h.revalidation_passed ? 'passed' : 'failed')}</li>`).join('')}</ol>`));
    const c = rep.commit || {};
    out.push(card('Delivery', c.sha
      ? `<div>commit <code>${esc(c.sha.slice(0, 12))}</code> on branch <code>${esc(rep.branch)}</code> — ${c.pushed ? 'pushed' : '<strong>not pushed</strong>'} · ${c.files_committed ?? 0} file(s)</div>` +
        `<div class="muted" style="margin-top:6px">How to merge (stays human on purpose): <code>git -C ${esc(run.project)} merge ${esc(rep.branch)}</code></div>`
      : `<div class="muted">branch <code>${esc(rep.branch)}</code> — no commit</div>`));
    const models = rep.models_used || {};
    if (Object.keys(models).length) out.push(card('Models used', Object.entries(models).map(([role, m]) =>
      `<span class="chip">${esc(role)} → <code>${esc(m)}</code></span>`).join('')));
  }
  if (rep.error) out.push(`<section class="banner err"><strong>error</strong> — ${esc(rep.error)}</section>`);
  return out.join('');
}

async function renderRun(id) {
  let d;
  try { d = await api('/api/ezai/runs/' + encodeURIComponent(id)); }
  catch (e) { $('subtitle').textContent = 'Run not found'; fail(e); return; }
  const r = d.run, rep = d.report || {};
  const active = !TERMINAL.includes(r.status);
  document.title = `Run ${r.run_id} · Local-EZAI Admin Center`;
  $('subtitle').textContent = `${r.kind} ${r.run_id} on ${r.project_name} — ${r.status}`;
  const progress = r.progress ? `${r.progress.events} journal events · last <code>${esc(r.progress.last_event)}</code> ${when(r.progress.last_ts)}` : '';
  const cancel = active ? `<button class="btn danger" id="cancel-btn">Cancel run</button> <span class="muted" id="cancel-status">` +
    `${r.cancel_requested ? 'cancellation requested — stops at the next model call' : ''}</span>` : '';
  const head = `<section class="card"><div class="card-header"><span class="name">${esc(r.kind)} <code>${esc(r.run_id)}</code> on <strong>${esc(r.project_name)}</strong></span>${badge(r.status)}</div>` +
    `<div class="grid" style="margin-top:12px">` +
    `<div class="kv"><label>request</label><div class="v">${esc(r.request)}</div></div>` +
    `<div class="kv"><label>started by</label><div class="v">${esc(r.actor)}</div></div>` +
    `<div class="kv"><label>submitted</label><div class="v">${when(r.submitted_at)}</div></div>` +
    `<div class="kv"><label>started</label><div class="v">${when(r.started_at)}</div></div>` +
    `<div class="kv"><label>finished</label><div class="v">${when(r.finished_at)}</div></div>` +
    `<div class="kv"><label>project path</label><div class="v"><code>${esc(r.project)}</code></div></div></div>` +
    (progress ? `<div class="muted" style="margin-top:8px">${progress}</div>` : '') +
    (r.error ? `<div class="banner err" style="margin-top:8px"><strong>error</strong> — ${esc(r.error)}</div>` : '') +
    (cancel ? `<div style="margin-top:12px">${cancel}</div>` : '') + `</section>`;
  const sections = d.report ? reportSections(r.kind, rep, r)
    : card('Report', `<div class="muted">${active ? 'not written yet — the run is ' + esc(r.status) : 'no report on disk — the journal below shows how far it got'}</div>`);
  const lines = d.journal.map(e => `${String(e.ts || '').replace('T', ' ')}  ${String(e.type || '').padEnd(16)} ${short(JSON.stringify(e.payload || {}), 180)}`);
  const journal = card(`Journal <span class="muted">· last ${d.journal.length} of ${d.journal_total} events · <code>ezai journal ${esc(r.run_id)}</code></span>`,
    `<pre class="journal">${esc(lines.join('\\n')) || 'no events yet'}</pre>`);
  $('app').innerHTML = head + sections + journal;
  const btn = $('cancel-btn');
  if (btn) btn.onclick = async () => {
    if (!confirm('Cancel this run? A running job stops at its next model call.')) return;
    btn.disabled = true;
    try {
      await api('/api/ezai/runs/' + encodeURIComponent(id) + '/cancel',
                { method: 'POST', headers: { 'X-Requested-With': 'admin-center' } });
      $('cancel-status').textContent = 'cancellation requested';
    }
    catch (e) { $('cancel-status').textContent = '✗ ' + e.message + (e.fix ? ' — ' + e.fix : ''); btn.disabled = false; }
  };
  footer(`run <code>${esc(r.run_id)}</code> · CLI: <code>local-ezai explain-run ${esc(r.run_id)}</code> · chat: <code>swe_report("${esc(r.run_id)}")</code>`);
  clearTimeout(timer);
  if (active) timer = setTimeout(() => renderRun(id), 3000);
}

// ── Models · Routing · Runtime (PR-17) ───────────────────────────
const isAdmin = d => d.role === 'admin';
const runtimeLabel = r => Array.isArray(r) ? (r.join(' + ') || 'none') : (r || 'none');
const tps = m => m.tokens_per_s ? m.tokens_per_s + ' tok/s' : 'not benchmarked';
const fitBadge = v => !v
  ? '<span class="badge fit unknown" title="not a catalog model on this runtime — no fit verdict; benchmarks are the evidence">fit: no verdict</span>'
  : `<span class="badge fit ${v.fits ? 'ok' : 'down'}" title="${esc((v.warnings || []).join(' · ') || ('needs ~' + v.required_memory_gb + ' GB of ' + v.available_memory_gb + ' GB'))}">` +
    `${v.fits ? '✓ fits' : '✗ no fit'} · ${esc(v.placement)} · ${esc(v.speed_band)}</span>`;
const contractText = c => Object.entries(c || {}).filter(([k, v]) => v).map(([k, v]) => v === true ? k : `${k} ≥ ${v}`).join(', ') || 'none';
const rolesTable = roles => `<table><thead><tr><th>role</th><th>source</th><th>primary</th><th>fallbacks</th><th>contract</th><th></th></tr></thead><tbody>` +
  roles.map(r => { const s = r.source || {}; return `<tr><td><strong>${esc(r.role)}</strong></td>` +
    `<td>${s.pin && s.pin.length ? '📌 ' + esc(s.pin.join(' → ')) : 'group ' + esc(s.group)}</td><td>${esc(r.primary)}</td>` +
    `<td class="muted">${esc((r.fallbacks || []).join(', ') || '—')}</td><td class="muted">${esc(contractText(r.contract))}</td>` +
    `<td>${badge(r.ok ? 'ok' : 'down')}</td></tr>`; }).join('') + `</tbody></table>`;
const historyTable = (history, extra) => `<table><thead><tr><th>gen</th><th>saved</th><th>note</th><th>active set</th><th>changes</th>${extra ? '<th></th>' : ''}</tr></thead><tbody>` +
  history.map(h => `<tr><td><strong>${h.generation}</strong></td><td class="muted">${when(h.saved_at)}</td><td>${esc(h.note)}</td>` +
    `<td>${esc((h.active || []).join(', '))}</td><td class="muted">${(h.diff || []).map(esc).join('<br>') || '—'}</td>${extra ? '<td>' + extra(h) + '</td>' : ''}</tr>`).join('') + `</tbody></table>`;

function report(text, ok) { const el = $('notice'); el.textContent = text; el.className = 'notice ' + (ok ? 'ok' : 'err'); }
async function act(path, method, body) {
  return api(path, { method, headers: { 'X-Requested-With': 'admin-center', 'Content-Type': 'application/json' },
                     body: body === undefined ? undefined : JSON.stringify(body) });
}
async function run(label, call, render, okOf, after) {
  report(`${label}…`, true);
  try { const r = await call(); report(render(r), okOf ? okOf(r) : true); setTimeout(after || renderModels, 800); }
  catch (e) { report(`✗ ${label}: ${e.message}${e.fix ? ' — fix: ' + e.fix : ''}`, false); }
}
const proposalText = r => { const q = r.request || {};
  if (r.applied) return `${q.id}: applied — ${r.applied.message}`;
  const affected = Object.keys(q.affected_roles || {});
  return `${q.id} queued for approval (${affected.length ? 'affects ' + affected.join(', ') : 'runtime switch'}) — review it at /governance/${q.id}`; };

async function modelAction(ds, d) {
  const n = ds.name, groups = d.groups.map(g => g.group), path = `/api/ezai/models/${encodeURIComponent(n || '')}`;
  switch (ds.act) {
    case 'benchmark': return run(`benchmark ${n}`, () => act(path + '/benchmark', 'POST'), r => `${r.message} — state ${r.state}`);
    case 'activate': { const g = prompt(`Activate ${n} as primary of which group? (${groups.join(', ')})`, groups[0] || '');
      if (!g) return; return run(`activate ${n} in ${g}`, () => act(path + '/activate', 'POST', { group: g }), proposalText); }
    case 'upgrade': { const to = prompt(`Upgrade ${n} to which benchmarked model? (swaps it in every group and pin; ${n} is retired)`);
      if (!to) return; return run(`upgrade ${n} → ${to}`, () => act('/api/ezai/models/upgrade', 'POST', { old: n, new: to }), proposalText); }
    case 'retire': if (!confirm(`Retire ${n}? It leaves resolution; the weights stay for rollback.`)) return;
      return run(`retire ${n}`, () => act(path + '/retire', 'POST'), r => r.message);
    case 'uninstall': { if (!confirm(`Uninstall ${n} and delete its weights?`)) return;
      const force = confirm('Force, even if a stored generation could still roll back to it? (Cancel = no force)');
      return run(`uninstall ${n}`, () => act(path + (force ? '?force=true' : ''), 'DELETE'), r => r.message); }
    case 'install': return run(`install ${ds.ref} (${ds.runtime})`, () => act('/api/ezai/models', 'POST', { ref: ds.ref, runtime: ds.runtime }),
      r => `${r.message}${r.ok ? ' — next: benchmark ' + r.name : r.error ? ' — ' + r.error : ''}`, r => r.ok);
    case 'rollback': { const reason = prompt(`Roll back to generation ${ds.gen}? Rollback applies at once (no approval queue) and is audited. Reason:`);
      if (reason === null) return; return run(`rollback to generation ${ds.gen}`, () => act('/api/ezai/generations/rollback', 'POST', { to_generation: Number(ds.gen), reason }), r => r.message, r => r.ok); }
  }
}

async function renderModels() {
  document.title = 'Models · Local-EZAI Admin Center';
  let d; try { d = await api('/api/ezai/models'); } catch (e) { fail(e); return; }
  const admin = isAdmin(d), runtime = runtimeLabel(d.runtime);
  $('subtitle').textContent = `Models · generation ${d.generation ?? '—'} · runtime ${runtime}${d.class ? ' · class ' + d.class : ''}`;
  const actions = m => { if (!admin) return ''; const b = [];
    if (['installed', 'benchmarked', 'active'].includes(m.state)) b.push(`<button class="btn sm" data-act="benchmark" data-name="${esc(m.name)}">benchmark</button>`);
    if (m.state === 'benchmarked' || m.state === 'retired') b.push(`<button class="btn sm primary" data-act="activate" data-name="${esc(m.name)}">activate…</button>`);
    if (m.state === 'active') b.push(`<button class="btn sm" data-act="upgrade" data-name="${esc(m.name)}">upgrade…</button>`, `<button class="btn sm" data-act="retire" data-name="${esc(m.name)}">retire</button>`);
    if (['retired', 'failed', 'registered'].includes(m.state)) b.push(`<button class="btn sm danger" data-act="uninstall" data-name="${esc(m.name)}">uninstall</button>`);
    return b.join(''); };
  const head = `<tr><th>#</th><th>model</th><th>state</th><th>runtime · format</th><th>size · context</th><th>measured here</th><th>fit</th><th></th></tr>`;
  const row = (m, i) => `<tr><td class="muted">${i == null ? '' : i + 1}</td><td><strong>${esc(m.name)}</strong>${m.license ? `<div class="muted">${esc(m.license)}</div>` : ''}</td>` +
    `<td>${badge(m.state)}</td><td>${esc(m.runtime)} · ${esc(m.format || '?')}</td><td>${(m.size_gb || 0).toFixed(1)} GB · ctx ${m.context || '?'}</td>` +
    `<td>${esc(tps(m))}</td><td>${fitBadge(m.fit)}</td><td>${actions(m)}</td></tr>`;
  const groups = d.groups.map(g => card(`${esc(g.group)} <span class="muted">· roles: ${esc(g.roles.join(', ') || 'none')} · ${g.serving.length} serving · ${g.candidates.length} catalog candidate${g.candidates.length === 1 ? '' : 's'} on ${esc(runtime)}</span>`,
    `<table><thead>${head}</thead><tbody>${g.serving.map(row).join('')}${g.others.map(m => row(m, null)).join('')}` +
    `${!g.serving.length && !g.others.length ? '<tr><td colspan="8" class="muted">no member</td></tr>' : ''}</tbody></table>`)).join('');
  const orphans = d.orphans.length ? card('Not in any group', `<table><thead>${head}</thead><tbody>${d.orphans.map(m => row(m, null)).join('')}</tbody></table>`) : '';
  const pending = d.pending || [];
  const gov = pending.length ? `<div class="banner warn"><strong>⚠ ${pending.length} change request${pending.length > 1 ? 's' : ''} await approval:</strong> ` +
    pending.map(q => `<a href="/governance/${encodeURIComponent(q.id)}"><code>${esc(q.id)}</code></a> ${esc(q.title)}`).join(' · ') +
    `<div class="muted"><a href="/governance">Open Governance</a> — evidence next to every decision · CLI twin: <code>local-ezai governance approve|reject &lt;id&gt;</code></div></div>` : '';
  const add = admin ? card('Add model <span class="muted">· a catalog id, or any hf:&lt;org/repo&gt; / gguf:&lt;url | path&gt; source (equal citizens)</span>',
    `<form id="install-form" class="toolbar"><input name="ref" placeholder="hf:Org/Repo · gguf:… · catalog id" required size="42">` +
    `<input name="name" placeholder="name (optional)" size="16"><select name="group"><option value="">group (optional)</option>${d.groups.map(g => `<option>${esc(g.group)}</option>`).join('')}</select>` +
    `<select name="runtime"><option value="">runtime (by format)</option>${(d.runtimes || []).map(r => `<option>${esc(r)}</option>`).join('')}</select><button class="btn" type="submit">Install</button></form>`) : '';
  const cat = card(`Catalog <span class="muted">· ${d.catalog.count} entries · fit verdicts for ${esc(runtime)} on class ${esc(d.class || '?')} · <code>local-ezai model catalog</code></span>`,
    `<table><thead><tr><th>entry</th><th>groups</th><th>license</th><th>context</th><th>variants · fit on this host</th></tr></thead><tbody>` +
    Object.entries(d.catalog.entries).sort().map(([id, e]) => `<tr><td><strong>${esc(e.display_name || id)}</strong><div class="muted"><code>${esc(id)}</code></div></td>` +
      `<td>${esc((e.groups || []).join(', '))}</td><td>${esc(e.license)}</td><td>${e.context}</td><td>` +
      Object.entries(e.variants).map(([fmt, v]) => { const c = (d.verdicts[id] || {})[fmt];
        return `<div>${esc(fmt)} · ${v.size_gb} GB${v.quant ? ' · ' + esc(v.quant) : ''} ` +
          (c ? fitBadge(c.verdict) + (c.contract_failures.length ? ` <span class="muted" title="${esc(c.contract_failures.join(' · '))}">contract ✗</span>` : '') : `<span class="muted">not served by ${esc(runtime)}</span>`) +
          (admin && c ? ` <button class="btn sm" data-act="install" data-ref="${esc(id)}" data-runtime="${esc(c.provider)}">install</button>` : '') + `</div>`; }).join('') +
      `</td></tr>`).join('') + `</tbody></table>`);
  const hist = card('Generations <span class="muted">· <code>local-ezai model history</code> · rollback restores an approved state at once</span>',
    historyTable(d.history, admin ? h => (h.generation !== d.generation ? `<button class="btn sm danger" data-act="rollback" data-gen="${h.generation}">roll back to ${h.generation}</button>` : '<span class="muted">current</span>') : null));
  $('app').innerHTML = gov + add + groups + orphans + card('Roles <span class="muted">· <a href="/routing">explain routing</a></span>', rolesTable(d.roles)) + cat + hist;
  const form = $('install-form');
  if (form) form.onsubmit = async e => { e.preventDefault(); const f = new FormData(form); const body = { ref: f.get('ref') };
    for (const k of ['name', 'group', 'runtime']) if (f.get(k)) body[k] = f.get(k);
    await run(`install ${body.ref}`, () => act('/api/ezai/models', 'POST', body), r => `${r.message}${r.ok ? ' — next: benchmark ' + r.name : r.error ? ' — ' + r.error : ''}`, r => r.ok); };
  document.querySelectorAll('button[data-act]').forEach(b => b.onclick = () => modelAction(b.dataset, d));
  footer(`rendered from generation ${d.generation ?? '—'} · runtime ${esc(runtime)} · CLI twins: <code>local-ezai model install|benchmark|activate|upgrade|rollback|retire|uninstall</code>`);
}

async function renderRouting() {
  document.title = 'Routing · Local-EZAI Admin Center';
  let d; try { d = await api('/api/ezai/routing'); } catch (e) { fail(e); return; }
  $('subtitle').textContent = `Routing · generation ${d.generation ?? '—'} · role → group → model`;
  const defined = new Set(d.roles.map(r => r.role));
  const cards = d.roles.map(r => { const s = r.source || {};
    const checks = Object.entries(r.checks || {}).map(([m, c]) => `<li>${c.ok ? '✅' : '❌'} <strong>${esc(m)}</strong> <span class="muted">${c.failures && c.failures.length ? esc(c.failures.join('; ')) : esc(Object.entries(c.checks || {}).filter(([k, v]) => v).map(([k]) => k).join(', ') || 'no requirements')}</span></li>`).join('');
    return `<section class="card" id="role-${esc(r.role)}"><div class="card-header"><span class="name">${esc(r.role)} <span class="muted">→ ${s.pin && s.pin.length ? '📌 pin ' + esc(s.pin.join(' → ')) : 'group ' + esc(s.group)}</span></span>${badge(r.ok ? 'ok' : 'down')}</div>` +
      `<div style="margin-top:8px"><strong>${esc(r.primary || '—')}</strong> <span class="muted">${r.fallbacks && r.fallbacks.length ? '→ ' + esc(r.fallbacks.join(' → ')) : '(no fallback)'}</span></div>` +
      `<ul class="checks" style="margin-top:6px">${(r.reason || []).map(x => `<li class="muted">${esc(x)}</li>`).join('')}</ul>` +
      `<div class="muted" style="margin-top:6px">contract: ${esc(contractText(r.contract))}</div><ul class="checks">${checks}</ul>` +
      `<div class="muted">resolved from generation ${r.generation}</div></section>`; }).join('');
  const missing = d.known_roles.filter(r => !defined.has(r));
  $('app').innerHTML = card('Standing table <span class="muted">· what serves each role now · <code>local-ezai models</code></span>',
      rolesTable(d.roles) + (missing.length ? `<div class="muted" style="margin-top:8px">roles not defined in this registry: ${esc(missing.join(', '))}</div>` : '')) +
    cards + card('Generation history <span class="muted">· diffs between consecutive generations</span>', historyTable(d.history));
  footer(`generation ${d.generation ?? '—'} · CLI: <code>local-ezai model explain &lt;role&gt;</code> · <code>local-ezai model history</code>`);
}

async function renderRuntime() {
  document.title = 'Runtime · Local-EZAI Admin Center';
  let d; try { d = await api('/api/ezai/runtime'); } catch (e) { fail(e); return; }
  const p = d.platform || {}, active = runtimeLabel(d.active);
  $('subtitle').textContent = `Runtime · engine slot: ${active} · class ${p.capability_class || '—'}`;
  const health = (d.services || []).map(s => `${esc(s.id)} ${badge(s.ok ? 'ok' : 'down')}`).join(' ') || '<span class="muted">not probed</span>';
  const activeCard = card('Active runtime <span class="muted">· the single engine slot behind the router</span>',
    `<div class="grid"><div class="kv"><label>runtime</label><div class="v">${esc(active)}</div></div>` +
    `<div class="kv"><label>capability class</label><div class="v">${esc(p.capability_class || '—')} · accelerator ${esc(p.accelerator || 'none')}</div></div>` +
    `<div class="kv"><label>this host</label><div class="v">${p.system_memory_gb ?? '?'} GB RAM · ${p.cpu_cores ?? '?'} cores</div></div>` +
    `<div class="kv"><label>slot health</label><div class="v">${health}</div></div>` +
    `<div class="kv"><label>generation</label><div class="v">${p.generation ?? '—'} (rendered ${p.rendered_generation ?? '—'})</div></div></div>` +
    `<table style="margin-top:10px"><thead><tr><th>active model</th><th>runtime</th><th>format</th><th>groups</th><th>measured</th></tr></thead><tbody>` +
    (d.active_models.map(m => `<tr><td><strong>${esc(m.name)}</strong></td><td>${esc(m.runtime)}</td><td>${esc(m.format || '?')}</td><td class="muted">${esc((m.groups || []).join(', '))}</td><td>${esc(tps(m))}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">no active model</td></tr>') + `</tbody></table>`);
  const pre = d.prechecks.map(pc => card(`Switch to ${esc(pc.runtime)} — pre-check ${badge(pc.ready ? 'ok' : 'down')}`,
    `<div><strong>Active models and ${esc(pc.runtime)}:</strong></div><ul class="checks">` +
    (pc.active_models.map(m => m.variant_in_catalog
      ? `<li>✓ ${esc(m.name)} — the catalog has a ${esc(pc.runtime)} variant: install it, then activate it</li>`
      : `<li>✗ ${esc(m.name)} is ${esc(m.format || '?')} (${esc(m.runtime)}) — no ${esc(pc.runtime)} variant in the catalog: install one (<code>hf:</code> / <code>gguf:</code>) on the <a href="/models">Models page</a> or keep ${esc(active)}</li>`).join('') || '<li class="muted">none</li>') + `</ul>` +
    `<div style="margin-top:8px"><strong>Catalog candidates per group on ${esc(pc.runtime)}:</strong></div>` +
    pc.groups.map(g => `<div style="margin-top:6px"><span class="chip">${esc(g.group)} · ${g.eligible}/${g.candidates.length} eligible</span><ul class="checks">` +
      (g.candidates.map(c => `<li>${c.eligible ? '✅' : '❌'} <code>${esc(c.id)}</code> ${esc(c.format)} ${fitBadge(c.verdict)}${c.contract_failures.length ? ' <span class="muted">' + esc(c.contract_failures.join('; ')) + '</span>' : ''}</li>`).join('') || '<li class="muted">no catalog entry served by this runtime</li>') + `</ul></div>`).join('') +
    `<div class="muted" style="margin-top:8px">How a switch happens: activate a model served by ${esc(pc.runtime)} (Models page). The activation request is flagged as a runtime switch and needs approval; on approval the next generation is rendered and the slot reloaded, with the previous generation one rollback away. The single engine slot serves one runtime — models of ${esc(active)} become unservable until they have a ${esc(pc.runtime)} variant.</div>`)).join('');
  $('app').innerHTML = activeCard + (pre || card('Other runtimes', '<div class="muted">no other runtime serves a catalog entry on this host</div>'));
  footer(`runtimes known to this host: ${esc(d.runtimes.join(', '))} · CLI: <code>local-ezai model catalog --group &lt;g&gt; --runtime &lt;r&gt;</code> · <code>local-ezai status</code>`);
}

// ── Governance (PR-18): the queue, the approval view, the two decisions ───
const chainText = c => c ? `<strong>${esc(c.primary)}</strong>${(c.fallbacks || []).length ? ' <span class="muted">→ ' + esc(c.fallbacks.join(' → ')) + '</span>' : ''}` : '<span class="muted">—</span>';
const REQ_HEAD = `<tr><th>request</th><th>status</th><th>kind</th><th>what</th><th>by</th><th>affects</th><th>created</th><th>decision</th></tr>`;
const reqRow = r => `<tr><td><a href="/governance/${encodeURIComponent(r.id)}"><code>${esc(r.id)}</code></a></td><td>${badge(r.status)}</td><td>${esc(r.kind)}</td>` +
  `<td>${esc(r.title)}</td><td class="muted">${esc(r.requested_by)}${r.proposed_by && r.proposed_by !== 'human' ? ' · proposed by ' + esc(r.proposed_by) : ''}</td>` +
  `<td class="muted">${esc((r.affected_roles || []).join(', ')) || (r.runtime_switch ? 'runtime switch' : (r.requires_approval ? '—' : 'policy'))}</td>` +
  `<td class="muted">${when(r.created_at)}</td><td class="muted">${r.decision ? esc(r.decision.by) + (r.decision.reason ? ' — “' + esc(r.decision.reason) + '”' : '') : ''}` +
  `${r.result && r.result.generation ? ` → gen ${r.result.generation}` : ''}</td></tr>`;

async function renderGovernance() {
  document.title = 'Governance · Local-EZAI Admin Center';
  const status = new URLSearchParams(location.search).get('status') || '';
  let d; try { d = await api('/api/ezai/governance' + (status ? '?status=' + encodeURIComponent(status) : '')); } catch (e) { fail(e); return; }
  $('subtitle').textContent = `Governance · ${d.counts.pending} pending · generation ${d.generation ?? '—'}`;
  const pending = d.requests.filter(r => r.status === 'pending'), decided = d.requests.filter(r => r.status !== 'pending');
  const queue = (!status || status === 'pending') ? card(`Awaiting approval <span class="muted">· ${pending.length} · agents propose, humans approve</span>`,
    pending.length ? `<table><thead>${REQ_HEAD}</thead><tbody>${pending.map(reqRow).join('')}</tbody></table>`
                   : '<div class="muted">nothing awaits approval</div>') : '';
  const filter = `<div class="toolbar"><select data-filter="status"><option value="">all statuses</option>` +
    ['pending', 'approved', 'applied', 'rejected', 'failed', 'superseded'].map(s => `<option value="${s}"${s === status ? ' selected' : ''}>${s} (${d.counts[s]})</option>`).join('') + `</select></div>`;
  const history = card('History <span class="muted">· every decision is an immutable audit record · <code>local-ezai governance list</code></span>',
    filter + (decided.length ? `<table><thead>${REQ_HEAD}</thead><tbody>${decided.map(reqRow).join('')}</tbody></table>` : '<div class="muted">no decided request' + (status ? ' with this status' : ' yet') + '</div>'));
  const note = `<div class="banner"><strong>One queue, three item types.</strong> <span class="muted">Model activations and upgrades enter it today (a request that changes the engine runtime is flagged as a runtime switch). ` +
    `Evolution proposals and release candidates join when their pipelines submit change requests — until then evolution runs end on the <a href="/runs?kind=evolve">Runs page</a> awaiting human review.</span></div>`;
  $('app').innerHTML = queue + history + note;
  document.querySelectorAll('select[data-filter]').forEach(s => s.onchange = () => { location.search = s.value ? 'status=' + encodeURIComponent(s.value) : ''; });
  footer(`generation ${d.generation ?? '—'} · CLI: <code>local-ezai governance list|show|approve|reject</code>`);
}

async function decide(kind, r) {
  const reason = prompt(kind === 'approve' ? `Approve ${r.id} and apply it now? Reason (optional):` : `Reject ${r.id}? A reason is required — it is recorded (and, for evolution proposals, remembered):`, '');
  if (reason === null) return;
  if (kind === 'reject' && !reason.trim()) { report('✗ a rejection needs a reason', false); return; }
  report(`${kind} ${r.id}…`, true);
  try {
    const out = await act(`/api/ezai/governance/${encodeURIComponent(r.id)}/${kind}`, 'POST', { reason });
    const q = out.request || {};
    report(out.applied ? `${q.id} approved — ${out.applied.message}` : `${q.id} ${q.status}`, out.applied ? out.applied.ok : true);
    setTimeout(() => renderRequest(r.id), 800);
  } catch (e) { report(`✗ ${kind} ${r.id}: ${e.message}${e.fix ? ' — fix: ' + e.fix : ''}`, false); }
}

async function renderRequest(id) {
  let d; try { d = await api('/api/ezai/governance/' + encodeURIComponent(id)); }
  catch (e) { $('subtitle').textContent = 'Request not found'; fail(e); return; }
  const r = d.request, ev = r.evidence || {}, rt = ev.runtime || {}, admin = isAdmin(d), pending = r.status === 'pending';
  document.title = `${r.id} · Governance · Local-EZAI Admin Center`;
  $('subtitle').textContent = `${r.kind} ${r.id} — ${r.status} · base generation ${r.base_generation}`;
  const roles = Object.entries(r.affected_roles || {});
  const what = card('What changes', `<ul class="checks">${(r.diff || []).map(x => `<li>${esc(x)}</li>`).join('') || '<li class="muted">no diff recorded</li>'}</ul>` +
    (roles.length ? `<table style="margin-top:8px"><thead><tr><th>role</th><th>before</th><th>after</th></tr></thead><tbody>${roles.map(([role, c]) =>
        `<tr><td><strong>${esc(role)}</strong></td><td>${chainText(c.before)}</td><td>${chainText(c.after)}</td></tr>`).join('')}</tbody></table>`
      : `<div class="muted" style="margin-top:8px">no serving role changes${rt.switch ? ' — but the engine runtime switches' : ''}</div>`));
  const bench = Object.entries(ev.benchmarks || {}).map(([m, b]) => `<tr><td><strong>${esc(m)}</strong></td><td>${b.tokens_per_s != null ? b.tokens_per_s + ' tok/s' : '—'}</td>` +
    `<td class="muted">${when(b.last)}${b.via ? ' · ' + esc(b.via) : ''}</td></tr>`).join('');
  const fits = Object.entries(ev.fit || {}).map(([m, v]) => `<li><strong>${esc(m)}</strong> ${fitBadge(v)}${(v.warnings || []).length ? ' <span class="muted">' + esc(v.warnings.join(' · ')) + '</span>' : ''}</li>`).join('');
  const cap = (ev.capability_report || []).map(c => `<li>${c.ok ? '✅' : '❌'} ${esc(c.role)} → <strong>${esc(c.model)}</strong> <span class="muted">(${esc(c.position)}, ${esc(c.runtime)}) ` +
    `${c.failures && c.failures.length ? esc(c.failures.join('; ')) : esc(Object.keys(c.checks || {}).filter(k => c.checks[k]).join(', '))}</span></li>`).join('');
  const evidence = card('Evidence <span class="muted">· measured on this host, never vendor claims</span>',
    `<div><strong>Benchmarks</strong></div>${bench ? `<table><thead><tr><th>model</th><th>measured</th><th>when</th></tr></thead><tbody>${bench}</tbody></table>` : '<div class="muted">none recorded</div>'}` +
    `<div style="margin-top:8px"><strong>Fit of newly active models</strong></div><ul class="checks">${fits || '<li class="muted">no newly active model</li>'}</ul>` +
    `<div style="margin-top:8px"><strong>Capability report</strong> <span class="muted">· render-time negotiation of the proposed generation on class ${esc(ev.capability_class || '?')}</span></div><ul class="checks">${cap || '<li class="muted">none</li>'}</ul>` +
    `<div style="margin-top:8px"><strong>Runtime</strong> ${esc(rt.before || '—')} → ${esc(rt.after || '—')} ${rt.switch ? '<span class="badge queued">runtime switch</span>' : '<span class="muted">(no switch)</span>'}</div>`);
  const proposer = card('Proposed by', `<div>requested by <strong>${esc(r.requested_by)}</strong> · proposed by <strong>${esc(r.proposed_by)}</strong>` +
    `${r.proposed_by === 'evolution' ? ' <span class="muted">— advisory: self-evolution proposes, never operates</span>' : ''} · ${when(r.created_at)}</div>` +
    (r.requires_approval ? '' : '<div class="muted">no serving role changed — approved by policy on submission</div>'));
  const nextGen = (d.generation ?? r.base_generation) + 1;
  const reversibility = card('Reversibility', pending
    ? `<div>On approval the platform renders generation ${nextGen} from this proposal, reloads and health-checks (a failed reload self-rolls back). Afterwards one rollback restores generation ${d.generation ?? r.base_generation} — <a href="/models">Models page</a> or <code>local-ezai model rollback --to-generation ${d.generation ?? r.base_generation}</code>.</div>`
    : r.status === 'applied' ? `<div>Applied as generation ${r.result && r.result.generation ? r.result.generation : '?'}; one rollback restores generation ${r.base_generation} (<a href="/models">Models page</a>).</div>`
    : '<div class="muted">Nothing changed on the platform for this request.</div>');
  const decision = (r.decision
    ? `<div>${badge(r.status)} by <strong>${esc(r.decision.by)}</strong> · ${when(r.decision.at)}${r.decision.reason ? ` — “${esc(r.decision.reason)}”` : ''}</div>` +
      (r.result && Object.keys(r.result).length ? `<div class="muted" style="margin-top:6px">${esc(r.result.message || '')}${r.result.generation ? ` · now generation ${r.result.generation}` : ''}</div>` : '')
    : '<div class="muted">pending — no decision yet</div>') +
    (pending && admin ? `<div style="margin-top:12px"><button class="btn danger" data-decide="reject">Reject (reason…)</button> <button class="btn" data-decide="approve">Approve &amp; apply</button></div>`
      : pending ? '<div class="muted" style="margin-top:8px">the admin role decides — viewer is read-only</div>' : '');
  $('app').innerHTML = `<section class="card"><div class="card-header"><span class="name">${esc(r.kind)} <code>${esc(r.id)}</code> — ${esc(r.title)}</span>${badge(r.status)}</div></section>` +
    what + evidence + proposer + reversibility + card('Decision', decision);
  document.querySelectorAll('button[data-decide]').forEach(b => b.onclick = () => decide(b.dataset.decide, r));
  footer(`request <code>${esc(r.id)}</code> · base generation ${r.base_generation} · <a href="/governance">queue</a> · CLI: <code>local-ezai governance show ${esc(r.id)}</code> · <code>local-ezai governance approve|reject ${esc(r.id)}</code>`);
}

// ── Projects · Sprints · Evolution · Memory (PR-19) ──────────────
const runLink = r => r ? `<a href="/runs/${encodeURIComponent(r.run_id)}"><code>${esc(r.run_id)}</code></a> ${badge(r.status)} <span class="muted">${esc(r.kind)} · ${when(r.submitted_at)}</span>` : '<span class="muted">none yet</span>';
const projectSelect = (projects, selected) => `<select name="project">${projects.map(p => `<option${p === selected ? ' selected' : ''}>${esc(p)}</option>`).join('')}</select>`;
const startedText = r => `started ${r.kind} ${r.run_id} on ${r.project_name} (${r.status}) — follow it at /runs/${r.run_id}`;
const TASK_HEAD = `<tr><th>wave</th><th>task</th><th>what</th><th>depends on</th><th>status</th><th>commit</th><th>error</th></tr>`;
const taskRows = tasks => (tasks || []).map(t => `<tr><td>${t.wave ?? ''}</td><td><code>${esc(t.task_id || t.index)}</code></td><td>${esc(short(t.task, 90))}</td>` +
  `<td class="muted">${esc((t.depends_on || []).join(', ') || '—')}</td><td>${badge(t.status)}</td><td><code>${esc((t.commit_sha || '').slice(0, 10))}</code></td><td class="muted">${esc(short(t.error, 80))}</td></tr>`).join('');
const noReport = r => `<div class="muted" style="margin-top:8px">${TERMINAL.includes(r.status) ? 'no report — the <a href="/runs/' + encodeURIComponent(r.run_id) + '">journal</a> shows how far it got' : 'running — the report follows'}</div>`;
const runHead = r => `<a href="/runs/${encodeURIComponent(r.run_id)}"><code>${esc(r.run_id)}</code></a> ${badge(r.status)} <span class="muted">· ${esc(r.project_name)} · ${when(r.submitted_at)} · by ${esc(r.actor)}</span>`;

async function renderProjects() {
  document.title = 'Projects · Local-EZAI Admin Center';
  let d; try { d = await api('/api/ezai/projects'); } catch (e) { fail(e); return; }
  const admin = isAdmin(d);
  $('subtitle').textContent = `Projects · ${d.projects.length} registered · the allowlist for chat-ops and console work`;
  const rows = d.projects.map(p => `<tr><td><strong>${esc(p.name)}</strong></td><td><code>${esc(p.path)}</code></td><td class="muted">${esc(p.added_by)} · ${when(p.added_at)}</td>` +
    `<td>${p.runs} run${p.runs === 1 ? '' : 's'}${p.active ? ` · ${p.active} active` : ''}<div class="muted">${runLink(p.last)}</div></td>` +
    `<td><a href="/runs?project=${encodeURIComponent(p.name)}">runs</a> · <a href="/memory?project=${encodeURIComponent(p.name)}">memory</a></td>` +
    `<td>${admin ? `<button class="btn sm danger" data-remove="${esc(p.name)}">remove</button>` : ''}</td></tr>`).join('');
  const add = admin ? card(`Register a repository <span class="muted">· a git repository on the control plane's filesystem · <code>local-ezai project add &lt;path&gt;</code></span>`,
    `<form id="project-form" class="toolbar"><input name="path" placeholder="/path/to/repository" required size="40"><input name="name" placeholder="name (optional)" size="16"><button class="btn" type="submit">Register</button></form>` +
    `<div class="muted">Registered projects are the only repositories chat, the console and the API may work on. The control plane needs the path on its own filesystem (a host daemon, or the same mount inside the container).</div>`) : '';
  $('app').innerHTML = add + card(`Registered projects <span class="muted">· ${d.projects.length}</span>`,
    d.projects.length ? `<table><thead><tr><th>project</th><th>path</th><th>registered</th><th>work</th><th>pages</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
                      : '<div class="muted">no project registered — register one above or with <code>local-ezai project add &lt;path&gt;</code></div>');
  const form = $('project-form');
  if (form) form.onsubmit = async e => { e.preventDefault(); const f = new FormData(form); const body = { path: f.get('path') }; if (f.get('name')) body.name = f.get('name');
    await run(`register ${body.path}`, () => act('/api/ezai/projects', 'POST', body), r => r.message, null, renderProjects); };
  document.querySelectorAll('button[data-remove]').forEach(b => b.onclick = async () => { const n = b.dataset.remove;
    if (!confirm(`Remove ${n} from the allowlist? Nothing is deleted on disk.`)) return;
    await run(`remove ${n}`, () => act('/api/ezai/projects?target=' + encodeURIComponent(n), 'DELETE'), r => r.message, null, renderProjects); });
  footer(`CLI twins: <code>local-ezai project add|list|remove</code> · chat: <code>swe_projects</code>`);
}

async function renderSprints() {
  document.title = 'Sprints · Local-EZAI Admin Center';
  let d; try { d = await api('/api/ezai/sprints'); } catch (e) { fail(e); return; }
  const admin = isAdmin(d);
  $('subtitle').textContent = `Sprints · ${d.sprints.length} shown · ${d.active} active`;
  const start = admin ? card('New sprint <span class="muted">· paste or write a markdown specification · <code>local-ezai sprint &lt;spec.md&gt;</code></span>',
    `<form id="sprint-form"><div class="toolbar">target project ${projectSelect(d.projects, d.projects[0])} <label><input type="checkbox" name="simple"> one task per line, sequential</label> <label><input type="checkbox" name="keep_going"> continue after a failed task</label></div>` +
    `<textarea name="spec" rows="8" placeholder="# Sprint goal&#10;&#10;- requirement …" required style="width:100%;margin-top:8px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px"></textarea>` +
    `<div class="toolbar" style="margin-top:8px"><button class="btn" type="submit"${d.projects.length ? '' : ' disabled'}>Start sprint</button><span class="muted">requirement analysis → dependency waves → parallel task pipelines → merged commits on a sprint branch; never pushed</span></div></form>`) : '';
  const cards = d.sprints.map(({ run: r, report: rep, mermaid }) => card(runHead(r),
    `<div class="muted">${esc(short(r.request, 160))}</div>` + (rep
      ? `<div style="margin-top:8px"><strong>Goal:</strong> ${esc(rep.plan && rep.plan.goal || '')} <span class="muted">· ${rep.waves ?? 0} wave${rep.waves === 1 ? '' : 's'} · branch <code>${esc(rep.branch)}</code>${rep.report_doc ? ` · report <code>${esc(rep.report_doc)}</code>` : ''}</span></div>` +
        `<table style="margin-top:8px"><thead>${TASK_HEAD}</thead><tbody>${taskRows(rep.tasks) || '<tr><td colspan="7" class="muted">no task</td></tr>'}</tbody></table>` +
        (mermaid ? `<div style="margin-top:8px"><strong>Dependency graph</strong> <span class="muted">· mermaid source, as the runtime writes it into the sprint report</span></div><pre class="journal">${esc(mermaid)}</pre>` : '')
      : noReport(r)))).join('');
  $('app').innerHTML = start + (cards || card('Sprints', '<div class="muted">no sprint yet — start one above, with <code>local-ezai sprint</code>, or <code>swe_sprint</code> in chat</div>'));
  const form = $('sprint-form');
  if (form) form.onsubmit = async e => { e.preventDefault(); const f = new FormData(form);
    const body = { kind: 'sprint', project: f.get('project'), spec: f.get('spec'), simple: !!f.get('simple'), keep_going: !!f.get('keep_going') };
    await run(`start sprint on ${body.project}`, () => act('/api/ezai/runs', 'POST', body), startedText, null, renderSprints); };
  footer(`CLI: <code>local-ezai sprint &lt;spec.md&gt;</code> · chat: <code>swe_sprint</code> · sprint reports live in the repository under <code>docs/sprints/</code>`);
  clearTimeout(timer);
  if (d.sprints.some(s => !TERMINAL.includes(s.run.status))) timer = setTimeout(renderSprints, 4000);
}

async function renderEvolution() {
  document.title = 'Evolution · Local-EZAI Admin Center';
  let d; try { d = await api('/api/ezai/evolution'); } catch (e) { fail(e); return; }
  const admin = isAdmin(d);
  $('subtitle').textContent = `Evolution · ${d.cycles.length} cycle${d.cycles.length === 1 ? '' : 's'} · ${d.active} active`;
  const start = admin ? card('Run evolution cycle <span class="muted">· analyze history, failures and bottlenecks → propose → implement → validate → benchmark → pull request or proposal bundle · <code>local-ezai evolve</code></span>',
    `<form id="evolve-form" class="toolbar">target project ${projectSelect(d.projects, d.projects[0])} <input name="focus" placeholder="focus (optional)" size="40"><button class="btn" type="submit"${d.projects.length ? '' : ' disabled'}>Run cycle</button></form>` +
    `<div class="muted">Every cycle ends awaiting human review — self-evolution proposes, never operates.</div>`) : '';
  const queue = d.queue.length
    ? `<div class="banner warn"><strong>⚠ ${d.queue.length} evolution proposal${d.queue.length > 1 ? 's' : ''} await approval</strong> — ${d.queue.map(q => `<a href="/governance/${encodeURIComponent(q.id)}"><code>${esc(q.id)}</code></a> ${esc(q.title)}`).join(' · ')}</div>`
    : `<div class="banner"><strong>Governance</strong> <span class="muted">— evolution proposals enter the <a href="/governance">queue</a> once the pipeline submits change requests; today a cycle's terminal artifact is its pull request or proposal bundle, reviewed and merged (or <code>git branch -D</code>) by a human on the forge.</span></div>`;
  const cards = d.cycles.map(({ run: r, report: rep }) => { const p = rep && rep.proposal, pr = (rep && rep.pull_request) || {}, b = rep && rep.benchmark_before, a = rep && rep.benchmark_after;
    return card(runHead(r) + (r.request ? ` <span class="muted">· focus: ${esc(short(r.request, 80))}</span>` : ''), rep
      ? `<div><strong>Proposal:</strong> ${esc(p ? p.title : '(none)')}</div>` +
        (p ? `<ul class="checks">${(p.failure_patterns || []).map(x => `<li class="muted">pattern: ${esc(x)}</li>`).join('')}${(p.bottlenecks || []).map(x => `<li class="muted">bottleneck: ${esc(x)}</li>`).join('')}</ul>` +
             `<ol class="heal">${(p.improvements || []).map(i => `<li><strong>${esc(i.title)}</strong>${i.rationale ? ' <span class="muted">— ' + esc(short(i.rationale, 160)) + '</span>' : ''}</li>`).join('')}</ol>` : '') +
        (b && a ? `<div style="margin-top:6px">benchmark: ${badge(b.passed ? 'passed' : 'failed')} ${b.checks} checks in ${b.duration_seconds}s → ${badge(a.passed ? 'passed' : 'failed')} ${a.checks} checks in ${a.duration_seconds}s</div>` : '') +
        (rep.tasks && rep.tasks.length ? `<table style="margin-top:8px"><thead>${TASK_HEAD}</thead><tbody>${taskRows(rep.tasks)}</tbody></table>` : '') +
        `<div style="margin-top:8px">${pr.url ? `pull request: <a href="${esc(pr.url)}">${esc(pr.url)}</a>` : pr.bundle_path ? `proposal bundle: <code>${esc(pr.bundle_path)}</code>` : 'no pull request or bundle recorded'}${pr.note ? ` <span class="muted">— ${esc(pr.note)}</span>` : ''}${rep.release_notes_updated ? ' <span class="muted">· release notes updated</span>' : ''}</div>` +
        `<div class="muted">branch <code>${esc(rep.branch)}</code> — <strong>awaiting human review</strong>; nothing merges itself</div>` +
        (rep.error ? `<div class="banner err" style="margin-top:6px"><strong>error</strong> — ${esc(rep.error)}</div>` : '')
      : noReport(r)); }).join('');
  $('app').innerHTML = start + queue + (cards || card('Cycles', '<div class="muted">no evolution cycle yet — run one above, with <code>local-ezai evolve</code>, or <code>swe_evolve</code> in chat</div>'));
  const form = $('evolve-form');
  if (form) form.onsubmit = async e => { e.preventDefault(); const f = new FormData(form); const body = { kind: 'evolve', project: f.get('project') }; if (f.get('focus')) body.focus = f.get('focus');
    await run(`start evolution on ${body.project}`, () => act('/api/ezai/runs', 'POST', body), startedText, null, renderEvolution); };
  footer(`CLI: <code>local-ezai evolve</code> · chat: <code>swe_evolve</code> · the pull request is the terminal artifact (GOVERNANCE §5)`);
  clearTimeout(timer);
  if (d.cycles.some(c => !TERMINAL.includes(c.run.status))) timer = setTimeout(renderEvolution, 4000);
}

const KIND_LABEL = { project_rule: 'Project rules', coding_style: 'Coding styles', architecture_decision: 'Architecture decisions',
  failed_fix: 'Failed fixes', successful_fix: 'Successful fixes', implementation: 'Implementation history' };
async function renderMemory() {
  document.title = 'Memory · Local-EZAI Admin Center';
  const params = new URLSearchParams(location.search), q = new URLSearchParams();
  for (const k of ['project', 'kind', 'search']) if (params.get(k)) q.set(k, params.get(k));
  let d; try { d = await api('/api/ezai/memory' + (q.toString() ? '?' + q : '')); } catch (e) { fail(e); return; }
  const admin = isAdmin(d), m = d.memory;
  $('subtitle').textContent = m ? `Memory · ${m.project} · ${m.total} entr${m.total === 1 ? 'y' : 'ies'}` : 'Memory · no project registered';
  const filters = `<form id="memory-filter" class="toolbar">project ${projectSelect(d.projects, d.project)} ` +
    `<select name="kind"><option value="">all kinds</option>${d.kinds.map(k => `<option value="${k}"${k === d.kind ? ' selected' : ''}>${KIND_LABEL[k] || k}${m ? ` (${m.counts[k] || 0})` : ''}</option>`).join('')}</select> ` +
    `<input name="search" placeholder="search title and content" value="${esc(d.search || '')}" size="28"><button class="btn sm" type="submit">show</button></form>`;
  const groups = {};
  (m ? m.records : []).forEach(r => (groups[r.kind] = groups[r.kind] || []).push(r));
  const sections = d.kinds.filter(k => groups[k]).map(k => card(`${KIND_LABEL[k] || k} <span class="muted">· ${groups[k].length}</span>`,
    `<table><thead><tr><th>#</th><th>entry</th><th>context</th><th>recorded</th></tr></thead><tbody>${groups[k].map(r =>
      `<tr><td class="muted">${r.id}</td><td><strong>${esc(r.title)}</strong>${r.content && r.content !== r.title ? `<div class="muted">${esc(short(r.content, 400))}</div>` : ''}</td>` +
      `<td class="muted">${r.error_signature ? `<code>${esc(short(r.error_signature, 60))}</code> ` : ''}${r.category ? esc(r.category) + ' ' : ''}${(r.files || []).length ? '<br>' + esc(r.files.slice(0, 5).join(', ')) : ''}</td>` +
      `<td class="muted">${when(r.created_at)}${r.run_id ? `<br>run ${esc(r.run_id)}` : ''}</td></tr>`).join('')}</tbody></table>`)).join('');
  const add = admin && m ? card('Remember something <span class="muted">· a curated rule, style or decision for this project · <code>local-ezai memory --add</code></span>',
    `<form id="memory-form" class="toolbar"><select name="kind">${d.curated.map(k => `<option value="${k}">${KIND_LABEL[k]}</option>`).join('')}</select>` +
    `<input name="text" placeholder="e.g. tests live next to the module they cover" required size="60"><button class="btn" type="submit">Remember</button></form>` +
    `<div class="muted">Fixes and implementation history are recorded by runs, never by hand; the planner and the debugger read this store on every run.</div>`) : '';
  $('app').innerHTML = card('Browse', filters + (m
      ? `<div class="muted" style="margin-top:8px">${m.exists ? `store <code>${esc(m.path)}</code> · ${m.total} total` : `no memory yet for ${esc(m.project)} — the first run creates <code>${esc(m.path)}</code>`}</div>`
      : '<div class="muted">register a project first (<a href="/projects">Projects</a>)</div>')) +
    add + (sections || (m ? card('Entries', `<div class="muted">${m.exists ? 'nothing matches' : 'no memory yet for this project'}</div>`) : ''));
  const filter = $('memory-filter');
  if (filter) filter.onsubmit = e => { e.preventDefault(); const f = new FormData(filter); const p = new URLSearchParams();
    for (const k of ['project', 'kind', 'search']) if (f.get(k)) p.set(k, f.get(k)); location.search = p.toString(); };
  const form = $('memory-form');
  if (form) form.onsubmit = async e => { e.preventDefault(); const f = new FormData(form);
    await run(`remember for ${d.project}`, () => act('/api/ezai/memory', 'POST', { project: d.project, kind: f.get('kind'), text: f.get('text') }), r => r.message, null, renderMemory); };
  footer(`${m ? `<code>${esc(m.path)}</code> · ` : ''}CLI: <code>local-ezai memory [--search …] [--add … --kind …]</code> · ADR-017`);
}

if (route.view === 'run') renderRun(route.id);
else if (route.view === 'runs') renderRuns();
else if (route.view === 'models') renderModels();
else if (route.view === 'routing') renderRouting();
else if (route.view === 'runtime') renderRuntime();
else if (route.view === 'governance') renderGovernance();
else if (route.view === 'request') renderRequest(route.id);
else if (route.view === 'projects') renderProjects();
else if (route.view === 'sprints') renderSprints();
else if (route.view === 'evolution') renderEvolution();
else if (route.view === 'memory') renderMemory();
else renderOverview();
</script>
</body>
</html>
"""
