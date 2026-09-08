"""ezai — command-line interface for the autonomous SWE runtime.

    ezai run  "add a --verbose flag"  --repo /path/to/repo [--push] [--in-place]
    ezai plan "add a --verbose flag"  --repo /path/to/repo
    ezai runs                       # list recent runs (observability)
    ezai journal <run-id>           # pretty-print a run's event journal
    ezai version
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentd import __version__
from agentd.config import AgentdConfig, load_config
from agentd.logging_setup import get_logger, setup_logging
from agentd.schemas import RunReport

log = get_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ezai",
        description="Local Autonomous Software Engineering runtime (local-ezai)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, with_request: bool = True) -> None:
        if with_request:
            p.add_argument("request", help="The change request, in natural language")
            p.add_argument("--repo", required=True,
                           help="Path to the target git repository")
        p.add_argument("--config", default=None,
                       help="Path to an agentd YAML config file")
        p.add_argument("--verbose", action="store_true", help="Debug logging")

    run_p = sub.add_parser("run", help="Plan, implement, validate, self-heal, commit")
    common(run_p)
    run_p.add_argument("--push", action="store_true",
                       help="Allow pushing the run branch (T3 action, off by default)")
    run_p.add_argument("--in-place", action="store_true",
                       help="Edit the repo directly instead of a git worktree")
    run_p.add_argument("--json", action="store_true", dest="as_json",
                       help="Print the full run report as JSON")
    run_p.add_argument("--max-iterations", type=int, default=None,
                       help="Override limits.max_heal_iterations for this run")

    plan_p = sub.add_parser("plan", help="Dry-run: produce the plan only (A0)")
    common(plan_p)

    runs_p = sub.add_parser("runs", help="List recent runs and their outcomes")
    common(runs_p, with_request=False)
    runs_p.add_argument("--limit", type=int, default=20)

    journal_p = sub.add_parser("journal", help="Pretty-print a run's event journal")
    journal_p.add_argument("run_id", help="Run id (see 'ezai runs')")
    common(journal_p, with_request=False)

    remember_p = sub.add_parser(
        "remember", help="Persist curated project knowledge into .agent/memory.db")
    remember_p.add_argument("content", help="The rule/style/decision to remember")
    remember_p.add_argument("--repo", required=True)
    remember_p.add_argument("--kind", default="project_rule",
                            choices=["project_rule", "coding_style",
                                     "architecture_decision"])
    remember_p.add_argument("--title", default="")
    common(remember_p, with_request=False)

    memory_p = sub.add_parser("memory", help="Inspect a repo's project memory")
    memory_p.add_argument("--repo", required=True)
    memory_p.add_argument("--kind", default=None)
    memory_p.add_argument("--search", default=None)
    memory_p.add_argument("--limit", type=int, default=20)
    common(memory_p, with_request=False)

    sub.add_parser("version", help="Print the version")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "version":
        print(f"agentd {__version__}")
        return 0

    config = load_config(args.config)
    if getattr(args, "verbose", False):
        config.log_level = "DEBUG"
    setup_logging(config.log_level)

    if args.command == "runs":
        return _cmd_runs(config, args.limit)
    if args.command == "journal":
        return _cmd_journal(config, args.run_id)
    if args.command == "remember":
        return _cmd_remember(config, args)
    if args.command == "memory":
        return _cmd_memory(config, args)

    repo = Path(args.repo).expanduser().resolve()

    from agentd.runner import execute_run, plan_only  # deferred: heavy imports
    from agentd.workspace import WorkspaceError

    try:
        if args.command == "plan":
            plan = plan_only(config, repo, args.request)
            print(plan.model_dump_json(indent=2))
            return 0

        if args.push:
            config.git.allow_push = True
        if args.in_place:
            config.workspace.mode = "in-place"
        if args.max_iterations is not None:
            config.limits.max_heal_iterations = max(0, args.max_iterations)

        report = execute_run(config, repo, args.request)
        _print_report(report, as_json=args.as_json)
        return 0 if report.status == "completed" else 1
    except WorkspaceError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130


# ── observability commands ────────────────────────────────────────────────────


def _cmd_runs(config: AgentdConfig, limit: int) -> int:
    runs_dir = Path(config.runs_dir)
    if not runs_dir.is_dir():
        print("(no runs yet)")
        return 0
    rows = []
    for run_dir in sorted(runs_dir.iterdir(), reverse=True)[: max(1, limit)]:
        report_file = run_dir / "report.json"
        if report_file.is_file():
            data = json.loads(report_file.read_text(encoding="utf-8"))
            goal = (data.get("plan") or {}).get("goal") or data.get("request", "")
            rows.append(
                f"{run_dir.name:24} {data.get('status', '?'):9} "
                f"iters={data.get('iterations_used', 0):<2} "
                f"branch={data.get('branch', '?'):22} {goal[:60]}"
            )
        elif run_dir.is_dir():
            rows.append(f"{run_dir.name:24} (no report — in progress or crashed)")
    print("\n".join(rows) if rows else "(no runs yet)")
    return 0


def _cmd_journal(config: AgentdConfig, run_id: str) -> int:
    journal_file = Path(config.runs_dir) / run_id / "journal.jsonl"
    if not journal_file.is_file():
        log.error("no journal for run '%s' under %s", run_id, config.runs_dir)
        return 2
    for line in journal_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        payload = event.get("payload", {})
        detail = ", ".join(
            f"{k}={_short(v)}" for k, v in payload.items() if v not in (None, [], "")
        )
        print(f"{event['seq']:4}  {event['type']:24} {detail[:160]}")
    return 0


def _short(value: object) -> str:
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _memory_store(config: AgentdConfig, repo: str):
    from agentd.memory import MemoryStore
    from agentd.workspace import ensure_git_repo

    repo_path = Path(repo).expanduser().resolve()
    ensure_git_repo(repo_path)
    return MemoryStore(repo_path / config.memory.dir)


def _cmd_remember(config: AgentdConfig, args) -> int:
    store = _memory_store(config, args.repo)
    record_id = store.record(
        kind=args.kind,
        title=args.title or args.content[:60],
        content=args.content,
        run_id="manual",
    )
    store.export_lessons()
    store.close()
    print(f"remembered #{record_id} [{args.kind}] — "
          f"{store.db_path} / {store.lessons_path.name}")
    return 0


def _cmd_memory(config: AgentdConfig, args) -> int:
    store = _memory_store(config, args.repo)
    if not store.exists:
        print("(no memory yet for this repository)")
        return 0
    if args.search:
        kinds = [args.kind] if args.kind else None
        records = store.search(args.search, kinds=kinds, limit=args.limit)
    else:
        kinds = [args.kind] if args.kind else None
        records = store.recent(kinds, limit=args.limit)
    for record in records:
        line_1 = (f"#{record.id:<4} {record.created_at}  [{record.kind}]"
                  f"  (run {record.run_id or '-'})")
        print(line_1)
        print(f"      {record.title}")
        first_line = record.content.splitlines()[0] if record.content else ""
        if first_line and first_line not in record.title:
            print(f"      {first_line[:120]}")
    print(f"\n{store.count()} total memories — {store.db_path}")
    store.close()
    return 0


# ── report rendering ──────────────────────────────────────────────────────────


def _print_report(report: RunReport, as_json: bool = False) -> None:
    if as_json:
        print(report.model_dump_json(indent=2))
        return
    lines = [
        "",
        f"run:        {report.run_id}  [{report.status.upper()}]",
        f"branch:     {report.branch}",
        f"workspace:  {report.workspace_path}",
    ]
    if report.plan:
        lines.append(f"goal:       {report.plan.goal}")
        for result in report.task_results:
            lines.append(f"  task {result.task_id}: {result.status} — "
                         f"{result.summary.splitlines()[0][:90]}")
    if report.validation:
        lines.append(f"validation: {report.validation.summary}")
        browser = report.validation.browser
        if browser and browser.enabled:
            for wf in browser.workflows:
                mark = "ok" if wf.passed else "FAILED"
                extra = ""
                if wf.console_errors:
                    extra = f", {len(wf.console_errors)} console error(s)"
                if wf.failed_step:
                    extra += f", {wf.failed_step[:60]}"
                lines.append(f"  browser {wf.name}: {mark} "
                             f"({len(wf.steps)} steps{extra})")
            if browser.error:
                lines.append(f"  browser setup: FAILED — {browser.error[:100]}")
    if report.healing:
        lines.append(f"healing:    {report.iterations_used} debug/fix iteration(s)")
        for h in report.healing:
            lines.append(
                f"  iter {h.iteration}: [{'/'.join(h.categories) or '?'}] "
                f"{h.root_cause[:70]} → fix {h.fix_task_id} ({h.fix_status}), "
                f"revalidation {'PASSED' if h.revalidation_passed else 'failed'}"
            )
    if report.commit and report.commit.sha:
        push_state = "pushed" if report.commit.pushed else (
            f"not pushed ({report.commit.push_error})" if report.commit.push_error
            else "not pushed"
        )
        lines.append(f"commit:     {report.commit.sha[:12]} on {report.commit.branch} "
                     f"({push_state})")
    elif report.commit:
        lines.append(f"commit:     {report.commit.message}")
    if report.error:
        lines.append(f"error:      {report.error}")
    lines.append(f"journal:    {report.journal_path}")
    lines.append("")
    print("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
