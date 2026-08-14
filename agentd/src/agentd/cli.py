"""ezai — command-line interface for the autonomous SWE runtime.

    ezai run  "add a --verbose flag"  --repo /path/to/repo [--push] [--in-place]
    ezai plan "add a --verbose flag"  --repo /path/to/repo
    ezai version
"""

from __future__ import annotations

import argparse
from pathlib import Path

from agentd import __version__
from agentd.config import load_config
from agentd.logging_setup import get_logger, setup_logging
from agentd.schemas import RunReport

log = get_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ezai",
        description="Local Autonomous Software Engineering runtime (local-ezai)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("request", help="The change request, in natural language")
        p.add_argument("--repo", required=True, help="Path to the target git repository")
        p.add_argument("--config", default=None, help="Path to an agentd YAML config file")
        p.add_argument("--verbose", action="store_true", help="Debug logging")

    run_p = sub.add_parser("run", help="Plan, implement, validate, and commit")
    common(run_p)
    run_p.add_argument("--push", action="store_true",
                       help="Allow pushing the run branch (T3 action, off by default)")
    run_p.add_argument("--in-place", action="store_true",
                       help="Edit the repo directly instead of a git worktree")
    run_p.add_argument("--json", action="store_true", dest="as_json",
                       help="Print the full run report as JSON")

    plan_p = sub.add_parser("plan", help="Dry-run: produce the plan only (A0)")
    common(plan_p)

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

        report = execute_run(config, repo, args.request)
        _print_report(report, as_json=args.as_json)
        return 0 if report.status == "completed" else 1
    except WorkspaceError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130


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
    if report.commit and report.commit.sha:
        push_state = "pushed" if report.commit.pushed else (
            f"not pushed ({report.commit.push_error})" if report.commit.push_error
            else "not pushed"
        )
        lines.append(f"commit:     {report.commit.sha[:12]} on {report.commit.branch} "
                     f"({push_state})")
    if report.error:
        lines.append(f"error:      {report.error}")
    lines.append(f"journal:    {report.journal_path}")
    lines.append("")
    print("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
