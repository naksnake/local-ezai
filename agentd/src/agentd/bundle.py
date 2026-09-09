"""Offline bundle — the air-gapped first run (PR-23, ADR-031;
docs/FIRST_RUN_EXPERIENCE.md §1 "install.sh --offline <bundle>", criterion F6).

On a connected, bootstrapped host::

    local-ezai bundle create <dir>          (make bundle BUNDLE=<dir>)

writes what a first run would otherwise fetch::

    <dir>/bundle.json                   manifest: platform, seeds, images, checksums
    <dir>/images.tar                    `docker save` of every compose image
    <dir>/weights/gguf/<file>.gguf      the registry's GGUF artifacts
    <dir>/weights/hf-cache/models--*/   the hub cache the engine and the embedding
                                        server mount (installed repositories, the
                                        embedding model and its code repository)

On the air-gapped host ``install.sh --offline <dir>`` consumes it before the
seeds are validated: images loaded with ``docker load``, weights placed where
the runtime descriptors mount them (GGUF checksums verified, present files
skipped), the seeds written to ``.env`` pointing at the local files, and
``EZAI_OFFLINE=1`` stamped so ``local-ezai setup`` skips image pull/build and
the fetchers refuse the network (``lifecycle.offline_fetcher``) — the same
steps, no egress. The bundle is a directory: ``tar`` it for transport.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentd import lifecycle
from agentd.bootstrap import RUNTIME_KEY, SEED_GROUPS
from agentd.fetch import gguf_filename, sha256_of
from agentd.installer import EnvText
from agentd.lifecycle import OFFLINE_KEY
from agentd.logging_setup import get_logger
from agentd.platform_cli import PlatformContext, compose_files
from agentd.runtime_descriptor import RuntimeDescriptor

log = get_logger("bundle")

MANIFEST = "bundle.json"
IMAGES_TAR = "images.tar"
WEIGHTS_DIR = "weights"
GGUF_SUBDIR = "gguf"
HF_SUBDIR = "hf-cache"
BUNDLE_VERSION = 1
#: Placeholder in the manifest's seeds for the consuming host's GGUF weights dir.
WEIGHTS_PLACEHOLDER = "{weights}"
_STAMP = "%Y-%m-%dT%H:%M:%S"

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class BundleError(ValueError):
    """The bundle cannot be created or consumed; the message names the fix."""


def _tail(proc: subprocess.CompletedProcess[str], lines: int = 3) -> str:
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip().splitlines()
    return " | ".join(line.strip() for line in text[-lines:] if line.strip())[:400]


@dataclass
class Manifest:
    version: int
    created_at: str
    capability_class: str
    accelerator: str
    runtime: str
    generation: int | None
    #: group → the seed to write into .env; ``{weights}`` = the GGUF weights dir.
    seeds: dict[str, str]
    images: list[str]
    gguf: list[dict[str, Any]] = field(default_factory=list)     # {file, sha256, size}
    hf_cache: list[str] = field(default_factory=list)            # models--org--repo dirs
    notes: str = ""

    def dump(self) -> str:
        return json.dumps(asdict(self), indent=2) + "\n"

    @classmethod
    def load(cls, path: Path) -> Manifest:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BundleError(f"{path} is not a bundle manifest: {exc}") from exc
        if data.get("version") != BUNDLE_VERSION:
            raise BundleError(f"{path}: bundle version {data.get('version')!r} is not "
                              f"{BUNDLE_VERSION} — create the bundle with this platform version")
        try:
            return cls(**data)
        except TypeError as exc:
            raise BundleError(f"{path}: malformed manifest ({exc})") from exc


@dataclass
class ConsumeReport:
    bundle: str
    images_loaded: int
    gguf_placed: int
    gguf_skipped: int
    hf_placed: int
    seeds: dict[str, str]
    runtime: str
    env_changes: list[str]

    def summary(self) -> str:
        return (f"consumed {self.bundle}: {self.images_loaded} image(s) loaded, "
                f"{self.gguf_placed} GGUF file(s) placed ({self.gguf_skipped} present), "
                f"{self.hf_cache_text()}; seeds → .env, {OFFLINE_KEY}=1")

    def hf_cache_text(self) -> str:
        return f"{self.hf_placed} hub repositor{'y' if self.hf_placed == 1 else 'ies'}"


# ── shared helpers ───────────────────────────────────────────────────────────


def compose_images(files: list[Path], runner: Runner) -> list[str]:
    """Every image the compose files reference — built or pulled."""
    command = ["docker", "compose"]
    for file in files:
        command += ["-f", str(file)]
    proc = runner(command + ["config", "--images"])
    if proc.returncode != 0:
        raise BundleError(f"docker compose config --images failed: {_tail(proc)}")
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def hf_cache_dir(root: Path, environ: Mapping[str, str]) -> Path:
    raw = (environ.get("MODELS_DIR") or "").strip()
    path = Path(raw).expanduser() if raw else root / "models" / "hf-cache"
    return path if path.is_absolute() else (root / path).resolve()


def gguf_weights_dir(root: Path, descriptors: Mapping[str, RuntimeDescriptor], runtime: str | None,
                     environ: Mapping[str, str]) -> Path:
    """Where GGUF weights live on this host: the chosen runtime's mount when it
    serves GGUF, else the first descriptor that does, else the default."""
    ordered = [descriptors[runtime]] if runtime in descriptors else []
    ordered += [d for name, d in sorted(descriptors.items()) if name != runtime]
    for descriptor in ordered:
        if "gguf" in descriptor.serves_formats:
            return lifecycle.weights_dir(descriptor, root, environ)
    return root / "models" / "gguf"


# ── create ───────────────────────────────────────────────────────────────────


def create_bundle(ctx: PlatformContext, target: Path, *, profile: str,
                  runner: Runner | None = None, environ: Mapping[str, str] | None = None,
                  now: str | None = None) -> Manifest:
    """Bundle THIS platform's images and weights. Requires a bootstrapped
    platform (the seeds are read from generation N, not from .env)."""
    import os

    env = os.environ if environ is None else environ
    run = runner or (lambda command: subprocess.run(command, cwd=str(ctx.root),
                                                    capture_output=True, text=True, check=False))
    registry = ctx.registry(required=False)
    if registry is None:
        raise BundleError("the platform is not bootstrapped — run `make setup` (or `local-ezai "
                          "bootstrap`) first; a bundle carries the models of a generation")
    target = Path(target).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    files = compose_files(ctx.root, profile, rendered=True)
    images = compose_images(files, run)
    if not images:
        raise BundleError("docker compose lists no images for this profile")
    saved = run(["docker", "save", "-o", str(target / IMAGES_TAR), *images])
    if saved.returncode != 0:
        raise BundleError(f"docker save failed: {_tail(saved)}")

    gguf_dir = target / WEIGHTS_DIR / GGUF_SUBDIR
    gguf_dir.mkdir(parents=True, exist_ok=True)
    gguf: list[dict[str, Any]] = []
    for name, entry in sorted(registry.models.items()):
        artifact = Path(entry.artifact) if entry.artifact else None
        if artifact is None or not artifact.is_file() or artifact.suffix.lower() != ".gguf":
            continue
        digest = sha256_of(artifact)
        copy = gguf_dir / artifact.name
        if not (copy.is_file() and sha256_of(copy) == digest):
            shutil.copy2(artifact, copy)
        gguf.append({"file": artifact.name, "model": name, "sha256": digest,
                     "size": artifact.stat().st_size})

    cache = hf_cache_dir(ctx.root, env)
    hf_dir = target / WEIGHTS_DIR / HF_SUBDIR
    hf_cache: list[str] = []
    if cache.is_dir():
        for repo_dir in sorted(cache.glob("models--*")):
            if not (repo_dir / "snapshots").is_dir():
                continue
            shutil.copytree(repo_dir, hf_dir / repo_dir.name, symlinks=True, dirs_exist_ok=True)
            hf_cache.append(repo_dir.name)

    seeds: dict[str, str] = {}
    runtimes: list[str] = []
    for key, group in SEED_GROUPS.items():
        members = registry.groups.get(group) or []
        if not members:
            raise BundleError(f"group '{group}' has no serving model — the bundle needs every "
                              "group of generation "
                              f"{registry.generation} filled ({key})")
        entry = registry.models[members[0]]
        runtimes.append(entry.provider)
        if "gguf" in entry.source:
            filename = Path(entry.artifact).name if entry.artifact else \
                gguf_filename(entry.source["gguf"])
            seeds[key] = f"gguf:{WEIGHTS_PLACEHOLDER}/{filename}"
        elif "hf" in entry.source:
            seeds[key] = f"hf:{entry.source['hf']}"
        else:
            raise BundleError(f"model '{members[0]}' has no gguf/hf source to bundle")
    manifest = Manifest(
        version=BUNDLE_VERSION, created_at=now or time.strftime(_STAMP),
        capability_class=ctx.platform.klass, accelerator=ctx.platform.accel,
        runtime=runtimes[0] if runtimes else (env.get(RUNTIME_KEY) or ""),
        generation=registry.generation, seeds=seeds, images=images, gguf=gguf,
        hf_cache=hf_cache,
        notes=(f"created on class {ctx.platform.klass} (accelerator {ctx.platform.accel}); "
               "consume with: ./install.sh --offline <this directory> && make setup-offline"))
    (target / MANIFEST).write_text(manifest.dump(), encoding="utf-8")
    log.info("bundle written to %s (%d images, %d gguf, %d hub repos)", target, len(images),
             len(gguf), len(hf_cache))
    return manifest


# ── consume ──────────────────────────────────────────────────────────────────


def consume_bundle(bundle: Path, root: Path, *, env_path: Path,
                   descriptors: Mapping[str, RuntimeDescriptor], runner: Runner | None = None,
                   environ: Mapping[str, str] | None = None, now: str | None = None,
                   ) -> ConsumeReport:
    """Place the bundle on this host and point ``.env`` at it. Idempotent:
    present, verified weights are skipped; the seeds are rewritten."""
    import os

    env = os.environ if environ is None else environ
    bundle = Path(bundle).expanduser().resolve()
    root = Path(root).resolve()
    run = runner or (lambda command: subprocess.run(command, cwd=str(root), capture_output=True,
                                                    text=True, check=False))
    manifest = Manifest.load(bundle / MANIFEST)
    if not env_path.is_file():
        raise BundleError(f"no {env_path} — ./install.sh writes it before consuming a bundle")

    images_loaded = 0
    tar = bundle / IMAGES_TAR
    if tar.is_file():
        loaded = run(["docker", "load", "-i", str(tar)])
        if loaded.returncode != 0:
            raise BundleError(f"docker load failed: {_tail(loaded)} — Docker must be installed "
                              "and running on the air-gapped host")
        images_loaded = len(manifest.images)

    weights = gguf_weights_dir(root, descriptors, manifest.runtime, env)
    weights.mkdir(parents=True, exist_ok=True)
    placed = skipped = 0
    for item in manifest.gguf:
        source = bundle / WEIGHTS_DIR / GGUF_SUBDIR / item["file"]
        if not source.is_file():
            raise BundleError(f"the bundle lists {item['file']} but the file is missing")
        if sha256_of(source) != item["sha256"]:
            raise BundleError(f"{item['file']}: checksum mismatch — the bundle is corrupt")
        destination = weights / item["file"]
        if destination.is_file() and sha256_of(destination) == item["sha256"]:
            skipped += 1
            continue
        shutil.copy2(source, destination)
        placed += 1

    cache = hf_cache_dir(root, env)
    hf_placed = 0
    for name in manifest.hf_cache:
        source = bundle / WEIGHTS_DIR / HF_SUBDIR / name
        if not source.is_dir():
            raise BundleError(f"the bundle lists hub repository {name} but it is missing")
        shutil.copytree(source, cache / name, symlinks=True, dirs_exist_ok=True)
        hf_placed += 1

    stamp = now or time.strftime(_STAMP)
    editor = EnvText(env_path.read_text(encoding="utf-8"))
    changes: list[str] = []
    seeds = {key: ref.replace(WEIGHTS_PLACEHOLDER, str(weights)) for key, ref in
             manifest.seeds.items()}
    how = editor.set(RUNTIME_KEY, manifest.runtime, comment=f"offline bundle {bundle.name}")
    changes.append(f"{how} {RUNTIME_KEY}={manifest.runtime}")
    for key, ref in seeds.items():
        how = editor.set(key, ref, comment=f"offline bundle {bundle.name}")
        changes.append(f"{how} {key}={ref}")
    how = editor.set(OFFLINE_KEY, "1", comment=f"no egress: images and weights came from "
                                               f"{bundle.name} on {stamp}; remove to go online")
    changes.append(f"{how} {OFFLINE_KEY}=1")
    env_path.write_text(editor.text(stamp), encoding="utf-8")
    log.info("bundle %s consumed: %d images, %d gguf placed, %d hub repos", bundle,
             images_loaded, placed, hf_placed)
    return ConsumeReport(str(bundle), images_loaded, placed, skipped, hf_placed, seeds,
                         manifest.runtime, changes)
