"""local-ezai — the production CLI of the autonomous SWE runtime (Phase 5).

    local-ezai [PATH] [COMMAND] ...

Path selection (cross-platform, pathlib throughout):
    local-ezai .                          # open chat for the current project
    local-ezai /home/test/project/CRM     # open chat for that project
    local-ezai -C /path run "..."         # git-style explicit path
    local-ezai run "..."                  # commands default to the cwd

Commands:
    chat                       interactive session with the local model
    plan "<task>"              produce an execution plan (dry-run, traceless)
    run  "<task>"              full pipeline: plan → code → validate →
                               self-heal → commit (worktree branch)
    code "<task>"              plan + implement only; changes left uncommitted
    test                       run validation (commands + Browser QA) in place
    fix                        repair failing validation via the debug loop,
                               commit on the current branch when green
    review                     adversarial review of the working-tree diff
    commit                     validate, then commit the working tree
                               (blocked until validation is green)
    memory                     inspect / add to the project's memory
    sprint <spec-file>         run a markdown spec's tasks sequentially on
                               one sprint/<id> branch

Every command drives the existing agents: Planner, Coder, Validator,
Debugger, Browser QA, Memory, Reviewer, Git.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentd import __version__
from agentd.config import AgentdConfig, load_config
from agentd.logging_setup import get_logger, setup_logging

log = get_logger("local-ezai")

COMMANDS = ("chat", "plan", "run", "code", "test", "fix", "review",
            "commit", "memory", "sprint", "version")


# ── argv preprocessing: leading path selection ────────────────────────────────


def split_path_argument(argv: list[str]) -> tuple[str | None, list[str]]:
    """Support ``local-ezai <path> [command ...]`` and bare ``local-ezai <path>``.

    The first argument is treated as the project path when it is not a
    command, not a flag, and names an existing directory.
    """
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        candidate = Path(argv[0]).expanduser()
        if candidate.is_dir():
            return argv[0], argv[1:]
    return None, argv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-ezai",
        description="Local Autonomous Software Engineer — runs entirely on "
                    "your own hardware.",
    )
    parser.add_argument("-C", "--path", default=None,
                        help="Project directory (default: current directory)")
    parser.add_argument("--config", default=None,
                        help="agentd YAML config file (or $AGENTD_CONFIG)")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    parser.add_argument("--version", action="version",
                        version=f"local-ezai {__version__}")

    # Global options are also accepted AFTER the subcommand
    # (`local-ezai test --config x.yaml`); SUPPRESS keeps a post-command
    # value from being clobbered by subparser defaults.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    common.add_argument("--verbose", action="store_true",
                        default=argparse.SUPPRESS)
    common.add_argument("-C", "--path", default=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("chat", help="Interactive session with the local model",
                   parents=[common])

    plan_p = sub.add_parser("plan", help="Produce an execution plan (dry-run)",
                            parents=[common])
    plan_p.add_argument("task")

    run_p = sub.add_parser("run", help="Full pipeline on a worktree branch",
                           parents=[common])
    run_p.add_argument("task")
    run_p.add_argument("--push", action="store_true",
                       help="Allow pushing the run branch (off by default)")
    run_p.add_argument("--in-place", action="store_true",
                       help="Edit the repo directly instead of a worktree")
    run_p.add_argument("--max-iterations", type=int, default=None)
    run_p.add_argument("--json", action="store_true", dest="as_json")

    code_p = sub.add_parser("code", help="Plan + implement only (no commit)",
                            parents=[common])
    code_p.add_argument("task")
    code_p.add_argument("--in-place", action="store_true")
    code_p.add_argument("--json", action="store_true", dest="as_json")

    test_p = sub.add_parser("test", help="Run validation (commands + Browser QA)",
                            parents=[common])
    test_p.add_argument("--json", action="store_true", dest="as_json")

    fix_p = sub.add_parser("fix", help="Repair failing validation in place",
                           parents=[common])
    fix_p.add_argument("--goal", default="repair failing validation checks")
    fix_p.add_argument("--max-iterations", type=int, default=None)
    fix_p.add_argument("--json", action="store_true", dest="as_json")

    review_p = sub.add_parser("review", help="Review the working-tree diff",
                              parents=[common])
    review_p.add_argument("--json", action="store_true", dest="as_json")

    commit_p = sub.add_parser("commit", help="Validate, then commit the tree",
                              parents=[common])
    commit_p.add_argument("-m", "--message", default=None)
    commit_p.add_argument("--push", action="store_true")

    memory_p = sub.add_parser("memory", help="Inspect / add project memory",
                              parents=[common])
    memory_p.add_argument("--add", default=None, metavar="TEXT",
                          help="Persist a curated memory entry")
    memory_p.add_argument("--kind", default="project_rule",
                          choices=["project_rule", "coding_style",
                                   "architecture_decision"])
    memory_p.add_argument("--search", default=None)
    memory_p.add_argument("--limit", type=int, default=20)

    sprint_p = sub.add_parser("sprint", help="Run a markdown spec's tasks",
                              parents=[common])
    sprint_p.add_argument("spec_file")
    sprint_p.add_argument("--keep-going", action="store_true",
                          help="Continue with remaining tasks after a failure")
    sprint_p.add_argument("--push", action="store_true")
    sprint_p.add_argument("--json", action="store_true", dest="as_json")

    sub.add_parser("version", help="Print the version")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:]) if argv is None else list(argv)
    leading_path, rest = split_path_argument(raw)
    args = build_parser().parse_args(rest)

    if args.command == "version":
        print(f"local-ezai {__version__}")
        return 0

    config = load_config(args.config)
    if args.verbose:
        config.log_level = "DEBUG"
    setup_logging(config.log_level)

    project = Path(args.path or leading_path or ".").expanduser().resolve()
    command = args.command or "chat"  # bare `local-ezai [path]` opens chat

    from agentd.workspace import WorkspaceError, ensure_git_repo

    try:
        if command not in ("chat",):  # chat works in any directory
            ensure_git_repo(project)
        return _dispatch(command, args, config, project)
    except WorkspaceError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        print()
        log.error("interrupted")
        return 130


def _dispatch(command: str, args, config: AgentdConfig, project: Path) -> int:
    if command == "chat":
        return cmd_chat(config, project)
    if command == "plan":
        return cmd_plan(config, project, args.task)
    if command == "run":
        return cmd_run(config, project, args)
    if command == "code":
        return cmd_code(config, project, args)
    if command == "test":
        return cmd_test(config, project, args.as_json)
    if command == "fix":
        return cmd_fix(config, project, args)
    if command == "review":
        return cmd_review(config, project, args.as_json)
    if command == "commit":
        return cmd_commit(config, project, args)
    if command == "memory":
        return cmd_memory(config, project, args)
    if command == "sprint":
        return cmd_sprint(config, project, args)
    raise ValueError(f"unknown command {command}")  # unreachable


# ── commands ──────────────────────────────────────────────────────────────────


def cmd_chat(config: AgentdConfig, project: Path) -> int:
    """Interactive REPL with the local model, aware of project memory."""
    from agentd.llm import LLMError, build_llm
    from agentd.memory import KIND_RULE, KIND_STYLE, MemoryStore

    llm = build_llm(config.llm)
    system = (
        f"You are local-ezai, an autonomous software-engineering assistant "
        f"running entirely on local hardware. Current project: {project}. "
        "Answer concisely. For actual code changes, tell the user to run "
        "'local-ezai run \"<task>\"' (full pipeline) or "
        "'local-ezai plan \"<task>\"' (dry run)."
    )
    store = MemoryStore(project / config.memory.dir)
    rules = store.recent([KIND_RULE, KIND_STYLE], limit=10) if store.exists else []
    if rules:
        system += "\nProject rules and styles:\n" + "\n".join(
            f"- {r.title}: {r.content[:150]}" for r in rules
        )
    store.close()

    print(f"local-ezai chat — project: {project}")
    print("type a message; /reset clears history, /exit quits")
    messages: list[dict] = [{"role": "system", "content": system}]
    while True:
        try:
            line = input("you> ").strip()
        except EOFError:
            print()
            return 0
        if not line:
            continue
        if line in ("/exit", "/quit"):
            return 0
        if line == "/reset":
            messages = messages[:1]
            print("(history cleared)")
            continue
        messages.append({"role": "user", "content": line})
        try:
            response = llm.chat("chat", messages)
        except LLMError as exc:
            log.error("model unavailable: %s", exc)
            return 3
        reply = response.content or "(no reply)"
        messages.append({"role": "assistant", "content": reply})
        print(f"ezai> {reply}")


def cmd_plan(config: AgentdConfig, project: Path, task: str) -> int:
    from agentd.runner import plan_only

    plan = plan_only(config, project, task)
    print(plan.model_dump_json(indent=2))
    return 0


def cmd_run(config: AgentdConfig, project: Path, args) -> int:
    from agentd.cli import _print_report
    from agentd.runner import execute_run

    if args.push:
        config.git.allow_push = True
    if args.in_place:
        config.workspace.mode = "in-place"
    if args.max_iterations is not None:
        config.limits.max_heal_iterations = max(0, args.max_iterations)
    report = execute_run(config, project, args.task)
    _print_report(report, as_json=args.as_json)
    return 0 if report.status == "completed" else 1


def cmd_code(config: AgentdConfig, project: Path, args) -> int:
    from agentd.cli import _print_report
    from agentd.runner import code_only

    if args.in_place:
        config.workspace.mode = "in-place"
    report = code_only(config, project, args.task)
    _print_report(report, as_json=args.as_json)
    if report.status == "completed":
        where = ("your working tree" if args.in_place
                 else f"workspace {report.workspace_path}")
        print(f"changes are UNCOMMITTED in {where} — review them, then use "
              f"'local-ezai test' and 'local-ezai commit'")
    return 0 if report.status == "completed" else 1


def cmd_test(config: AgentdConfig, project: Path, as_json: bool) -> int:
    from agentd.runner import validate_repo

    report, journal = validate_repo(config, project)
    if as_json:
        print(report.model_dump_json(indent=2))
    else:
        for check in report.checks:
            mark = "PASS" if check.ok else "FAIL"
            print(f"  [{mark}] {check.name:24} {check.command[:70]}")
            if not check.ok and check.output_tail:
                tail = check.output_tail.strip().splitlines()[-3:]
                for line in tail:
                    print(f"         {line[:100]}")
        print(f"\nvalidation: {report.summary}")
        print(f"journal:    {journal.path}")
    return 0 if report.passed else 1


def cmd_fix(config: AgentdConfig, project: Path, args) -> int:
    from agentd.cli import _print_report
    from agentd.runner import heal_run

    if args.max_iterations is not None:
        config.limits.max_heal_iterations = max(0, args.max_iterations)
    report = heal_run(config, project, goal=args.goal)
    _print_report(report, as_json=args.as_json)
    return 0 if report.status == "completed" else 1


def cmd_review(config: AgentdConfig, project: Path, as_json: bool) -> int:
    from agentd.runner import review_repo

    review, journal = review_repo(config, project)
    if as_json:
        print(review.model_dump_json(indent=2))
    else:
        print(f"review: {review.verdict.upper()} — {review.summary}")
        for finding in review.findings:
            location = finding.file + (f":{finding.line}" if finding.line else "")
            print(f"  [{finding.severity:6}] {location}: {finding.issue}")
            if finding.suggestion:
                print(f"           suggestion: {finding.suggestion}")
        print(f"journal: {journal.path}")
    return 0 if review.verdict == "approve" else 1


def cmd_commit(config: AgentdConfig, project: Path, args) -> int:
    from agentd.runner import commit_repo

    if args.push:
        config.git.allow_push = True
    try:
        info, report = commit_repo(config, project, message=args.message)
    except RuntimeError as exc:  # the commit gate
        log.error("%s", exc)
        return 1
    if not info.sha:
        print("nothing to commit — working tree is clean")
        return 0
    push_state = ("pushed" if info.pushed else "not pushed")
    print(f"committed {info.sha[:12]} on {info.branch} ({push_state})")
    print(f"validation: {report.summary}")
    return 0


def cmd_memory(config: AgentdConfig, project: Path, args) -> int:
    from agentd.memory import MemoryStore

    store = MemoryStore(project / config.memory.dir)
    try:
        if args.add:
            record_id = store.record(kind=args.kind,
                                     title=args.add[:60], content=args.add,
                                     run_id="manual")
            store.export_lessons()
            print(f"remembered #{record_id} [{args.kind}] — {store.db_path}")
            return 0
        if not store.exists:
            print("(no memory yet for this project)")
            return 0
        records = (store.search(args.search, limit=args.limit) if args.search
                   else store.recent(limit=args.limit))
        for record in records:
            print(f"#{record.id:<4} {record.created_at}  [{record.kind}]"
                  f"  (run {record.run_id or '-'})")
            print(f"      {record.title}")
        print(f"\n{store.count()} total memories — {store.db_path}")
        return 0
    finally:
        store.close()


def cmd_sprint(config: AgentdConfig, project: Path, args) -> int:
    from agentd.runner import run_sprint
    from agentd.sprint import load_sprint_tasks

    spec = Path(args.spec_file).expanduser()
    if not spec.is_file():
        spec = project / args.spec_file  # allow specs relative to the project
    if not spec.is_file():
        log.error("sprint spec not found: %s", args.spec_file)
        return 2
    try:
        tasks = load_sprint_tasks(spec)
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    if args.push:
        config.git.allow_push = True
    print(f"sprint: {len(tasks)} task(s) from {spec.name}")
    report = run_sprint(config, project, tasks, spec_file=str(spec),
                        keep_going=args.keep_going)
    if args.as_json:
        print(report.model_dump_json(indent=2))
    else:
        for task in report.tasks:
            mark = {"completed": "DONE", "failed": "FAIL",
                    "skipped": "SKIP"}[task.status]
            commit = f" ({task.commit_sha[:10]})" if task.commit_sha else ""
            print(f"  [{mark}] {task.index}. {task.task[:70]}{commit}")
            if task.error:
                print(f"         {task.error[:100]}")
        print(f"\nsprint {report.sprint_id}: {report.status.upper()} — "
              f"{report.completed_count}/{len(report.tasks)} task(s) on "
              f"branch {report.branch}")
        print(f"workspace: {report.workspace_path}")
    return 0 if report.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
