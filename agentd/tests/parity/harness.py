"""The cross-surface parity harness (PR-24, phase P6; CLI_AND_WEBUI_STRATEGY
§7 "consistency test (release gate)").

One matrix row = one scenario executed on **identical, separate worlds** —
one per surface — and compared afterwards:

- ``cli-direct``     the verb in-process (``--transport direct``; repo-work
                     verbs are always in-process);
- ``cli-connected``  the same verb through the daemon (``--transport
                     connected``, the CLI's connected mode);
- ``api``            the control-plane operation as the Admin Center calls
                     it (service token, ``X-EZAI-User`` = the human,
                     ``X-EZAI-Client: admin-center``, an idempotency key).

Three equivalences are asserted per row: the **response bodies** (the CLI's
``--json`` output is the API body), the **declarative state** afterwards
(registry, generations, queue, projects, a repository's memory) and the
**operation audit** (what the platform's own operations recorded). What a
transport adds on top is normalised *explicitly*, never silently: the
``via <client>`` suffix of the forwarded identity, the daemon's own events
(``api.<operation>``, ``run.*``, ``control.*``, ``auth.*``), timestamps, run
ids, commit hashes, wall-clock durations and each world's paths. The
normaliser is data (``DROP_KEYS``, ``PATTERNS``) and has its own test.

Offline like the rest of the suite: the daemon is the FastAPI app served
in-process behind the CLI's ``client_factory`` seam; docker/engine edges are
the fakes of the PR-6..11 tests; git and the pipelines are real.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from agentd import platform_cli
from agentd.activation import Platform
from agentd.capability import CapabilityVector
from agentd.config import ControlConfig, load_config
from agentd.control import (
    API_PREFIX,
    CLIENT_HEADER,
    TOKEN_ENV,
    TRANSPORT_ENV,
    URL_ENV,
    USER_HEADER,
)
from agentd.control import api as control_api
from agentd.control import client as control_client
from agentd.control.app import create_app
from agentd.control.idempotency import HEADER as IDEMPOTENCY_HEADER
from agentd.main_cli import main
from agentd.memory import MemoryStore
from agentd.platform_cli import PlatformContext, build_context
from agentd.registry_v2 import load_registry, save_generation
from agentd.render import write_rendered
from agentd.runtime_descriptor import load_descriptors
from tests.conftest import BUGGY_CALCULATOR, REPO_AGENTD_YAML, git_commit_all
from tests.unit.test_activation import seed_registry
from tests.unit.test_lifecycle import FakeEngineHTTP, FakeRunner

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The human every surface acts for; the transports annotate the client.
HUMAN = "nita"
TOKEN = "parity-gate-token"
#: The API surface identifies as the console — the third column of the matrix.
CONSOLE_CLIENT = "admin-center"

DIRECT, CONNECTED, API = "cli-direct", "cli-connected", "api"
SURFACES = (DIRECT, CONNECTED, API)
#: The CLI transports of the two CLI surfaces.
TRANSPORT = {DIRECT: "direct", CONNECTED: "connected"}

#: Events the daemon records because it acts on someone's behalf: the call
#: wrapper, run supervision, its own life, rejected authentication. They
#: are the transport's annotation, not the operation's audit.
DAEMON_EVENT_PREFIXES = ("api.", "run.", "control.", "auth.")

# ── the normaliser (data) ────────────────────────────────────────────────────

#: Keys whose values are wall-clock or transport facts, dropped before comparing.
DROP_KEYS = frozenset({
    "transport",          # `status` names the surface that answered — by design
    "idempotency_key",    # a fresh UUID per mutation
    "uptime_s", "latency_s", "duration_ms", "duration_seconds", "avg_run_seconds",
    "measured_at",
})
#: Substrings rewritten inside every string value (and key-less places).
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"), "<ts>"),
    (re.compile(r"\b\d{8}-\d{6}-[0-9a-f]{6}\b"), "<run>"),      # runner.new_run_id()
    (re.compile(r"\b[0-9a-f]{40}\b"), "<sha>"),
    (re.compile(r"\b[0-9a-f]{12}\b"), "<sha>"),                 # short hashes in messages
    (re.compile(r"\b\d+\.\d+s\b"), "<s>s"),                     # "… in 0.2s"
    (re.compile(r" via [\w-]+"), ""),                           # the forwarded client
)


def scrub(value: Any, roots: tuple[str, ...] = ()) -> Any:
    """``value`` with every transport/time/place fact normalised."""
    if isinstance(value, dict):
        return {k: scrub(v, roots) for k, v in value.items() if k not in DROP_KEYS}
    if isinstance(value, (list, tuple)):
        return [scrub(v, roots) for v in value]
    if isinstance(value, str):
        for root in sorted(roots, key=len, reverse=True):
            value = value.replace(root, "<world>")
        for pattern, replacement in PATTERNS:
            value = pattern.sub(replacement, value)
        return value
    return value


# ── worlds ───────────────────────────────────────────────────────────────────


class RecordingClient(TestClient):
    """The daemon, in-process, behind the CLI's httpx seam: records every
    request; a no-op context manager so the liveness probe's ``with`` does
    not restart the app (no lifespan events in the audit)."""

    def __init__(self, app) -> None:
        super().__init__(app)
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def request(self, method, url, **kwargs):  # type: ignore[override]
        self.requests.append((method, str(url), dict(kwargs.get("headers") or {})))
        return super().request(method, url, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


@dataclass
class World:
    """One surface's copy of the platform: a bootstrapped checkout, a git
    repository to work on, a local weights file to install, the daemon over
    it (in-process) and the CLI config that points at it."""

    surface: str
    home: Path
    root: Path
    cfg: Path
    repo: Path
    weights: Path
    ctx: PlatformContext
    app: Any
    client: RecordingClient
    url: str
    script: Path
    factory_calls: list[tuple[str, float | None]] = field(default_factory=list)

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def roots(self) -> tuple[str, ...]:
        return (str(self.home),)

    def inspector(self) -> PlatformContext:
        """A fresh direct context to read the world's state (never to act)."""
        return build_context(load_config(self.cfg), self.root, actor="inspector")

    def audit(self):
        return self.ctx.queue.audit()

    def shutdown(self) -> None:
        runs = getattr(self.app.state, "runs", None)
        if runs is not None:
            runs.shutdown(wait=True)


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)
    (path / "calculator.py").write_text(BUGGY_CALCULATOR, encoding="utf-8")
    (path / ".agentd.yaml").write_text(REPO_AGENTD_YAML, encoding="utf-8")
    git_commit_all(path, "initial commit")
    return path


def make_world(base: Path, surface: str, *, script: list[dict] | None = None) -> World:
    """An identical world for ``surface`` under ``base/<surface>``."""
    home = base / surface
    root = home / "platform"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config" / "providers", root / "config" / "providers")
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / "docker-compose.n97.yml").write_text("services: {}\n")
    saved = save_generation(seed_registry(), root / "config", note="bootstrap")
    platform = Platform(config_dir=root / "config", platform_root=root,
                        descriptors=load_descriptors(root / "config"),
                        vector=CapabilityVector(system_memory_gb=15.5, cpu_cores=4),
                        capability_class="cpu-low", accelerator="none")
    write_rendered(platform.render(saved), platform.rendered)
    repo = make_repo(home / "sample")
    weights = home / "delta-q4.gguf"
    weights.write_bytes(b"g" * 4096)
    script_file = home / "script.json"
    script_file.write_text(json.dumps(script or []), encoding="utf-8")
    cfg = home / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({
        "llm": {"provider": "scripted", "script_path": str(script_file)},
        "platform": {"config_dir": str(root / "config")},
        "workspace": {"root": str(home / "ws")},
        "runs_dir": str(home / "runs"),
        "validation": {"autodetect": False},
    }), encoding="utf-8")
    ctx = build_context(load_config(cfg), root, actor="ezaid")
    app = create_app(ControlConfig(token=TOKEN), ctx, prober=lambda url, headers: (200, "ok"))
    return World(surface=surface, home=home, root=root, cfg=cfg, repo=repo, weights=weights,
                 ctx=ctx, app=app, client=RecordingClient(app),
                 url=f"http://{surface}.parity.test:8010", script=script_file)


# ── steps and their execution ────────────────────────────────────────────────

Argv = tuple[str, ...] | Callable[[World], tuple[str, ...]]
ApiCall = tuple[str, str, Any, Any] | Callable[[World], tuple[str, str, Any, Any]]


@dataclass(frozen=True)
class Step:
    """One matrix cell pair: the CLI verb and the API operation that are
    the same operation. ``cli`` is the platform-verb argv (``--json``,
    ``--config`` and ``--transport`` are added); ``api`` is
    ``(method, path, json, params)``. ``error`` marks a refusal both
    surfaces must report identically."""

    label: str
    cli: Argv
    api: ApiCall
    error: bool = False


@dataclass
class Outcome:
    surface: str
    label: str
    code: int          # CLI exit code, or 0/1 from the API status
    status: int | None # HTTP status on the API surface
    body: dict[str, Any]


def _resolve(spec, world: World):
    return spec(world) if callable(spec) else spec


def parse_json_output(text: str) -> dict[str, Any]:
    """The CLI's ``--json`` output must be one JSON document; anything else
    on stdout is a parity defect and fails here loudly."""
    stripped = text.strip()
    if not stripped:
        raise AssertionError("no JSON on stdout")
    return json.loads(stripped)


class Arena:
    """Builds worlds, drives surfaces, wires the seams once per test."""

    def __init__(self, base: Path, monkeypatch) -> None:
        self.base = base
        self.monkeypatch = monkeypatch
        self.worlds: dict[str, World] = {}
        self.runner = FakeRunner()
        monkeypatch.delenv("AGENTD_CONFIG", raising=False)
        monkeypatch.delenv(TRANSPORT_ENV, raising=False)
        monkeypatch.delenv(URL_ENV, raising=False)
        monkeypatch.setenv(TOKEN_ENV, TOKEN)
        monkeypatch.setenv("USER", HUMAN)  # the CLI acts as the same human the API forwards
        monkeypatch.setattr(platform_cli, "default_runner", self.runner)
        monkeypatch.setattr(platform_cli, "http_probe", lambda url: 200)
        monkeypatch.setattr(platform_cli, "engine_http", lambda: FakeEngineHTTP(
            timings={"predicted_per_second": 15.5, "prompt_per_second": 80.0,
                     "predicted_n": 100}))
        monkeypatch.setattr(platform_cli, "build_validator",
                            lambda ctx: lambda descriptor, name, entry: platform_cli.lifecycle
                            .ProbeResult(True, "", 0.2, name))
        monkeypatch.setattr(control_api, "docker_available", lambda: False)
        # every connected call reaches the world the URL names
        monkeypatch.setattr(control_client, "client_factory", self._factory)

    def _factory(self, url: str, timeout: float | None):
        for world in self.worlds.values():
            if world.url == url:
                world.factory_calls.append((url, timeout))
                return world.client
        raise AssertionError(f"no parity world serves {url}")

    def build(self, *surfaces: str, script: list[dict] | None = None) -> dict[str, World]:
        for surface in surfaces:
            if surface not in self.worlds:
                self.worlds[surface] = make_world(self.base, surface, script=script)
        return {s: self.worlds[s] for s in surfaces}

    def close(self) -> None:
        for world in self.worlds.values():
            world.shutdown()

    # ── CLI ──────────────────────────────────────────────────────────────

    def cli(self, world: World, argv: tuple[str, ...], *, transport: str | None,
            as_json: bool = True, repo: bool = False) -> tuple[int, str]:
        """Run the CLI on ``world``: a platform verb (``transport`` set), or a
        repo-work verb on the world's repository (``repo=True``, no transport
        flag exists — it is always in-process)."""
        full = [str(world.repo)] if repo else []
        full += list(argv)
        if as_json:
            full.append("--json")
        if transport is not None:
            full += ["--transport", transport]
        full += ["--config", str(world.cfg)]
        self.monkeypatch.setenv(URL_ENV, world.url)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(full)
        return code, out.getvalue()

    # ── API ──────────────────────────────────────────────────────────────

    def headers(self, mutating: bool) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {TOKEN}", USER_HEADER: HUMAN,
                   CLIENT_HEADER: CONSOLE_CLIENT}
        if mutating:
            headers[IDEMPOTENCY_HEADER] = str(uuid.uuid4())
        return headers

    def api(self, world: World, method: str, path: str, json_body: Any = None,
            params: Any = None):
        return world.client.request(method, f"{API_PREFIX}{path}", json=json_body,
                                    params=params, headers=self.headers(method != "GET"))

    # ── a step on a surface ──────────────────────────────────────────────

    def execute(self, step: Step, surface: str) -> Outcome:
        world = self.worlds[surface]
        if surface == API:
            method, path, json_body, params = _resolve(step.api, world)
            response = self.api(world, method, path, json_body, params)
            body = response.json()
            failed = response.status_code >= 400
            assert failed == step.error, (step.label, surface, response.status_code, body)
            return Outcome(surface, step.label, 1 if failed else 0, response.status_code, body)
        code, out = self.cli(world, _resolve(step.cli, world), transport=TRANSPORT[surface])
        body = parse_json_output(out)
        assert (code != 0) == step.error, (step.label, surface, code, body)
        return Outcome(surface, step.label, code, None, body)

    def run_scenario(self, steps: list[Step], surfaces: tuple[str, ...] = SURFACES,
                     ) -> dict[str, list[Outcome]]:
        return {surface: [self.execute(step, surface) for step in steps] for surface in surfaces}


# ── what is compared ─────────────────────────────────────────────────────────


def memory_records(repo: Path, memory_dir: str) -> list[dict[str, Any]]:
    store = MemoryStore(repo / memory_dir)
    try:
        if not store.exists:
            return []
        return [{"kind": r.kind, "title": r.title, "content": r.content, "run_id": r.run_id}
                for r in store.recent(limit=100)]
    finally:
        store.close()


def state(world: World) -> dict[str, Any]:
    """The declarative state a surface may have changed, normalised."""
    ctx = world.inspector()
    registry = load_registry(world.config_dir)
    return scrub({
        "registry": registry.model_dump(mode="json"),
        "rendered_generation": platform_cli._manifest_generation(ctx),
        "generations": platform_cli.generation_history(ctx, limit=50)["generations"],
        "queue": platform_cli.list_requests(ctx)["requests"],
        "projects": platform_cli.list_projects(ctx)["projects"],
        "memory": memory_records(world.repo, ctx.config.memory.dir),
    }, world.roots)


@dataclass
class AuditView:
    """The platform's operation events (what the operation itself recorded)
    and the daemon-side events (what a transport added)."""

    operations: list[dict[str, Any]]
    daemon: list[dict[str, Any]]

    @property
    def operation_events(self) -> list[str]:
        return [r["event"] for r in self.operations]

    @property
    def daemon_events(self) -> list[str]:
        return [r["event"] for r in self.daemon]


def audit(world: World) -> AuditView:
    operations, daemon = [], []
    for record in world.audit():
        entry = scrub(record.model_dump(mode="json"), world.roots)
        entry.pop("ts", None)
        if record.event.startswith(DAEMON_EVENT_PREFIXES):
            entry["details"].pop("client", None)   # cli vs admin-center: the annotation
            daemon.append(entry)
        else:
            operations.append(entry)
    return AuditView(operations, daemon)


def bodies(outcomes: dict[str, list[Outcome]], world_of: dict[str, World]) -> dict[str, list]:
    return {surface: [scrub(o.body, world_of[surface].roots) for o in results]
            for surface, results in outcomes.items()}


def all_equal(values: dict[str, Any], what: str) -> None:
    """Every surface's ``value`` equals the first one's — with the differing
    surfaces named."""
    surfaces = list(values)
    reference = values[surfaces[0]]
    for surface in surfaces[1:]:
        assert values[surface] == reference, (
            f"{what}: {surface} differs from {surfaces[0]}\n"
            f"{surfaces[0]}: {json.dumps(reference, indent=1, default=str)[:4000]}\n"
            f"{surface}: {json.dumps(values[surface], indent=1, default=str)[:4000]}")


def wait_terminal(arena: Arena, world: World, run_id: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = arena.api(world, "GET", f"/runs/{run_id}").json()
        if body.get("status") in ("completed", "failed", "cancelled"):
            return body
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not finish: {body}")


def journal_types(path: Path) -> list[str]:
    return [json.loads(line)["type"] for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
