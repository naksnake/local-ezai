"""Model lifecycle — install · validate · benchmark (PR-4, ADR-027;
docs/MODEL_LIFECYCLE_MANAGEMENT.md §1–§2, docs/PROVIDER_ABSTRACTION.md
§5–§6).

State machine (data, enforced by ``transition``)::

    registered ──install──► installed ──benchmark──► benchmarked ──► active …
        │                      │                         │           (PR-5)
        └──────────── failed ◄─┴─────────────────────────┘

- ``install``: resolve the reference (registry name · hf: · gguf: · catalog
  id · auto) → fetch into the runtime's weights directory → provider
  ``validate_model`` (side-loaded engine, one-token probe) → ``installed``
  with measured ``size_gb``, or ``failed`` with the reason. Idempotent:
  present-and-verified weights are not re-downloaded.
- ``benchmark``: ``bench`` verb against a side-loaded engine (or a live
  base URL) → tokens/sec recorded on the entry and appended to the
  ``.agent/model_benchmarks.json`` trend history (capped) → ``benchmarked``.
- Every operation persists the registry as the next generation when a
  config dir is given (self-service, audited — never approval-gated).

Runtime knowledge stays in descriptors: the side-load reuses the PR-3
``materialize_service`` verbatim; probe/bench field names come from the
descriptor's ``verbs``. Process and network edges are injectable so the
suite stays offline.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml

from agentd.capability import CapabilityVector, classify
from agentd.catalog import (
    Catalog,
    CatalogError,
    entry_to_model,
    provider_for_format,
    recommend_one,
    variant_for,
)
from agentd.fetch import (
    Fetched,
    Fetcher,
    FetchError,
    GGUFFetcher,
    HFFetcher,
    SourceError,
    parse_source,
)
from agentd.logging_setup import get_logger
from agentd.registry_v2 import (
    ModelEntry,
    RegistryResolutionError,
    RegistryV2,
    save_generation,
)
from agentd.render import ENGINE_PORT, materialize_service, served_id_for
from agentd.runtime_descriptor import RuntimeDescriptor

log = get_logger("lifecycle")

BENCHMARKS_FILENAME = "model_benchmarks.json"
TREND_CAP = 20
BENCH_PROMPT = "Count from one to thirty in English words, comma separated."
PROBE_PROMPT = "Reply with the single word: ready"

#: Allowed transitions (docs/MODEL_LIFECYCLE_MANAGEMENT.md §1). Same-state
#: transitions are idempotent re-runs. Activation paths are data here and
#: exercised by PR-5.
TRANSITIONS: dict[str, frozenset[str]] = {
    "registered": frozenset({"installed", "failed"}),
    "installed": frozenset({"installed", "benchmarked", "failed"}),
    "benchmarked": frozenset({"installed", "benchmarked", "active", "failed"}),
    "active": frozenset({"active", "retired"}),
    "retired": frozenset({"active", "installed"}),
    "failed": frozenset({"installed", "failed"}),
}


class LifecycleError(ValueError):
    """An operation that cannot proceed; the message names the fix."""


def transition(entry: ModelEntry, to: str, *, reason: str = "") -> ModelEntry:
    """Move an entry to ``to`` if the state machine allows it."""
    if to not in TRANSITIONS.get(entry.state, frozenset()):
        raise LifecycleError(
            f"transition {entry.state} → {to} is not allowed"
            + (f" ({reason})" if reason else "")
            + f"; from '{entry.state}' you can reach: "
            + ", ".join(sorted(TRANSITIONS.get(entry.state, ()))))
    entry.state = to  # type: ignore[assignment]
    return entry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── host paths (compose interpolation of descriptor data) ────────────────────

_INTERP = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def interpolate_compose(value: str, env: Mapping[str, str]) -> str:
    """Resolve ``${VAR:-default}`` / ``${VAR-default}`` / ``${VAR}`` / ``$VAR``
    the way docker compose does, from the given environment."""
    def substitute(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(3)
        default = match.group(2)
        current = env.get(name)
        if current is None or (current == "" and match.group(0).count(":-")):
            return default or ""
        return current
    return _INTERP.sub(substitute, value)


def weights_dir(descriptor: RuntimeDescriptor, platform_root: Path,
                env: Mapping[str, str] | None = None) -> Path:
    """The host directory a runtime mounts its weights from — the descriptor
    declares it (with compose interpolation); relative paths are anchored
    at the platform root (where docker-compose.yml lives)."""
    raw = interpolate_compose(descriptor.weights.host_dir,
                              os.environ if env is None else env)
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (Path(platform_root) / path).resolve()


def default_fetcher(fmt: str, directory: Path) -> Fetcher:
    """GGUF → native fetcher; every hub format → the hub client."""
    if fmt == "gguf":
        return GGUFFetcher(directory)
    return HFFetcher(directory)


FetcherFactory = Callable[[str, Path], Fetcher]


# ── engine access: HTTP + process edges (fakeable) ───────────────────────────


class EngineHTTP(Protocol):
    def get(self, url: str) -> tuple[int, Any]: ...
    def post(self, url: str, payload: dict[str, Any], timeout: float) -> tuple[int, Any]: ...


class HttpxEngineHTTP:
    def get(self, url: str) -> tuple[int, Any]:
        import httpx

        try:
            response = httpx.get(url, timeout=5.0)
        except httpx.HTTPError:
            return 0, None
        return response.status_code, _json_or_none(response)

    def post(self, url: str, payload: dict[str, Any], timeout: float) -> tuple[int, Any]:
        import httpx

        response = httpx.post(url, json=payload, timeout=timeout)
        return response.status_code, _json_or_none(response)


def _json_or_none(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class ProbeResult:
    ok: bool
    error: str = ""
    latency_s: float = 0.0
    served_id: str = ""


@dataclass
class BenchmarkResult:
    model: str
    runtime: str
    tokens_per_s: float
    prompt_tokens_per_s: float | None
    completion_tokens: int
    latency_s: float
    via: str  # "side-load" | "live"
    measured_at: str = field(default_factory=_now)
    persisted: bool = False

    def as_record(self) -> dict[str, Any]:
        return {"last": self.measured_at, "tokens_per_s": round(self.tokens_per_s, 1),
                "prompt_tokens_per_s": (round(self.prompt_tokens_per_s, 1)
                                        if self.prompt_tokens_per_s is not None else None),
                "completion_tokens": self.completion_tokens,
                "latency_s": round(self.latency_s, 2), "via": self.via,
                "runtime": self.runtime}


class SideLoad:
    """A short-lived second engine on an ephemeral host port
    (PROVIDER_ABSTRACTION §6): the PR-3 materialization of exactly one model,
    run as its own compose project, torn down on exit. Never registered in
    LiteLLM's alias space — only validation and benchmarking address it."""

    def __init__(self, descriptor: RuntimeDescriptor, vector: CapabilityVector,
                 capability_class: str, accelerator: str, name: str, entry: ModelEntry, *,
                 platform_root: Path, workdir: Path, runner: Runner = default_runner,
                 http: EngineHTTP | None = None, port: int | None = None,
                 env: Mapping[str, str] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.descriptor, self.vector = descriptor, vector
        self.capability_class, self.accelerator = capability_class, accelerator
        self.name, self.entry = name, entry
        self.platform_root, self.workdir = Path(platform_root), Path(workdir)
        self._run, self.http = runner, http or HttpxEngineHTTP()
        self.port = port or free_port()
        self._env = os.environ if env is None else env
        self._sleep, self._clock = sleep, clock
        self.project = f"ezai-sideload-{re.sub(r'[^a-z0-9]+', '-', name.lower())}"
        self.compose_file = self.workdir / "docker-compose.sideload.yml"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def compose_document(self) -> tuple[dict[str, Any], str | None]:
        service, preset = materialize_service(
            self.descriptor, self.vector, self.capability_class, self.accelerator,
            {self.name: self.entry}, rendered_dir_rel=str(self.workdir))
        service = dict(service)
        if "deploy" in service:  # standalone project: no base file to override
            service["deploy"] = dict(service["deploy"])
        volumes = [interpolate_compose(v, self._env) for v in service.get("volumes", [])]
        named: dict[str, dict] = {}
        resolved: list[str] = []
        for spec in volumes:
            source = spec.split(":", 1)[0]
            if source and not source.startswith(("/", ".", "~", "$")):
                named[source] = {}
            elif source.startswith("."):
                spec = str((self.platform_root / source).resolve()) + spec[len(source):]
            resolved.append(spec)
        service["volumes"] = resolved
        service["ports"] = [f"{self.port}:{ENGINE_PORT}"]
        service.pop("container_name", None)
        document: dict[str, Any] = {"services": {"engine": service}}
        if named:
            document["volumes"] = named
        return document, preset

    def __enter__(self) -> SideLoad:
        version = self._run(["docker", "compose", "version"])
        if version.returncode != 0:
            raise LifecycleError(
                "docker compose is not available — model validation and "
                "benchmarking side-load the engine through it")
        document, preset = self.compose_document()
        self.workdir.mkdir(parents=True, exist_ok=True)
        if preset is not None:
            (self.workdir / "engine-models.ini").write_text(preset, encoding="utf-8")
        self.compose_file.write_text(yaml.safe_dump(document, sort_keys=False),
                                     encoding="utf-8")
        up = self._run(["docker", "compose", "-p", self.project, "-f",
                        str(self.compose_file), "up", "-d"])
        if up.returncode != 0:
            self._down()
            tail = (up.stderr or up.stdout or "").strip().splitlines()[-3:]
            raise LifecycleError(f"side-load of '{self.name}' failed to start: "
                                 + " | ".join(tail))
        return self

    def wait_ready(self) -> float:
        """Poll the descriptor's readiness probe; returns seconds waited."""
        ready = self.descriptor.verbs.ready
        started = self._clock()
        url = f"{self.base_url}{ready.path}"
        while True:
            status, _ = self.http.get(url)
            if status == 200:
                return self._clock() - started
            if self._clock() - started > ready.timeout_s:
                raise LifecycleError(
                    f"'{self.name}' did not become ready within {ready.timeout_s}s "
                    f"({url} last answered HTTP {status or 'nothing'})")
            self._sleep(min(ready.interval_s, 5))

    def __exit__(self, *exc: object) -> None:
        self._down()

    def _down(self) -> None:
        self._run(["docker", "compose", "-p", self.project, "-f", str(self.compose_file),
                   "down", "-v", "--remove-orphans"])


def probe_model(http: EngineHTTP, base_url: str, served_id: str, max_tokens: int,
                timeout: float = 120.0,
                clock: Callable[[], float] = time.monotonic) -> ProbeResult:
    """``validate_model``: one chat completion must come back well-formed."""
    started = clock()
    payload = {"model": served_id, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": PROBE_PROMPT}]}
    try:
        status, body = http.post(f"{base_url}/v1/chat/completions", payload, timeout)
    except Exception as exc:  # noqa: BLE001 — a failed probe is a result
        return ProbeResult(False, f"{type(exc).__name__}: {str(exc)[:200]}",
                           clock() - started, served_id)
    latency = clock() - started
    if status != 200 or not isinstance(body, dict) or not body.get("choices"):
        return ProbeResult(False, f"probe HTTP {status}: "
                           f"{json.dumps(body)[:200] if body is not None else 'no body'}",
                           latency, served_id)
    return ProbeResult(True, "", latency, served_id)


def measure_tokens_per_s(http: EngineHTTP, base_url: str, served_id: str,
                         descriptor: RuntimeDescriptor, *, model: str, via: str,
                         timeout: float = 600.0,
                         clock: Callable[[], float] = time.monotonic) -> BenchmarkResult:
    """``bench``: the ``make bench`` measurement — server-side timings when
    the descriptor names them, else completion tokens over wall clock."""
    bench = descriptor.verbs.bench
    payload = {"model": served_id, "max_tokens": bench.max_tokens,
               "messages": [{"role": "user", "content": BENCH_PROMPT}]}
    started = clock()
    status, body = http.post(f"{base_url}/v1/chat/completions", payload, timeout)
    elapsed = max(clock() - started, 1e-6)
    if status != 200 or not isinstance(body, dict):
        raise LifecycleError(f"benchmark request failed: HTTP {status}")
    timings = body.get(bench.timings_field) if bench.timings_field else None
    usage = body.get("usage") or {}
    completion = int(usage.get("completion_tokens") or 0)
    if isinstance(timings, dict) and bench.rate_key and timings.get(bench.rate_key) is not None:
        rate = float(timings[bench.rate_key])
        prompt_rate = timings.get(bench.prompt_rate_key) if bench.prompt_rate_key else None
        completion = int(timings.get(bench.count_key) or completion) if bench.count_key \
            else completion
    else:
        if completion <= 0:
            raise LifecycleError("benchmark response carries neither server timings "
                                 "nor usage.completion_tokens — cannot compute tokens/s")
        rate, prompt_rate = completion / elapsed, None
    return BenchmarkResult(model=model, runtime=descriptor.runtime, tokens_per_s=rate,
                           prompt_tokens_per_s=(float(prompt_rate) if prompt_rate is not None
                                                else None),
                           completion_tokens=completion, latency_s=elapsed, via=via)


# ── install ──────────────────────────────────────────────────────────────────


@dataclass
class Resolved:
    name: str
    entry: ModelEntry
    fmt: str
    ref: str
    is_new: bool
    origin: str  # "registry" | "hf" | "gguf" | "catalog" | "auto"


def resolve_target(registry: RegistryV2, target: str, descriptors: dict[str, RuntimeDescriptor],
                   *, catalog: Catalog | None = None, runtime: str | None = None,
                   group: str | None = None, name: str | None = None,
                   vector: CapabilityVector | None = None,
                   capability_class: str | None = None,
                   accelerator: str | None = None) -> Resolved:
    """Registry name · hf: · gguf: · catalog id · auto → a model entry ready
    to fetch, with the format/reference the serving runtime uses."""
    if target in registry.models and not name:
        entry = registry.models[target].model_copy(deep=True)
        descriptor = descriptors.get(entry.provider)
        if descriptor is None:
            raise LifecycleError(f"model '{target}' uses runtime '{entry.provider}' which "
                                 "has no descriptor under config/providers/")
        source = descriptor.source_for(entry.source)
        if source is None:
            raise LifecycleError(
                f"model '{target}' has no source in a format runtime "
                f"'{descriptor.runtime}' serves ({', '.join(descriptor.serves_formats)})")
        return Resolved(target, entry, source[0], source[1], False, "registry")

    ref = parse_source(target)
    if ref.kind in ("hf", "gguf"):
        provider = provider_for_format(ref.kind, descriptors, runtime)
        if provider is None:
            wanted = f"runtime '{runtime}'" if runtime else "no shipped runtime"
            raise LifecycleError(
                f"{wanted} serves the {ref.kind} format of '{target}' — install a "
                "variant in a served format or select another runtime")
        entry = ModelEntry(provider=provider, source=ref.as_source())
        return Resolved(name or ref.default_name(), entry, ref.kind, ref.ref, True, ref.kind)

    if catalog is None:
        raise LifecycleError(f"'{target}' is a catalog reference but no catalog was loaded")
    if ref.kind == "auto":
        if not group:
            raise LifecycleError("'auto' needs a group (reasoning | coding | chat) so the "
                                 "recommender knows which contracts to satisfy")
        if vector is None:
            raise LifecycleError("'auto' needs this host's capability vector")
        contracts = [spec.requires for spec in registry.roles.values() if spec.group == group]
        pick = recommend_one(catalog, group, vector, descriptors, runtime=runtime,
                             contracts=contracts, capability_class=capability_class,
                             accelerator=accelerator)
        log.info("auto (%s): %s", group, pick.explain())
        return Resolved(name or pick.catalog_id, pick.model, pick.format,
                        pick.model.source[pick.format], True, "auto")

    catalog_entry = catalog.entries.get(ref.ref)
    if catalog_entry is None:
        raise LifecycleError(
            f"'{ref.ref}' is neither a registry model nor a catalog id (catalog has: "
            + ", ".join(sorted(catalog.entries)) + ") — or pass hf:/gguf: explicitly")
    fmt, provider = variant_for(catalog_entry, descriptors, runtime)
    entry = entry_to_model(catalog_entry, fmt, provider)
    return Resolved(name or ref.ref, entry, fmt, entry.source[fmt], True, "catalog")


Validator = Callable[[RuntimeDescriptor, str, ModelEntry], ProbeResult]


@dataclass
class InstallResult:
    registry: RegistryV2
    name: str
    state: str
    message: str
    fetched: Fetched | None = None
    probe: ProbeResult | None = None
    #: False when a persist dir was given but the registry is not yet
    #: servable (pre-bootstrap: no activation exists) — the in-memory
    #: result is complete; the caller persists after activation.
    persisted: bool = False

    @property
    def ok(self) -> bool:
        return self.state == "installed"


UNSERVABLE_HINT = ("registry not persisted: no generation can be written before a "
                   "model set is active (bootstrap / activation creates it) — the "
                   "in-memory result is complete")


def persist(registry: RegistryV2, persist_dir: Path | None, note: str) -> tuple[RegistryV2, bool]:
    """Save the next generation when asked and servable; never let the
    write-time invariant of PR-1 turn a completed install into a crash."""
    if persist_dir is None:
        return registry, False
    try:
        return save_generation(registry, persist_dir, note=note), True
    except RegistryResolutionError as exc:
        log.warning("%s (%s)", UNSERVABLE_HINT, str(exc).splitlines()[0])
        return registry, False


class SideLoadValidator:
    """The default ``validate_model``: side-load, wait for readiness, probe."""

    def __init__(self, vector: CapabilityVector, capability_class: str, accelerator: str, *,
                 platform_root: Path, workdir: Path, runner: Runner = default_runner,
                 http: EngineHTTP | None = None, env: Mapping[str, str] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.vector, self.capability_class, self.accelerator = vector, capability_class, accelerator
        self.platform_root, self.workdir = Path(platform_root), Path(workdir)
        self.runner, self.http, self.env, self.sleep, self.clock = runner, http, env, sleep, clock

    def __call__(self, descriptor: RuntimeDescriptor, name: str, entry: ModelEntry) -> ProbeResult:
        served = served_id_for(descriptor, name, entry, self.capability_class, self.accelerator)
        try:
            with SideLoad(descriptor, self.vector, self.capability_class, self.accelerator,
                          name, entry, platform_root=self.platform_root,
                          workdir=self.workdir / name, runner=self.runner, http=self.http,
                          env=self.env, sleep=self.sleep, clock=self.clock) as engine:
                engine.wait_ready()
                return probe_model(engine.http, engine.base_url, served,
                                   descriptor.verbs.validate_model.max_tokens,
                                   clock=self.clock)
        except LifecycleError as exc:
            return ProbeResult(False, str(exc), served_id=served)


def install(registry: RegistryV2, target: str, *, descriptors: dict[str, RuntimeDescriptor],
            vector: CapabilityVector, platform_root: Path, validator: Validator,
            catalog: Catalog | None = None, runtime: str | None = None,
            group: str | None = None, name: str | None = None,
            capability_class: str | None = None, accelerator: str | None = None,
            fetcher_factory: FetcherFactory = default_fetcher, refetch: bool = False,
            persist_dir: Path | None = None,
            env: Mapping[str, str] | None = None) -> InstallResult:
    """``model install <name|source>`` — see module docstring. Returns the
    updated registry (persisted as the next generation when ``persist_dir``
    is given); never raises for fetch/validation failures — those become
    state ``failed`` with the reason on the entry."""
    capability_class = capability_class or classify(vector)
    accelerator = accelerator or vector.accelerator
    updated = registry.model_copy(deep=True)
    try:
        resolved = resolve_target(updated, target, descriptors, catalog=catalog,
                                  runtime=runtime, group=group, name=name, vector=vector,
                                  capability_class=capability_class, accelerator=accelerator)
    except (SourceError, CatalogError) as exc:
        raise LifecycleError(str(exc)) from exc
    entry, model_name = resolved.entry, resolved.name
    descriptor = descriptors[entry.provider]
    if resolved.is_new:
        if model_name in updated.models:
            raise LifecycleError(
                f"a model named '{model_name}' already exists — pass name=… to add "
                "this source under another name, or install by its registry name")
        if updated.providers and entry.provider not in updated.providers:
            updated.providers.append(entry.provider)
        log.info("registered %s (%s via %s → %s)", model_name, resolved.fmt,
                 resolved.origin, entry.provider)
    updated.models[model_name] = entry  # resolved entries are copies: bind the working one

    directory = weights_dir(descriptor, platform_root, env)
    try:
        fetched = fetcher_factory(resolved.fmt, directory).fetch(
            resolved.ref, expected_sha256=entry.source.get("sha256", ""), refetch=refetch)
    except FetchError as exc:
        transition(entry, "failed", reason="fetch failed")
        entry.error = f"fetch: {exc}"
        return _finish(updated, model_name, f"install {model_name} failed: {entry.error}",
                       persist_dir, fetched=None, probe=None)
    entry.artifact = fetched.artifact
    entry.size_gb = round(fetched.size_bytes / 1024**3, 2)
    if fetched.sha256:
        entry.source["sha256"] = fetched.sha256

    probe = validator(descriptor, model_name, entry)
    if probe.ok:
        transition(entry, "installed", reason="validated")
        entry.installed_at, entry.error = _now(), ""
        message = (f"install {model_name}: installed ({entry.size_gb} GB, "
                   f"{'weights reused' if fetched.reused else 'downloaded'}, probe "
                   f"{probe.latency_s:.1f}s)")
    else:
        transition(entry, "failed", reason="validation failed")
        entry.error = f"validate_model: {probe.error}"
        message = f"install {model_name} failed: {entry.error}"
    return _finish(updated, model_name, message, persist_dir, fetched, probe)


def _finish(updated: RegistryV2, name: str, message: str, persist_dir: Path | None,
            fetched: Fetched | None, probe: ProbeResult | None) -> InstallResult:
    validated = RegistryV2.model_validate(updated.model_dump(mode="json"))
    validated, persisted = persist(validated, persist_dir, message)
    if persist_dir is not None and not persisted:
        message = f"{message} ({UNSERVABLE_HINT})"
    log.info(message)
    return InstallResult(validated, name, validated.models[name].state, message, fetched,
                         probe, persisted)


# ── benchmark ────────────────────────────────────────────────────────────────


def record_trend(agent_dir: Path, name: str, record: dict[str, Any]) -> Path:
    """Append a measurement to ``models[name]`` in the ADR-024 benchmarks
    file (capped), leaving evaluate-models' own sections untouched."""
    agent_dir = Path(agent_dir)
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / BENCHMARKS_FILENAME
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            data = {}
    models = data.setdefault("models", {})
    history = list(models.get(name, []))
    history.append(record)
    models[name] = history[-TREND_CAP:]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def benchmark(registry: RegistryV2, name: str, *, descriptors: dict[str, RuntimeDescriptor],
              vector: CapabilityVector, platform_root: Path, workdir: Path,
              capability_class: str | None = None, accelerator: str | None = None,
              base_url: str | None = None, runner: Runner = default_runner,
              http: EngineHTTP | None = None, agent_dir: Path | None = None,
              persist_dir: Path | None = None, env: Mapping[str, str] | None = None,
              sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic,
              ) -> tuple[RegistryV2, BenchmarkResult]:
    """``model benchmark <name>`` — tokens/sec on THIS box, recorded on the
    entry and in the trend file; the entry becomes ``benchmarked`` (an
    ``active`` model keeps serving and keeps its state)."""
    if name not in registry.models:
        raise LifecycleError(f"unknown model '{name}'")
    entry = registry.models[name]
    if entry.state not in ("installed", "benchmarked", "active"):
        raise LifecycleError(f"model '{name}' is {entry.state} — install it first")
    descriptor = descriptors.get(entry.provider)
    if descriptor is None:
        raise LifecycleError(f"runtime '{entry.provider}' has no descriptor")
    capability_class = capability_class or classify(vector)
    accelerator = accelerator or vector.accelerator
    served = served_id_for(descriptor, name, entry, capability_class, accelerator)
    http = http or HttpxEngineHTTP()

    if base_url:
        result = measure_tokens_per_s(http, base_url.rstrip("/"), served, descriptor,
                                      model=name, via="live", clock=clock)
    else:
        with SideLoad(descriptor, vector, capability_class, accelerator, name, entry,
                      platform_root=platform_root, workdir=Path(workdir) / name,
                      runner=runner, http=http, env=env, sleep=sleep, clock=clock) as engine:
            engine.wait_ready()
            result = measure_tokens_per_s(engine.http, engine.base_url, served, descriptor,
                                          model=name, via="side-load", clock=clock)

    updated = registry.model_copy(deep=True)
    target = updated.models[name]
    target.benchmarks = {**result.as_record(), "capability_class": capability_class}
    if target.state != "active":
        transition(target, "benchmarked", reason="benchmark recorded")
    if agent_dir is not None:
        record_trend(agent_dir, name, {**result.as_record(), "capability_class": capability_class})
    updated, result.persisted = persist(updated, persist_dir,
                                        f"benchmark {name}: {result.tokens_per_s:.1f} tok/s")
    log.info("benchmark %s: %.1f tok/s (%s)", name, result.tokens_per_s, result.via)
    return updated, result


# ── retire / uninstall (PR-5; MODEL_LIFECYCLE §2, MODEL_GOVERNANCE_V2 §2) ─────


def retire(registry: RegistryV2, name: str, *,
           persist_dir: Path | None = None) -> tuple[RegistryV2, bool]:
    """``model retire <name>``: remove an ACTIVE model from resolution while
    keeping its weights for rollback. Self-service only when the model
    serves nothing: a current primary, or the last active member a role
    depends on, is blocked — activate a replacement (approval-gated) first."""
    entry = registry.models.get(name)
    if entry is None:
        raise LifecycleError(f"unknown model '{name}'")
    if entry.state != "active":
        raise LifecycleError(f"'{name}' is {entry.state} — only active models are retired "
                             "(uninstall removes installed/failed ones)")
    serving = sorted(role for role, resolution in registry.resolve_all().items()
                     if resolution.primary == name)
    if serving:
        raise LifecycleError(
            f"'{name}' is the primary of role(s) {', '.join(serving)} — retiring it changes "
            "what serves users: activate a replacement (approval-gated) first")
    updated = registry.model_copy(deep=True)
    transition(updated.models[name], "retired", reason="retire")
    try:
        updated.resolve_all()
    except RegistryResolutionError as exc:
        raise LifecycleError(
            f"'{name}' is the last active member a role depends on — retire blocked:\n{exc}"
        ) from exc
    updated, persisted = persist(updated, persist_dir, f"retire {name}")
    log.info("retired %s", name)
    return updated, persisted


def _remove_path(path: Path) -> None:
    import shutil

    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def rollback_targets(config_dir: Path, name: str) -> list[int]:
    """Generations in which the model was active — rolling back to them
    needs its weights."""
    from agentd.registry_v2 import list_generations, load_generation

    targets: list[int] = []
    for number in list_generations(config_dir):
        snapshot = load_generation(config_dir, number)
        entry = snapshot.models.get(name)
        if entry is not None and entry.state == "active":
            targets.append(number)
    return targets


def uninstall(registry: RegistryV2, name: str, *, config_dir: Path | None = None,
              force: bool = False, remover: Callable[[Path], None] = _remove_path,
              persist_dir: Path | None = None) -> tuple[RegistryV2, bool]:
    """``model uninstall <name>``: delete weights and forget the model.
    Blocked unless the model is retired (or never validly installed);
    needs ``force`` when a stored generation would need it for rollback."""
    entry = registry.models.get(name)
    if entry is None:
        raise LifecycleError(f"unknown model '{name}'")
    if entry.state in ("installed", "benchmarked", "active"):
        raise LifecycleError(f"'{name}' is {entry.state} — retire it first (an active model "
                             "needs a replacement; installed/benchmarked ones: retire is "
                             "not applicable, activate-then-retire or keep it)")
    if config_dir is not None:
        targets = rollback_targets(config_dir, name)
        if targets and not force:
            raise LifecycleError(
                f"generation(s) {', '.join(map(str, targets))} list '{name}' as active — "
                "rolling back to them would need its weights; pass force=True to delete "
                "anyway (those generations stop being rollback targets)")
    if entry.artifact:
        remover(Path(entry.artifact))
    updated = registry.model_copy(deep=True)
    del updated.models[name]
    for group, members in updated.groups.items():
        updated.groups[group] = [m for m in members if m != name]
    for spec in updated.roles.values():
        spec.pin = [p for p in spec.pin if p != name]
    updated = RegistryV2.model_validate(updated.model_dump(mode="json"))
    updated, persisted = persist(updated, persist_dir, f"uninstall {name}")
    log.info("uninstalled %s", name)
    return updated, persisted
