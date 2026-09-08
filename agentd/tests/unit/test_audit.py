"""The platform's single append-only audit log (PR-8): the primitive the
governance queue (PR-5) and the ezaid control plane write through."""

from __future__ import annotations

import json
import threading

from agentd.audit import AuditLog, AuditRecord
from agentd.governance import GovernanceQueue


def test_record_appends_and_tail_reads_the_newest(tmp_path):
    log = AuditLog(tmp_path / "governance" / "log.jsonl")
    assert log.tail() == [] and log.count() == 0
    first = log.record("request.submitted", "nita", request_id="cr-0001", generation=1, note="x")
    log.record("request.approved", "policy")
    assert isinstance(first, AuditRecord)
    assert first.details == {"note": "x"} and first.generation == 1 and first.ts
    assert [r.event for r in log.tail()] == ["request.submitted", "request.approved"]
    assert [r.event for r in log.tail(1)] == ["request.approved"]
    assert log.count() == 2


def test_records_are_appended_never_rewritten(tmp_path):
    log = AuditLog(tmp_path / "log.jsonl")
    log.record("a", "x")
    before = log.path.read_text()
    log.record("b", "y")
    after = log.path.read_text()
    assert after.startswith(before) and after.count("\n") == 2
    assert all(json.loads(line) for line in after.splitlines())  # one record per line


def test_concurrent_writers_keep_one_record_per_line(tmp_path):
    log = AuditLog(tmp_path / "log.jsonl")

    def worker(i: int) -> None:
        for j in range(25):
            log.record("evt", f"t{i}", n=j)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = log.path.read_text().splitlines()
    assert len(lines) == 200 == log.count()
    assert {json.loads(line)["actor"] for line in lines} == {f"t{i}" for i in range(8)}


def test_governance_queue_and_control_plane_share_one_log(tmp_path):
    queue = GovernanceQueue(tmp_path / "config")
    queue.record("request.submitted", "nita", request_id="cr-0001")
    AuditLog(queue.log_path).record("control.started", "ezaid", port=8010)
    records = queue.audit()
    assert [r.event for r in records] == ["request.submitted", "control.started"]
    assert records[-1].details == {"port": 8010} and records[0].request_id == "cr-0001"
    assert queue.audit_log.path == queue.log_path
