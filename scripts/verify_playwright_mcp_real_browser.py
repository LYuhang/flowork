#!/usr/bin/env python3
"""Retired entry point: Browser acceptance must run through the real Agent."""

import sys


def main() -> int:
    print(
        "RETIRED: the direct Playwright MCP verifier bypassed the application's "
        "Agent and authorization path. Use "
        "scripts/verify_real_sidepanel_browser_agent.py --help instead. "
        "No browser was opened and no acceptance was performed.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
