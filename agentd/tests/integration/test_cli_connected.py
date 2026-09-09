"""CLI connected mode (PR-11, ADR-028; CLI_AND_WEBUI_STRATEGY §2): the
management verbs use the control plane when it answers, run in-process
otherwise — same commands, same outputs, only the transport differs.

Offline: the daemon is the FastAPI app served in-process (an httpx-compatible
test client injected through the ``client_factory`` seam) plus one real
socket smoke through uvicorn on a loopback port; docker/HTTP edges faked as
in the direct-mode CLI tests."""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from agentd import platform_cli
from agentd.activation import Platform
from agentd.capability import CapabilityVector
from agentd.config import ControlConfig, load_config
from agentd.control import CLIENT_HEADER, TOKEN_ENV, TRANSPORT_ENV, URL_ENV, USER_HEADER
from agentd.control import client as control_client
from agentd.control.app import create_app
from agentd.control.client import ControlPlaneError, ControlUnreachable
from agentd.control.idempotency import HEADER as IDEMPOTENCY_HEADER
from agentd.main_cli import main
from agentd.platform_cli import build_context
from agentd.registry_v2 import load_registry, save_generation
from agentd.render import write_rendered
from agentd.runtime_descriptor import load_descriptors
from tests.unit.test_activation import seed_registry
from tests.unit.test_lifecycle import FakeEngineHTTP, FakeRunner

REPO_ROOT = Path(__file__).resolve().parents[3]
TOKEN = "cli-connected-token"
DAEMON_URL = "http://daemon.test:8010"
REAL_CLIENT_FACTORY = control_client.client_factory  # captured before any patch


class RecordingClient(TestClient):
    """Stands in for httpx against the daemon: in-process, records every
    request's method, path and headers; a no-op context manager so the
    liveness probe's ``with client_factory(...)`` does not restart the app."""

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


@pytest.fixture
def world(tmp_path: Path, monkeypatch):
    """One bootstrapped platform, a daemon over it (in-process), a CLI config
    pointing at the same platform, the connected transport wired to the
    daemon through the seam, token + URL in the environment."""
    root = tmp_path / "platform"
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

    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    monkeypatch.delenv(TRANSPORT_ENV, raising=False)
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.setenv(URL_ENV, DAEMON_URL)
    runner = FakeRunner()
    monkeypatch.setattr(platform_cli, "default_runner", runner)
    monkeypatch.setattr(platform_cli, "http_probe", lambda url: 200)
    monkeypatch.setattr(platform_cli, "engine_http", lambda: FakeEngineHTTP(
        timings={"predicted_per_second": 15.5, "prompt_per_second": 80.0, "predicted_n": 100}))
    monkeypatch.setattr(platform_cli, "build_validator",
                        lambda ctx: lambda descriptor, name, entry: platform_cli.lifecycle
                        .ProbeResult(True, "", 0.2, name))
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "llm": {"provider": "scripted", "script_path": str(tmp_path / "script.json")},
        "platform": {"config_dir": str(root / "config")},
        "runs_dir": str(tmp_path / "runs")}), encoding="utf-8")
    (tmp_path / "script.json").write_text("[]")

    daemon_ctx = build_context(load_config(cfg_file), root, actor="ezaid")
    app = create_app(ControlConfig(token=TOKEN), daemon_ctx,
                     prober=lambda url, headers: (200, "ok"))
    client = RecordingClient(app)
    factory_calls: list[tuple[str, float | None]] = []

    def factory(url: str, timeout: float | None):
        factory_calls.append((url, timeout))
        return client

    monkeypatch.setattr(control_client, "client_factory", factory)
    return SimpleNamespace(cfg=cfg_file, root=root, app=app, client=client,
                           factory_calls=factory_calls, runner=runner, daemon_ctx=daemon_ctx)


def invoke(world, *argv: str, capsys) -> tuple[int, str]:
    code = main([*argv, "--config", str(world.cfg)])
    return code, capsys.readouterr().out


def invoke_json(world, *argv: str, capsys) -> tuple[int, dict]:
    code, out = invoke(world, *argv, "--json", capsys=capsys)
    return code, json.loads(out)


# ── detection · parity · shared state ────────────────────────────────────────


def test_auto_detects_the_daemon_and_uses_it(world, capsys):
    code, data = invoke_json(world, "status", capsys=capsys)
    assert code == 0
    assert data["transport"] == f"connected {DAEMON_URL}"
    assert data["health"] == {"engine": True, "router": True, "control": True}
    assert data["generation"] == 1 and data["models"]["alpha"]["state"] == "active"
    # one bounded liveness probe, then the long-lived client for the verb
    assert world.factory_calls[0] == (DAEMON_URL, 1.0)
    assert world.factory_calls[1] == (DAEMON_URL, None)
    methods = [(m, p) for m, p, _ in world.client.requests]
    assert methods == [("GET", "/health"), ("GET", "/v1/health")]
    code, out = invoke(world, "status", capsys=capsys)
    assert code == 0 and f"transport:  connected {DAEMON_URL}" in out
    assert "engine up · router up · control up" in out


@pytest.mark.parametrize("argv", [
    ("status",),
    ("model", "explain", "coder"),
    ("model", "history"),
    ("model", "catalog"),
    ("model", "catalog", "--group", "coding"),
    ("governance", "list"),
    ("project", "list"),
])
def test_direct_and_connected_produce_the_same_output(world, argv, capsys):
    """The parity smoke (CLI_AND_WEBUI §7 seed): same verb, both transports,
    same JSON and same text — the transport line of `status` excepted."""
    code_d, json_d = invoke_json(world, *argv, "--transport", "direct", capsys=capsys)
    code_c, json_c = invoke_json(world, *argv, "--transport", "connected", capsys=capsys)
    assert code_d == code_c == 0
    assert json_d.pop("transport", "direct (in-process)") == "direct (in-process)"
    assert json_c.pop("transport", None) in (None, f"connected {DAEMON_URL}")
    assert json_d == json_c
    code_d, text_d = invoke(world, *argv, "--transport", "direct", capsys=capsys)
    code_c, text_c = invoke(world, *argv, "--transport", "connected", capsys=capsys)
    strip = [line for line in text_d.splitlines() if not line.startswith("transport:")]
    assert strip == [line for line in text_c.splitlines() if not line.startswith("transport:")]


def test_connected_mutations_go_through_the_daemon_and_share_the_platform(world, capsys):
    code, out = invoke(world, "model", "activate", "beta", "--group", "coding", "--by", "nita",
                       capsys=capsys)
    assert code == 0
    assert "change request cr-0001: activate beta" in out
    assert "awaiting human approval: local-ezai governance approve cr-0001" in out
    assert load_registry(world.root / "config").generation == 1

    code, data = invoke_json(world, "governance", "show", "cr-0001", capsys=capsys)
    assert data["request"]["requested_by"] == "nita via cli"  # the forwarded identity

    code, out = invoke(world, "governance", "approve", "cr-0001", "--reason", "ok", "--by", "nita",
                       capsys=capsys)
    assert code == 0 and "approved cr-0001 by nita — applying" in out
    assert "applied as generation 2" in out and "rendered only" in out
    after = load_registry(world.root / "config")
    assert after.generation == 2 and after.resolve("coder").primary == "beta"

    # the daemon and direct mode act on ONE platform
    code, data = invoke_json(world, "model", "history", "--transport", "direct", capsys=capsys)
    assert [g["generation"] for g in data["generations"]] == [1, 2]
    events = [(r.event, r.actor) for r in world.daemon_ctx.queue.audit()]
    assert ("api.model_activate", "nita via cli") in events
    assert ("api.governance_approve", "nita via cli") in events
    assert ("request.approved", "nita via cli") in events

    # every call carried the token + identity; mutations carried a fresh key
    posts = [(p, h) for m, p, h in world.client.requests if m == "POST"]
    assert [p for p, _ in posts] == ["/v1/models/beta/activate", "/v1/governance/cr-0001/approve"]
    keys = set()
    for _, headers in posts:
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert headers[USER_HEADER] == "nita" and headers[CLIENT_HEADER] == "cli"
        keys.add(str(uuid.UUID(headers[IDEMPOTENCY_HEADER])))
    assert len(keys) == 2
    gets = [h for m, p, h in world.client.requests if m == "GET" and p.startswith("/v1")]
    assert gets and all(IDEMPOTENCY_HEADER not in h and h[CLIENT_HEADER] == "cli" for h in gets)


def test_connected_errors_are_the_shared_object(world, capsys):
    code_c, json_c = invoke_json(world, "model", "retire", "nope", "--transport", "connected",
                                 capsys=capsys)
    code_d, json_d = invoke_json(world, "model", "retire", "nope", "--transport", "direct",
                                 capsys=capsys)
    assert code_c == code_d == 1
    assert json_c == json_d == {"error": {"code": "not_found", "message": "unknown model 'nope'",
                                          "fix": ""}}
    code, out = invoke(world, "model", "retire", "alpha", capsys=capsys)  # serving primary
    assert code == 1 and out == ""  # text mode: logged to stderr, nothing on stdout
    code, data = invoke_json(world, "governance", "reject", "cr-9", "--reason", "x",
                             "--by", "nita", capsys=capsys)
    assert code == 1 and data["error"]["code"] == "not_found"


# ── fail fast · fallback · precedence · host-only verbs ──────────────────────


def test_requested_connected_fails_fast_when_unreachable(world, monkeypatch, capsys):
    def refused(url: str, timeout: float | None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(control_client, "client_factory", refused)
    code, out = invoke(world, "status", "--transport", "connected", capsys=capsys)
    assert code == 2 and out == ""
    code, data = invoke_json(world, "status", "--transport", "connected", capsys=capsys)
    assert code == 2 and data["error"]["code"] == "platform_unavailable"
    assert "control plane unreachable" in data["error"]["message"]
    assert "--transport direct" in data["error"]["message"]
    # auto: no daemon → direct, silently (offline-first)
    code, data = invoke_json(world, "status", capsys=capsys)
    assert code == 0 and data["transport"] == "direct (in-process)"


def test_reachable_daemon_without_a_token_fails_fast(world, monkeypatch, capsys):
    monkeypatch.delenv(TOKEN_ENV)
    code, data = invoke_json(world, "status", capsys=capsys)
    assert code == 2 and data["error"]["code"] == "platform_unavailable"
    assert TOKEN_ENV in data["error"]["message"]
    code, data = invoke_json(world, "status", "--transport", "direct", capsys=capsys)
    assert code == 0 and data["transport"] == "direct (in-process)"


def test_transport_flag_and_environment_precedence(world, monkeypatch, capsys):
    monkeypatch.setenv(TRANSPORT_ENV, "direct")
    code, data = invoke_json(world, "status", capsys=capsys)
    assert data["transport"] == "direct (in-process)" and world.factory_calls == []
    code, data = invoke_json(world, "status", "--transport", "connected", capsys=capsys)
    assert data["transport"] == f"connected {DAEMON_URL}"  # the flag wins over the env
    monkeypatch.setenv(TRANSPORT_ENV, "bogus")
    code, data = invoke_json(world, "status", capsys=capsys)
    assert code == 2 and "unknown transport" in data["error"]["message"]


def test_host_only_verbs_never_use_the_daemon(world, capsys):
    code, out = invoke(world, "up", "--profile", "n97", "--transport", "connected", capsys=capsys)
    assert code == 0
    assert world.runner.calls[-1][:2] == ["docker", "compose"] and world.factory_calls == []
    (world.root / ".env").write_text("AI_RUNTIME=llamacpp\nREASONING_MODEL=auto\n"
                                     "CODING_MODEL=auto\nCHAT_MODEL=auto\n")
    code, out = invoke(world, "bootstrap", "--dry-run", "--transport", "connected", capsys=capsys)
    assert code == 1 and "already exists" in out and world.factory_calls == []


def test_daemon_rejecting_the_token_is_reported_verbatim(world, monkeypatch, capsys):
    monkeypatch.setenv(TOKEN_ENV, "wrong-token")
    code, data = invoke_json(world, "status", capsys=capsys)
    assert code == 1 and data["error"]["code"] == "unauthorized"
    assert data["error"]["message"] == "invalid service token"
    assert TOKEN_ENV in data["error"]["fix"]
    rejected = [r for r in world.daemon_ctx.queue.audit() if r.event == "auth.rejected"]
    assert rejected and "wrong-token" not in world.daemon_ctx.queue.log_path.read_text()


def test_control_plane_error_objects():
    response = httpx.Response(409, json={"error": {"code": "lifecycle_refused",
                                                   "message": "alpha serves", "fix": "swap"}},
                              request=httpx.Request("POST", "http://d/v1/models/alpha/retire"))
    error = ControlPlaneError.from_response(response)
    assert (error.status, error.code, error.message, error.fix, error.exit_code) == (
        409, "lifecycle_refused", "alpha serves", "swap", 1)
    assert error.as_dict() == {"code": "lifecycle_refused", "message": "alpha serves",
                               "fix": "swap"}
    plain = ControlPlaneError.from_response(httpx.Response(
        502, text="bad gateway", request=httpx.Request("GET", "http://d/v1/health")))
    assert plain.code == "http_502" and plain.message == "bad gateway" and plain.exit_code == 1
    unreachable = ControlUnreachable("http://d:8010", "ConnectError: refused")
    assert unreachable.exit_code == 2 and unreachable.code == "control_unreachable"
    assert "make control-up" in unreachable.fix and "http://d:8010" in unreachable.message


# ── a real socket ────────────────────────────────────────────────────────────


@contextmanager
def serving(app):
    import uvicorn

    from agentd.lifecycle import free_port

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                          log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)


def test_connected_mode_over_a_real_socket(world, monkeypatch, capsys):
    monkeypatch.setattr(control_client, "client_factory", REAL_CLIENT_FACTORY)
    with serving(world.app) as url:
        monkeypatch.setenv(URL_ENV, url)
        code, data = invoke_json(world, "status", capsys=capsys)
        assert code == 0 and data["transport"] == f"connected {url}"
        code_c, connected = invoke_json(world, "model", "explain", "coder", capsys=capsys)
        code_d, direct = invoke_json(world, "model", "explain", "coder", "--transport", "direct",
                                     capsys=capsys)
        assert code_c == code_d == 0 and connected == direct
        code, data = invoke_json(world, "model", "activate", "beta", "--group", "spare",
                                 "--by", "nita", capsys=capsys)
        assert code == 0 and data["applied"]["generation"] == 2
    assert load_registry(world.root / "config").models["beta"].state == "active"
    events = [(r.event, r.actor) for r in world.daemon_ctx.queue.audit()]
    assert ("api.model_activate", "nita via cli") in events
