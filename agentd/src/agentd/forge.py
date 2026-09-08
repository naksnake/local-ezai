"""Pull-request delivery — the Evolution workflow's final step (ADR-020).

Fail-closed by design (ADR-008): with no forge configured (``forge.kind:
none``, the default) a **PR proposal bundle** is written locally — a
complete, reviewable markdown document with title, body, branch, and the
exact commands a human needs to open the PR themselves. Nothing leaves the
machine.

Configured kinds:
- ``gh``  — the GitHub CLI (``gh pr create``), using its own auth
- ``api`` — generic REST: ``POST {api_base}/repos/{repo}/pulls`` with a
  token from ``$<token_env>``; this endpoint shape is shared by GitHub and
  Gitea/Forgejo (LAN forges)

Branch pushing remains governed by the existing T3 ``git.allow_push`` gate;
this module never pushes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import httpx

from agentd.config import AgentdConfig
from agentd.logging_setup import get_logger
from agentd.schemas import PullRequestResult

log = get_logger("forge")


def create_pull_request(
    config: AgentdConfig,
    repo_root: Path,
    branch: str,
    title: str,
    body: str,
    out_dir: Path,
) -> PullRequestResult:
    forge = config.forge
    if forge.kind == "gh":
        return _via_gh_cli(forge, repo_root, branch, title, body)
    if forge.kind == "api":
        return _via_rest_api(forge, branch, title, body)
    return _bundle(forge, branch, title, body, out_dir)


def _bundle(forge, branch: str, title: str, body: str,
            out_dir: Path) -> PullRequestResult:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "PR_PROPOSAL.md"
    path.write_text(
        f"# Pull request proposal\n\n"
        f"**Title:** {title}\n\n"
        f"**Head branch:** `{branch}`\n"
        f"**Base branch:** `{forge.base_branch}`\n\n"
        f"## Body\n\n{body}\n\n"
        f"## How to open this PR\n\n"
        f"```bash\n"
        f"git push -u origin {branch}\n"
        f"# then open a PR {branch} -> {forge.base_branch} on your forge, or:\n"
        f"gh pr create --head {branch} --base {forge.base_branch} "
        f"--title {title!r} --body-file {path.name}\n"
        f"```\n",
        encoding="utf-8",
    )
    log.info("no forge configured — PR proposal bundle written to %s", path)
    return PullRequestResult(
        created=False, bundle_path=str(path),
        note="forge.kind is 'none' — proposal bundle written for human review",
    )


def _via_gh_cli(forge, repo_root: Path, branch: str, title: str,
                body: str) -> PullRequestResult:
    result = subprocess.run(
        ["gh", "pr", "create", "--head", branch, "--base", forge.base_branch,
         "--title", title, "--body", body],
        cwd=str(repo_root), capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        return PullRequestResult(
            created=False,
            note=f"gh pr create failed: {(result.stderr or result.stdout)[:300]}",
        )
    url = (result.stdout or "").strip().splitlines()[-1] if result.stdout else ""
    return PullRequestResult(created=True, url=url, note="created via gh CLI")


def _via_rest_api(forge, branch: str, title: str, body: str) -> PullRequestResult:
    token = os.environ.get(forge.token_env, "")
    if not forge.api_base or not forge.repo:
        return PullRequestResult(
            created=False,
            note="forge.kind is 'api' but api_base/repo are not configured",
        )
    if not token:
        return PullRequestResult(
            created=False,
            note=f"forge token missing: set ${forge.token_env}",
        )
    try:
        response = httpx.post(
            f"{forge.api_base.rstrip('/')}/repos/{forge.repo}/pulls",
            headers={"Authorization": f"token {token}",
                     "Accept": "application/json"},
            json={"title": title, "head": branch,
                  "base": forge.base_branch, "body": body},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        return PullRequestResult(created=False,
                                 note=f"forge API unreachable: {exc}")
    if response.status_code not in (200, 201):
        return PullRequestResult(
            created=False,
            note=f"forge API rejected the PR ({response.status_code}): "
                 f"{response.text[:300]}",
        )
    data = response.json()
    url = data.get("html_url") or data.get("url") or ""
    return PullRequestResult(created=True, url=url, note="created via forge API")
