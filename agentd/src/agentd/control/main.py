"""``ezaid`` — command-line entry point of the control plane (PR-8)::

    ezaid                       serve (host/port/token from the agentd config
                                or EZAI_CONTROL_TOKEN / EZAI_CONTROL_PORT)
    ezaid --print-spec          the OpenAPI document (needs no platform/token)
    ezaid --write-spec PATH     regenerate the contract artifact
    ezaid --version

Serving requires the ``agentd[control]`` extra and a platform (found like
the CLI finds it: ``platform.config_dir`` / ``AGENTD_PLATFORM__CONFIG_DIR`` /
walk-up from the working directory). No token → refuses to start with the
fix printed (fail closed).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentd import __version__
from agentd.config import load_config
from agentd.control import CONTRACT_VERSION, PORT_ENV, SPEC_ARTIFACT, TOKEN_ENV
from agentd.logging_setup import get_logger, setup_logging
from agentd.platform_cli import PlatformError, build_context

log = get_logger("ezaid")

EXTRA_FIX = "install the control extra:  pip install -e './agentd[control]'  (or: make swe-install)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ezaid", description="Local-EZAI Platform Control Plane (OpenAPI, service-token "
                                  "authenticated, single audit log)")
    parser.add_argument("--config", help="agentd config file (default: AGENTD_CONFIG)")
    parser.add_argument("--platform", metavar="CONFIG_DIR",
                        help="the platform's config/ directory (default: discovery)")
    parser.add_argument("--host", help="bind address (default: control.host)")
    parser.add_argument("--port", type=int, help=f"bind port (default: control.port / {PORT_ENV})")
    parser.add_argument("--print-spec", action="store_true",
                        help="print the OpenAPI document and exit")
    parser.add_argument("--write-spec", metavar="PATH", nargs="?", const=SPEC_ARTIFACT,
                        help=f"write the OpenAPI document (default {SPEC_ARTIFACT}) and exit")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"ezaid {__version__} · contract {CONTRACT_VERSION}")
        return 0
    try:
        from agentd.control.app import create_app, spec_json
    except ImportError as exc:  # fastapi missing: direct mode never needs it
        print(f"ezaid: the control plane needs FastAPI ({exc}) — {EXTRA_FIX}", file=sys.stderr)
        return 2

    config = load_config(args.config)
    if args.platform:
        config.platform.config_dir = Path(args.platform)
    if args.host:
        config.control.host = args.host
    if args.port:
        config.control.port = args.port

    if args.print_spec or args.write_spec:
        settings = config.control.model_copy(update={"token": config.control.token or "spec"})
        text = spec_json(create_app(settings))
        if args.write_spec:
            target = Path(args.write_spec)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            print(f"wrote {target} (contract {CONTRACT_VERSION})")
        else:
            print(text, end="")
        return 0

    setup_logging(config.log_level)
    if not config.control.token:
        log.error("no service token configured — set %s in .env (compose) or control.token in "
                  "the agentd config; ezaid does not start without one", TOKEN_ENV)
        return 2
    try:
        ctx = build_context(config, Path.cwd(), actor="ezaid")
    except PlatformError as exc:
        log.error("%s", exc)
        return 2
    try:
        import uvicorn
    except ImportError as exc:
        log.error("uvicorn missing (%s) — %s", exc, EXTRA_FIX)
        return 2
    app = create_app(config.control, ctx)
    uvicorn.run(app, host=config.control.host, port=config.control.port,
                log_level=config.log_level.lower())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
