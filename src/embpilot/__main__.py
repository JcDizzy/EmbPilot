"""
EmbPilot CLI entry point.

Usage:
    embpilot                          # start MCP server with defaults
    embpilot --data-dir ~/embdata    # custom data directory
    embpilot tools                    # list available MCP tools
    embpilot tool connect_serial --json '{"port": "COM3"}'
    embpilot shell                    # interactive REPL
"""

from __future__ import annotations

import sys


def main() -> None:
    from embpilot.cli import main as cli_main

    cli_main(sys.argv[1:])


if __name__ == "__main__":
    main()