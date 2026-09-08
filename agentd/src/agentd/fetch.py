"""Model sources and fetchers (PR-4, docs/MODEL_LIFECYCLE_MANAGEMENT.md §2
``model install``; docs/FINAL_FIRST_RUN_EXPERIENCE.md §2 rule 1).

A model reference is one of::

    hf:<org/repo>                      Hugging Face repository (any family)
    gguf:<https url>                   one GGUF file
    gguf:hf://<org/repo>/<file.gguf>   one GGUF file from a Hugging Face repo
    gguf:<path>                        a GGUF file already on this host
    <catalog id>                       an entry of the catalog (catalog.py)
    auto                               let the recommender choose

Fetchers put weights where the runtime descriptor mounts them:

- ``GGUFFetcher`` — native, resumable (HTTP Range), SHA-256 verified while
  streaming; local files are hard-linked or copied into the weights dir.
- ``HFFetcher`` — delegates to the ``hf download`` path the stack already
  uses (locally when installed, otherwise inside a throwaway container,
  exactly like scripts/download-models*.sh); the hub client verifies LFS
  checksums and resumes on its own.

Every network/process edge is injectable so the suite stays offline.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agentd.logging_setup import get_logger

log = get_logger("fetch")

HF_RESOLVE_URL = "https://huggingface.co/{repo}/resolve/main/{file}"
_HF_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_CATALOG_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CHUNK = 1 << 20

#: Runner used by HFFetcher: (command, extra_env) → CompletedProcess.
Runner = Callable[[list[str], dict[str, str]], "subprocess.CompletedProcess[str]"]


class SourceError(ValueError):
    """A model reference that cannot be understood — names the accepted forms."""


class FetchError(RuntimeError):
    """Download/verification failed; the model goes to state ``failed``."""


# ── references ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceRef:
    """A parsed model reference. ``kind`` is hf | gguf | catalog | auto."""

    kind: str
    ref: str

    @property
    def format(self) -> str:
        """The served format this reference yields (registry ``source`` key)."""
        return self.kind if self.kind in ("hf", "gguf") else ""

    def as_source(self) -> dict[str, str]:
        return {self.kind: self.ref} if self.format else {}

    def default_name(self) -> str:
        """A registry model name derived from the reference (users may
        override): repo name for hf, file stem for gguf, the id otherwise."""
        if self.kind == "hf":
            return self.ref.rsplit("/", 1)[-1].lower()
        if self.kind == "gguf":
            return Path(gguf_filename(self.ref)).stem.lower()
        return self.ref


ACCEPTED_FORMS = ("hf:<org/repo>", "gguf:<https url>", "gguf:hf://<org/repo>/<file.gguf>",
                  "gguf:<local path>", "<catalog id>", "auto")


def parse_source(text: str) -> SourceRef:
    """The resolver matrix. Unknown forms fail with every accepted form
    listed (F8: problems are printed with the fix)."""
    text = (text or "").strip()
    hint = "accepted: " + ", ".join(ACCEPTED_FORMS)
    if not text:
        raise SourceError(f"empty model reference — {hint}")
    if text == "auto":
        return SourceRef("auto", "")
    scheme, sep, rest = text.partition(":")
    if sep and scheme == "hf":
        if not _HF_REPO.match(rest):
            raise SourceError(
                f"'{text}': an hf: reference is '<org>/<repo>' (one slash, no "
                f"spaces) — {hint}")
        return SourceRef("hf", rest)
    if sep and scheme == "gguf":
        if not rest:
            raise SourceError(f"'{text}': gguf: needs a url, hf://org/repo/file or path — {hint}")
        if rest.startswith("hf://"):
            repo_file = rest[len("hf://"):]
            parts = repo_file.split("/")
            if len(parts) < 3:
                raise SourceError(
                    f"'{text}': gguf:hf:// needs '<org>/<repo>/<file.gguf>' — {hint}")
            repo, file = "/".join(parts[:2]), "/".join(parts[2:])
            rest = HF_RESOLVE_URL.format(repo=repo, file=file)
        if not gguf_filename(rest).lower().endswith(".gguf"):
            raise SourceError(
                f"'{text}': the referenced file is not a .gguf artifact — {hint}")
        return SourceRef("gguf", rest)
    if "://" in text:
        raise SourceError(
            f"'{text}': bare URLs are ambiguous — prefix with gguf: (one file) "
            f"or use hf:<org/repo> — {hint}")
    if sep and scheme not in ("hf", "gguf"):
        raise SourceError(f"'{text}': unknown source scheme '{scheme}:' — {hint}")
    if not _CATALOG_ID.match(text):
        raise SourceError(f"'{text}': not a valid catalog id — {hint}")
    return SourceRef("catalog", text)


def gguf_filename(ref: str) -> str:
    """Basename of a GGUF url/path (query strings stripped)."""
    tail = ref.split("?", 1)[0].rstrip("/")
    return tail.rsplit("/", 1)[-1]


# ── results ──────────────────────────────────────────────────────────────────


@dataclass
class Fetched:
    """What landed on disk."""

    artifact: str        # GGUF file path, or the hub cache dir of the repo
    size_bytes: int
    sha256: str = ""     # GGUF only (the hub client verifies LFS itself)
    reused: bool = False  # already present and verified — nothing downloaded


class Fetcher(Protocol):
    def fetch(self, ref: str, *, expected_sha256: str = "", refetch: bool = False) -> Fetched: ...


# ── GGUF: native resumable download ──────────────────────────────────────────


class ByteStream(Protocol):
    """The slice of an HTTP response the fetcher needs (fakeable)."""

    status: int
    headers: dict[str, str]

    def iter_bytes(self) -> Iterator[bytes]: ...
    def close(self) -> None: ...


#: (url, request headers) → ByteStream
Opener = Callable[[str, dict[str, str]], ByteStream]


class _HttpxStream:
    def __init__(self, url: str, headers: dict[str, str]) -> None:
        import httpx

        self._client = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0))
        self._response = self._client.send(
            self._client.build_request("GET", url, headers=headers), stream=True)
        self.status = self._response.status_code
        self.headers = {k.lower(): v for k, v in self._response.headers.items()}

    def iter_bytes(self) -> Iterator[bytes]:
        return self._response.iter_bytes(_CHUNK)

    def close(self) -> None:
        self._response.close()
        self._client.close()


def httpx_opener(url: str, headers: dict[str, str]) -> ByteStream:
    return _HttpxStream(url, headers)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


class GGUFFetcher:
    """One-file artifacts into the runtime's weights directory."""

    def __init__(self, weights_dir: Path, opener: Opener = httpx_opener) -> None:
        self.weights_dir = Path(weights_dir)
        self._open = opener

    def fetch(self, ref: str, *, expected_sha256: str = "",
              refetch: bool = False) -> Fetched:
        self.weights_dir.mkdir(parents=True, exist_ok=True)
        final = self.weights_dir / gguf_filename(ref)
        if final.is_file() and not refetch:
            digest = sha256_of(final)
            if not expected_sha256 or digest == expected_sha256:
                log.info("weights present, verified: %s", final)
                return Fetched(str(final), final.stat().st_size, digest, reused=True)
            log.warning("checksum mismatch on existing %s — re-downloading", final.name)
            final.unlink()
        if "://" in ref:
            return self._download(ref, final, expected_sha256)
        return self._import_local(Path(ref).expanduser(), final, expected_sha256)

    def _import_local(self, source: Path, final: Path, expected: str) -> Fetched:
        if not source.is_file():
            raise FetchError(f"no such file: {source}")
        if source.resolve() != final.resolve():
            if final.exists():
                final.unlink()
            try:
                os.link(source, final)  # same filesystem: instant
            except OSError:
                shutil.copy2(source, final)
        digest = sha256_of(final)
        if expected and digest != expected:
            if source.resolve() != final.resolve():
                final.unlink()
            raise FetchError(f"{source.name}: sha256 {digest} does not match the "
                             f"declared {expected}")
        return Fetched(str(final), final.stat().st_size, digest)

    def _download(self, url: str, final: Path, expected: str) -> Fetched:
        part = final.with_name(final.name + ".part")
        digest = hashlib.sha256()
        offset = 0
        if part.is_file():
            offset = part.stat().st_size
            with part.open("rb") as handle:  # resume: re-hash what we already have
                for chunk in iter(lambda: handle.read(_CHUNK), b""):
                    digest.update(chunk)
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        stream = self._open(url, headers)
        try:
            if stream.status == 206 and offset:
                mode = "ab"
                log.info("resuming %s at byte %d", final.name, offset)
            elif stream.status == 200:
                mode, digest, offset = "wb", hashlib.sha256(), 0
            elif stream.status == 416 and offset:  # already complete
                mode = "ab"
            else:
                raise FetchError(f"GET {url} → HTTP {stream.status}")
            with part.open(mode) as handle:
                if stream.status != 416:
                    for chunk in stream.iter_bytes():
                        handle.write(chunk)
                        digest.update(chunk)
        finally:
            stream.close()
        hexdigest = digest.hexdigest()
        if expected and hexdigest != expected:
            part.unlink(missing_ok=True)
            raise FetchError(f"{final.name}: sha256 {hexdigest} does not match the "
                             f"declared {expected} — download discarded")
        part.replace(final)
        return Fetched(str(final), final.stat().st_size, hexdigest)


# ── Hugging Face repositories: delegated to the hub client ───────────────────


def default_runner(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env={**os.environ, **env}, capture_output=True,
                          text=True, check=False)


class HFFetcher:
    """Whole repositories into the hub cache the engine mounts. Uses the
    local ``hf`` CLI when present, otherwise the same throwaway-container
    invocation as scripts/download-models.sh (no host Python required)."""

    CONTAINER_IMAGE = "python:3.11-slim"

    def __init__(self, cache_dir: Path, runner: Runner = default_runner,
                 mode: str = "auto") -> None:
        self.cache_dir = Path(cache_dir)
        self._run = runner
        if mode == "auto":
            mode = "local" if shutil.which("hf") else "container"
        self.mode = mode

    def repo_dir(self, repo: str) -> Path:
        return self.cache_dir / f"models--{repo.replace('/', '--')}"

    def command(self, repo: str) -> tuple[list[str], dict[str, str]]:
        if self.mode == "local":
            return ["hf", "download", repo], {"HF_HUB_CACHE": str(self.cache_dir)}
        script = "pip install -q 'huggingface_hub[cli]' && hf download " + repo
        return (["docker", "run", "--rm", "-v", f"{self.cache_dir}:/hf-cache",
                 "-e", "HF_HUB_CACHE=/hf-cache", "-e", "HF_TOKEN",
                 self.CONTAINER_IMAGE, "bash", "-c", script], {})

    def fetch(self, ref: str, *, expected_sha256: str = "",
              refetch: bool = False) -> Fetched:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        repo_dir = self.repo_dir(ref)
        if not refetch and (repo_dir / "snapshots").is_dir() and self._size(repo_dir):
            log.info("repository present: %s", repo_dir)
            return Fetched(str(repo_dir), self._size(repo_dir), reused=True)
        command, env = self.command(ref)
        proc = self._run(command, env)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            raise FetchError(f"hf download {ref} failed (exit {proc.returncode}): "
                             + " | ".join(tail))
        if not (repo_dir / "snapshots").is_dir():
            raise FetchError(f"hf download {ref} finished but no snapshot exists at "
                             f"{repo_dir}")
        return Fetched(str(repo_dir), self._size(repo_dir))

    @staticmethod
    def _size(repo_dir: Path) -> int:
        snapshots = repo_dir / "snapshots"
        total = 0
        for path in snapshots.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size  # follows symlinks into blobs/
            except OSError:
                continue
        return total
