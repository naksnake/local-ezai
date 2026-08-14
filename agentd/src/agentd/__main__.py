"""Allow `python -m agentd`."""

from agentd.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
