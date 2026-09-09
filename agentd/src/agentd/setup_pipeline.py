"""Setup pipeline — steps 4–8 of the first run, and the ``init`` fallback
(PR-22, ADR-031; docs/FINAL_FIRST_RUN_EXPERIENCE.md §3–§4,
docs/FIRST_RUN_EXPERIENCE.md §3–§5).

``make setup`` = ``install.sh`` (steps 1–3, PR-21) + ``local-ezai setup``::

    4 fetch      bootstrap (PR-7: models fetched, validated, benchmarked,
                 generation 1 rendered) when no registry exists · images
                 (``docker compose pull`` / ``build`` with the rendered engine
                 override) · the RAG embedding model (scripts/download-embed.sh)
    5 render     (inside the bootstrap)
    6 up         ``docker compose up -d`` for the profile + the rendered engine
                 → wait-ready: the active runtime descriptor's ``ready`` verb
                 on the host port, then every service of the health table
    7 verify     smoke: one chat turn on the chat role alias (required) · a RAG
                 answer over a sample document (embedded with the stack's own
                 embed script) · ``plan_only`` on the bundled sample project ·
                 the evaluate-models probes — the last three are advisory
    8 report     config/first-run/report.{json,md} · the ✔ block with the WebUI
                 and Admin Center URLs · the "Platform ready" card as an
                 OpenWebUI banner (``WEBUI_BANNERS`` through an optional
                 env_file, rolled back if OpenWebUI does not come back) · the
                 Orchestrator persona attempted (after the first login when no
                 account exists yet)

Every step is idempotent, so the pipeline is safe to re-run. ``local-ezai
init`` is the fallback of FIRST_RUN_EXPERIENCE §3 when the seeds are missing:
hardware check → the recommended model set per group (the catalog
recommender, fit verdicts shown) → seeds written to ``.env`` → the pipeline.

Docker, HTTP, the planner and the evaluator are injectable seams; the module
names no runtime, model or vendor.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from agentd import bootstrap as bs
from agentd import platform_cli
from agentd.bootstrap import RUNTIME_KEY, SEED_GROUPS, parse_env, read_seeds, validate_seeds
from agentd.catalog import Recommendation, recommend
from agentd.config import AgentdConfig
from agentd.control.health import DEFAULT_TARGETS, ServiceTarget, probe_services
from agentd.fetch import SourceError, parse_source
from agentd.installer import EnvText, choose_runtime, suggest_profile
from agentd.lifecycle import LifecycleError
from agentd.logging_setup import get_logger
from agentd.platform_cli import PlatformContext, PlatformError, compose_files
from agentd.registry_v2 import load_registry, reference_registry, registry_path

log = get_logger("setup")

FIRST_RUN_DIRNAME = "first-run"                     # under <root>/config/
BANNER_ENV_FILENAME = "openwebui.env"               # compose env_file of the openwebui service
BANNER_KEY = "WEBUI_BANNERS"
BANNER_ID = "local-ezai-first-run"
SAMPLE_PROJECT_SOURCE = Path("examples") / "sample-project"
SAMPLE_PROJECT_DIRNAME = "sample-project"
SAMPLE_PROJECT_NAME = "sample-project"
SAMPLE_DOC_FILENAME = "local-ezai-first-run.md"
SAMPLE_CODEWORD = "LIGHTHOUSE-7"
SMOKE_TASK = 'add a GET /health endpoint that returns {"status": "ok"} and a test for it'
#: The OpenWebUI model id the persona installer writes (scripts/openwebui_orchestrator.py).
ORCHESTRATOR_MODEL_ID = "local-ezai-orchestrator"
#: Compose services pulled as images (the rest are built) — the Makefile's `pull` list.
PULLED_SERVICES = ("openwebui", "litellm", "vllm", "qdrant", "searxng")
#: Host port variable + default per health-target id (docker-compose.yml,
#: scripts/health-check.sh); the health table itself is control/health.py.
HOST_PORTS: dict[str, tuple[str, int]] = {
    "engine": ("LLM_PORT", 8000), "router": ("LITELLM_PORT", 4000),
    "embed": ("EMBED_PORT", 8001), "qdrant": ("QDRANT_PORT", 6333),
    "searxng": ("SEARXNG_PORT", 8092), "mcpo": ("MCPO_PORT", 8200),
    "openwebui": ("OPENWEBUI_PORT", 3000), "monitor": ("MONITOR_PORT", 8888),
}
REQUIRED_SERVICES = ("engine", "router")
SERVICES_TIMEOUT_S = 240.0
BANNER_TIMEOUT_S = 90.0
POLL_S = 5.0
CHAT_TIMEOUT_S = 180.0
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REVIEW = 0, 1, 2, 3
_STAMP = "%Y-%m-%dT%H:%M:%S"


class SetupError(ValueError):
    """A usage error (exit 2) — the message names the fix."""


# ── seams ────────────────────────────────────────────────────────────────────


class Http(Protocol):
    def get(self, url: str, headers: dict[str, str]) -> tuple[int, str]: ...
    def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str],
                  timeout: float) -> tuple[int, Any]: ...


class HttpxHttp:
    """Default HTTP: short GETs for probes, a long POST for the chat turn."""

    def get(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        import httpx

        with httpx.Client(timeout=5.0, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
            return response.status_code, response.text[:4000]

    def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str],
                  timeout: float) -> tuple[int, Any]:
        import httpx

        response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text[:4000]


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]
PlanFn = Callable[[AgentdConfig, Path, str], Any]
EvaluateFn = Callable[[AgentdConfig, Path], Any]


def runner_for(root: Path) -> Runner:
    """Subprocesses run from the checkout (compose finds .env, scripts their
    relative paths)."""
    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, cwd=str(root), capture_output=True, text=True,
                              check=False)
    return run


def _tail(proc: subprocess.CompletedProcess[str], lines: int = 3) -> str:
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip().splitlines()
    return " | ".join(line.strip() for line in text[-lines:] if line.strip())[:400]


# ── report ───────────────────────────────────────────────────────────────────


@dataclass
class StepResult:
    name: str
    status: str            # ok · skipped · warning · failed
    detail: str = ""
    seconds: float = 0.0

    @property
    def mark(self) -> str:
        return {"ok": "✓", "skipped": "↷", "warning": "!", "failed": "✗"}[self.status]


@dataclass
class SmokeCheck:
    name: str
    ok: bool
    detail: str = ""
    required: bool = False


@dataclass
class SetupOptions:
    root: Path
    profile: str | None = None
    skip_images: bool = False
    skip_smoke: bool = False
    skip_banner: bool = False
    env_path: Path | None = None


@dataclass
class SetupReport:
    root: str
    profile: str = ""
    capability_class: str = ""
    accelerator: str = ""
    runtime: str | None = None
    generation: int | None = None
    models: dict[str, str] = field(default_factory=dict)      # group → model
    steps: list[StepResult] = field(default_factory=list)
    health: dict[str, bool] = field(default_factory=dict)
    smoke: list[SmokeCheck] = field(default_factory=list)
    urls: dict[str, str] = field(default_factory=dict)
    ready: bool = False
    banner: str = ""
    persona: str = ""
    report_path: str = ""
    next_steps: list[str] = field(default_factory=list)
    created_at: str = ""
    exit_code: int = EXIT_OK

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.exit_code == EXIT_OK}

    def card(self) -> list[str]:
        """The "Platform ready" card — terminal, report.md and the WebUI banner."""
        groups = " · ".join(f"{g} ← {m}" for g, m in self.models.items()) or "no models yet"
        smoke = " · ".join(f"{c.name} {'✓' if c.ok else '✗'}" for c in self.smoke) or "skipped"
        head = ("✔ Platform ready" if self.ready else "✗ Platform not ready yet") + \
            f" — {self.urls.get('webui', '')}"
        lines = [head,
                 f"  Admin Center  {self.urls.get('admin', '')}   ·   local-ezai status",
                 f"  {groups} · {self.runtime or '?'} on {self.capability_class}"
                 + (f" (generation {self.generation})" if self.generation else ""),
                 f"  smoke: {smoke}"]
        lines += [f"  {step}" for step in self.next_steps]
        return lines

    def lines(self) -> list[str]:
        out = [f"local-ezai setup — {self.root} · profile {self.profile} · class "
               f"{self.capability_class} · runtime {self.runtime or '?'}"]
        for i, step in enumerate(self.steps, 1):
            took = f" ({step.seconds:.0f}s)" if step.seconds >= 1 else ""
            first, *rest = step.detail.splitlines() or [""]
            out.append(f"  [{i}/{len(self.steps)}] {step.name:12} {step.mark} {first}{took}")
            out += [f"                       {line}" for line in rest]
        if self.report_path:
            out.append(f"  report        {self.report_path} · banner: {self.banner} · "
                       f"persona: {self.persona}")
        return out + [""] + self.card()


# ── the pipeline ─────────────────────────────────────────────────────────────


class SetupPipeline:
    def __init__(self, ctx: PlatformContext, options: SetupOptions, *,
                 runner: Runner | None = None, http: Http | None = None,
                 plan_fn: PlanFn | None = None, evaluate_fn: EvaluateFn | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 environ: Mapping[str, str] | None = None, now: str | None = None,
                 say: Callable[[str], None] = print) -> None:
        self.ctx, self.options = ctx, options
        self.root = Path(options.root).resolve()
        self.env_path = (options.env_path or self.root / ".env").resolve()
        self.runner = runner or runner_for(self.root)
        self.http = http or HttpxHttp()
        self.plan_fn, self.evaluate_fn = plan_fn, evaluate_fn
        self.sleep, self.clock, self.say = sleep, clock, say
        self.now = now or time.strftime(_STAMP)
        env_text = self.env_path.read_text(encoding="utf-8") if self.env_path.is_file() else ""
        # the process environment wins over .env — compose's own precedence
        self.env: dict[str, str] = {**parse_env(env_text),
                                    **(os.environ if environ is None else environ)}
        self.profile = options.profile or suggest_profile(ctx.platform.klass, ctx.platform.accel)
        self.report = SetupReport(root=str(self.root), profile=self.profile,
                                  capability_class=ctx.platform.klass,
                                  accelerator=ctx.platform.accel, created_at=self.now)
        self.files: list[Path] = []

    # ── helpers ──────────────────────────────────────────────────────────────

    def port(self, service: str) -> int:
        var, default = HOST_PORTS[service]
        try:
            return int(self.env.get(var) or default)
        except ValueError:
            return default

    def host(self) -> str:
        return (self.env.get("LAN_HOST") or "localhost").strip() or "localhost"

    def host_url(self, service: str, path: str = "") -> str:
        return f"http://localhost:{self.port(service)}{path}"

    def public_url(self, service: str, path: str = "") -> str:
        return f"http://{self.host()}:{self.port(service)}{path}"

    def host_targets(self) -> tuple[ServiceTarget, ...]:
        """The daemon's health table addressed from this host."""
        targets = []
        for target in DEFAULT_TARGETS:
            if target.id not in HOST_PORTS:
                continue
            path = urlparse(target.url).path or "/"
            targets.append(ServiceTarget(target.id, self.host_url(target.id, path),
                                         target.pattern, target.auth_env))
        return tuple(targets)

    def compose(self, *verb: str) -> subprocess.CompletedProcess[str]:
        command = ["docker", "compose"]
        for file in self.files:
            command += ["-f", str(file)]
        return self.runner(command + list(verb))

    def step(self, name: str, fn: Callable[[], tuple[str, str]]) -> StepResult:
        start = self.clock()
        try:
            status, detail = fn()
        except (bs.BootstrapError, PlatformError, LifecycleError, OSError) as exc:
            status, detail = "failed", str(exc).strip()[:1500]  # every fix line stays
        result = StepResult(name, status, detail, round(self.clock() - start, 1))
        self.report.steps.append(result)
        first, *rest = detail.splitlines() or [""]
        self.say(f"  {result.mark} {name}: {first}")
        for line in rest:
            self.say(f"      {line}")
        return result

    def registry(self):
        return load_registry(self.ctx.config_dir) if registry_path(self.ctx.config_dir).is_file() \
            else None

    def active_runtime(self) -> str | None:
        registry = self.registry()
        if registry is not None:
            runtimes = sorted({e.provider for e in registry.models.values() if e.state == "active"})
            if runtimes:
                return runtimes[0]
        return (self.env.get(RUNTIME_KEY) or "").strip() or None

    def router_key(self) -> str:
        return (self.env.get("LITELLM_MASTER_KEY") or self.ctx.config.llm.api_key
                or "").strip()

    def agent_config(self) -> AgentdConfig:
        """The CLI's config pointed at this host's router (ports may be relocated)."""
        config = self.ctx.config.model_copy(deep=True)
        config.llm.base_url = self.host_url("router", "/v1")
        if not config.llm.api_key:
            config.llm.api_key = self.router_key()
        return config

    # ── steps ────────────────────────────────────────────────────────────────

    def step_bootstrap(self) -> tuple[str, str]:
        registry = self.registry()
        if registry is not None:
            return "skipped", (f"generation {registry.generation} exists — models are managed "
                               "with `local-ezai model …` or the Admin Center")
        if not self.env_path.is_file():
            raise PlatformError(f"no {self.env_path} — run ./install.sh first")
        result = platform_cli.run_bootstrap(self.ctx, self.env_path)
        return "ok", result.message

    def step_images(self) -> tuple[str, str]:
        if self.options.skip_images:
            return "skipped", "--skip-images"
        pulled = self.compose("pull", *PULLED_SERVICES)
        if pulled.returncode != 0:
            raise PlatformError(f"docker compose pull failed: {_tail(pulled)}")
        built = self.compose("build")
        if built.returncode != 0:
            raise PlatformError(f"docker compose build failed: {_tail(built)}")
        return "ok", f"pulled {len(PULLED_SERVICES)} images · built the platform images"

    def step_embed_model(self) -> tuple[str, str]:
        proc = self.runner(["bash", str(self.root / "scripts" / "download-embed.sh")])
        if proc.returncode != 0:
            return "warning", (f"embedding model download failed ({_tail(proc)}) — RAG stays off "
                               "until `make download-embed` succeeds")
        return "ok", _tail(proc, 1) or "present"

    def step_up(self) -> tuple[str, str]:
        proc = self.compose("up", "-d")
        if proc.returncode != 0:
            raise PlatformError(f"docker compose up failed: {_tail(proc)}")
        return "ok", f"stack started (profile {self.profile}, rendered engine override)"

    def step_wait_ready(self) -> tuple[str, str]:
        runtime = self.active_runtime()
        descriptor = self.ctx.descriptors.get(runtime or "")
        ready = descriptor.verbs.ready if descriptor else None
        path, timeout = (ready.path, float(ready.timeout_s)) if ready else ("/health", 600.0)
        url = self.host_url("engine", path)
        start = self.clock()
        engine_up = False
        while self.clock() - start < timeout:
            try:
                status, _ = self.http.get(url, {})
                engine_up = status < 400
            except Exception:  # not answering yet
                engine_up = False
            if engine_up:
                break
            self.sleep(POLL_S)
        engine_seconds = self.clock() - start
        if not engine_up:
            self.report.health = {"engine": False}
            raise PlatformError(f"the engine did not answer {url} within {timeout:.0f}s — "
                                "docker compose logs vllm | tail -30")
        targets = self.host_targets()
        start = self.clock()
        while True:
            results = probe_services(targets, self.http.get, environ=self.env, clock=self.clock)
            self.report.health = {r.id: r.ok for r in results}
            if all(r.ok for r in results) or self.clock() - start >= SERVICES_TIMEOUT_S:
                break
            self.sleep(POLL_S)
        down = [r.id for r in results if not r.ok]
        healthy = f"{len(results) - len(down)}/{len(results)} services healthy"
        if any(service in down for service in REQUIRED_SERVICES):
            raise PlatformError(f"{', '.join(down)} not healthy after {SERVICES_TIMEOUT_S:.0f}s — "
                                f"docker compose logs {down[0]} | tail -30")
        if down:
            return "warning", (f"engine ready in {engine_seconds:.0f}s · {healthy} — down: "
                               f"{', '.join(down)} (docker compose logs <service>)")
        return "ok", f"engine ready in {engine_seconds:.0f}s · {healthy}"

    # ── step 7: smoke ────────────────────────────────────────────────────────

    def chat(self, prompt: str, *, max_tokens: int = 32) -> tuple[bool, str, float]:
        alias = self.ctx.config.llm.model_for_role("chat")
        start = self.clock()
        try:
            status, body = self.http.post_json(
                self.host_url("router", "/v1/chat/completions"),
                {"model": alias, "messages": [{"role": "user", "content": prompt}],
                 "max_tokens": max_tokens, "temperature": 0},
                {"Authorization": f"Bearer {self.router_key()}"}, CHAT_TIMEOUT_S)
        except Exception as exc:  # transport errors are a result here
            return False, f"{type(exc).__name__}: {exc}"[:200], self.clock() - start
        seconds = self.clock() - start
        if status != 200 or not isinstance(body, dict):
            return False, f"HTTP {status}: {str(body)[:160]}", seconds
        try:
            content = (body["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            return False, f"unexpected response: {str(body)[:160]}", seconds
        return bool(content), content, seconds

    def smoke_chat(self) -> SmokeCheck:
        ok, content, seconds = self.chat("Reply with the single word: ready")
        alias = self.ctx.config.llm.model_for_role("chat")
        detail = (f"{alias} answered in {seconds:.1f}s: {content[:60]!r}" if ok
                  else f"{alias}: {content}")
        return SmokeCheck("chat", ok, detail, required=True)

    def smoke_rag(self) -> SmokeCheck:
        documents = Path(self.env.get("DOCUMENTS_DIR") or (self.root / "documents"))
        documents.mkdir(parents=True, exist_ok=True)
        (documents / SAMPLE_DOC_FILENAME).write_text(
            "# Local-EZAI first-run sample document\n\nThis document was written by the "
            "first run so the knowledge base has something to retrieve. The Local-EZAI "
            f"sample codeword is {SAMPLE_CODEWORD}. Ask the chat what the sample codeword "
            "is — the answer comes from this file.\n", encoding="utf-8")
        embedded = self.runner(["bash", str(self.root / "scripts" / "embed-documents.sh")])
        if embedded.returncode != 0:
            return SmokeCheck("RAG", False, f"embedding the sample document failed: "
                                            f"{_tail(embedded)}")
        ok, content, seconds = self.chat(
            "What is the Local-EZAI sample codeword? Answer with the codeword only.",
            max_tokens=24)
        found = ok and SAMPLE_CODEWORD.lower() in content.lower()
        return SmokeCheck("RAG", found, (f"answer in {seconds:.1f}s: {content[:60]!r}"
                                        + ("" if found else f" — expected {SAMPLE_CODEWORD}")))

    def ensure_sample_project(self) -> Path:
        target = self.root / SAMPLE_PROJECT_DIRNAME
        if not (target / ".git").exists():
            source = self.root / SAMPLE_PROJECT_SOURCE
            if not source.is_dir():
                raise PlatformError(f"no bundled sample project at {source}")
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
            for command in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                            ["git", "-c", "user.name=local-ezai",
                             "-c", "user.email=first-run@local-ezai", "commit", "-q", "-m",
                             "sample project (Local-EZAI first run)"]):
                proc = self.runner(["git", "-C", str(target), *command[1:]])
                if proc.returncode != 0:
                    raise PlatformError(f"could not initialise the sample project: {_tail(proc)}")
        try:
            platform_cli.add_project(self.ctx, str(target), name=SAMPLE_PROJECT_NAME)
        except LifecycleError as exc:
            if "already registered" not in str(exc):
                raise
        return target

    def smoke_plan(self) -> SmokeCheck:
        project = self.ensure_sample_project()
        plan_fn = self.plan_fn
        if plan_fn is None:
            from agentd.runner import plan_only
            plan_fn = plan_only
        try:
            plan = plan_fn(self.agent_config(), project, SMOKE_TASK)
        except Exception as exc:  # the planner may refuse or the model may misbehave
            return SmokeCheck("swe_plan", False, f"{type(exc).__name__}: {exc}"[:200])
        tasks = list(getattr(plan, "tasks", []) or [])
        return SmokeCheck("swe_plan", bool(tasks),
                          f"{len(tasks)} task(s): {getattr(plan, 'goal', '')[:70]}"
                          if tasks else "the planner returned no tasks")

    def smoke_evaluate(self) -> SmokeCheck:
        project = self.root / SAMPLE_PROJECT_DIRNAME
        evaluate_fn = self.evaluate_fn
        if evaluate_fn is None:
            from agentd.evaluate import evaluate_models
            evaluate_fn = evaluate_models
        try:
            report = evaluate_fn(self.agent_config(), project)
        except Exception as exc:
            return SmokeCheck("models", False, f"{type(exc).__name__}: {exc}"[:200])
        results = list(getattr(report, "results", []) or [])
        okay = [r for r in results if getattr(r, "ok", False)]
        failed = [f"{r.role} ({(r.error or '')[:40]})" for r in results
                  if not getattr(r, "ok", False)]
        return SmokeCheck("models", bool(getattr(report, "passed", False)),
                          f"{len(okay)}/{len(results)} roles answered"
                          + (f" — failed: {', '.join(failed)}" if failed else ""))

    def step_verify(self) -> tuple[str, str]:
        if self.options.skip_smoke:
            return "skipped", "--skip-smoke"
        checks = [self.smoke_chat()]
        if checks[0].ok:
            checks += [self.smoke_rag(), self.smoke_plan(), self.smoke_evaluate()]
        self.report.smoke = checks
        summary = " · ".join(f"{c.name} {'✓' if c.ok else '✗'}" for c in checks)
        if not checks[0].ok:
            raise PlatformError(f"the chat turn failed — {checks[0].detail}")
        if all(c.ok for c in checks):
            return "ok", summary
        return "warning", summary + " — " + "; ".join(f"{c.name}: {c.detail}"
                                                       for c in checks if not c.ok)

    # ── step 8: report ───────────────────────────────────────────────────────

    def banner_json(self) -> str:
        groups = " · ".join(f"{g}: {m}" for g, m in self.report.models.items())
        smoke_ok = sum(1 for c in self.report.smoke if c.ok)
        content = (f"Platform ready on {self.report.capability_class} with "
                   f"{self.report.runtime}. {groups}. Smoke {smoke_ok}/{len(self.report.smoke)}. "
                   f"Admin Center: {self.report.urls['admin']} - Orchestrator: pick "
                   f"'Local-EZAI Orchestrator' in the model list (make orchestrator after your "
                   "first login) - CLI: local-ezai status")
        content = content.replace("'", "").replace("#", "").replace("$", "")
        banner = [{"id": BANNER_ID, "type": "success", "title": "Local-EZAI: platform ready",
                   "content": content, "dismissible": True, "timestamp": int(time.time())}]
        return json.dumps(banner, ensure_ascii=False)

    def banner_env_path(self) -> Path:
        return self.ctx.config_dir / FIRST_RUN_DIRNAME / BANNER_ENV_FILENAME

    def openwebui_healthy(self, timeout: float) -> bool:
        target = next(t for t in self.host_targets() if t.id == "openwebui")
        start = self.clock()
        while True:
            [result] = probe_services((target,), self.http.get, environ=self.env, clock=self.clock)
            if result.ok or self.clock() - start >= timeout:
                return result.ok
            self.sleep(POLL_S)

    def show_banner(self) -> str:
        if self.options.skip_banner:
            return "skipped (--skip-banner)"
        if not self.report.ready:
            return "not shown (platform not ready)"
        path = self.banner_env_path()
        text = EnvText(path.read_text(encoding="utf-8") if path.is_file() else "")
        text.set(BANNER_KEY, f"'{self.banner_json()}'")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.text(self.now), encoding="utf-8")
        recreated = self.compose("up", "-d", "openwebui")
        if recreated.returncode == 0 and self.openwebui_healthy(BANNER_TIMEOUT_S):
            return "shown in the WebUI"
        path.unlink(missing_ok=True)  # never leave chat broken for a banner
        self.compose("up", "-d", "openwebui")
        return ("removed — OpenWebUI did not come back with WEBUI_BANNERS set; chat is "
                "unaffected, the card stays in config/first-run/report.md")

    def install_persona(self) -> str:
        proc = self.runner(["bash", str(self.root / "scripts" / "register-orchestrator.sh")])
        if proc.returncode == 0:
            return "Local-EZAI Orchestrator installed in the WebUI"
        if "no user account" in (proc.stdout or "") + (proc.stderr or ""):
            return "after your first login (the first account is admin): make orchestrator"
        return f"not installed ({_tail(proc, 1)}) — retry: make orchestrator"

    def step_report(self) -> tuple[str, str]:
        registry = self.registry()
        report = self.report
        report.runtime = self.active_runtime()
        if registry is not None:
            report.generation = registry.generation
            report.models = {group: members[0] for group, members in registry.groups.items()
                             if members}
        report.urls = {"webui": self.public_url("openwebui"),
                       "admin": self.public_url("monitor", "/overview"),
                       "orchestrator": self.public_url(
                           "openwebui", f"/?models={ORCHESTRATOR_MODEL_ID}")}
        chat_ok = any(c.name == "chat" and c.ok for c in report.smoke) or self.options.skip_smoke
        report.ready = bool(report.health.get("engine") and report.health.get("router")
                            and chat_ok and registry is not None)
        report.next_steps = [
            f"sign up at {report.urls['webui']} — the first account is the admin",
            f"then: make orchestrator · try the Orchestrator at {report.urls['orchestrator']} "
            f"with: Plan: {SMOKE_TASK.split(' and ')[0]} in the sample project",
            "manage models, approvals and runs in the Admin Center or with local-ezai; "
            "you will not need to edit any file again"]
        report.banner = self.show_banner()
        report.persona = self.install_persona() if report.ready else "skipped (not ready)"
        out = self.ctx.config_dir / FIRST_RUN_DIRNAME
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(report.as_dict(), indent=2, default=str)
                                         + "\n", encoding="utf-8")
        card = "\n".join(report.card())
        steps = "\n".join(f"- {s.name}: {s.mark} {s.detail}" for s in report.steps)
        smoke = "\n".join(f"- {c.name}: {'✓' if c.ok else '✗'} {c.detail}" for c in report.smoke)
        (out / "report.md").write_text(
            f"# Local-EZAI first run — {report.created_at}\n\n```\n{card}\n```\n\n"
            f"## Steps\n\n{steps}\n\n## Smoke\n\n{smoke or '- skipped'}\n\n## Health\n\n"
            + "\n".join(f"- {k}: {'up' if v else 'down'}" for k, v in report.health.items())
            + "\n", encoding="utf-8")
        report.report_path = str(out / "report.md")
        return "ok", f"{report.report_path} · banner {report.banner} · persona: {report.persona}"

    # ── run ──────────────────────────────────────────────────────────────────

    def run(self) -> SetupReport:
        self.say(f"local-ezai setup — profile {self.profile} · class {self.ctx.platform.klass}"
                 f" · accelerator {self.ctx.platform.accel}")
        failed = False
        for name, fn, required in (("bootstrap", self.step_bootstrap, True),
                                   ("images", self.step_images, True),
                                   ("embed model", self.step_embed_model, False),
                                   ("up", self.step_up, True),
                                   ("wait-ready", self.step_wait_ready, True),
                                   ("verify", self.step_verify, True)):
            if name == "images":  # the rendered override exists once the bootstrap ran
                try:
                    self.files = compose_files(self.root, self.profile, rendered=True)
                except PlatformError as exc:
                    self.report.steps.append(StepResult(name, "failed", str(exc)))
                    self.say(f"  ✗ {name}: {exc}")
                    failed = True
                    break
            result = self.step(name, fn)
            if result.status == "failed" and required:
                failed = True
                break
        try:
            self.step("report", self.step_report)
        except OSError as exc:  # the report is best effort
            self.say(f"  ! report: {exc}")
        self.report.exit_code = EXIT_OK if self.report.ready and not failed else EXIT_FAILED
        return self.report


# ── init: the fallback wizard ────────────────────────────────────────────────


@dataclass
class InitOptions:
    root: Path
    assume_yes: bool = False
    profile: str | None = None
    interactive: bool | None = None
    env_path: Path | None = None


@dataclass
class Proposal:
    group: str
    ref: str                     # what goes into .env (a catalog id, or the typed reference)
    explanation: str


def recommended_set(ctx: PlatformContext, runtime: str | None) -> list[Proposal]:
    """One eligible catalog entry per group for this host, best first — the
    "recommended set" of FIRST_RUN_EXPERIENCE §3 step 2."""
    roles = reference_registry().roles
    proposals: list[Proposal] = []
    for group in SEED_GROUPS.values():
        contracts = [spec.requires for spec in roles.values() if spec.group == group]
        ranked: list[Recommendation] = recommend(
            ctx.catalog, group, ctx.vector, ctx.descriptors, runtime=runtime,
            contracts=contracts, capability_class=ctx.platform.klass,
            accelerator=ctx.platform.accel)
        eligible = [r for r in ranked if r.eligible]
        if eligible:
            proposals.append(Proposal(group, eligible[0].catalog_id, eligible[0].explain()))
        else:
            detail = ("; ".join(r.explain() for r in ranked[:3])
                      or "no catalog entry lists this group")
            proposals.append(Proposal(group, "", f"nothing in the catalog fits — {detail}"))
    return proposals


def run_init(ctx: PlatformContext, options: InitOptions, *, ask: Callable[[str], str] = input,
             say: Callable[[str], None] = print,
             pipeline_factory: Callable[..., SetupPipeline] = SetupPipeline,
             **pipeline_kwargs: Any) -> SetupReport | int:
    """FIRST_RUN_EXPERIENCE §3: HARDWARE CHECK → MODEL SET (recommended,
    confirmed or overridden) → seeds written to .env → the pipeline (INSTALL,
    BENCHMARK, ACTIVATE = bootstrap; SMOKE = verify). Returns the pipeline's
    report, or an exit code when it stops before it."""
    root = Path(options.root).resolve()
    env_path = (options.env_path or root / ".env").resolve()
    if not env_path.is_file():
        raise SetupError(f"no {env_path} — run ./install.sh first (it detects the hardware, "
                         "mints the secrets and validates the seeds)")
    text = env_path.read_text(encoding="utf-8")
    env = parse_env(text)
    vector = ctx.vector
    say(f"hardware check: accelerator {ctx.platform.accel} · {vector.system_memory_gb:g} GB RAM "
        f"· {vector.cpu_cores} cores → class {ctx.platform.klass}")
    if registry_path(ctx.config_dir).is_file():
        say("model set: the platform is bootstrapped already — continuing with setup")
    else:
        seeds = read_seeds(env)
        problems = validate_seeds(seeds, ctx.descriptors, ctx.catalog, ctx.platform) \
            if not seeds.problems else seeds.problems
        if problems and (seeds.models and not seeds.migrated_from):
            say("model set: the seeds in .env have problems — fix them, or clear them to "
                "let init propose a set:")
            for problem in problems:
                say(f"  - {problem}")
            return EXIT_FAILED
        if problems or not seeds.models or seeds.migrated_from:
            runtime = (seeds.runtime or "").strip() or None
            if runtime is None or runtime not in ctx.descriptors:
                runtime, _, _ = choose_runtime(ctx.descriptors, ctx.platform.klass,
                                               ctx.platform.accel)
            proposals = recommended_set(ctx, runtime)
            say(f"model set (recommended for class {ctx.platform.klass} on {runtime}):")
            for proposal in proposals:
                say(f"  {proposal.group:10} {proposal.ref or '(none)':32} {proposal.explanation}")
            missing = [p.group for p in proposals if not p.ref]
            if missing:
                say(f"no fitting catalog model for {', '.join(missing)} — set "
                    f"{' / '.join(k for k, g in SEED_GROUPS.items() if g in missing)} in "
                    ".env to a source you choose (hf:… gguf:… or a catalog id), then re-run")
                return EXIT_FAILED
            interactive = options.interactive
            if interactive is None:
                interactive = sys.stdin.isatty() and sys.stdout.isatty()
            if not options.assume_yes and not interactive:
                say("no terminal — accept this set with `local-ezai init --yes`, or set the "
                    "seeds in .env yourself, then re-run")
                return EXIT_REVIEW
            chosen: dict[str, str] = {}
            for proposal in proposals:
                ref = proposal.ref
                if interactive and not options.assume_yes:
                    typed = ask(f"  {proposal.group} [{proposal.ref}] (Enter to accept, or a "
                                "catalog id / hf:… / gguf:…): ").strip()
                    if typed:
                        try:
                            parse_source(typed)
                        except SourceError as exc:
                            say(f"  not a model reference: {exc}")
                            return EXIT_FAILED
                        ref = typed
                chosen[proposal.group] = ref
            editor = EnvText(text)
            editor.set(RUNTIME_KEY, runtime or "", comment="set by local-ezai init")
            for key, group in SEED_GROUPS.items():
                editor.set(key, chosen[group], comment="chosen with local-ezai init")
            env_path.write_text(editor.text(time.strftime(_STAMP)), encoding="utf-8")
            seeds = read_seeds(parse_env(env_path.read_text(encoding="utf-8")))
            problems = validate_seeds(seeds, ctx.descriptors, ctx.catalog, ctx.platform)
            if problems:
                say("the chosen set does not validate:")
                for problem in problems:
                    say(f"  - {problem}")
                return EXIT_FAILED
            say("seeds written to .env: " + " · ".join(f"{g} ← {r}" for g, r in chosen.items()))
    pipeline = pipeline_factory(ctx, SetupOptions(root=root, profile=options.profile,
                                                  env_path=env_path), say=say, **pipeline_kwargs)
    return pipeline.run()
