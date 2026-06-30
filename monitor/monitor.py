"""
monitor/monitor.py
──────────────────────────────────────────────────────────────────────────────
Web monitoring dashboard for the AI service stack.

Polls all 7 services on a background thread and serves:
  GET /           → dashboard HTML
  GET /api/status → current status JSON
  GET /api/stream → SSE stream (push updates to browser)
"""
import asyncio
import json
import os
import time
from collections import deque
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# ── Configuration ─────────────────────────────────────────────────────────────
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "15"))   # seconds between polls
HISTORY_POINTS = int(os.getenv("HISTORY_POINTS", "60")) # keep last N readings

LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "sk-ai-service-2024")
MCP_KEY     = os.getenv("MCP_API_KEY",         "local-tools-key")

SERVICES: list[dict] = [
    {
        "id":      "vllm",
        "name":    "vLLM",
        "desc":    "LLM inference (GPU)",
        "url":     "http://vllm:8000/health",
        "method":  "GET",
        "pattern": "healthy",
        "headers": {},
        "link":    "http://localhost:8000",
    },
    {
        "id":      "embed",
        "name":    "Embed Server",
        "desc":    "Embedding model (CPU)",
        "url":     "http://embed-server:8001/health",
        "method":  "GET",
        "pattern": "healthy",
        "headers": {},
        "link":    "http://localhost:8001/health",
    },
    {
        "id":      "qdrant",
        "name":    "Qdrant",
        "desc":    "Vector database",
        "url":     "http://qdrant:6333/healthz",
        "method":  "GET",
        "pattern": "qdrant",
        "headers": {},
        "link":    "http://localhost:6333/dashboard",
    },
    {
        "id":      "searxng",
        "name":    "SearXNG",
        "desc":    "Private web search",
        "url":     "http://searxng:8080/search?q=test&format=json",
        "method":  "GET",
        "pattern": "results",
        "headers": {},
        "link":    "http://localhost:8090",
    },
    {
        "id":      "litellm",
        "name":    "LiteLLM",
        "desc":    "Model router / proxy",
        "url":     "http://litellm:4000/models",
        "method":  "GET",
        "pattern": "data",
        "headers": {"Authorization": f"Bearer {LITELLM_KEY}"},
        "link":    "http://localhost:4000",
    },
    {
        "id":      "mcpo",
        "name":    "mcpo (MCP Tools)",
        "desc":    "MCP tool proxy",
        "url":     "http://mcpo:8200/openapi.json",
        "method":  "GET",
        "pattern": "openapi",
        "headers": {"Authorization": f"Bearer {MCP_KEY}"},
        "link":    "http://localhost:8200",
    },
    {
        "id":      "openwebui",
        "name":    "OpenWebUI",
        "desc":    "Chat interface",
        "url":     "http://openwebui:8080",
        "method":  "GET",
        "pattern": "Open WebUI",
        "headers": {},
        "link":    "http://localhost:3000",
    },
]

# ── In-memory state ───────────────────────────────────────────────────────────
_status: dict[str, dict] = {
    svc["id"]: {
        "id":           svc["id"],
        "name":         svc["name"],
        "desc":         svc["desc"],
        "link":         svc["link"],
        "up":           None,
        "response_ms":  None,
        "last_check":   None,
        "consecutive_failures": 0,
        "history":      deque(maxlen=HISTORY_POINTS),  # list of {"t": ts, "ms": ms, "up": bool}
    }
    for svc in SERVICES
}

_subscribers: list[asyncio.Queue] = []


def _snapshot() -> dict:
    """Return JSON-serialisable status snapshot."""
    out = {}
    for sid, s in _status.items():
        out[sid] = {
            "id":          s["id"],
            "name":        s["name"],
            "desc":        s["desc"],
            "link":        s["link"],
            "up":          s["up"],
            "response_ms": s["response_ms"],
            "last_check":  s["last_check"],
            "consecutive_failures": s["consecutive_failures"],
            "history": list(s["history"])[-20:],   # last 20 points for sparkline
        }
    return {
        "services":    out,
        "server_time": time.time(),
        "poll_interval": POLL_INTERVAL,
    }


async def _check_service(client: httpx.AsyncClient, svc: dict) -> None:
    sid   = svc["id"]
    start = time.time()
    up    = False
    ms    = None
    try:
        resp = await client.request(
            svc["method"], svc["url"],
            headers=svc["headers"],
            timeout=8.0,
            follow_redirects=True,
        )
        ms   = round((time.time() - start) * 1000)
        up   = resp.status_code < 400 and svc["pattern"] in resp.text
    except Exception:
        ms = round((time.time() - start) * 1000)

    state = _status[sid]
    state["up"]          = up
    state["response_ms"] = ms
    state["last_check"]  = time.time()
    if up:
        state["consecutive_failures"] = 0
    else:
        state["consecutive_failures"] += 1
    state["history"].append({"t": state["last_check"], "ms": ms, "up": up})


async def _poll_loop() -> None:
    async with httpx.AsyncClient() as client:
        while True:
            tasks = [_check_service(client, svc) for svc in SERVICES]
            await asyncio.gather(*tasks, return_exceptions=True)
            snapshot = _snapshot()
            for q in list(_subscribers):
                try:
                    q.put_nowait(snapshot)
                except asyncio.QueueFull:
                    pass
            await asyncio.sleep(POLL_INTERVAL)


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="AI Service Monitor", version="1.0.0")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_poll_loop())


@app.get("/api/status")
async def api_status() -> dict:
    return _snapshot()


@app.get("/api/stream")
async def api_stream() -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=5)
    _subscribers.append(queue)

    async def _gen() -> AsyncGenerator[str, None]:
        try:
            # Send current state immediately on connect
            yield f"data: {json.dumps(_snapshot())}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"   # keep-alive
        finally:
            _subscribers.remove(queue)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    return _DASHBOARD_HTML


# ── Embedded dashboard HTML ───────────────────────────────────────────────────
_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Service Monitor</title>
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
         font-size: 14px; min-height: 100vh; }
  header { background: var(--card); border-bottom: 1px solid var(--border);
           padding: 16px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 18px; font-weight: 600; }
  header .subtitle { color: var(--muted); font-size: 13px; }
  .conn-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--yellow);
              flex-shrink: 0; transition: background .3s; }
  .conn-dot.live { background: var(--green); }
  .conn-dot.dead { background: var(--red); }
  .summary { display: flex; gap: 24px; padding: 20px 24px;
             border-bottom: 1px solid var(--border); }
  .sum-card { background: var(--card); border: 1px solid var(--border);
              border-radius: 8px; padding: 14px 20px; min-width: 120px; }
  .sum-card .val { font-size: 28px; font-weight: 700; }
  .sum-card .lbl { font-size: 12px; color: var(--muted); margin-top: 2px; }
  .val.green { color: var(--green); }
  .val.red   { color: var(--red); }
  .val.yellow{ color: var(--yellow); }
  main { padding: 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
          gap: 16px; }
  .card { background: var(--card); border: 1px solid var(--border);
          border-radius: 10px; padding: 18px; position: relative; overflow: hidden; }
  .card.up   { border-left: 3px solid var(--green); }
  .card.down { border-left: 3px solid var(--red); }
  .card.unknown { border-left: 3px solid var(--yellow); }
  .card-header { display: flex; align-items: center; justify-content: space-between;
                 margin-bottom: 10px; }
  .card-header .name { font-weight: 600; font-size: 15px; }
  .badge { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 12px;
           text-transform: uppercase; letter-spacing: .5px; }
  .badge.up   { background: rgba(34,197,94,.15); color: var(--green); }
  .badge.down { background: rgba(239,68,68,.15);  color: var(--red); }
  .badge.unknown { background: rgba(234,179,8,.15); color: var(--yellow); }
  .card-desc { font-size: 12px; color: var(--muted); margin-bottom: 12px; }
  .metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 12px; }
  .metric label { color: var(--muted); display: block; }
  .metric .v { font-weight: 600; margin-top: 2px; }
  .sparkline { margin-top: 14px; height: 32px; position: relative; }
  .sparkline canvas { width: 100%; height: 32px; display: block; }
  .link-btn { margin-top: 14px; display: inline-block; font-size: 12px;
              color: var(--blue); text-decoration: none; }
  .link-btn:hover { text-decoration: underline; }
  .last-updated { font-size: 12px; color: var(--muted); padding: 0 24px 16px; }
  @media (max-width: 480px) { .summary { flex-wrap: wrap; } .sum-card { min-width: 100px; } }
</style>
</head>
<body>
<header>
  <div class="conn-dot" id="conn-dot"></div>
  <div>
    <h1>AI Service Monitor</h1>
    <div class="subtitle" id="subtitle">Connecting…</div>
  </div>
</header>

<div class="summary" id="summary"></div>
<div class="last-updated" id="last-updated"></div>
<main><div class="grid" id="grid"></div></main>

<script>
const $ = id => document.getElementById(id);
const fmt_ms = ms => ms == null ? '—' : ms < 1000 ? ms + ' ms' : (ms/1000).toFixed(1) + ' s';
const fmt_ts = ts => ts == null ? '—' : new Date(ts * 1000).toLocaleTimeString();
const rel_ts = ts => {
  if (!ts) return '—';
  const s = Math.round(Date.now()/1000 - ts);
  if (s < 5)  return 'just now';
  if (s < 60) return s + 's ago';
  return Math.round(s/60) + 'm ago';
};

const canvases = {};

function drawSparkline(canvas, history) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.offsetWidth, h = 32;
  canvas.width  = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  if (!history || history.length < 2) return;
  const pts = history.slice(-40);
  const maxMs = Math.max(...pts.map(p => p.ms || 0), 1);
  ctx.clearRect(0, 0, w, h);
  const step = w / (pts.length - 1);
  ctx.beginPath();
  pts.forEach((p, i) => {
    const x = i * step;
    const y = p.ms == null ? h : h - ((p.ms / maxMs) * (h - 4)) - 2;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.strokeStyle = 'rgba(59,130,246,0.7)';
  ctx.lineWidth = 1.5;
  ctx.stroke();
  // dots for down points
  pts.forEach((p, i) => {
    if (!p.up) {
      ctx.beginPath();
      ctx.arc(i * step, h/2, 3, 0, Math.PI*2);
      ctx.fillStyle = 'rgba(239,68,68,0.8)';
      ctx.fill();
    }
  });
}

function renderSummary(services) {
  const vals = Object.values(services);
  const up   = vals.filter(s => s.up === true).length;
  const down = vals.filter(s => s.up === false).length;
  const unk  = vals.filter(s => s.up === null).length;
  const avgMs = (() => {
    const ms = vals.filter(s => s.response_ms).map(s => s.response_ms);
    return ms.length ? Math.round(ms.reduce((a,b)=>a+b,0)/ms.length) : null;
  })();
  $('summary').innerHTML = `
    <div class="sum-card"><div class="val green">${up}</div><div class="lbl">Online</div></div>
    <div class="sum-card"><div class="val ${down?'red':'green'}">${down}</div><div class="lbl">Offline</div></div>
    <div class="sum-card"><div class="val yellow">${unk}</div><div class="lbl">Unknown</div></div>
    <div class="sum-card"><div class="val">${fmt_ms(avgMs)}</div><div class="lbl">Avg Response</div></div>
  `;
}

function renderCards(services) {
  const grid = $('grid');
  Object.values(services).forEach(svc => {
    const statusCls = svc.up === true ? 'up' : svc.up === false ? 'down' : 'unknown';
    const badgeText = svc.up === true ? 'Online' : svc.up === false ? 'Offline' : 'Unknown';
    let card = document.getElementById('card-' + svc.id);
    if (!card) {
      card = document.createElement('div');
      card.id = 'card-' + svc.id;
      grid.appendChild(card);
    }
    card.className = 'card ' + statusCls;
    card.innerHTML = `
      <div class="card-header">
        <span class="name">${svc.name}</span>
        <span class="badge ${statusCls}">${badgeText}</span>
      </div>
      <div class="card-desc">${svc.desc}</div>
      <div class="metrics">
        <div class="metric"><label>Response</label><div class="v">${fmt_ms(svc.response_ms)}</div></div>
        <div class="metric"><label>Last check</label><div class="v">${rel_ts(svc.last_check)}</div></div>
        ${svc.consecutive_failures > 1 ? `<div class="metric" style="grid-column:1/-1"><label>Consecutive failures</label><div class="v" style="color:var(--red)">${svc.consecutive_failures}</div></div>` : ''}
      </div>
      <div class="sparkline"><canvas id="spark-${svc.id}"></canvas></div>
      <a class="link-btn" href="${svc.link}" target="_blank" rel="noopener">Open ↗</a>
    `;
    // draw sparkline after DOM update
    requestAnimationFrame(() => {
      const canvas = document.getElementById('spark-' + svc.id);
      if (canvas) drawSparkline(canvas, svc.history);
    });
  });
}

function applyData(data) {
  renderSummary(data.services);
  renderCards(data.services);
  $('last-updated').textContent = 'Last updated: ' + new Date().toLocaleTimeString()
    + '  ·  Polling every ' + data.poll_interval + 's';
}

// SSE connection with auto-reconnect
let evtSrc;
function connect() {
  evtSrc = new EventSource('/api/stream');
  $('conn-dot').className = 'conn-dot';
  $('subtitle').textContent = 'Connecting…';

  evtSrc.onopen = () => {
    $('conn-dot').className = 'conn-dot live';
    $('subtitle').textContent = 'Live — updates every ' + (window._pi || '…') + 's';
  };
  evtSrc.onmessage = e => {
    const data = JSON.parse(e.data);
    window._pi = data.poll_interval;
    $('subtitle').textContent = 'Live — updates every ' + data.poll_interval + 's';
    applyData(data);
  };
  evtSrc.onerror = () => {
    $('conn-dot').className = 'conn-dot dead';
    $('subtitle').textContent = 'Disconnected — reconnecting…';
    evtSrc.close();
    setTimeout(connect, 3000);
  };
}
connect();

// Redraw sparklines on resize
window.addEventListener('resize', () => {
  document.querySelectorAll('[id^="spark-"]').forEach(c => {
    const id = c.id.replace('spark-', '');
    const svc = window._lastData?.services?.[id];
    if (svc) drawSparkline(c, svc.history);
  });
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888)
