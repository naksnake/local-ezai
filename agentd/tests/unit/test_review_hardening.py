"""Regressions for the post-V1 review fixes (docs/prs/PR-27-review-fixes.md)
that belong to no single module's suite: the governance queue parses each
request file once per version (the console polls it), the audit log serves
its page from one read, and a hub fetch on a host without docker or the hub
CLI is a fetch failure with the fix — not a traceback."""

from __future__ import annotations

import pytest

from agentd import governance
from agentd.audit import AuditLog
from agentd.fetch import FetchError, HFFetcher
from agentd.governance import ChangeRequest, GovernanceQueue


def request(title: str) -> ChangeRequest:
    return ChangeRequest(kind="activation", title=title, requested_by="t", base_generation=1,
                         proposed={}, affected_roles={"chat": {}})


def test_queue_parses_each_request_file_once_per_version(tmp_path, monkeypatch):
    queue = GovernanceQueue(tmp_path / "config")
    first = queue.submit(request("first"))
    parses: list[int] = []
    real = governance.yaml.safe_load
    monkeypatch.setattr(governance.yaml, "safe_load", lambda text: parses.append(1) or real(text))

    assert [r.title for r in queue.list()] == ["first"] and len(parses) == 1
    queue.list(status="pending")
    queue.get(first.id)
    assert len(parses) == 1  # the same file version: no re-parse

    handed_out = queue.get(first.id)
    handed_out.title = "mutated by a caller"
    assert queue.get(first.id).title == "first"  # callers get their own copy

    queue.approve(first.id, by="nita", reason="ok")  # a write → the next read re-parses
    assert queue.get(first.id).status == "approved" and len(parses) == 2
    second = queue.submit(request("second"))
    assert [r.title for r in queue.list()] == ["first", "second"] and len(parses) == 3
    assert queue.get(second.id).title == "second" and len(parses) == 3


def test_audit_page_is_one_read_with_total_and_tail(tmp_path):
    log = AuditLog(tmp_path / "log.jsonl")
    for index in range(5):
        log.record(f"e{index}", "t")
    total, records = log.page(2)
    assert total == 5 and [r.event for r in records] == ["e3", "e4"]
    assert log.page() == (5, log.tail())
    assert AuditLog(tmp_path / "missing.jsonl").page(3) == (0, [])


def test_hub_fetch_without_docker_or_hub_cli_is_a_fetch_error(tmp_path):
    def no_binary(command, env):
        raise FileNotFoundError("docker")

    fetcher = HFFetcher(tmp_path, runner=no_binary, mode="container")
    with pytest.raises(FetchError, match="no `hf` CLI and no docker"):
        fetcher.fetch("Example/Repo")
