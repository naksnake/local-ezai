"""ezaid control plane skeleton (PR-8, ADR-028; docs/CLI_AND_WEBUI_STRATEGY.md
§1/§6): service-token auth + forwarded identity, the shared error envelope,
health aggregation, the single audit log through the API, the versioned
OpenAPI contract artifact, the compose overlay.

Offline: FastAPI's in-process test client, fake probers, a bootstrapped
platform under tmp_path. Model names are fixtures."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agentd import __version__
from agentd.activation import Platform
from agentd.capability import CapabilityVector
from agentd.config import ControlConfig, load_config
from agentd.control import (
    API_PREFIX,
    CLIENT_HEADER,
    CONTRACT_VERSION,
    PORT_ENV,
    SPEC_ARTIFACT,
    TOKEN_ENV,
    USER_HEADER,
)
from agentd.control.app import contract_surface, create_app
from agentd.control.auth import AuthError, authenticate
from agentd.control.health import DEFAULT_TARGETS, ServiceTarget, probe_services, targets_from
from agentd.control.main import main as ezaid_main
from agentd.platform_cli import build_context
from agentd.registry_v2 import save_generation
from agentd.render import write_rendered
from agentd.runtime_descriptor import load_descriptors
from tests.unit.test_activation import seed_registry

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROL_SRC = REPO_ROOT / "agentd" / "src" / "agentd" / "control"
TOKEN = "test-service-token-3f9a"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def platform_root(tmp_path: Path) -> Path:
    """A bootstrapped platform: descriptors, generation 1, rendered."""
    root = tmp_path / "platform"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config" / "providers", root / "config" / "providers")
    (root / "docker-compose.yml").write_text("services: {}\n")
    saved = save_generation(seed_registry(), root / "config", note="bootstrap")
    platform = Platform(config_dir=root / "config", platform_root=root,
                        descriptors=load_descriptors(root / "config"),
                        vector=CapabilityVector(system_memory_gb=15.5, cpu_cores=4),
                        capability_class="cpu-low", accelerator="none")
    write_rendered(platform.render(saved), platform.rendered)
    return root


@pytest.fixture
def ctx(platform_root: Path):
    config = load_config(environ={"AGENTD_PLATFORM__CONFIG_DIR": str(platform_root / "config")})
    return build_context(config, platform_root, actor="ezaid")


def up_prober(url: str, headers: dict[str, str]) -> tuple[int, str]:
    return 200, "healthy · healthz check passed · openapi · ok"


def make_client(ctx, prober=up_prober, **kwargs) -> TestClient:
    settings = kwargs.pop("settings", ControlConfig(token=TOKEN))
    app = create_app(settings, ctx, prober=prober, environ={"MCP_API_KEY": "mcp-k"}, **kwargs)
    return TestClient(app)


# ── liveness · auth · identity ───────────────────────────────────────────────


def test_liveness_is_open_and_names_the_contract(ctx):
    with make_client(ctx) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["service"] == "ezaid"
    assert body["version"] == __version__ and body["contract"] == CONTRACT_VERSION
    assert body["uptime_s"] >= 0


def test_v1_requires_the_service_token_and_audits_rejections(ctx):
    with make_client(ctx) as client:
        missing = client.get(f"{API_PREFIX}/whoami")
        wrong = client.get(f"{API_PREFIX}/whoami", headers={"Authorization": "Bearer nope"})
        basic = client.get(f"{API_PREFIX}/whoami", headers={"Authorization": "Basic abc",
                                                            CLIENT_HEADER: "cli"})
    for response in (missing, wrong, basic):
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        error = response.json()["error"]
        assert error["code"] == "unauthorized" and TOKEN_ENV in error["fix"]
    assert missing.json()["error"]["message"] == "missing bearer token"
    assert wrong.json()["error"]["message"] == "invalid service token"
    # audited — without the presented secret
    rejected = [r for r in ctx.queue.audit() if r.event == "auth.rejected"]
    assert len(rejected) == 3 and rejected[0].actor == "anonymous"
    assert rejected[0].details["path"] == f"{API_PREFIX}/whoami"
    assert rejected[1].details["reason"] == "invalid service token"
    assert rejected[2].details["claimed_client"] == "cli"
    log_text = ctx.queue.log_path.read_text()
    assert "nope" not in log_text and TOKEN not in log_text


def test_whoami_reports_the_forwarded_identity(ctx):
    with make_client(ctx) as client:
        plain = client.get(f"{API_PREFIX}/whoami", headers=AUTH).json()
        forwarded = client.get(f"{API_PREFIX}/whoami", headers={
            **AUTH, USER_HEADER: "nita", CLIENT_HEADER: "admin-center"}).json()
        dirty = client.get(f"{API_PREFIX}/whoami", headers={
            **AUTH, USER_HEADER: "ni\tta\x01", CLIENT_HEADER: " cli "}).json()
    assert plain == {"actor": "api", "client": "api", "user": None}
    assert forwarded == {"actor": "nita via admin-center", "client": "admin-center", "user": "nita"}
    assert dirty == {"actor": "nita via cli", "client": "cli", "user": "nita"}


def test_authenticate_fails_closed_without_a_configured_token():
    with pytest.raises(AuthError) as err:
        authenticate("", "Bearer anything")
    assert TOKEN_ENV in err.value.fix and "no service token" in err.value.message
    caller = authenticate("s3", "bearer s3", user=None, client=None)
    assert caller.actor == "api"


# ── health aggregation ───────────────────────────────────────────────────────


def test_aggregated_health_combines_platform_snapshot_and_service_probes(ctx):
    seen: dict[str, dict[str, str]] = {}

    def prober(url: str, headers: dict[str, str]) -> tuple[int, str]:
        seen[url] = headers
        return up_prober(url, headers)

    with make_client(ctx, prober=prober) as client:
        response = client.get(f"{API_PREFIX}/health", headers=AUTH)
    assert response.status_code == 200
    report = response.json()
    assert report["ok"] is True
    assert [s["id"] for s in report["services"]] == [t.id for t in DEFAULT_TARGETS]
    assert all(s["ok"] and s["status"] == 200 for s in report["services"])
    platform = report["platform"]
    assert platform["generation"] == 1 and platform["rendered_generation"] == 1
    assert platform["slot_runtime"] == "llamacpp" and platform["pending_approvals"] == 0
    assert platform["models"]["alpha"]["state"] == "active"
    assert report["control"]["contract"] == CONTRACT_VERSION
    assert report["control"]["platform_root"] == platform["platform_root"]
    # credentials reach only the targets that declare them; the engine slot
    # is probed through its neutral alias
    assert seen["http://mcpo:8200/openapi.json"] == {"Authorization": "Bearer mcp-k"}
    assert seen["http://engine:8000/health"] == {}


def test_aggregated_health_reports_down_services_without_failing(ctx):
    def prober(url: str, headers: dict[str, str]) -> tuple[int, str]:
        if "qdrant" in url:
            raise ConnectionError("connection refused")
        if "litellm" in url:
            return 503, "loading"
        if "embed" in url:
            return 200, "starting"  # pattern 'healthy' absent
        return 200, "ok passed openapi"

    with make_client(ctx, prober=prober) as client:
        report = client.get(f"{API_PREFIX}/health", headers=AUTH).json()
    by_id = {s["id"]: s for s in report["services"]}
    assert report["ok"] is False
    assert by_id["engine"]["ok"] is True
    assert by_id["qdrant"]["ok"] is False and "connection refused" in by_id["qdrant"]["error"]
    assert by_id["router"]["ok"] is False and by_id["router"]["status"] == 503
    assert by_id["embed"]["ok"] is False and by_id["embed"]["status"] == 200


def test_health_targets_are_data(ctx):
    targets = targets_from({"engine": "http://host:1/health", "monitor": "", "extra": "http://x/y"})
    by_id = {t.id: t for t in targets}
    assert by_id["engine"].url == "http://host:1/health" and "monitor" not in by_id
    assert by_id["extra"] == ServiceTarget("extra", "http://x/y")
    assert by_id["mcpo"].auth_env == "MCP_API_KEY"  # untouched defaults keep their credential
    settings = ControlConfig(token=TOKEN, health_targets={"extra": "http://x/y", "searxng": ""})
    with make_client(ctx, settings=settings) as client:
        ids = [s["id"] for s in client.get(f"{API_PREFIX}/health", headers=AUTH).json()["services"]]
    assert "extra" in ids and "searxng" not in ids
    assert probe_services((), up_prober) == []


def test_health_without_a_platform_is_a_503_envelope():
    app = create_app(ControlConfig(token=TOKEN), None, prober=up_prober)
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/health", headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "platform_unavailable"


# ── audit through the API · error envelope ───────────────────────────────────


def test_audit_endpoint_tails_the_single_log(ctx):
    ctx.queue.record("request.submitted", "nita", request_id="cr-0001")
    with make_client(ctx) as client:  # the context manager runs the lifespan
        started = [r for r in ctx.queue.audit() if r.event == "control.started"]
        assert len(started) == 1 and started[0].actor == "ezaid"
        assert started[0].details["contract"] == CONTRACT_VERSION
        page = client.get(f"{API_PREFIX}/audit", headers=AUTH, params={"limit": 1}).json()
        everything = client.get(f"{API_PREFIX}/audit", headers=AUTH).json()
        invalid = client.get(f"{API_PREFIX}/audit", headers=AUTH, params={"limit": 0})
    assert page["total"] == 2 and [r["event"] for r in page["records"]] == ["control.started"]
    assert [r["event"] for r in everything["records"]] == ["request.submitted", "control.started"]
    assert everything["records"][0]["request_id"] == "cr-0001"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"
    assert "limit" in invalid.json()["error"]["message"]
    assert ctx.queue.audit()[-1].event == "control.stopped"


def test_unknown_routes_use_the_error_envelope(ctx):
    with make_client(ctx) as client:
        response = client.get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert "/openapi.json" in response.json()["error"]["fix"]


# ── the contract ─────────────────────────────────────────────────────────────


def test_openapi_document_is_the_contract(ctx):
    with make_client(ctx) as client:
        spec = client.get("/openapi.json").json()  # open: clients type themselves against it
    assert spec["info"]["version"] == CONTRACT_VERSION
    assert spec["components"]["securitySchemes"]["serviceToken"] == {
        "type": "http", "scheme": "bearer",
        "description": f"the platform service token ({TOKEN_ENV})"}
    operation_ids = {op["operationId"] for methods in spec["paths"].values()
                     for op in methods.values()}
    assert operation_ids >= {"liveness", "aggregated_health", "whoami", "audit_tail"}
    assert "security" not in spec["paths"]["/health"]["get"]
    for path, methods in spec["paths"].items():
        if not path.startswith(API_PREFIX):
            continue
        for op in methods.values():
            assert op["security"] == [{"serviceToken": []}], path
            assert "401" in op["responses"], path
            assert {p["name"] for p in op["parameters"]} >= {USER_HEADER, CLIENT_HEADER}, path


def test_committed_spec_artifact_matches_the_app():
    """Tripwire: a contract change must ship its regenerated artifact
    (``make control-spec``). Compared on the contract surface, so framework
    rendering details cannot fail it."""
    artifact = REPO_ROOT / SPEC_ARTIFACT
    committed = json.loads(artifact.read_text(encoding="utf-8"))
    generated = create_app(ControlConfig(token="spec")).openapi()
    assert committed["info"]["version"] == CONTRACT_VERSION
    assert contract_surface(committed) == contract_surface(generated), (
        f"{SPEC_ARTIFACT} is stale — regenerate with `make control-spec` and review the diff")
    surface = contract_surface(generated)
    assert set(surface["operations"]) >= {"GET /health", "GET /v1/audit", "GET /v1/health",
                                          "GET /v1/whoami"}
    assert "ErrorEnvelope" in surface["schemas"] and surface["securitySchemes"] == ["serviceToken"]


def test_ezaid_prints_or_writes_the_spec_without_a_platform_or_token(tmp_path, monkeypatch,
                                                                     capsys):
    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    assert ezaid_main(["--print-spec"]) == 0
    spec = json.loads(capsys.readouterr().out)
    assert spec["info"]["version"] == CONTRACT_VERSION
    target = tmp_path / "out" / "spec.json"
    assert ezaid_main(["--write-spec", str(target)]) == 0
    assert json.loads(target.read_text())["info"]["title"] == spec["info"]["title"]
    assert "wrote" in capsys.readouterr().out
    assert ezaid_main(["--version"]) == 0
    assert CONTRACT_VERSION in capsys.readouterr().out


def test_ezaid_refuses_to_serve_without_a_token(tmp_path, monkeypatch):
    from agentd.control import main as ezaid_module

    monkeypatch.delenv("AGENTD_CONFIG", raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    errors: list[str] = []
    monkeypatch.setattr(ezaid_module.log, "error",
                        lambda msg, *args: errors.append(msg % args if args else msg))
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({"llm": {"provider": "scripted"}}))
    assert ezaid_main(["--config", str(cfg)]) == 2  # fail closed, before touching a platform
    assert len(errors) == 1 and TOKEN_ENV in errors[0] and "does not start" in errors[0]


# ── configuration · deployment ───────────────────────────────────────────────


def test_control_config_is_seeded_by_the_product_env(tmp_path):
    env = load_config(environ={TOKEN_ENV: "abc", PORT_ENV: "9000"}).control
    assert env.token == "abc" and env.port == 9000
    overridden = load_config(environ={PORT_ENV: "9000", "AGENTD_CONTROL__PORT": "7000",
                                      "AGENTD_CONTROL__TOKEN": "t2"}).control
    assert overridden.port == 7000 and overridden.token == "t2"
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({"control": {"token": "from-file", "port": 8011,
                                               "health_targets": {"monitor": ""}}}))
    from_file = load_config(cfg, environ={TOKEN_ENV: "abc", PORT_ENV: "9000"}).control
    assert from_file.token == "from-file" and from_file.port == 8011
    assert from_file.health_targets == {"monitor": ""}
    assert load_config(environ={}).control.token is None  # never defaulted in code


def test_compose_overlay_is_additive_and_the_base_stack_untouched():
    overlay = yaml.safe_load((REPO_ROOT / "docker-compose.control.yml").read_text())
    assert list(overlay["services"]) == ["ezaid"]
    service = overlay["services"]["ezaid"]
    assert service["ports"] == ["${EZAI_CONTROL_PORT:-8010}:8010"]
    assert service["networks"] == ["ai-net"]
    assert "./config:/platform/config" in service["volumes"]
    env = dict(item.split("=", 1) for item in service["environment"])
    assert env["AGENTD_PLATFORM__CONFIG_DIR"] == "/platform/config"
    assert env["EZAI_CONTROL_TOKEN"].startswith("${EZAI_CONTROL_TOKEN")
    assert service["build"] == {"context": ".", "dockerfile": "agentd/Dockerfile"}
    base = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    assert "ezaid" not in base["services"]  # ADR-002: overlay only
    dockerfile = (REPO_ROOT / "agentd" / "Dockerfile").read_text()
    assert "fastapi==" in dockerfile and "uvicorn==" in dockerfile and 'CMD ["ezaid"]' in dockerfile
    assert "EZAI_CONTROL_TOKEN=" in (REPO_ROOT / ".env.example").read_text()


def test_control_plane_code_names_no_engine_runtime_or_vendor():
    """H1 discipline for the new package: service topology is data
    (``health.py`` targets) and nothing names a model family, an engine, an
    image, or a GPU vendor."""
    forbidden = re.compile(r"qwen|llama-?3|hermes|deepseek|mistral|nvidia|cuda|rocm|intel|n97|"
                           r"ghcr\.io|vllm/|--gpu-layers|tensor-parallel", re.IGNORECASE)
    for source in sorted(CONTROL_SRC.glob("*.py")):
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            assert not forbidden.search(line), f"{source.name}:{number}: {line.strip()}"
