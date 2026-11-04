"""MCP server for the OSS-Contributor-Agent using FastMCP.

Exposes the agent's issue discovery and evaluation capabilities as MCP tools
over stdio transport. The connected AI client supplies the LLM and handles
reasoning; this server executes operations and returns structured results.
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any

from fastmcp import FastMCP

# Configure logging to stderr ONLY — stdout is reserved for MCP protocol messages.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("oss_agent.mcp_server")

from oss_agent.config import AppConfig, load_config, get_project_root
from oss_agent.github import GitHubClient
from oss_agent.state import StateStore
from oss_agent.stages.stage1_repo_discovery import RepoDiscoveryStage
from oss_agent.stages.stage2_issue_discovery import IssueDiscoveryStage
from oss_agent.stages.stage3_worth_it import WorthItAnalysisStage, IssueAnalysisData

# ---------------------------------------------------------------------------
# Core application (shared between CLI and MCP)
# ---------------------------------------------------------------------------

_CONFIG: AppConfig | None = None
_STATE: StateStore | None = None
_GITHUB: GitHubClient | None = None


def _get_app() -> tuple[AppConfig, StateStore, GitHubClient]:
    """Lazily initialize and return the shared app components.

    Each component is initialized independently so that a failure in one
    (e.g. StateStore) doesn't leave the others stuck at None on retry.
    """
    global _CONFIG, _STATE, _GITHUB
    if _CONFIG is None:
        _CONFIG = load_config()
    if _STATE is None:
        _STATE = StateStore()
    if _GITHUB is None:
        _GITHUB = GitHubClient(_CONFIG)
    return _CONFIG, _STATE, _GITHUB


def _close_app() -> None:
    """Close shared resources such as the HTTP client on shutdown."""
    global _GITHUB
    if _GITHUB is not None:
        _GITHUB.http_client.close()


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

server = FastMCP(
    name="oss-contributor-agent-mcp",
    version="0.2.0",
    instructions="""
OSS-Contributor-Agent MCP Server

This server exposes open-source issue discovery and evaluation capabilities
as MCP tools. The connected AI client supplies the LLM and handles reasoning,
planning, and tool selection. This server executes operations and returns
structured results.

Available tools:
- discover_repositories: Search GitHub for candidate repositories
- discover_issues: Find unassigned candidate issues with preferred labels
- analyze_issue: Gather comprehensive issue context for triage
- get_capabilities: List available tools and schemas
- get_state: Inspect persistent state store
""",
)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

@server.tool()
def discover_repositories(
    max_repos: int = 6,
    tech_stack: list[str] | None = None,
) -> str:
    """Search GitHub for public repositories matching the configured tech stack.

    Returns a list of candidate repositories with basic metadata (stars, language,
    open issues, etc.). Ranking/recommendation is left to the AI client; this tool
    only gathers and filters data.
    """
    try:
        max_repos = int(max_repos)
        if max_repos < 1 or max_repos > 20:
            max_repos = min(max(max_repos, 1), 20)
    except (TypeError, ValueError):
        max_repos = 6

    config, state, github = _get_app()
    stage1 = RepoDiscoveryStage(config, github, state)
    repos = stage1.run(max_repos_to_rank=max_repos, tech_stack=tech_stack)

    return json.dumps(
        {
            "operation": "discover_repositories",
            "count": len(repos),
            "repositories": repos,
            "note": "Ranking and recommendations are the responsibility of the AI client.",
        },
        indent=2,
        default=str,
    )


@server.tool()
def discover_issues(
    repos: list[str] | None = None,
    max_issues_per_repo: int = 5,
    preferred_labels: list[str] | None = None,
) -> str:
    """Find unassigned candidate issues with preferred labels across discovered repositories.

    Returns a list of unassigned, unlocked, non-PR issues ready for evaluation.
    Requires repositories to have been discovered first (or pass repo names directly).
    """
    try:
        max_issues_per_repo = int(max_issues_per_repo)
        if max_issues_per_repo < 1 or max_issues_per_repo > 20:
            max_issues_per_repo = min(max(max_issues_per_repo, 1), 20)
    except (TypeError, ValueError):
        max_issues_per_repo = 5

    config, state, github = _get_app()

    if repos is None:
        # Use repositories from the last discovery checkpoint
        checkpoint = state.get_checkpoint("stage1_repos")
        if checkpoint and "repos" in checkpoint:
            repos = [r["repo_full_name"] for r in checkpoint["repos"]]
        else:
            return json.dumps(
                {
                    "error": "No repositories provided and no prior discovery found. "
                    "Run discover_repositories first or pass repos explicitly.",
                },
                indent=2,
            )

    stage2 = IssueDiscoveryStage(config, github, state)
    issues = stage2.run(
        [{"repo_full_name": r} for r in repos],
        max_issues_per_repo=max_issues_per_repo,
        preferred_labels=preferred_labels,
    )

    return json.dumps(
        {
            "operation": "discover_issues",
            "count": len(issues),
            "issues": issues,
        },
        indent=2,
        default=str,
    )


@server.tool()
def analyze_issue(issue_url: str) -> str:
    """Gather comprehensive context for a specific GitHub issue.

    Returns structured data for the AI client to perform its own triage/worth-it
    reasoning. Does not make a triage decision.
    """
    if not issue_url or not isinstance(issue_url, str):
        return json.dumps({"error": "issue_url is required and must be a string"}, indent=2)

    config, state, github = _get_app()

    # Parse issue URL using existing CLI utility
    from oss_agent.cli import parse_issue_url

    try:
        repo_full_name, issue_number = parse_issue_url(issue_url)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)

    owner, repo_name = repo_full_name.split("/", 1)

    try:
        raw_issue = github.get_issue(owner, repo_name, issue_number)
    except Exception as exc:
        logger.exception("Failed to fetch issue %s#%s", repo_full_name, issue_number)
        return json.dumps({"error": f"Failed to fetch issue: {exc}"}, indent=2)

    labels = [l["name"] for l in raw_issue.get("labels", []) if isinstance(l, dict)]
    issue_dict = {
        "repo_full_name": repo_full_name,
        "issue_number": issue_number,
        "title": raw_issue.get("title", f"Issue #{issue_number}"),
        "author": raw_issue.get("user", {}).get("login", "unknown"),
        "url": raw_issue.get("html_url", issue_url),
        "labels": labels,
        "assignees": [
            a.get("login", "unknown")
            for a in raw_issue.get("assignees", [])
            if isinstance(a, dict)
        ],
        "body": raw_issue.get("body", "") or "",
        "created_at": raw_issue.get("created_at", ""),
        "updated_at": raw_issue.get("updated_at", ""),
    }

    stage3 = WorthItAnalysisStage(config, github, state)
    try:
        analysis = stage3.analyze_issue(issue_dict)
    except Exception as exc:
        logger.exception("Failed to analyze issue %s#%s", repo_full_name, issue_number)
        return json.dumps({"error": f"Failed to analyze issue: {exc}"}, indent=2)

    return json.dumps(
        {
            "operation": "analyze_issue",
            "issue": analysis.model_dump(),
            "note": "Triage decision (worth score, status, etc.) is the responsibility of the AI client.",
        },
        indent=2,
        default=str,
    )


@server.tool()
def get_capabilities() -> str:
    """List all available MCP tools with descriptions and parameter schemas.

    Use this to understand what operations the server can perform.
    """
    tools_info = [
        {
            "name": "discover_repositories",
            "description": "Search GitHub for public repositories matching the configured tech stack. "
            "Returns a list of candidate repositories with basic metadata.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "max_repos": {
                        "type": "integer",
                        "description": "Maximum number of repositories to return (default: 6, max: 20)",
                        "default": 6,
                        "minimum": 1,
                        "maximum": 20,
                    },
                    "tech_stack": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tech stack keywords to search for (overrides config.yaml tech_stack). e.g. [\"java\", \"spring-boot\", \"gradle\"]",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "discover_issues",
            "description": "Find unassigned candidate issues with preferred labels across "
            "discovered repositories.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "repos": {
                        "type": "array",
                        "description": "List of repository full names (owner/repo). "
                        "If omitted, uses repositories from the last discover_repositories call.",
                        "items": {"type": "string"},
                    },
                    "max_issues_per_repo": {
                        "type": "integer",
                        "description": "Maximum issues per repository (default: 5)",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 20,
                    },
                    "preferred_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional preferred labels to filter issues (overrides config.yaml preferred_labels). e.g. [\"good first issue\", \"help wanted\"]",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "analyze_issue",
            "description": "Gather comprehensive context for a specific GitHub issue: "
            "details, comments, timeline, linked PRs, referencing commits.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "issue_url": {
                        "type": "string",
                        "description": "GitHub issue URL "
                        "(e.g. https://github.com/owner/repo/issues/123) "
                        "or shorthand (owner/repo#123)",
                    },
                },
                "required": ["issue_url"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_capabilities",
            "description": "List all available MCP tools with descriptions and parameter schemas.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "get_state",
            "description": "Inspect the persistent state store: evaluated repositories, "
            "analyzed issues, and pipeline checkpoints.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "include_repos": {
                        "type": "boolean",
                        "description": "Include evaluated repository records (default: true)",
                        "default": True,
                    },
                    "include_issues": {
                        "type": "boolean",
                        "description": "Include analyzed issue records (default: true)",
                        "default": True,
                    },
                },
                "additionalProperties": False,
            },
        },
    ]

    return json.dumps(
        {
            "operation": "get_capabilities",
            "server": "oss-contributor-agent-mcp",
            "version": "0.2.0",
            "tools": tools_info,
        },
        indent=2,
    )


@server.tool()
def get_state(
    include_repos: bool = True,
    include_issues: bool = True,
) -> str:
    """Inspect the persistent state store: evaluated repositories, analyzed issues,
    and pipeline checkpoints. Useful for understanding what has been discovered so far."""
    config, state, github = _get_app()

    result: dict[str, Any] = {"operation": "get_state"}

    if include_repos:
        try:
            repo_names = state.get_evaluated_repo_names()
            result["evaluated_repos"] = repo_names
        except Exception as exc:
            result["evaluated_repos_error"] = str(exc)

    if include_issues:
        try:
            checkpoint = state.get_checkpoint("stage3_analyzed_issues")
            result["analyzed_issues"] = checkpoint or {}
        except Exception as exc:
            result["analyzed_issues_error"] = str(exc)

    # Also include stage1 and stage2 checkpoints
    result["checkpoints"] = {
        "stage1_cursor": state.get_checkpoint("stage1_cursor"),
        "stage1_repos": state.get_checkpoint("stage1_repos"),
        "stage2_issues": state.get_checkpoint("stage2_issues"),
        "stage3_analyzed_issues": state.get_checkpoint("stage3_analyzed_issues"),
    }

    return json.dumps(result, indent=2, default=str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _windows_ancestor_pids(max_depth: int = 4) -> list[int]:
    """Return PIDs of this process's ancestors (parent, grandparent, ...).

    The ``vscout-mcp`` console script is a launcher .exe that spawns the real
    python.exe, so the direct parent is only a shim; the client/terminal that
    actually dies is further up the chain. We watch every ancestor.
    """
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return []
    procs: dict[int, tuple[int, str]] = {}
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            procs[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)

    # Never watch system/shell-root processes that outlive terminals.
    ignore = {"explorer.exe", "services.exe", "wininit.exe", "winlogon.exe",
              "svchost.exe", "csrss.exe", "smss.exe", "system"}
    chain: list[int] = []
    pid = os.getpid()
    for _ in range(max_depth):
        info = procs.get(pid)
        if not info:
            break
        ppid = info[0]
        pinfo = procs.get(ppid)
        if not pinfo or pinfo[1] in ignore:
            break
        chain.append(ppid)
        pid = ppid
    return chain


def _start_parent_watchdog() -> None:
    """Exit this process when the client/terminal that launched it goes away.

    stdio servers get orphaned when the terminal is closed without stdin
    reaching EOF (notably on Windows, where a launcher shim sits between the
    client and python.exe). A daemon thread waits for any ancestor to die and
    then force-exits. It never reads stdin, so it can't steal protocol bytes.
    """
    import threading
    import time

    parent_pid = os.getppid()
    if parent_pid <= 1:
        return

    def _watch() -> None:
        try:
            if sys.platform == "win32":
                import ctypes

                SYNCHRONIZE = 0x00100000
                kernel32 = ctypes.windll.kernel32
                kernel32.OpenProcess.restype = ctypes.c_void_p
                handles = []
                for pid in _windows_ancestor_pids() or [parent_pid]:
                    h = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
                    if h:
                        handles.append(h)
                if not handles:
                    return
                arr = (ctypes.c_void_p * len(handles))(*handles)
                # bWaitAll=False: wake as soon as ANY ancestor exits.
                kernel32.WaitForMultipleObjects(len(handles), arr, False, 0xFFFFFFFF)
            else:
                while os.getppid() == parent_pid:
                    time.sleep(1.0)
        except Exception:
            return
        logger.info("Parent process exited; shutting down MCP server")
        os._exit(0)

    threading.Thread(target=_watch, name="parent-watchdog", daemon=True).start()


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def main(argv: list[str] | None = None) -> None:
    """Entry point for the vscout-mcp console script.

    Default transport is stdio (client launches the server). With ``--http`` the
    server listens on ``http://HOST:PORT/mcp`` so it lives in the terminal that
    started it and stops when that terminal closes.

    On Windows, installs a console control handler so that CTRL-C / CTRL-BREAK /
    window close exits cleanly with code 0 (fastmcp does not install one itself).
    On Unix, asyncio's default signal handling raises KeyboardInterrupt.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="vscout-mcp")
    parser.add_argument("--http", action="store_true",
                        help="serve over streamable HTTP instead of stdio")
    parser.add_argument("--host", default=os.environ.get("VSCOUT_MCP_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("VSCOUT_MCP_PORT", DEFAULT_PORT)))
    args = parser.parse_args(argv)

    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32

            def _ctrl_handler(ctrl_type: int) -> bool:
                # CTRL_C, CTRL_BREAK, CTRL_CLOSE, CTRL_LOGOFF, CTRL_SHUTDOWN
                os._exit(0)

            _handler = ctypes.WINFUNCTYPE(
                ctypes.c_bool, ctypes.c_uint
            )(_ctrl_handler)
            kernel32.SetConsoleCtrlHandler(_handler, True)
        except Exception:
            logger.debug("Could not install Windows console control handler")

    _start_parent_watchdog()

    try:
        if args.http:
            logger.info("Serving MCP over HTTP at http://%s:%s/mcp", args.host, args.port)
            asyncio.run(server.run_http_async(
                transport="http", host=args.host, port=args.port, show_banner=False))
        else:
            asyncio.run(server.run_stdio_async(show_banner=False))
    except KeyboardInterrupt:
        logger.info("MCP server stopped by user")
    except Exception:
        logger.exception("MCP server failed to start")
        sys.exit(1)
    finally:
        _close_app()


if __name__ == "__main__":
    main()
