"""The offline bundle's edges (PR-23): refusals with their fixes, the weights
directory resolution, idempotent consumption, the CLI verbs. The happy path
end to end is acceptance criterion F6 (tests/acceptance)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentd import bundle as bundle_module
from agentd import platform_cli
from agentd.bundle import (
    BUNDLE_VERSION,
    IMAGES_TAR,
    MANIFEST,
    BundleError,
    Manifest,
    consume_bundle,
    create_bundle,
    gguf_weights_dir,
)
from agentd.lifecycle import OFFLINE_KEY
from agentd.main_cli import main
from agentd.runtime_descriptor import load_descriptors
from tests.unit import test_setup_pipeline as pipeline_tests
from tests.unit.test_setup_pipeline import FakeDocker

#: The PR-22 harness fixture.
platform = pipeline_tests.platform

REPO_ROOT = Path(__file__).resolve().parents[3]
DESCRIPTORS = load_descriptors(REPO_ROOT / "config")


def manifest_for(root: Path, **overrides) -> Manifest:
    data = dict(version=BUNDLE_VERSION, created_at="now", capability_class="cpu-low",
                accelerator="none", runtime="llamacpp", generation=1,
                seeds={"REASONING_MODEL": "gguf:{weights}/a.gguf",
                       "CODING_MODEL": "gguf:{weights}/a.gguf",
                       "CHAT_MODEL": "gguf:{weights}/a.gguf"},
                images=["img/a:1"], gguf=[], hf_cache=[])
    data.update(overrides)
    return Manifest(**data)


def write_bundle(directory: Path, manifest: Manifest, payload: bytes = b"g" * 64) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "weights" / "gguf").mkdir(parents=True, exist_ok=True)
    for item in manifest.gguf:
        (directory / "weights" / "gguf" / item["file"]).write_bytes(payload)
    (directory / IMAGES_TAR).write_bytes(b"images")
    (directory / MANIFEST).write_text(manifest.dump(), encoding="utf-8")
    return directory


def test_create_requires_a_bootstrapped_platform(platform, tmp_path):
    with pytest.raises(BundleError, match="not bootstrapped"):
        create_bundle(platform.ctx, tmp_path / "b", profile="n97", runner=FakeDocker(),
                      environ={})
    assert not (tmp_path / "b" / MANIFEST).exists()


def test_manifest_refusals_name_the_fix(tmp_path):
    with pytest.raises(BundleError, match="not a bundle manifest"):
        Manifest.load(tmp_path / "missing.json")
    (tmp_path / "old.json").write_text(json.dumps({"version": 0}), encoding="utf-8")
    with pytest.raises(BundleError, match="bundle version 0 is not"):
        Manifest.load(tmp_path / "old.json")
    (tmp_path / "bad.json").write_text(json.dumps({"version": BUNDLE_VERSION, "x": 1}),
                                       encoding="utf-8")
    with pytest.raises(BundleError, match="malformed manifest"):
        Manifest.load(tmp_path / "bad.json")


def test_consume_verifies_checksums_needs_env_and_loads_images(tmp_path):
    import hashlib

    root = tmp_path / "root"
    root.mkdir()
    payload = b"g" * 64
    manifest = manifest_for(root, gguf=[{"file": "a.gguf", "model": "a",
                                         "sha256": hashlib.sha256(payload).hexdigest(),
                                         "size": 64}])
    bundle = write_bundle(tmp_path / "bundle", manifest, payload)
    docker = FakeDocker()
    with pytest.raises(BundleError, match="install.sh writes it"):
        consume_bundle(bundle, root, env_path=root / ".env", descriptors=DESCRIPTORS,
                       runner=docker, environ={})
    (root / ".env").write_text("LITELLM_MASTER_KEY=sk\n", encoding="utf-8")
    report = consume_bundle(bundle, root, env_path=root / ".env", descriptors=DESCRIPTORS,
                            runner=docker, environ={}, now="2026-09-09T12:00:00")
    assert report.images_loaded == 1 and report.gguf_placed == 1 and report.gguf_skipped == 0
    assert ["docker", "load", "-i", str(bundle / IMAGES_TAR)] in docker.calls
    weights = gguf_weights_dir(root, DESCRIPTORS, "llamacpp", {})
    assert weights == root / "models" / "gguf" and (weights / "a.gguf").read_bytes() == payload
    text = (root / ".env").read_text(encoding="utf-8")
    assert f"{OFFLINE_KEY}=1" in text and f"REASONING_MODEL=gguf:{weights}/a.gguf" in text
    assert text.startswith("LITELLM_MASTER_KEY=sk\n")   # the user's line is untouched
    # idempotent: a second consume skips the placed file
    again = consume_bundle(bundle, root, env_path=root / ".env", descriptors=DESCRIPTORS,
                           runner=docker, environ={})
    assert again.gguf_skipped == 1 and again.gguf_placed == 0
    # a corrupt bundle is refused before anything is placed
    (bundle / "weights" / "gguf" / "a.gguf").write_bytes(b"x" * 64)
    with pytest.raises(BundleError, match="checksum mismatch"):
        consume_bundle(bundle, tmp_path / "other", env_path=root / ".env",
                       descriptors=DESCRIPTORS, runner=docker, environ={})
    # docker load failing is the air-gapped host's problem to fix, named
    failing = FakeDocker(fail={"docker load": "Cannot connect to the Docker daemon"})
    (bundle / "weights" / "gguf" / "a.gguf").write_bytes(payload)
    with pytest.raises(BundleError, match="docker load failed"):
        consume_bundle(bundle, root, env_path=root / ".env", descriptors=DESCRIPTORS,
                       runner=failing, environ={})


def test_gguf_weights_dir_follows_the_descriptors_and_the_environment(tmp_path):
    assert gguf_weights_dir(tmp_path, DESCRIPTORS, "llamacpp", {}) == tmp_path / "models" / "gguf"
    # a runtime that does not serve GGUF → the first descriptor that does
    assert gguf_weights_dir(tmp_path, DESCRIPTORS, "vllm", {}) == tmp_path / "models" / "gguf"
    assert gguf_weights_dir(tmp_path, DESCRIPTORS, "llamacpp",
                            {"N97_GGUF_DIR": "/srv/weights"}) == Path("/srv/weights")
    assert gguf_weights_dir(tmp_path, {}, None, {}) == tmp_path / "models" / "gguf"


def test_cli_bundle_verbs(platform, monkeypatch, capsys, tmp_path):
    made = {}

    def fake_create(ctx, target, *, profile, **kwargs):
        made["target"], made["profile"] = target, profile
        return manifest_for(platform.root)

    monkeypatch.setattr(bundle_module, "create_bundle", fake_create)
    code = main(["bundle", "create", str(tmp_path / "out"), "--config", str(platform.cfg)])
    out = capsys.readouterr().out
    assert code == 0 and "bundle written to" in out and "1 image(s)" in out
    assert made == {"target": tmp_path / "out", "profile": "n97"}   # this host's profile
    monkeypatch.setattr(bundle_module, "create_bundle",
                        lambda *a, **k: (_ for _ in ()).throw(BundleError("not bootstrapped")))
    assert main(["bundle", "create", str(tmp_path / "out"), "--config", str(platform.cfg)]) == 1
    assert "not bootstrapped" in capsys.readouterr().out

    def fake_consume(bundle, root, *, env_path, descriptors, **kwargs):
        return bundle_module.ConsumeReport(str(bundle), 1, 2, 0, 0, {"CHAT_MODEL": "gguf:x"},
                                           "llamacpp", ["added CHAT_MODEL=gguf:x"])

    monkeypatch.setattr(bundle_module, "consume_bundle", fake_consume)
    code = main(["bundle", "consume", str(tmp_path / "b"), "--json", "--config", str(platform.cfg)])
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["ok"] and data["gguf_placed"] == 2
    assert "bundle" in platform_cli.HOST_ONLY_COMMANDS


def test_setup_offline_flag_and_env_key_imply_offline(platform):
    from agentd.setup_pipeline import SetupOptions, SetupPipeline

    run = SetupPipeline(platform.ctx, SetupOptions(root=platform.root, offline=True),
                        runner=FakeDocker(), environ={}, say=lambda text: None)
    assert run.offline and run.report.offline
    (platform.root / ".env").write_text((platform.root / ".env").read_text() + f"{OFFLINE_KEY}=1\n")
    run = SetupPipeline(platform.ctx, SetupOptions(root=platform.root), runner=FakeDocker(),
                        environ={}, say=lambda text: None)
    assert run.offline
    # offline images step: present → skipped; missing → the fix
    run.files = [platform.root / "docker-compose.yml"]
    run.runner = FakeDocker(stdout={"config --images": "img/a:1\nimg/b:2\n"})
    assert run.step_images() == ("skipped", "offline: 2 images present locally — no pull, no build")
    run.runner = FakeDocker(stdout={"config --images": "img/a:1\nimg/b:2\n"},
                            fail={"image inspect img/b:2": "No such image"})
    with pytest.raises(platform_cli.PlatformError, match="img/b:2 — consume the bundle"):
        run.step_images()
    assert isinstance(run.runner(["docker", "image", "inspect", "img/a:1"]),
                      subprocess.CompletedProcess)
