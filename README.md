# OSS-Contributor-Agent (`vscout`)

> A **model-independent MCP server** that discovers and evaluates appropriate open-source issues on GitHub.
> Connect it to any MCP-compatible AI client (Claude Code, OpenAI Codex, etc.).

The agent discovers repositories matching your tech stack, finds unassigned candidate issues,
and gathers comprehensive issue context (comments, timeline, linked PRs, commits).
It does **not** fork, implement, submit, or monitor pull requests — that remains in your hands.

---

## Architecture

The agent follows a clean separation between the MCP server and the underlying Python logic:

```
AI Client (Claude Code / Codex / Other)
        |  LLM reasoning and tool selection
        |  MCP protocol (stdio)
        v
Python MCP Server (vscout-mcp)
        |
        v
Existing Python Logic (GitHub API, SQLite state, etc.)
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
AI Client (interprets results, applies reasoning)
```

The connected AI client is responsible for deciding which MCP tools to invoke
and interpreting their results. The Python MCP server is responsible for
exposing the tools, validating inputs, executing operations, and returning results.

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

## Quick Start

### 1. Install

```powershell
pip install -r requirements.txt
# or install the package (provides both `agent` and `vscout-mcp` entry points):
pip install -e .
```

### 2. Configure

Copy the example config and add your GitHub token (optional if you use `gh` CLI):

```powershell
cp .env.example .env
cp config.example.yaml config.yaml
# Edit config.yaml to set your tech_stack, github.username, etc.
# Edit .env to set GITHUB_TOKEN if needed
```

### 3. Start the MCP Server

```powershell
vscout-mcp
```

The server communicates over **stdio** (JSON-RPC). Connect your AI client using
the client-specific configurations below.

To stop the server, press `Ctrl+C` in the terminal where `vscout-mcp` is running.

---

## Client Integration

### Claude Code

Add to your Claude Code MCP configuration (typically `~/.claude.json` or
the project's `.mcp.json`):

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

Or using the full path to the Python executable:

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

## Configuration

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

---

## Security Considerations

- The agent only reads public GitHub data. It never forks, clones, pushes, or opens Pull Requests.
- The `GITHUB_TOKEN` (if provided) is used only for GitHub API authentication.
- The MCP server communicates over stdio only — no network server is opened.
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

---

## Project Layout

```
vscout/
├── .env.example                 # Template for GitHub token
├── config.example.yaml          # Sample configuration template
├── pyproject.toml               # Package specifications (defines `agent` and `vscout-mcp` scripts)
├── requirements.txt             # Python dependencies (includes mcp SDK)
├── agent.py                     # Root CLI entrypoint
├── README.md                    # This file
├── config.yaml                  # User configuration (created from config.example.yaml)
├── .env                         # Environment variables (created from .env.example)
├── oss_agent/
│   ├── cli.py                   # Command-line entrypoint & routing
│   ├── config.py                # Pydantic configuration & template renderer
│   ├── github.py                # GitHub API client (gh CLI + REST)
│   ├── state.py                 # SQLite persistent state store
│   ├── mcp_server.py            # MCP server (stdio transport, tool definitions)
│   ├── sandbox.py               # Sandbox execution (Docker/venv)
│   └── stages/
│       ├── stage1_repo_discovery.py   # Repository discovery (data gathering only)
│       ├── stage2_issue_discovery.py  # Issue discovery
│       └── stage3_worth_it.py         # Issue context gathering
├── prompts/
│   ├── repo_ranker.txt          # Prompt template (retained for reference)
│   └── issue_triage.txt         # Prompt template (retained for reference)
├── state/                       # SQLite state database
├── logs/                        # Application logs
├── tests/
│   ├── test_issue_status.py     # Stage 3 tests
│   └── test_mcp_protocol.py     # End-to-end MCP protocol tests
└── workspaces/                  # Sandbox workspaces
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

