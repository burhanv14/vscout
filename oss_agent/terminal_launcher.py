"""Terminal launcher for the vscout MCP server.

Double-clicking a platform launcher (``vscout.bat`` / ``vscout.command`` /
``vscout.sh``) opens a terminal window that:

1. Ensures a GitHub token is available (prompts + writes ``.env`` on first run).
2. Starts the MCP server, displaying logs in the terminal.
3. Shuts the server down when the terminal is closed / Ctrl+C is pressed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from oss_agent.config import ensure_github_token, load_config
from oss_agent.mcp_server import main as mcp_main

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    """Ensure token, then run the MCP server in this terminal."""
    # First-time setup: ask for a GitHub token if none is configured.
    ensure_github_token(interactive=True)

    # Load config so the server picks up .env / config.yaml values.
    try:
        load_config()
    except Exception as exc:  # pragma: no cover - config errors are non-fatal here
        print(f"[vscout] Warning: could not load config: {exc}", file=sys.stderr)

    print(
        "[vscout] Starting MCP server at http://127.0.0.1:8000/mcp "
        "(Ctrl+C or close this window to stop)",
        file=sys.stderr,
    )
    mcp_main([])


if __name__ == "__main__":
    main()
