# OSS-Contributor-Agent (`vscout`)

> A local, CLI-driven autonomous agent that finds, evaluates, and prepares high-quality open-source contributions on GitHub — powered entirely by your local **OpenCode CLI**.

---

## ⚡ Quick CLI Commands

Get started immediately with the command-line shortcuts:

```powershell
# 1. Run the full discovery, triage, and ranking pipeline up to the approval gate
python agent.py run

# 2. Discover matching repos and unassigned candidate issues
python agent.py discover

# 3. Perform deep worth-it triage and contribution policy analysis on a specific issue
python agent.py analyze https://github.com/owner/repo/issues/123

# 4. Fork, clone, baseline test, implement fix + tests with OpenCode, and self-review
python agent.py prepare https://github.com/owner/repo/issues/123

# 5. Push branch to fork and open PR (guarded by confirmation gate)
python agent.py submit https://github.com/owner/repo/issues/123

# 6. Monitor active PRs for review comments, CI results, and draft responses
python agent.py monitor

# Global Flags:
python agent.py --help                # Show usage help
python agent.py --yes <command>       # Auto-approve non-critical confirmation prompts
python agent.py --config custom.yaml  # Specify alternative config file
```

---

## 📋 Prerequisites

Before running the agent, make sure your local environment meets the following requirements:

### 1. Python 3.10+
- Python 3.10, 3.11, 3.12, 3.13, or 3.14 (tested with Python 3.14).
- Verify in your terminal:
  ```powershell
  python --version
  ```

### 2. Local OpenCode CLI
The agent uses your locally installed `opencode` CLI as its reasoning engine. No cloud API keys or external LLM SDKs are required.
- Verify that `opencode` is installed:
  ```powershell
  opencode --version
  opencode run --help
  ```
- **Verified flags utilized by this agent**:
  - `--format json`: Formats model output as JSON.
  - `--model <provider/model#variant>`: Sets a specific model variant (optional, configured in `config.yaml`).
  - `--server <url>`: Connects to a persistent background server if using `opencode serve`.
  - `--auto`: Auto-approves permissions for automated non-interactive code generation in the workspace.
- **Windows npm / pnpm Auto-Resolution**: The agent automatically resolves `opencode.cmd` located in `%APPDATA%\npm`, `%LOCALAPPDATA%\pnpm`, or your system `PATH`. You can also configure an explicit binary path using `opencode.opencode_path` in `config.yaml`.
- **Optional (Zero Cold Starts)**: Run `opencode serve --port 4096` in a background terminal, and set `backend: "server"` with `server_url: "http://localhost:4096"` in `config.yaml`.

### 3. Git
- Git must be installed. The agent automatically detects standard installations (e.g. `C:\Program Files\Git\cmd\git.exe` or system `PATH`).
- Verify:
  ```powershell
  git --version
  ```

### 4. GitHub Access
The agent supports two authentication methods (preferring `gh` CLI):
- **Option A: GitHub CLI (`gh`) [Recommended]**
  - Verify with:
    ```powershell
    gh auth status
    ```
- **Option B: GitHub Personal Access Token**
  - Generate a classic or fine-grained token with `repo` permissions.
  - Place it in your local `.env` file as `GITHUB_TOKEN=ghp_...`.

### 5. Docker (Optional for Sandboxing)
- If Docker Desktop is running, the agent will run tests and dependency installs inside ephemeral Linux containers.
- If Docker is not installed or running, the agent cleanly falls back to an isolated local virtual environment.

---

## 📖 How to Use

Follow this step-by-step workflow to find, evaluate, prepare, and submit open-source contributions.

### Step 1: Clone and Install Dependencies
```powershell
cd "C:\Users\Burhan Vora\Desktop\Projects\vscout"
python -m pip install -r requirements.txt
```

### Step 2: Configure Environment and Settings
1. Create your local `.env` file from the example:
   ```powershell
   cp .env.example .env
   ```
   Add your GitHub token (if not using `gh` CLI):
   ```env
   GITHUB_TOKEN=ghp_your_github_token_here
   ```

2. Review and edit [`config.yaml`](file:///c:/Users/Burhan%20Vora/Desktop/Projects/vscout/config.yaml):
   ```yaml
   tech_stack:
     - python
     - fastapi
     - typescript
     - react

   experience_level: "intermediate"
   preferred_labels:
     - "good first issue"
     - "help wanted"
     - "bug"

   min_repo_stars: 50
   max_repo_stars: 25000
   max_prs_per_day: 2
   max_open_prs_at_once: 2
   mode: "suggest" # "suggest" | "prepare" | "submit"
   require_confirmation: true

   github:
     username: "your_github_username"

   git:
     author_name: "Your Name"
     author_email: "you@example.com"
   ```

---

### Step 3: Run the Autonomous Pipeline (`python agent.py run`)
Run the end-to-end pipeline through Stages 1 to 5:
```powershell
python agent.py run
```
The agent will:
1. Search GitHub for active repositories matching your `tech_stack` and activity filters.
2. Call `opencode` to evaluate maintainer responsiveness, review speed, and repo health.
3. Fetch candidate open issues with preferred labels (skipping assigned issues).
4. Run deep worth-it analysis: gathering timeline events, linked PRs, commits on default branch, and claim status.
5. Audit contribution policies (`CONTRIBUTING.md`, AI policy, CLA/DCO requirements).
6. Present a ranked terminal shortlist with direct links to generated reports.

---

### Step 4: Review the "ACTION REQUIRED (you)" Report
For every evaluated issue, the agent generates a markdown report in `reports/<owner>__<repo>_issue_<number>.md` and displays a terminal panel:

```text
╭───────────────────── 🚨 ACTION REQUIRED (YOU) ─────────────────────╮
│ 1. Comment on issue #42 requesting assignment before opening PR    │
│ 2. Sign the project CLA via EasyCLA web portal                     │
│ 3. Review proposed git diff in workspace                           │
╰────────────────────────────────────────────────────────────────────╯
```

> ⚠️ **AI Policy Rule**: If a repository's contribution guidelines ban AI-generated contributions (`ai_policy: "forbidden"`), the agent **does not stop**; it highlights this in the report and pauses for your explicit human confirmation before taking any further action.

---

### Step 5: Prepare Code & Tests (`python agent.py prepare`)
Once you decide to work on an issue, run:
```powershell
python agent.py prepare https://github.com/owner/repo/issues/123
```
The agent will:
1. Fork the repo to your GitHub account and clone it to `workspaces/<owner>__<repo>`.
2. Check out a working branch (e.g. `fix/issue-123-parse-payload`).
3. Run baseline test commands in the sandbox to verify the environment.
4. Shell out to OpenCode in repo `cwd` to understand the code, implement the fix, and add regression tests.
5. Run the repository's test suite and loop on any failures.
6. Conduct a strict maintainer self-review pass on the diff (checking for scope creep, missing tests, and style).
7. Draft a conventional commit message and PR description matching the repo's PR template.

---

### Step 6: Submit the Pull Request (`python agent.py submit`)
When you are ready to publish the contribution:
```powershell
python agent.py submit https://github.com/owner/repo/issues/123
```
- Checks daily PR submission rate limits (max 1–2 per day) and concurrent open PR limits.
- Scans git diff for accidental secret leaks (tokens, private keys).
- Prompts for interactive `[y/N]` confirmation before pushing.
- Pushes to your fork origin (never touches default branch or upstream).
- Opens the Pull Request on GitHub and prints the live PR URL.

---

### Step 7: Monitor Pull Requests (`python agent.py monitor`)
Track maintainer feedback and CI test runs:
```powershell
python agent.py monitor
```
The agent inspects all your open PRs, pulls review comments and CI status, and uses OpenCode to draft friendly follow-up responses or code fix plans.

---

## 🏛️ Architecture Overview

All reasoning (repo ranking, issue triage, governance interpretation, code writing, self-review, and PR drafting) is handled by your local `opencode` CLI.

```mermaid
flowchart TD
    subgraph Discovery ["1 & 2. Discovery"]
        S1["Stage 1: Repo Discovery<br/>(GitHub API/gh + Tech Stack Filter)"] --> R1["Brain Ranks Repo Health & Fit"]
        R1 --> S2["Stage 2: Issue Discovery<br/>(Labels, Unassigned, Active)"]
    end

    subgraph Evaluation ["3, 4 & 5. Triage & Governance"]
        S2 --> S3["Stage 3: Worth-It Analysis<br/>(Timeline, PRs, Commits, Claim Heuristics)"]
        S3 --> S4["Stage 4: Policy Audit (Hard Gate)<br/>(CONTRIBUTING, AI Policy, CLA/DCO)"]
        S4 --> S5["Stage 5: Decision & Shortlist<br/>(CONTRIBUTE / SKIP / NEEDS_HUMAN)"]
        S5 --> RPT["Human Action Report<br/>reports/<repo>_<issue>.md"]
    end

    subgraph Execution ["6 & 7. Workspace & Submission"]
        RPT -->|Human Approval Gate| S6["Stage 6: Preparation<br/>(Fork, Clone, Baseline Tests, OpenCode Edit in cwd)"]
        S6 --> TST["Sandbox Test & Lint Loop<br/>(Docker / Isolated Venv)"]
        TST --> REV["Maintainer Self-Review Pass<br/>(Strict 2nd Brain Call)"]
        REV --> PRD["PR Description & Commit Generation"]
        PRD -->|Explicit [y/N] Confirmation| S7["Stage 7: Submission<br/>(Push to Fork, Open PR via GitHub)"]
    end

    subgraph Followup ["8. Monitoring"]
        S7 --> S8["Stage 8: Follow-up & Monitor<br/>(CI Checks, Review Comments, Draft Replies)"]
    end

    subgraph StateAndLogs ["Persistence & Logging"]
        DB[("SQLite State Store<br/>state/pipeline.db")]
        LOGS["Brain Call Logs<br/>logs/brain_calls.jsonl"]
    end

    S1 -.-> DB
    S3 -.-> DB
    S4 -.-> DB
    S6 -.-> DB
    S7 -.-> DB
    R1 -.-> LOGS
    S3 -.-> LOGS
    REV -.-> LOGS
```

---

## 🛡️ Safety Rules & Guardrails

- **Suggest Mode Default**: `mode: "suggest"` guarantees no public mutation or code submission occurs without explicit user sign-off.
- **Interactive Gates**: Every public action (commenting, forking, branch pushing, PR creation) requires interactive `[y/N]` approval unless `--yes` is passed.
- **AI Policy Compliance**: If a repository's policy is `forbidden`, the agent pauses and waits for human approval. If `disclosure_required`, honest disclosure is included in the PR.
- **No Duplicate Effort**: Never opens a PR for an issue another contributor is actively working on or where a PR already exists.
- **Protected Branches**: Never pushes to upstream and never modifies `main`, `master`, or `develop`.
- **Secret Detection**: Scans diffs for GitHub PATs, private keys, or API tokens before committing or pushing.
- **Daily Rate Limiting**: Capped at 1–2 PRs per day and 2 concurrent open PRs to prevent open-source spam.

---

## 🗂️ Project Layout

```text
vscout/
├── .env.example                 # Template for GitHub token and server config
├── config.example.yaml          # Sample configuration template
├── config.yaml                  # Local configuration
├── pyproject.toml               # Package specifications
├── requirements.txt             # Python dependencies
├── agent.py                     # Root CLI entrypoint
├── prompts/                     # Tunable prompt templates
│   ├── repo_ranker.txt          # Repo health and fit evaluation
│   ├── issue_triage.txt         # Worth-it analysis schema
│   ├── policy_check.txt         # Contribution & AI policy audit
│   ├── decision.txt             # Outcome synthesis
│   ├── code_implement.txt       # Instructions for OpenCode in workspace cwd
│   ├── maintainer_review.txt    # Strict maintainer self-review pass
│   ├── pr_description.txt       # PR description and commit generator
│   └── monitor_response.txt     # PR follow-up & CI diagnosis
├── state/                       # SQLite resumable pipeline database (state/pipeline.db)
├── logs/                        # OpenCode call logs (logs/brain_calls.jsonl)
├── reports/                     # Human action reports (reports/<repo>_<issue>.md)
├── workspaces/                  # Cloned repository contributor workspaces
├── oss_agent/
│   ├── cli.py                   # Command-line entrypoint & routing
│   ├── config.py                # Pydantic configuration & safe template renderer
│   ├── brain.py                 # LLMBrain calling local opencode CLI with 3x retry loop
│   ├── github.py                # Dual gh CLI & REST/GraphQL API client with backoff
│   ├── state.py                 # SQLite persistent state store
│   ├── safety.py                # Safety guardrails, limits, and secret scanner
│   ├── sandbox.py               # Docker / venv test command runner
│   ├── reporter.py              # ACTION REQUIRED (you) markdown & terminal reporter
│   └── stages/
│       ├── stage1_repo_discovery.py
│       ├── stage2_issue_discovery.py
│       ├── stage3_worth_it.py
│       ├── stage4_policy_check.py
│       ├── stage5_decision.py
│       ├── stage6_prepare.py
│       ├── stage7_submit.py
│       └── stage8_monitor.py
└── tests/
    ├── test_brain.py            # Mocked OpenCode subprocess, validation, 3x retry
    ├── test_issue_status.py     # Claim detection, linked PRs, worth-score drops
    ├── test_policy_check.py     # AI policy handling & CLA/assignment steps
    └── test_safety.py           # PR rate limits, branch guards, secret detection
```

---

## 🧪 Running Tests

Execute the unit test suite using `pytest`:

```powershell
python -m pytest -v
```

All 15 unit tests validate:
- OpenCode subprocess execution and JSON markdown codeblock parsing.
- Pydantic schema validation and 3x retry feedback loop on malformed outputs.
- Issue triage claim detection, linked PR heuristics, and worth-score filtering.
- Policy audit compliance, including `ai_policy: "forbidden"` human approval handling.
- Safety limits (daily PR limits, concurrent open PR limits, protected branch protection, and credential leak prevention).
