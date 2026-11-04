# OSS-Contributor-Agent (`vscout`)

> A **model-independent MCP server** that discovers and evaluates appropriate open-source issues on GitHub.
> Connect it to any MCP-compatible AI client (Claude Code, OpenAI Codex, OpenCode, Cursor, VS Code, etc.).

The agent discovers repositories matching your tech stack, finds unassigned candidate issues,
and gathers comprehensive issue context (comments, timeline, linked PRs, commits).
It does **not** fork, implement, submit, or monitor pull requests — that remains in your hands.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [MCP Tools Exposed](#mcp-tools-exposed)
4. [Installation](#installation)
5. [First-Time Setup (GitHub Token)](#first-time-setup-github-token)
6. [Starting the MCP Server](#starting-the-mcp-server)
7. [IDE / AI Client Integration](#ide--ai-client-integration)
8. [CLI Interface](#cli-interface)
9. [Terminal Launchers](#terminal-launchers)
10. [State Database (pipeline.db)](#state-database-pipelinedb)
11. [Configuration Reference](#configuration-reference)
12. [Security Considerations](#security-considerations)
13. [Troubleshooting](#troubleshooting)
14. [Project Layout](#project-layout)
15. [Running Tests](#running-tests)

---

## Overview

`vscout` is a standalone MCP server built with [FastMCP](https://github.com/jlowin/fastmcp)
that helps you find good first issues and contribution-friendly repositories on GitHub.
It performs **data gathering only** — searching repos, filtering issues, and collecting
issue context. The connected AI client (Claude Code, Codex, OpenCode, etc.) supplies the
LLM reasoning and decides which issues are worth working on.

---

## Architecture

```
AI Client (Claude Code / Codex / OpenCode / Cursor / etc.)
        |  LLM reasoning and tool selection
        |  MCP protocol (stdio or HTTP)
        v
Python MCP Server (vscout-mcp, powered by FastMCP)
        |
        v
Existing Python logic (GitHub API, SQLite state, etc.)
        |
+-------+-------+-------+
|       |       |       |
v       v       v       v
discover_repositories  discover_issues  analyze_issue  get_state / get_capabilities
        |
        v
Execution Results (structured JSON)
        |
        v
AI Client (interprets results, applies triage reasoning)
```

The AI client decides which MCP tools to invoke and interprets their results.
The Python MCP server exposes the tools, validates inputs, executes operations,
and returns structured JSON results.

---

## MCP Tools Exposed

| Tool | Description |
|------|-------------|
| `discover_repositories` | Search GitHub for public repositories matching your tech stack. Returns candidate repos with stars, language, open issues, etc. |
| `discover_issues` | Find unassigned candidate issues with preferred labels across repositories. |
| `analyze_issue` | Gather comprehensive context for a specific issue: details, comments, timeline, linked PRs, referencing commits. |
| `get_capabilities` | List all available MCP tools with descriptions and input schemas. |
| `get_state` | Inspect the persistent state store (evaluated repos, analyzed issues, checkpoints). |

All tools return structured JSON results. Input validation is performed before execution.
Errors are returned as MCP `isError` responses.

---

## Installation

```powershell
pip install -r requirements.txt

# Or install the package (provides both `agent` and `vscout-mcp` entry points):
pip install -e .
```

Dependencies are listed in `requirements.txt` and include `fastmcp`, `httpx`, `pydantic`,
`pyyaml`, `rich`, and `pytest`.

---

## First-Time Setup (GitHub Token)

1. Copy the example config files:

```powershell
cp .env.example .env
cp config.example.yaml config.yaml
```

2. Edit `config.yaml` to set your `tech_stack`, `github.username`, and other preferences.

3. Start the server. On the **first run**, if no `GITHUB_TOKEN` is found in `.env` or the
   environment, you will be prompted to enter a GitHub Personal Access Token. It is saved to
   `.env` so you are not asked again.

A GitHub token is optional if you have the `gh` CLI authenticated (`gh auth login`).
Without a token or `gh` auth, you'll hit unauthenticated GitHub API rate limits quickly.

---

## Starting the MCP Server

### Option 1: Terminal launcher (recommended for IDE connections)

Open one of the platform terminal launchers. It starts the MCP server in a terminal window
and shows its logs:

| Platform | File | How to open |
|----------|------|-------------|
| Windows | `vscout.bat` | Double-click |
| macOS | `vscout.command` | Double-click |
| Linux | `vscout.sh` | Run `./vscout.sh` in a terminal |

By default the launcher runs the server over **stdio** (the MCP client launches and owns
the process). Press `Ctrl+C` or close the terminal window to stop the server.

### Option 2: Standalone stdio mode

```powershell
vscout-mcp
```

or via Python:

```powershell
python -m oss_agent.mcp_server
```

### Option 3: Standalone HTTP mode (for remote MCP clients)

```powershell
vscout-mcp --http                  # http://127.0.0.1:8000/mcp
vscout-mcp --http --port 9000      # custom port (or VSCOUT_MCP_PORT / VSCOUT_MCP_HOST)
vscout-mcp --http --host 0.0.0.0   # custom host (not recommended; see Security)
```

HTTP mode keeps the server alive in its own terminal. Close the terminal or press `Ctrl+C`
to stop it. On Windows, closing the window triggers a clean exit via the console control handler.

---

## IDE / AI Client Integration

### Claude Code

Add to your Claude Code MCP configuration (typically `~/.claude.json` or the project's
`.mcp.json`):

```json
{
  "mcpServers": {
    "vscout": {
      "command": "vscout-mcp",
      "args": []
    }
  }
}
```

Or using the full path to the executable:

```json
{
  "mcpServers": {
    "vscout": {
      "command": "C:\\Users\\Burhan Vora\\AppData\\Roaming\\Python\\Python314\\Scripts\\vscout-mcp.exe",
      "args": []
    }
  }
}
```

Claude Code launches `vscout-mcp` as a stdio subprocess and connects over the MCP protocol.

---

### OpenAI Codex

Register the MCP server with Codex:

```bash
codex mcp add vscout vscout-mcp
```

Or via the Codex MCP configuration file:

```json
{
  "mcpServers": {
    "vscout": {
      "command": "vscout-mcp",
      "args": []
    }
  }
}
```

Codex launches `vscout-mcp` as a stdio subprocess.

---

### OpenCode

OpenCode supports both local (stdio) and remote (HTTP) MCP servers.

**Local (stdio)** — OpenCode launches the server itself:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "vscout": {
      "type": "local",
      "command": ["vscout-mcp"],
      "enabled": true,
      "environment": {}
    }
  }
}
```

**Remote (HTTP)** — Start `vscout-mcp --http` in a terminal, then point OpenCode at it:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "vscout": {
      "type": "remote",
      "url": "http://127.0.0.1:8000/mcp",
      "enabled": true
    }
  }
}
```

The `opencode.json` at the repo root is pre-configured for remote HTTP mode.

---

### VS Code (via MCP-compatible extensions)

If your VS Code extension supports MCP (e.g. an MCP client extension), add:

```json
{
  "mcpServers": {
    "vscout": {
      "command": "vscout-mcp",
      "args": []
    }
  }
}
```

Or for HTTP mode:

```json
{
  "mcpServers": {
    "vscout": {
      "type": "remote",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

---

### Cursor

Add to your Cursor MCP settings (usually `~/.cursor/mcp.json` or the project's MCP config):

```json
{
  "mcpServers": {
    "vscout": {
      "command": "vscout-mcp",
      "args": []
    }
  }
}
```

---

### Generic MCP Clients

Any MCP-compatible client can connect using either:

- **Stdio**: `"command": "vscout-mcp", "args": []` — client launches and owns the process
- **HTTP**: Start `vscout-mcp --http`, then connect to `http://127.0.0.1:8000/mcp`

---

## CLI Interface

The original CLI is retained and shares the same underlying Python logic:

```powershell
# Discover repositories and candidate issues
agent discover

# Gather context for a specific issue
agent analyze https://github.com/owner/repo/issues/123

# Run the full discovery pipeline (Stages 1-3, data gathering only)
agent run

# Specify an alternative config file
agent --config custom.yaml run
```

**Note:** The CLI performs data gathering only (search, filter, context collection).
Reasoning tasks (ranking, triage) are handled by an AI client connected via MCP.

---

## Terminal Launchers

`vscout.bat` (Windows), `vscout.command` (macOS), and `vscout.sh` (Linux) are terminal
launcher scripts. Double-clicking (Windows/macOS) or running (Linux) one opens a terminal
window that:

1. Prompts for and saves a GitHub token on the **first run** (if none is set).
2. Starts the MCP server, printing its logs to the terminal.
3. Keeps the server running until you press `Ctrl+C` or close the window.

Use these when you want the MCP server running in its own terminal so an MCP client can
connect to it. Close the window to stop the server.

---

## State Database (pipeline.db)

`vscout` uses a SQLite database at `state/pipeline.db` to persist state across runs:

| Table | Purpose |
|-------|---------|
| `repos` | Evaluated repository metadata (stars, language, health/fit scores, recommendation) |
| `issues` | Issue triage data (status, worth score, difficulty, estimated hours, risks, reasoning) |
| `checkpoints` | Pipeline stage checkpoints (resume cursor, discovered repos, analyzed issues) |

The database is created automatically on first run — you do not need to create it manually.
It enables:
- **Resumable discovery** — the pagination cursor persists between runs
- **Cross-stage state** — the `run` command carries results from Stage 1 → 2 → 3
- **`get_state` MCP tool** — lets an AI client inspect what has been discovered so far

The `state/*.db` files are gitignored (local runtime state only).

---

## Configuration Reference

### `config.yaml`

| Key | Description | Default |
|-----|-------------|---------|
| `tech_stack` | List of technologies to match against repositories | `["python", "typescript"]` |
| `experience_level` | Contributor experience level | `"intermediate"` |
| `preferred_labels` | GitHub labels to filter issues by | `["good first issue", "help wanted", "bug", "docs"]` |
| `min_repo_stars` | Minimum repository stars | `50` |
| `max_repo_stars` | Maximum repository stars | `25000` |
| `min_recent_activity_days` | Repo must have activity within N days | `30` |
| `excluded_repos_or_orgs` | Repos/orgs to skip | `[]` |
| `max_candidates_per_run` | Max candidates per discovery run | `5` |
| `worth_score_threshold` | Threshold for issue worth score (used by AI client) | `65` |
| `github.username` | Your GitHub username | `""` |
| `github.gh_path` | Path to `gh` binary (optional) | `""` |
| `github.prefer_gh_cli` | Prefer `gh` CLI over REST API | `true` |
| `git.author_name` | Git author name | `"OSS Contributor"` |
| `git.author_email` | Git author email | `"contributor@example.com"` |
| `sandbox.mode` | Sandbox execution mode (`auto`, `docker`, `venv`) | `"auto"` |
| `sandbox.timeout_seconds` | Sandbox command timeout | `120` |

### Environment Variables (`.env`)

| Variable | Description |
|----------|-------------|
| `GITHUB_TOKEN` | GitHub Personal Access Token (used if `gh` CLI is unavailable) |
| `VSCOUT_MCP_HOST` | Host for `--http` mode | `127.0.0.1` |
| `VSCOUT_MCP_PORT` | Port for `--http` mode | `8000` |

---

## Security Considerations

- The agent only reads public GitHub data. It never forks, clones, pushes, or opens Pull Requests.
- The `GITHUB_TOKEN` (if provided) is used only for GitHub API authentication.
- In stdio mode no network server is opened.
- In `--http` mode the server binds to `127.0.0.1` only by default. Do not expose the port publicly — the endpoint has no authentication.
- Tool inputs are validated before execution. Malformed inputs return structured error responses.

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `vscout-mcp` not found | Ensure `pip install -e .` was run successfully. The script installs to your Python Scripts directory. |
| No repositories found | Check your `tech_stack`, `min_repo_stars`, and `max_repo_stars` in `config.yaml`. Verify GitHub API access (gh auth or GITHUB_TOKEN). |
| GitHub API rate limit errors | Authenticate with `gh auth login` or set `GITHUB_TOKEN` in `.env`. |
| Tool returns `isError: true` | Check the error message in the response. Common causes: invalid issue URL format, network issues, rate limiting. |
| MCP client can't connect | Ensure `vscout-mcp` is on your system PATH. Verify the client configuration points to the correct executable. |
| Server exits immediately in HTTP mode | Check that the port is not already in use; try a different `--port`. |

---

## Project Layout

```
vscout/
├── .env.example                 # Template for GitHub token
├── config.example.yaml          # Sample configuration template
├── pyproject.toml               # Package specifications (defines `agent` and `vscout-mcp` scripts)
├── requirements.txt             # Python dependencies (includes fastmcp)
├── vscout.bat                   # Windows terminal launcher for the MCP server
├── vscout.command               # macOS terminal launcher for the MCP server
├── vscout.sh                    # Linux terminal launcher for the MCP server
├── agent.py                     # Root CLI entrypoint
├── README.md                    # This file
├── config.yaml                  # User configuration (created from config.example.yaml)
├── .env                         # Environment variables (created from .env.example)
├── opencode.json                # OpenCode MCP client configuration
├── oss_agent/
│   ├── cli.py                   # Command-line entrypoint & routing
│   ├── config.py                # Pydantic configuration & token setup
│   ├── github.py                # GitHub API client (gh CLI + REST)
│   ├── state.py                 # SQLite persistent state store
│   ├── mcp_server.py            # MCP server (stdio/HTTP transport, tool definitions)
│   ├── terminal_launcher.py     # Terminal launcher: token setup + server entrypoint
│   └── stages/
│       ├── stage1_repo_discovery.py   # Repository discovery (data gathering only)
│       ├── stage2_issue_discovery.py  # Issue discovery
│       └── stage3_worth_it.py         # Issue context gathering
├── prompts/
│   ├── repo_ranker.txt          # Prompt template (retained for reference)
│   └── issue_triage.txt         # Prompt template (retained for reference)
├── state/                       # SQLite state database (gitignored)
├── logs/                        # Application logs (gitignored)
├── tests/
│   ├── test_issue_status.py     # Stage 3 tests
│   └── test_mcp_protocol.py     # End-to-end MCP protocol tests
└── workspaces/                  # Sandbox workspaces (gitignored)
```

---

## Running Tests

```powershell
python -m pytest -v
```

Tests validate:
- MCP server initialization and tool discovery
- Tool input validation and error handling
- Structured result serialization
- Repeated request handling
- stdout contains only MCP protocol messages
- Original CLI continues to work
- Clean shutdown on signal while stdin is open
