"""Model lifecycle: sources, fetchers, catalog + recommender, install /
validate / benchmark, state machine, trend recording (PR-4, ADR-027;
docs/MODEL_LIFECYCLE_MANAGEMENT.md §1–§2, docs/PROVIDER_ABSTRACTION.md
§5–§6, ADR-026 R-5).

Fully offline: HTTP streams, engine HTTP, and the docker compose runner are
fakes; weights are tiny synthetic files. Model names are fixtures."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from agentd.capability import CapabilityVector
from agentd.catalog import (
    Catalog,
    CatalogError,
    load_catalog,
    packaged_catalog,
    parse_catalog,
    provider_for_format,
    recommend,
    recommend_one,
)
from agentd.fetch import (
    FetchError,
    GGUFFetcher,
    HFFetcher,
    SourceError,
    parse_source,
    sha256_of,
)
from agentd.lifecycle import (
    TRANSITIONS,
    LifecycleError,
    ProbeResult,
    SideLoad,
    SideLoadValidator,
    benchmark,
    install,
    interpolate_compose,
    measure_tokens_per_s,
    probe_model,
    record_trend,
    transition,
    weights_dir,
)
from agentd.registry_v2 import ModelEntry, RegistryV2, list_generations, load_registry
from agentd.runtime_descriptor import load_descriptors

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

CPU_LOW = CapabilityVector(system_memory_gb=15.5, cpu_cores=4, cpu_flags=["avx2"])
ACCEL_LARGE = CapabilityVector(accelerator="cuda", accel_memory_gb=24, system_memory_gb=64,
                               cpu_cores=16, cpu_flags=["avx2"])


@pytest.fixture(scope="module")
def descriptors():
    return load_descriptors(CONFIG_DIR)


@pytest.fixture
def platform(tmp_path: Path) -> Path:
    """A fake platform root with its own weights dirs (never the repo's)."""
    root = tmp_path / "platform"
    (root / "config").mkdir(parents=True)
    return root


def env_for(root: Path) -> dict[str, str]:
    return {"N97_GGUF_DIR": str(root / "models" / "gguf"),
            "MODELS_DIR": str(root / "models" / "hf-cache")}


ROLES = {"coder": {"group": "coding", "requires": {"tool_calling": True, "min_context": 8192}},
         "chat": {"group": "chat", "requires": {"min_context": 4096}}}


def base_registry() -> RegistryV2:
    """A bootstrapped platform: one active fixture model serves every group,
    so lifecycle operations can persist generations."""
    return RegistryV2.model_validate({
        "generation": 0, "providers": ["llamacpp", "vllm"],
        "models": {"seed": {"provider": "llamacpp", "state": "active", "context": 8192,
                            "tool_call_format": "hermes",
                            "source": {"gguf": "https://example.invalid/w/seed.gguf"}}},
        "groups": {"reasoning": ["seed"], "coding": ["seed"], "chat": ["seed"]},
        "roles": ROLES,
    })


def unservable_registry() -> RegistryV2:
    """Pre-bootstrap: roles declared, nothing active yet."""
    return RegistryV2.model_validate({
        "generation": 0, "providers": ["llamacpp", "vllm"], "models": {},
        "groups": {"reasoning": [], "coding": [], "chat": []}, "roles": ROLES})


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeStream:
    """A byte stream honoring Range requests over an in-memory payload."""

    def __init__(self, payload: bytes, headers: dict[str, str], log: list[str]) -> None:
        start = 0
        self.status = 200
        if "Range" in headers:
            start = int(headers["Range"].split("=")[1].rstrip("-"))
            self.status = 206 if start < len(payload) else 416
        log.append(f"GET range={headers.get('Range', 'none')} → {self.status}")
        self._body = payload[start:]
        self.headers = {"content-length": str(len(self._body))}
        self.closed = False

    def iter_bytes(self):
        for i in range(0, len(self._body), 7):
            yield self._body[i:i + 7]

    def close(self) -> None:
        self.closed = True


def fake_opener(payload: bytes, log: list[str]):
    def opener(url: str, headers: dict[str, str]) -> FakeStream:
        return FakeStream(payload, headers, log)
    return opener


class FakeEngineHTTP:
    """Answers /health after `ready_after` polls and chat completions with a
    canned body (llama.cpp-style timings when `timings` is given)."""

    def __init__(self, *, ready_after: int = 0, timings: dict | None = None,
                 completion_tokens: int = 60, fail_probe: bool = False) -> None:
        self.ready_after, self.timings = ready_after, timings
        self.completion_tokens, self.fail_probe = completion_tokens, fail_probe
        self.polls = 0
        self.posts: list[dict] = []

    def get(self, url: str):
        self.polls += 1
        return (200, {"status": "ok"}) if self.polls > self.ready_after else (503, None)

    def post(self, url: str, payload: dict, timeout: float):
        self.posts.append(payload)
        if self.fail_probe:
            return 500, {"error": "model failed to load"}
        body = {"choices": [{"message": {"role": "assistant", "content": "ready"}}],
                "usage": {"completion_tokens": self.completion_tokens, "prompt_tokens": 12}}
        if self.timings:
            body["timings"] = self.timings
        return 200, body


class FakeRunner:
    """Records docker compose invocations; `fail` makes `up` exit non-zero."""

    def __init__(self, fail_up: bool = False, no_compose: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail_up, self.no_compose = fail_up, no_compose

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(command)
        if command[:3] == ["docker", "compose", "version"] and self.no_compose:
            return subprocess.CompletedProcess(command, 127, "", "docker: not found")
        if "up" in command and self.fail_up:
            return subprocess.CompletedProcess(command, 1, "", "no such image")
        return subprocess.CompletedProcess(command, 0, "", "")

    def verbs(self) -> list[str]:
        return [next((c for c in ("version", "up", "down") if c in call), "?")
                for call in self.calls]


def ok_validator(descriptor, name, entry) -> ProbeResult:
    return ProbeResult(True, "", 0.4, name)


def failing_validator(descriptor, name, entry) -> ProbeResult:
    return ProbeResult(False, "probe HTTP 500: model failed to load", 0.1, name)


class FakeFetcherFactory:
    """GGUF via the in-memory stream; hf via a runner that fakes the cache."""

    def __init__(self, payload: bytes) -> None:
        self.payload, self.log = payload, []

    def __call__(self, fmt: str, directory: Path):
        if fmt == "gguf":
            return GGUFFetcher(directory, opener=fake_opener(self.payload, self.log))
        return HFFetcher(directory, runner=self.fake_hf_runner(directory), mode="local")

    def fake_hf_runner(self, cache: Path):
        def run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
            self.log.append(" ".join(command))
            repo = command[-1]
            snap = cache / f"models--{repo.replace('/', '--')}" / "snapshots" / "abc"
            snap.mkdir(parents=True, exist_ok=True)
            (snap / "model.safetensors").write_bytes(b"w" * 2048)
            return subprocess.CompletedProcess(command, 0, "", "")
        return run


# ── sources: the resolver matrix ─────────────────────────────────────────────


def test_parse_source_matrix():
    assert parse_source("hf:org/repo") .as_source() == {"hf": "org/repo"}
    gguf = parse_source("gguf:https://example.invalid/m/file.gguf")
    assert gguf.kind == "gguf" and gguf.default_name() == "file"
    hub = parse_source("gguf:hf://org/repo/sub/file-q4.gguf")
    assert hub.ref == "https://huggingface.co/org/repo/resolve/main/sub/file-q4.gguf"
    local = parse_source("gguf:./models/gguf/x.gguf")
    assert local.ref == "./models/gguf/x.gguf" and local.format == "gguf"
    assert parse_source("qwen2.5-1.5b-instruct").kind == "catalog"
    assert parse_source("auto").kind == "auto"
    assert parse_source("hf:Org/Repo-Name").default_name() == "repo-name"


@pytest.mark.parametrize("bad, fragment", [
    ("", "empty model reference"),
    ("hf:norepo", "'<org>/<repo>'"),
    ("gguf:", "needs a url"),
    ("gguf:hf://org/repo", "'<org>/<repo>/<file.gguf>'"),
    ("gguf:https://x.invalid/weights.bin", "not a .gguf artifact"),
    ("https://x.invalid/file.gguf", "prefix with gguf:"),
    ("s3:bucket/key", "unknown source scheme 's3:'"),
    ("not a valid id!", "not a valid catalog id"),
])
def test_parse_source_rejects_with_accepted_forms(bad, fragment):
    with pytest.raises(SourceError) as err:
        parse_source(bad)
    assert fragment in str(err.value)
    assert "accepted:" in str(err.value)  # F8: the fix is printed with the error


# ── fetchers ─────────────────────────────────────────────────────────────────


def test_gguf_download_verifies_and_resumes(tmp_path):
    payload = bytes(range(256)) * 40
    digest = hashlib.sha256(payload).hexdigest()
    log: list[str] = []
    fetcher = GGUFFetcher(tmp_path / "gguf", opener=fake_opener(payload, log))
    url = "https://example.invalid/m/model-q4_k_m.gguf"

    fetched = fetcher.fetch(url, expected_sha256=digest)
    assert Path(fetched.artifact).name == "model-q4_k_m.gguf"
    assert fetched.size_bytes == len(payload) and fetched.sha256 == digest
    assert not fetched.reused and log == ["GET range=none → 200"]

    # present + verified → reused, no request
    again = fetcher.fetch(url, expected_sha256=digest)
    assert again.reused and len(log) == 1

    # interrupted download: a .part exists → Range request → identical hash
    Path(fetched.artifact).unlink()
    part = tmp_path / "gguf" / "model-q4_k_m.gguf.part"
    part.write_bytes(payload[:3000])
    resumed = fetcher.fetch(url, expected_sha256=digest)
    assert log[-1] == "GET range=bytes=3000- → 206"
    assert resumed.sha256 == digest and not part.exists()


def test_gguf_checksum_mismatch_discards_download(tmp_path):
    fetcher = GGUFFetcher(tmp_path / "gguf", opener=fake_opener(b"garbage" * 100, []))
    with pytest.raises(FetchError, match="does not match the declared"):
        fetcher.fetch("https://example.invalid/m/bad.gguf", expected_sha256="0" * 64)
    assert not list((tmp_path / "gguf").iterdir())


def test_gguf_local_file_is_imported_into_weights_dir(tmp_path):
    source = tmp_path / "downloads" / "local.gguf"
    source.parent.mkdir()
    source.write_bytes(b"local weights")
    fetched = GGUFFetcher(tmp_path / "gguf").fetch(str(source))
    assert Path(fetched.artifact) == tmp_path / "gguf" / "local.gguf"
    assert fetched.sha256 == sha256_of(source)
    with pytest.raises(FetchError, match="no such file"):
        GGUFFetcher(tmp_path / "gguf").fetch(str(tmp_path / "missing.gguf"))


def test_hf_fetcher_delegates_to_hub_client_and_measures_snapshot(tmp_path):
    calls: list[tuple[list[str], dict[str, str]]] = []

    def runner(command, env):
        calls.append((command, env))
        snap = tmp_path / "hf" / "models--org--repo" / "snapshots" / "rev"
        snap.mkdir(parents=True)
        (snap / "a.safetensors").write_bytes(b"x" * 1000)
        return subprocess.CompletedProcess(command, 0, "", "")

    fetcher = HFFetcher(tmp_path / "hf", runner=runner, mode="local")
    fetched = fetcher.fetch("org/repo")
    assert calls[0][0] == ["hf", "download", "org/repo"]
    assert calls[0][1]["HF_HUB_CACHE"] == str(tmp_path / "hf")
    assert fetched.size_bytes == 1000 and fetched.artifact.endswith("models--org--repo")
    assert fetcher.fetch("org/repo").reused and len(calls) == 1  # idempotent
    # container mode mirrors scripts/download-models.sh
    command, _ = HFFetcher(tmp_path / "hf", runner=runner, mode="container").command("org/repo")
    assert command[:3] == ["docker", "run", "--rm"] and "hf download org/repo" in command[-1]


def test_hf_fetcher_reports_failed_download(tmp_path):
    def runner(command, env):
        return subprocess.CompletedProcess(command, 1, "", "401 Client Error: gated repo")
    with pytest.raises(FetchError, match="gated repo"):
        HFFetcher(tmp_path / "hf", runner=runner, mode="local").fetch("org/gated")


# ── host paths from descriptor data ──────────────────────────────────────────


def test_interpolate_compose_and_weights_dir(descriptors, tmp_path):
    assert interpolate_compose("${A:-x}/${B}", {"B": "b"}) == "x/b"
    assert interpolate_compose("${A:-x}", {"A": ""}) == "x"
    assert interpolate_compose("${A-x}", {"A": ""}) == ""
    assert interpolate_compose("$A/y", {"A": "a"}) == "a/y"
    # descriptor default anchors at the platform root; env overrides win
    default = weights_dir(descriptors["llamacpp"], tmp_path, env={})
    assert default == (tmp_path / "models" / "gguf").resolve()
    override = weights_dir(descriptors["vllm"], tmp_path, env={"MODELS_DIR": "/data/hf"})
    assert override == Path("/data/hf")


# ── state machine ────────────────────────────────────────────────────────────


def test_transitions_follow_the_lifecycle_diagram():
    entry = ModelEntry(provider="anyruntime")
    transition(entry, "installed")
    transition(entry, "benchmarked")
    transition(entry, "installed", reason="re-install")  # weights replaced
    transition(entry, "failed")
    transition(entry, "installed")  # retry after a fix
    with pytest.raises(LifecycleError) as err:
        transition(entry, "retired")  # only from active
    assert "installed → retired is not allowed" in str(err.value)
    assert "you can reach: benchmarked, failed, installed" in str(err.value)
    with pytest.raises(LifecycleError):
        transition(ModelEntry(provider="anyruntime"), "active")  # registered → active
    assert TRANSITIONS["benchmarked"] >= {"active"}  # the PR-5 approval path exists as data


# ── catalog + recommender (R-5) ──────────────────────────────────────────────


def test_packaged_catalog_declares_requirements_not_brands():
    catalog = packaged_catalog()
    assert catalog.entries
    text = (REPO_ROOT / "agentd/src/agentd/defaults/catalog.yaml").read_text().lower()
    for brand in ("nvidia", "rtx", "geforce", "radeon", "cuda"):
        assert brand not in text
    for entry in catalog.entries.values():
        assert entry.groups and entry.context > 0 and entry.license
        for fmt, variant in entry.variants.items():
            assert fmt in ("hf", "gguf") and variant.size_gb > 0


def test_operator_catalog_sources_override_packaged_entries(tmp_path):
    (tmp_path / "catalog").mkdir()
    (tmp_path / "catalog" / "site.yaml").write_text(yaml.safe_dump({"entries": {
        "qwen2.5-1.5b-instruct": {"groups": ["chat"], "context": 4096, "license": "x",
                                  "variants": {"gguf": {"ref": "gguf:/local.gguf",
                                                        "size_gb": 1}}},
        "site-model": {"groups": ["coding"], "context": 8192, "license": "x",
                       "tool_call_format": "hermes",
                       "variants": {"hf": {"ref": "site/model", "size_gb": 3}}},
    }}))
    catalog = load_catalog(tmp_path)
    assert catalog.entries["qwen2.5-1.5b-instruct"].context == 4096  # overridden
    assert "site-model" in catalog.entries
    assert len(catalog.entries) == len(packaged_catalog().entries) + 1
    with pytest.raises(CatalogError, match="at least one variant"):
        parse_catalog(yaml.safe_dump({"entries": {"x": {"variants": {}}}}), "fixture")
    assert isinstance(Catalog.model_validate({"entries": {}}), Catalog)


def test_provider_for_format_follows_descriptors(descriptors):
    assert provider_for_format("gguf", descriptors) == "llamacpp"
    assert provider_for_format("hf", descriptors) == "vllm"
    assert provider_for_format("gguf", descriptors, runtime="vllm") is None
    assert provider_for_format("hf", descriptors, runtime="vllm") == "vllm"


def test_recommender_fits_class_and_contracts(descriptors):
    catalog = packaged_catalog()
    coder = base_registry().roles["coder"].requires
    # cpu-low + llama.cpp: a small GGUF wins; the 7B coder GGUF would spill
    low = recommend(catalog, "coding", CPU_LOW, descriptors, runtime="llamacpp",
                    contracts=[coder])
    assert low[0].eligible and low[0].format == "gguf"
    assert all(r.verdict.fits for r in low if r.eligible)
    # accel-large + vLLM: the largest fitting hf variant with tool calling wins
    big = recommend(catalog, "coding", ACCEL_LARGE, descriptors, runtime="vllm",
                    contracts=[coder])
    assert big[0].eligible and big[0].format == "hf" and big[0].provider == "vllm"
    assert big[0].verdict.placement == "accelerator"
    assert big[0].model.size_gb >= max(r.model.size_gb for r in big if r.eligible)
    # a reasoning entry whose tool format only a template-driven runtime handles
    # is rejected for vLLM by the contract, with the parser named
    rejected = [r for r in big if not r.eligible and r.contract_failures]
    assert any("missing capability tool_calling" in f for r in rejected
               for f in r.contract_failures)
    assert "eligible" in big[0].explain()


def test_recommend_one_explains_when_nothing_fits(descriptors):
    tiny = CapabilityVector(system_memory_gb=1.0, cpu_cores=2)
    with pytest.raises(CatalogError) as err:
        recommend_one(packaged_catalog(), "chat", tiny, descriptors, runtime="llamacpp")
    message = str(err.value)
    assert "no catalog model fits group 'chat'" in message
    assert "rejected" in message and "config/catalog/" in message


# ── install ──────────────────────────────────────────────────────────────────


def test_install_gguf_url_registers_fetches_validates_and_persists(descriptors, platform):
    payload = b"\x00gguf" * 500
    factory = FakeFetcherFactory(payload)
    result = install(base_registry(), "gguf:https://example.invalid/w/alpha-q4_k_m.gguf",
                     descriptors=descriptors, vector=CPU_LOW, platform_root=platform,
                     validator=ok_validator, fetcher_factory=factory,
                     persist_dir=platform / "config", env=env_for(platform))
    assert result.ok and result.state == "installed"
    entry = result.registry.models["alpha-q4_k_m"]
    assert entry.provider == "llamacpp" and entry.state == "installed"
    assert entry.artifact == str(platform / "models" / "gguf" / "alpha-q4_k_m.gguf")
    assert entry.size_gb == round(len(payload) / 1024**3, 2)
    assert entry.source["sha256"] == hashlib.sha256(payload).hexdigest()
    assert entry.installed_at and entry.error == ""
    # persisted as generation 1 with the audit note
    assert result.persisted and list_generations(platform / "config") == [1]
    assert load_registry(platform / "config").note.startswith("install alpha-q4_k_m: installed")
    assert "downloaded" in result.message
    # routing is untouched by an install: groups keep their order, the seed serves
    assert result.registry.groups["coding"] == ["seed"]
    assert result.registry.resolve("coder").primary == "seed"


def test_install_before_any_activation_completes_in_memory(descriptors, platform):
    """Pre-bootstrap (no active set): PR-1 refuses to write an unservable
    generation, so install completes and says so instead of crashing."""
    result = install(unservable_registry(), "gguf:https://example.invalid/w/first.gguf",
                     descriptors=descriptors, vector=CPU_LOW, platform_root=platform,
                     validator=ok_validator, fetcher_factory=FakeFetcherFactory(b"w" * 32),
                     persist_dir=platform / "config", env=env_for(platform))
    assert result.ok and not result.persisted
    assert "registry not persisted" in result.message
    assert list_generations(platform / "config") == []
    assert result.registry.models["first"].state == "installed"


def test_install_is_idempotent_and_reuses_verified_weights(descriptors, platform):
    factory = FakeFetcherFactory(b"weights" * 100)
    first = install(base_registry(), "gguf:https://example.invalid/w/beta.gguf",
                    descriptors=descriptors, vector=CPU_LOW, platform_root=platform,
                    validator=ok_validator, fetcher_factory=factory, env=env_for(platform))
    second = install(first.registry, "beta", descriptors=descriptors, vector=CPU_LOW,
                     platform_root=platform, validator=ok_validator,
                     fetcher_factory=factory, env=env_for(platform))
    assert second.ok and second.fetched.reused
    assert factory.log.count("GET range=none → 200") == 1
    assert "weights reused" in second.message


def test_install_hf_reference_uses_hub_fetcher_and_selected_runtime(descriptors, platform):
    factory = FakeFetcherFactory(b"")
    result = install(base_registry(), "hf:Example/Coder-7B", descriptors=descriptors,
                     vector=ACCEL_LARGE, platform_root=platform, validator=ok_validator,
                     fetcher_factory=factory, env=env_for(platform), name="coder-7b")
    entry = result.registry.models["coder-7b"]
    assert result.ok and entry.provider == "vllm"
    assert entry.artifact.endswith("models--Example--Coder-7B")
    assert entry.size_gb == round(2048 / 1024**3, 2)
    assert factory.log == ["hf download Example/Coder-7B"]
    # a GGUF cannot be installed for a runtime that does not serve it
    with pytest.raises(LifecycleError, match="serves the gguf format"):
        install(base_registry(), "gguf:https://x.invalid/a.gguf", descriptors=descriptors,
                vector=ACCEL_LARGE, platform_root=platform, validator=ok_validator,
                fetcher_factory=factory, runtime="vllm", env=env_for(platform))


def test_install_failures_become_state_failed_with_reason(descriptors, platform):
    # validation failure
    factory = FakeFetcherFactory(b"w" * 10)
    failed = install(base_registry(), "gguf:https://example.invalid/w/gamma.gguf",
                     descriptors=descriptors, vector=CPU_LOW, platform_root=platform,
                     validator=failing_validator, fetcher_factory=factory,
                     persist_dir=platform / "config", env=env_for(platform))
    entry = failed.registry.models["gamma"]
    assert failed.state == "failed" and not failed.ok
    assert entry.error.startswith("validate_model: probe HTTP 500")
    assert entry.artifact  # weights kept for the retry
    # retry after the fix: failed → installed
    retried = install(failed.registry, "gamma", descriptors=descriptors, vector=CPU_LOW,
                      platform_root=platform, validator=ok_validator,
                      fetcher_factory=factory, persist_dir=platform / "config",
                      env=env_for(platform))
    assert retried.ok and retried.registry.models["gamma"].error == ""
    assert list_generations(platform / "config") == [1, 2]
    # fetch failure
    def broken_factory(fmt, directory):
        return GGUFFetcher(directory, opener=fake_opener(b"x", []))
    bad = install(base_registry(), "gguf:https://example.invalid/w/delta.gguf",
                  descriptors=descriptors, vector=CPU_LOW, platform_root=platform,
                  validator=ok_validator, fetcher_factory=broken_factory,
                  env=env_for(platform), name="delta")
    bad.registry.models["delta"].source["sha256"] = "f" * 64
    again = install(bad.registry, "delta", descriptors=descriptors, vector=CPU_LOW,
                    platform_root=platform, validator=ok_validator,
                    fetcher_factory=broken_factory, env=env_for(platform), refetch=True)
    assert again.state == "failed" and again.registry.models["delta"].error.startswith("fetch:")


def test_install_from_catalog_and_auto(descriptors, platform):
    catalog = packaged_catalog()
    factory = FakeFetcherFactory(b"g" * 64)
    by_id = install(base_registry(), "qwen2.5-coder-1.5b-instruct", descriptors=descriptors,
                    vector=CPU_LOW, platform_root=platform, validator=ok_validator,
                    fetcher_factory=factory, catalog=catalog, env=env_for(platform))
    entry = by_id.registry.models["qwen2.5-coder-1.5b-instruct"]
    assert by_id.ok and entry.provider == "llamacpp" and entry.license == "apache-2.0"
    assert entry.groups == ["coding"] and entry.tool_call_format == "hermes"

    auto = install(base_registry(), "auto", group="coding", descriptors=descriptors,
                   vector=ACCEL_LARGE, platform_root=platform, validator=ok_validator,
                   fetcher_factory=factory, catalog=catalog, runtime="vllm",
                   env=env_for(platform))
    picked = auto.registry.models[auto.name]
    assert auto.ok and picked.provider == "vllm" and "coding" in picked.groups

    with pytest.raises(LifecycleError, match="needs a group"):
        install(base_registry(), "auto", descriptors=descriptors, vector=CPU_LOW,
                platform_root=platform, validator=ok_validator, fetcher_factory=factory,
                catalog=catalog, env=env_for(platform))
    with pytest.raises(LifecycleError, match="neither a registry model nor a catalog id"):
        install(base_registry(), "nonexistent-model", descriptors=descriptors, vector=CPU_LOW,
                platform_root=platform, validator=ok_validator, fetcher_factory=factory,
                catalog=catalog, env=env_for(platform))
    with pytest.raises(LifecycleError, match="already exists"):
        install(by_id.registry, "gguf:https://x.invalid/other.gguf", descriptors=descriptors,
                vector=CPU_LOW, platform_root=platform, validator=ok_validator,
                fetcher_factory=factory, env=env_for(platform),
                name="qwen2.5-coder-1.5b-instruct")


# ── side-load: validate_model + bench through the descriptor ─────────────────


def installed_entry(platform: Path) -> ModelEntry:
    return ModelEntry(provider="llamacpp", state="installed", context=8192,
                      tool_call_format="hermes",
                      source={"gguf": "https://example.invalid/w/alpha.gguf"},
                      artifact=str(platform / "models/gguf/alpha.gguf"))


def test_sideload_writes_standalone_compose_and_tears_down(descriptors, platform):
    runner, http = FakeRunner(), FakeEngineHTTP(ready_after=2)
    entry = installed_entry(platform)
    with SideLoad(descriptors["llamacpp"], CPU_LOW, "cpu-low", "none", "alpha", entry,
                  platform_root=platform, workdir=platform / "tmp", runner=runner,
                  http=http, port=18000, env=env_for(platform), sleep=lambda s: None) as engine:
        waited = engine.wait_ready()
        assert waited >= 0 and http.polls == 3
        doc = yaml.safe_load(engine.compose_file.read_text())
        service = doc["services"]["engine"]
        assert service["image"] == descriptors["llamacpp"].accelerators["none"].image
        assert service["ports"] == ["18000:8000"]
        assert service["command"][:2] == ["-m", "/models/alpha.gguf"]
        # host paths resolved from the descriptor's compose interpolation
        assert service["volumes"] == [f"{platform}/models/gguf:/models:ro"]
        assert service["deploy"] == {"resources": {"limits": {"memory": "6g"}}}
        assert "container_name" not in service
        assert engine.base_url == "http://127.0.0.1:18000"
    assert runner.verbs() == ["version", "up", "down"]
    assert "-p" in runner.calls[1] and runner.calls[1][runner.calls[1].index("-p") + 1] \
        == "ezai-sideload-alpha"


def test_sideload_declares_named_volumes_for_hub_runtimes(descriptors, platform):
    entry = ModelEntry(provider="vllm", state="installed", source={"hf": "org/model"},
                       tool_call_format="hermes")
    engine = SideLoad(descriptors["vllm"], ACCEL_LARGE, "accel-large", "cuda", "m", entry,
                      platform_root=platform, workdir=platform / "tmp", runner=FakeRunner(),
                      http=FakeEngineHTTP(), port=18001, env=env_for(platform))
    doc, preset = engine.compose_document()
    assert preset is None
    assert doc["volumes"] == {"vllm-cache": {}}  # named volume declared for a standalone project
    assert doc["services"]["engine"]["deploy"]["resources"]["reservations"]


def test_sideload_failures_are_loud_and_always_tear_down(descriptors, platform):
    entry = installed_entry(platform)
    with pytest.raises(LifecycleError, match="docker compose is not available"):
        with SideLoad(descriptors["llamacpp"], CPU_LOW, "cpu-low", "none", "alpha", entry,
                      platform_root=platform, workdir=platform / "tmp",
                      runner=FakeRunner(no_compose=True), http=FakeEngineHTTP(), port=1):
            pass
    runner = FakeRunner(fail_up=True)
    with pytest.raises(LifecycleError, match="failed to start"):
        with SideLoad(descriptors["llamacpp"], CPU_LOW, "cpu-low", "none", "alpha", entry,
                      platform_root=platform, workdir=platform / "tmp", runner=runner,
                      http=FakeEngineHTTP(), port=1, env=env_for(platform)):
            pass
    assert runner.verbs()[-1] == "down"
    # readiness timeout uses the descriptor's budget
    clock = iter([0.0, 0.0, 700.0, 700.0])
    runner = FakeRunner()
    with pytest.raises(LifecycleError, match="did not become ready within 600s"):
        with SideLoad(descriptors["llamacpp"], CPU_LOW, "cpu-low", "none", "alpha", entry,
                      platform_root=platform, workdir=platform / "tmp", runner=runner,
                      http=FakeEngineHTTP(ready_after=99), port=1, env=env_for(platform),
                      sleep=lambda s: None, clock=lambda: next(clock)) as engine:
            engine.wait_ready()
    assert runner.verbs()[-1] == "down"


def test_probe_and_sideload_validator(descriptors, platform):
    http = FakeEngineHTTP()
    ok = probe_model(http, "http://127.0.0.1:1", "alpha", 1)
    assert ok.ok and http.posts[0]["max_tokens"] == 1 and http.posts[0]["model"] == "alpha"
    bad = probe_model(FakeEngineHTTP(fail_probe=True), "http://127.0.0.1:1", "alpha", 1)
    assert not bad.ok and "probe HTTP 500" in bad.error

    validator = SideLoadValidator(CPU_LOW, "cpu-low", "none", platform_root=platform,
                                  workdir=platform / "tmp", runner=FakeRunner(),
                                  http=FakeEngineHTTP(), env=env_for(platform),
                                  sleep=lambda s: None)
    result = validator(descriptors["llamacpp"], "alpha", installed_entry(platform))
    assert result.ok and result.served_id == "alpha"
    unavailable = SideLoadValidator(CPU_LOW, "cpu-low", "none", platform_root=platform,
                                    workdir=platform / "tmp", runner=FakeRunner(no_compose=True),
                                    http=FakeEngineHTTP(), env=env_for(platform))
    assert not unavailable(descriptors["llamacpp"], "alpha", installed_entry(platform)).ok


# ── benchmark ────────────────────────────────────────────────────────────────


def test_measure_uses_descriptor_timings_or_wall_clock(descriptors):
    timings = {"predicted_per_second": 41.7, "prompt_per_second": 512.3, "predicted_n": 118}
    server_side = measure_tokens_per_s(
        FakeEngineHTTP(timings=timings), "http://e", "alpha", descriptors["llamacpp"],
        model="alpha", via="live")
    assert server_side.tokens_per_s == 41.7 and server_side.prompt_tokens_per_s == 512.3
    assert server_side.completion_tokens == 118
    ticks = iter([10.0, 12.0])
    client_side = measure_tokens_per_s(
        FakeEngineHTTP(completion_tokens=60), "http://e", "alpha", descriptors["vllm"],
        model="alpha", via="live", clock=lambda: next(ticks))
    assert client_side.tokens_per_s == 30.0 and client_side.prompt_tokens_per_s is None
    assert client_side.latency_s == 2.0
    with pytest.raises(LifecycleError, match="neither server timings nor usage"):
        measure_tokens_per_s(FakeEngineHTTP(completion_tokens=0), "http://e", "alpha",
                             descriptors["vllm"], model="alpha", via="live")


def test_benchmark_records_entry_trend_and_state(descriptors, platform):
    registry = base_registry()
    registry.models["alpha"] = installed_entry(platform)
    http = FakeEngineHTTP(timings={"predicted_per_second": 12.5, "prompt_per_second": 90.0,
                                   "predicted_n": 120})
    runner = FakeRunner()
    updated, result = benchmark(registry, "alpha", descriptors=descriptors, vector=CPU_LOW,
                                platform_root=platform, workdir=platform / "tmp",
                                runner=runner, http=http, agent_dir=platform / ".agent",
                                persist_dir=platform / "config", env=env_for(platform),
                                sleep=lambda s: None)
    assert result.via == "side-load" and result.tokens_per_s == 12.5
    assert runner.verbs() == ["version", "up", "down"]
    entry = updated.models["alpha"]
    assert entry.state == "benchmarked"
    assert entry.benchmarks["tokens_per_s"] == 12.5
    assert entry.benchmarks["capability_class"] == "cpu-low" and entry.benchmarks["last"]
    trend = json.loads((platform / ".agent" / "model_benchmarks.json").read_text())
    assert trend["models"]["alpha"][0]["tokens_per_s"] == 12.5
    assert load_registry(platform / "config").note == "benchmark alpha: 12.5 tok/s"
    # live mode skips the side-load; an active model keeps its state
    updated.models["alpha"].state = "active"
    runner = FakeRunner()
    live, _ = benchmark(updated, "alpha", descriptors=descriptors, vector=CPU_LOW,
                        platform_root=platform, workdir=platform / "tmp", runner=runner,
                        http=http, base_url="http://engine:8000/", env=env_for(platform))
    assert runner.calls == [] and live.models["alpha"].state == "active"
    with pytest.raises(LifecycleError, match="install it first"):
        benchmark(base_registry_with("registered", platform), "alpha", descriptors=descriptors,
                  vector=CPU_LOW, platform_root=platform, workdir=platform / "tmp",
                  runner=FakeRunner(), http=http, env=env_for(platform))


def base_registry_with(state: str, platform: Path) -> RegistryV2:
    registry = base_registry()
    entry = installed_entry(platform)
    entry.state = state  # type: ignore[assignment]
    registry.models["alpha"] = entry
    return registry


def test_trend_is_capped_and_coexists_with_evaluate_models(tmp_path, config, tmp_repo):
    from agentd.evaluate import evaluate_models
    from agentd.llm import ScriptedLLM
    from tests.unit.test_transparency import _probe_responses

    agent_dir = tmp_repo / ".agent"
    for i in range(25):
        record_trend(agent_dir, "alpha", {"tokens_per_s": float(i)})
    data = json.loads((agent_dir / "model_benchmarks.json").read_text())
    assert len(data["models"]["alpha"]) == 20
    assert data["models"]["alpha"][-1]["tokens_per_s"] == 24.0
    # evaluate-models owns the rest of the file and carries the models section forward
    report = evaluate_models(config, tmp_repo, llm=ScriptedLLM(_probe_responses()))
    assert report.history == []  # a lifecycle-only file is not a previous evaluation
    assert len(report.models["alpha"]) == 20
    after = json.loads((agent_dir / "model_benchmarks.json").read_text())
    assert after["results"] and len(after["models"]["alpha"]) == 20
    record_trend(agent_dir, "beta", {"tokens_per_s": 1.0})
    again = json.loads((agent_dir / "model_benchmarks.json").read_text())
    assert again["results"] and set(again["models"]) == {"alpha", "beta"}


# ── platform tripwires ───────────────────────────────────────────────────────


def test_lifecycle_code_carries_no_runtime_or_vendor_knowledge():
    """H1 discipline extended to PR-4: engine flags, timing field names,
    images and brands live in descriptors / catalog data only."""
    src = Path(__file__).resolve().parents[2] / "src" / "agentd"
    for module in ("lifecycle.py", "fetch.py", "catalog.py"):
        text = (src / module).read_text(encoding="utf-8").lower()
        for forbidden in ("nvidia", "ghcr.io", "vllm/", "llama.cpp", "/dev/dri",
                          "predicted_per_second", "--served-model-name", "qwen", "hermes"):
            assert forbidden not in text, f"{module} mentions {forbidden!r}"
