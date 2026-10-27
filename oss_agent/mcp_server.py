"""MCP server for the OSS-Contributor-Agent.

Exposes the agent's issue discovery and evaluation capabilities as MCP tools
over stdio transport. The connected AI client (Claude Code, Codex, OpenCode,
etc.) supplies the LLM and handles reasoning; this server executes operations
and returns structured results.

No OpenCode instance or LLM API key is required.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

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
        Does NOT make an LLM triage decision.""",
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

    config, state, github = _get_app()
    stage1 = RepoDiscoveryStage(config, github, state)
    repos = stage1.run(max_repos_to_rank=max_repos)

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

    stage2 = IssueDiscoveryStage(config, github, state)
    issues = stage2.run([{"repo_full_name": r} for r in repos], max_issues_per_repo=max_issues_per_repo)

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
        raw_issue = github.get_issue(owner, repo_name, issue_number)
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
        analysis = stage3.analyze_issue(issue_dict)
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
as MCP tools. The connected AI client (Claude Code, Codex, OpenCode, etc.)
supplies the LLM and handles reasoning, planning, and tool selection. This
server executes operations and returns structured results.

No OpenCode instance or separate LLM API key is required.

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
# Entry point
# ---------------------------------------------------------------------------

async def _run_server() -> None:
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
            raise_exceptions=False,
        )


def main() -> None:
    """Entry point for the vscout-mcp console script."""
    try:
        asyncio.run(_run_server())
    except KeyboardInterrupt:
        logger.info("MCP server stopped by user")
    except Exception:
        logger.exception("MCP server failed to start")
        sys.exit(1)


if __name__ == "__main__":
    main()
