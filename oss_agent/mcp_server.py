"""MCP server for the OSS-Contributor-Agent.

Exposes the agent's issue discovery and evaluation capabilities as MCP tools
over stdio transport. The connected AI client supplies the LLM and handles
reasoning; this server executes operations and returns structured results.
"""

import asyncio
import codecs
from contextlib import suppress
import json
import logging
import os
import queue
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Callable

# Configure logging to stderr ONLY — stdout is reserved for MCP protocol messages.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("oss_agent.mcp_server")

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

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
    """Lazily initialize and return the shared app components."""
    global _CONFIG, _STATE, _GITHUB
    if _CONFIG is None:
        _CONFIG = load_config()
        _STATE = StateStore()
        _GITHUB = GitHubClient(_CONFIG)
    return _CONFIG, _STATE, _GITHUB


def _close_app() -> None:
    """Close shared resources such as the HTTP client on shutdown."""
    global _GITHUB
    if _GITHUB is not None:
        with suppress(Exception):
            _GITHUB.http_client.close()


async def _run_in_daemon_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking callable on a daemon thread so the event loop stays responsive to signals."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()

    def _target() -> None:
        try:
            res = func(*args, **kwargs)
        except BaseException as exc:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(
                    lambda: fut.set_exception(exc) if not fut.done() else None
                )
        else:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(
                    lambda: fut.set_result(res) if not fut.done() else None
                )

    worker = threading.Thread(target=_target, name="mcp-tool-worker", daemon=True)
    worker.start()
    return await fut


# ---------------------------------------------------------------------------
# MCP tool definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[Tool] = [
    Tool(
        name="discover_repositories",
        description="""Search GitHub for public repositories matching the configured tech stack.
        Returns a list of candidate repositories with basic metadata (stars, language, open issues, etc.).
        Ranking/recommendation is left to the AI client; this tool only gathers and filters data.""",
        input_schema={
            "type": "object",
            "properties": {
                "max_repos": {
                    "type": "integer",
                    "description": "Maximum number of repositories to return (default: 6, max: 20)",
                    "default": 6,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="discover_issues",
        description="""Find unassigned candidate issues with preferred labels across discovered repositories.
        Returns a list of unassigned, unlocked, non-PR issues ready for evaluation.
        Requires repositories to have been discovered first (or pass repo names directly).""",
        input_schema={
            "type": "object",
            "properties": {
                "repos": {
                    "type": "array",
                    "description": "List of repository full names (owner/repo). If omitted, uses repositories from the last discover_repositories call.",
                    "items": {"type": "string"},
                },
                "max_issues_per_repo": {
                    "type": "integer",
                    "description": "Maximum issues per repository (default: 5)",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="analyze_issue",
        description="""Gather comprehensive context for a specific GitHub issue:
        issue details, comments, timeline events, linked pull requests, and referencing commits.
        Returns structured data for the AI client to perform its own triage/worth-it reasoning.
        Does not make a triage decision.""",
        input_schema={
            "type": "object",
            "properties": {
                "issue_url": {
                    "type": "string",
                    "description": "GitHub issue URL (e.g. https://github.com/owner/repo/issues/123) or shorthand (owner/repo#123)",
                },
            },
            "required": ["issue_url"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_capabilities",
        description="""List all available MCP tools with descriptions and parameter schemas.
        Use this to understand what operations the server can perform.""",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_state",
        description="""Inspect the persistent state store: evaluated repositories, analyzed issues,
        and pipeline checkpoints. Useful for understanding what has been discovered so far.""",
        input_schema={
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
    ),
]


# ---------------------------------------------------------------------------
# Tool implementations (thin wrappers around existing Python logic)
# ---------------------------------------------------------------------------

async def _discover_repositories(arguments: dict[str, Any]) -> CallToolResult:
    try:
        max_repos = int(arguments.get("max_repos", 6))
        if max_repos < 1 or max_repos > 20:
            max_repos = min(max(max_repos, 1), 20)
    except (TypeError, ValueError):
        max_repos = 6

    def _run_stage1() -> list[dict[str, Any]]:
        config, state, github = _get_app()
        stage1 = RepoDiscoveryStage(config, github, state)
        return stage1.run(max_repos_to_rank=max_repos)

    repos = await _run_in_daemon_thread(_run_stage1)

    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "operation": "discover_repositories",
                        "count": len(repos),
                        "repositories": repos,
                        "note": "Ranking and recommendations are the responsibility of the AI client.",
                    },
                    indent=2,
                    default=str,
                ),
            )
        ],
    )


async def _discover_issues(arguments: dict[str, Any]) -> CallToolResult:
    repos = arguments.get("repos")
    max_issues_per_repo = int(arguments.get("max_issues_per_repo", 5))
    if max_issues_per_repo < 1 or max_issues_per_repo > 20:
        max_issues_per_repo = min(max(max_issues_per_repo, 1), 20)

    config, state, github = _get_app()

    if repos is None:
        # Use repositories from the last discovery checkpoint
        checkpoint = state.get_checkpoint("stage1_repos")
        if checkpoint and "repos" in checkpoint:
            repos = [r["repo_full_name"] for r in checkpoint["repos"]]
        else:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "No repositories provided and no prior discovery found. "
                                "Run discover_repositories first or pass repos explicitly.",
                            },
                            indent=2,
                        ),
                    )
                ],
            )

    def _run_stage2() -> list[dict[str, Any]]:
        stage2 = IssueDiscoveryStage(config, github, state)
        return stage2.run([{"repo_full_name": r} for r in repos], max_issues_per_repo=max_issues_per_repo)

    issues = await _run_in_daemon_thread(_run_stage2)

    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "operation": "discover_issues",
                        "count": len(issues),
                        "issues": issues,
                    },
                    indent=2,
                    default=str,
                ),
            )
        ],
    )


async def _analyze_issue(arguments: dict[str, Any]) -> CallToolResult:
    issue_url = arguments.get("issue_url")
    if not issue_url or not isinstance(issue_url, str):
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"error": "issue_url is required and must be a string"},
                        indent=2,
                    ),
                )
            ],
        )

    config, state, github = _get_app()

    # Parse issue URL using existing CLI utility
    from oss_agent.cli import parse_issue_url

    try:
        repo_full_name, issue_number = parse_issue_url(issue_url)
    except ValueError as exc:
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps({"error": str(exc)}, indent=2),
                )
            ],
        )

    owner, repo_name = repo_full_name.split("/", 1)

    try:
        raw_issue = await _run_in_daemon_thread(github.get_issue, owner, repo_name, issue_number)
    except Exception as exc:
        logger.exception("Failed to fetch issue %s#%s", repo_full_name, issue_number)
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"error": f"Failed to fetch issue: {exc}"},
                        indent=2,
                    ),
                )
            ],
        )

    labels = [l["name"] for l in raw_issue.get("labels", []) if isinstance(l, dict)]
    issue_dict = {
        "repo_full_name": repo_full_name,
        "issue_number": issue_number,
        "title": raw_issue.get("title", f"Issue #{issue_number}"),
        "author": raw_issue.get("user", {}).get("login", "unknown"),
        "url": raw_issue.get("html_url", issue_url),
        "labels": labels,
        "assignees": [a.get("login", "unknown") for a in raw_issue.get("assignees", []) if isinstance(a, dict)],
        "body": raw_issue.get("body", "") or "",
        "created_at": raw_issue.get("created_at", ""),
        "updated_at": raw_issue.get("updated_at", ""),
    }

    stage3 = WorthItAnalysisStage(config, github, state)
    try:
        analysis = await _run_in_daemon_thread(stage3.analyze_issue, issue_dict)
    except Exception as exc:
        logger.exception("Failed to analyze issue %s#%s", repo_full_name, issue_number)
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"error": f"Failed to analyze issue: {exc}"},
                        indent=2,
                    ),
                )
            ],
        )

    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "operation": "analyze_issue",
                        "issue": analysis.model_dump(),
                        "note": "Triage decision (worth score, status, etc.) is the responsibility of the AI client.",
                    },
                    indent=2,
                    default=str,
                ),
            )
        ],
    )


async def _get_capabilities(arguments: dict[str, Any]) -> CallToolResult:
    tools_info = []
    for tool in TOOL_DEFINITIONS:
        tools_info.append({
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        })

    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "operation": "get_capabilities",
                        "server": "oss-contributor-agent-mcp",
                        "version": "0.2.0",
                        "tools": tools_info,
                    },
                    indent=2,
                ),
            )
        ],
    )


async def _get_state(arguments: dict[str, Any]) -> CallToolResult:
    config, state, github = _get_app()
    include_repos = arguments.get("include_repos", True)
    include_issues = arguments.get("include_issues", True)

    result: dict[str, Any] = {"operation": "get_state"}

    if include_repos:
        try:
            repo_names = state.get_evaluated_repo_names()
            repos = []
            for name in repo_names:
                # We don't have a direct getter for full repo records; return names
                repos.append(name)
            result["evaluated_repos"] = repos
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

    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(result, indent=2, default=str),
            )
        ],
    )


_TOOL_HANDLERS = {
    "discover_repositories": _discover_repositories,
    "discover_issues": _discover_issues,
    "analyze_issue": _analyze_issue,
    "get_capabilities": _get_capabilities,
    "get_state": _get_state,
}


# ---------------------------------------------------------------------------
# MCP server construction
# ---------------------------------------------------------------------------

def create_server() -> Server:
    """Create and configure the MCP server instance."""
    server = Server(
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

    async def _handle_list_tools(
        request_context, request_params: PaginatedRequestParams | None  # type: ignore[valid-type]
    ) -> ListToolsResult:
        return ListToolsResult(tools=TOOL_DEFINITIONS)

    async def _handle_call_tool(
        request_context, request_params: CallToolRequestParams  # type: ignore[valid-type]
    ) -> CallToolResult:
        tool_name = request_params.name
        arguments = request_params.arguments or {}

        handler = _TOOL_HANDLERS.get(tool_name)
        if handler is None:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps({"error": f"Unknown tool: {tool_name}"}, indent=2),
                    )
                ],
                isError=True,
            )

        try:
            return await handler(arguments)
        except Exception as exc:
            logger.exception("Tool %s execution failed", tool_name)
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"error": f"Tool execution failed: {exc}"},
                            indent=2,
                        ),
                    )
                ],
                isError=True,
            )

    server.add_request_handler("tools/list", PaginatedRequestParams, _handle_list_tools)
    server.add_request_handler("tools/call", CallToolRequestParams, _handle_call_tool)

    return server


# ---------------------------------------------------------------------------
# Entry point & interruptible stdio transport
# ---------------------------------------------------------------------------

class _InterruptibleStdin:
    """Async line reader for stdin backed by a single daemon thread using raw ``os.read``.

    Why this is needed:
    1. ``mcp.server.stdio.stdio_server()`` wraps ``sys.stdin`` using
       ``anyio.wrap_file()``, whose ``readline()`` calls
       ``anyio.to_thread.run_sync(fp.readline, abandon_on_cancel=False)``.
    2. In AnyIO's asyncio backend, ``abandon_on_cancel=False`` wraps the worker
       future inside ``CancelScope(shield=True)`` and executes ``fp.readline()``
       on a non-daemon ``AnyIO worker thread``.
    3. When ``Ctrl+C`` or console close occurs while ``stdin`` has not sent EOF
       (e.g. interactive terminal ``CONIN$``), ``stdio_server()``'s ``TaskGroup``
       cannot cancel the shielded ``readline()`` call, and Python's interpreter
       shutdown (``threading._shutdown()``) blocks forever joining the non-daemon
       worker thread, leaving ``vscout-mcp`` running in the background.
    4. Reading via raw ``os.read(fd, ...)`` instead of ``sys.stdin.buffer``
       (``_io.BufferedReader``) avoids holding the C ``BufferedReader`` lock on
       ``<stdin>``, which would otherwise cause CPython's ``Py_FinalizeEx`` to
       abort with ``Fatal Python error: _enter_buffered_busy`` at exit.
    """

    def __init__(self, fd: int, loop: asyncio.AbstractEventLoop) -> None:
        self._fd = fd
        self._loop = loop
        self._closed = False
        self._eof = False
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._request_queue: queue.Queue[asyncio.Future[str] | None] = queue.Queue()
        self._current_future: asyncio.Future[str] | None = None
        self._thread = threading.Thread(
            target=self._reader_loop,
            name="mcp-stdin-reader",
            daemon=True,
        )
        self._thread.start()

    def _read_next_line(self) -> str:
        while "\n" not in self._buffer and not self._eof and not self._closed:
            try:
                chunk = os.read(self._fd, 4096)
            except BaseException:
                chunk = b""
            if not chunk:
                self._eof = True
                self._buffer += self._decoder.decode(b"", final=True)
                break
            self._buffer += self._decoder.decode(chunk, final=False)

        if not self._buffer:
            return ""

        newline_idx = self._buffer.find("\n")
        if newline_idx != -1:
            line = self._buffer[: newline_idx + 1]
            self._buffer = self._buffer[newline_idx + 1 :]
            return line

        line = self._buffer
        self._buffer = ""
        return line

    def _reader_loop(self) -> None:
        while True:
            fut = self._request_queue.get()
            if fut is None or self._closed:
                break
            line = self._read_next_line()
            try:
                self._loop.call_soon_threadsafe(self._deliver_line, fut, line)
            except RuntimeError:
                break
            if not line:
                break

    @staticmethod
    def _deliver_line(fut: asyncio.Future[str], line: str) -> None:
        if not fut.done():
            fut.set_result(line)

    async def readline(self) -> str:
        if self._closed:
            return ""
        fut: asyncio.Future[str] = self._loop.create_future()
        self._current_future = fut
        self._request_queue.put_nowait(fut)
        try:
            return await fut
        finally:
            if self._current_future is fut:
                self._current_future = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._request_queue.put_nowait(None)
        if self._current_future is not None and not self._current_future.done():
            self._current_future.set_result("")

        # On Windows, cancel any pending synchronous ReadFile on the underlying
        # console/pipe handle so the reader thread unblocks cleanly.
        if sys.platform == "win32":
            try:
                import ctypes
                import msvcrt

                kernel32 = ctypes.windll.kernel32
                if self._thread.ident is not None:
                    # THREAD_ALL_ACCESS = 0x1FFFFF
                    h_thread = kernel32.OpenThread(0x1FFFFF, False, int(self._thread.ident))
                    if h_thread:
                        kernel32.CancelSynchronousIo(ctypes.c_void_p(h_thread))
                        kernel32.CloseHandle(ctypes.c_void_p(h_thread))
                handle = msvcrt.get_osfhandle(self._fd)
                kernel32.CancelIoEx(ctypes.c_void_p(handle), None)
            except Exception:
                pass

    async def aclose(self) -> None:
        self.close()

    def __aiter__(self) -> "_InterruptibleStdin":
        return self

    async def __anext__(self) -> str:
        line = await self.readline()
        if line:
            return line
        raise StopAsyncIteration


async def _run_server(shutdown_complete: threading.Event | None = None) -> None:
    """Run the MCP stdio server with clean shutdown on signals / console close."""
    server = create_server()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    try:
        stdin_fd = sys.stdin.fileno()
    except Exception:
        stdin_fd = 0
    stdin_stream = _InterruptibleStdin(stdin_fd, loop)

    def _request_stop() -> None:
        if not stop_event.is_set():
            logger.info("Shutdown requested, stopping MCP server.")
            stop_event.set()
        stdin_stream.close()

    def _request_stop_threadsafe() -> None:
        """Thread-safe stop trigger for OS signal and Windows console handlers."""
        try:
            loop.call_soon_threadsafe(_request_stop)
        except RuntimeError:
            pass

    def _start_force_exit_watchdog(timeout_sec: float = 1.5) -> None:
        """Ensure the process cannot linger in the background if teardown stalls."""
        def _watchdog() -> None:
            if shutdown_complete is not None and shutdown_complete.wait(timeout=timeout_sec):
                return
            os._exit(0)

        threading.Thread(target=_watchdog, name="mcp-shutdown-watchdog", daemon=True).start()

    # asyncio-native signal handlers (Unix) with standard signal.signal fallback (Windows).
    for sig_name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, ValueError, RuntimeError):
            with suppress(ValueError, OSError):
                signal.signal(
                    sig,
                    lambda _signum, _frame: (
                        _request_stop_threadsafe(),
                        _start_force_exit_watchdog(),
                    ),
                )

    # Windows console control handler (Ctrl+C, Ctrl+Break, window close, logoff, shutdown).
    # Windows invokes this callback on a separate OS thread (CtrlRoutine), so we MUST
    # use loop.call_soon_threadsafe() to wake the ProactorEventLoop.
    win_handler_ref = None
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32

            def _ctrl_handler(ctrl_type: int) -> bool:
                _request_stop_threadsafe()
                _start_force_exit_watchdog(timeout_sec=1.0)
                # For CTRL_CLOSE_EVENT (2), CTRL_LOGOFF_EVENT (5), CTRL_SHUTDOWN_EVENT (6),
                # wait briefly so graceful cleanup completes before Windows tears down the console.
                if ctrl_type in (2, 5, 6) and shutdown_complete is not None:
                    shutdown_complete.wait(timeout=0.8)
                return True

            win_handler_ref = ctypes.WINFUNCTYPE(
                ctypes.c_bool, ctypes.c_uint
            )(_ctrl_handler)
            kernel32.SetConsoleCtrlHandler(win_handler_ref, True)
        except Exception:
            logger.debug("Could not install Windows console control handler")

    try:
        async with stdio_server(stdin=stdin_stream) as (read_stream, write_stream):  # type: ignore[arg-type]
            server_task = asyncio.create_task(
                server.run(
                    read_stream,
                    write_stream,
                    server.create_initialization_options(),
                    raise_exceptions=False,
                )
            )
            stop_task = asyncio.create_task(stop_event.wait())

            _, pending = await asyncio.wait(
                {server_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Ensure stdin reader is unblocked and cancel whichever task is still running.
            stdin_stream.close()
            for task in pending:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task

            # Explicitly close streams so stdio_server's internal TaskGroup
            # (stdin_reader and stdout_writer) finishes immediately.
            with suppress(Exception):
                await read_stream.aclose()
            with suppress(Exception):
                await write_stream.aclose()
    finally:
        stdin_stream.close()
        _close_app()
        if win_handler_ref is not None:
            with suppress(Exception):
                import ctypes

                ctypes.windll.kernel32.SetConsoleCtrlHandler(win_handler_ref, False)
        if shutdown_complete is not None:
            shutdown_complete.set()


def main() -> None:
    """Entry point for the vscout-mcp console script."""
    shutdown_complete = threading.Event()
    try:
        asyncio.run(_run_server(shutdown_complete))
    except KeyboardInterrupt:
        logger.info("MCP server stopped by user")
        sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        logger.exception("MCP server failed to start")
        sys.exit(1)
    finally:
        shutdown_complete.set()
        with suppress(Exception):
            sys.stdout.flush()
        with suppress(Exception):
            sys.stderr.flush()


if __name__ == "__main__":
    main()
