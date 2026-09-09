"""
monitor/admin_center.py
──────────────────────────────────────────────────────────────────────────────
Admin Center pages of the monitor (V1 P4 · PR-16 · ADR-030;
docs/WEBUI_ADMIN_CENTER.md §1–§2, docs/CLI_AND_WEBUI_STRATEGY.md §3).

The monitor renders ONLY what the ezaid control plane (:8010) serves. This
module is a thin client over the frozen 1.0.0 contract plus the first two
pages — Overview, and Runs with a deep-linkable run detail (cancel for
admins). The service token never reaches the browser: the page's JavaScript
calls this service under /api/ezai/…, which calls the daemon with the token
and forwards the monitor login as the human identity (audit actor
"<user> via admin-center"). Nothing here touches files or docker; anything
the pages show, `local-ezai` shows too (parity, not dependence).

  GET  /overview                        Overview page
  GET  /runs                            Runs page (filters: kind, status)
  GET  /runs/{run_id}                   Run detail — the deep link CLI + chat print
  GET  /api/ezai/overview               health · platform · roles · queue · recent runs
  GET  /api/ezai/runs                   run list (kind / status / project / limit)
  GET  /api/ezai/runs/{run_id}          record + report (once written) + journal tail
  POST /api/ezai/runs/{run_id}/cancel   admin role only
"""
# ruff: noqa: E501  — the embedded page template carries long markup lines
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, Query
from fastapi.responses import HTMLResponse, JSONResponse

CLIENT_NAME = "admin-center"
API = "/v1"
DEFAULT_CONTROL_URL = "http://ezaid:8010"
TERMINAL = ("completed", "failed", "cancelled")
#: Roles the Overview explains (ADR-026: roles are the interface, models are
#: evidence). A role the registry does not define is simply not shown.
OVERVIEW_ROLES = ("orchestrator", "planner", "coder", "debugger", "reviewer", "chat")
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
    explained = await asyncio.gather(*(plane.get(f"/roles/{role}", user) for role in OVERVIEW_ROLES),
                                     return_exceptions=True)
    roles = []
    for role, result in zip(OVERVIEW_ROLES, explained, strict=True):
        if isinstance(result, ControlPlaneError):
            continue  # the registry has no such role — nothing to explain
        if isinstance(result, BaseException):
            raise result
        source = result.get("source") or {}
        roles.append({"role": role, "group": source.get("group") or "",
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

    @app.post("/api/ezai/runs/{run_id}/cancel")
    async def ezai_cancel(run_id: str, role: str = Depends(admin),
                          x_requested_with: str | None = Header(None)):
        # The monitor login is ambient (HTTP Basic): a cross-site form could
        # post here with the browser's credentials. Forms cannot set custom
        # headers — the page's JavaScript does, so mutations require one.
        if x_requested_with != CLIENT_NAME:
            return ControlPlaneError(
                400, "same_origin_required",
                "mutations need the X-Requested-With header the Admin Center's own page sends",
                "use the button on the run page, or send "
                f"'X-Requested-With: {CLIENT_NAME}' with the request").response()
        try:
            return await plane.post(f"/runs/{run_id}/cancel", role)
        except ControlPlaneError as exc:
            return exc.response()

    @app.get("/overview", response_class=HTMLResponse)
    async def overview_page(role: str = Depends(viewer)) -> str:
        return ADMIN_HTML

    @app.get("/runs", response_class=HTMLResponse)
    async def runs_page(role: str = Depends(viewer)) -> str:
        return ADMIN_HTML

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(run_id: str, role: str = Depends(viewer)) -> str:
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
  button.btn { background: var(--red); color: white; border: 0; border-radius: 6px; padding: 7px 14px;
               font-size: 13px; font-weight: 600; cursor: pointer; }
  button.btn:disabled { opacity: .5; cursor: wait; }
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
    <a href="/runs" data-nav="runs">Runs</a>
    <a href="/" data-nav="health">Health &amp; Knowledge</a>
  </nav>
</header>
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
  if (p === '/runs') return { view: 'runs' };
  return { view: 'overview' };
})();
document.querySelectorAll('#nav a').forEach(a => {
  if (a.dataset.nav === (route.view === 'run' ? 'runs' : route.view)) a.classList.add('active');
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
    ? `<div class="banner warn"><strong>⚠ ${pending.length} item${pending.length > 1 ? 's' : ''} await your approval</strong>` +
      `<table style="margin-top:8px">${pending.map(q => `<tr><td><code>${esc(q.id)}</code></td><td>${esc(q.kind)}</td>` +
      `<td>${esc(q.title)}</td><td class="muted">${esc(q.requested_by)} · ${when(q.created_at)}</td></tr>`).join('')}</table>` +
      `<div class="muted">Decide with <code>local-ezai governance approve|reject &lt;id&gt;</code> — the Governance page arrives in the next Admin Center slice.</div></div>`
    : `<div class="banner"><strong>Governance queue empty</strong> <span class="muted">— nothing awaits approval</span></div>`;
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
  const kind = params.get('kind') || '', status = params.get('status') || '';
  const q = new URLSearchParams();
  if (kind) q.set('kind', kind);
  if (status) q.set('status', status);
  let d;
  try { d = await api('/api/ezai/runs' + (q.toString() ? '?' + q : '')); } catch (e) { fail(e); return; }
  const sel = (name, value, options) => `<select data-filter="${name}">${options.map(o =>
    `<option value="${o}"${o === value ? ' selected' : ''}>${o || 'all ' + name + 's'}</option>`).join('')}</select>`;
  $('app').innerHTML = `<section class="card"><div class="toolbar">` +
    sel('kind', kind, ['', 'run', 'fix', 'sprint', 'evolve', 'plan']) + sel('status', status, ['', 'queued', 'running', 'completed', 'failed', 'cancelled']) +
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
  const cancel = active ? `<button class="btn" id="cancel-btn">Cancel run</button> <span class="muted" id="cancel-status">` +
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

if (route.view === 'run') renderRun(route.id);
else if (route.view === 'runs') renderRuns();
else renderOverview();
</script>
</body>
</html>
"""
