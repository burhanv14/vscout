import json
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from oss_agent.brain import LLMBrain
from oss_agent.config import AppConfig, get_project_root, render_template
from oss_agent.github import GitHubClient
from oss_agent.safety import SafetyGuard
from oss_agent.state import StateStore

logger = logging.getLogger("oss_agent.stage8")
console = Console()


class MonitorDraftResult(BaseModel):
    status_summary: str
    action_needed: str
    draft_reply: Optional[str] = None
    recommended_code_changes: List[str] = Field(default_factory=list)
    human_action_items: List[str] = Field(default_factory=list)


class MonitorStage:
    """Stage 8: Follow-up and monitoring of open pull requests, CI status, and review feedback."""

    def __init__(
        self,
        config: AppConfig,
        brain: LLMBrain,
        github: GitHubClient,
        state: StateStore,
        safety: SafetyGuard,
    ):
        self.config = config
        self.brain = brain
        self.github = github
        self.state = state
        self.safety = safety
        self.root = get_project_root()

    def run(self, auto_approve: bool = False) -> List[Dict[str, Any]]:
        console.print("[bold cyan]Stage 8: Monitoring Active Pull Requests & Review Feedback[/bold cyan]")
        username = self.config.github.username
        if not username:
            console.print("[yellow]GitHub username not configured in config.yaml. Unable to query author PRs.[/yellow]")
            return []

        open_prs = self.github.get_user_open_prs(username)
        if not open_prs:
            console.print(f"[green]No active open pull requests found for @{username}.[/green]")
            return []

        console.print(f"Tracking [bold green]{len(open_prs)}[/bold green] open pull requests for @{username}...")
        template = (self.root / "prompts" / "monitor_response.txt").read_text(encoding="utf-8")
        monitor_reports = []

        for pr in open_prs:
            # Parse repository owner and name from repository_url or html_url
            repo_url = pr.get("repository_url", "")
            if "/repos/" in repo_url:
                owner, repo_name = repo_url.split("/repos/")[1].split("/", 1)
            else:
                parts = pr.get("html_url", "").split("/")
                owner, repo_name = parts[3], parts[4]

            repo_full_name = f"{owner}/{repo_name}"
            pr_number = pr["number"]
            pr_title = pr["title"]

            console.print(f"Checking status for [cyan]{repo_full_name}#{pr_number}[/cyan]: {pr_title}...")

            # 1. Fetch reviews and review comments
            reviews = self.github.get_pull_request_reviews(owner, repo_name, pr_number)
            comments = self.github.get_pull_request_comments(owner, repo_name, pr_number)

            review_threads = []
            for r in reviews:
                review_threads.append({
                    "author": r.get("user", {}).get("login"),
                    "state": r.get("state"),
                    "submitted_at": r.get("submitted_at"),
                    "body": r.get("body", ""),
                })
            for c in comments:
                review_threads.append({
                    "author": c.get("user", {}).get("login"),
                    "path": c.get("path"),
                    "line": c.get("line"),
                    "body": c.get("body", ""),
                })

            # 2. Fetch CI status
            pr_details = self.github.get_pull_request(owner, repo_name, pr_number)
            head_sha = pr_details.get("head", {}).get("sha", "")
            ci_status_data = self.github.get_combined_status(owner, repo_name, head_sha) if head_sha else {}
            check_runs = self.github.get_check_runs(owner, repo_name, head_sha) if head_sha else []

            ci_summary = f"State: {ci_status_data.get('state', 'unknown')}. Check runs: {len(check_runs)}."

            # 3. Brain analysis of PR follow-up
            prompt = render_template(
                template,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                pr_title=pr_title,
                pr_url=pr.get("html_url", ""),
                ci_status=ci_summary,
                review_threads=json.dumps(review_threads, indent=2),
            )

            result: MonitorDraftResult = self.brain.think(prompt, schema=MonitorDraftResult)  # type: ignore

            monitor_reports.append({
                "repo_full_name": repo_full_name,
                "pr_number": pr_number,
                "title": pr_title,
                "url": pr.get("html_url"),
                "analysis": result.model_dump(),
            })

            # Print status panel
            panel_text = (
                f"[bold]Summary:[/bold] {result.status_summary}\n"
                f"[bold]Action Needed:[/bold] {result.action_needed}\n"
            )
            if result.draft_reply:
                panel_text += f"\n[bold green]Draft Reply Proposed:[/bold green]\n{result.draft_reply}\n"
            if result.human_action_items:
                panel_text += "\n[bold red]ACTION REQUIRED (you):[/bold red]\n"
                for act in result.human_action_items:
                    panel_text += f" - {act}\n"

            console.print(Panel(panel_text, title=f"PR #{pr_number} - {repo_full_name}", border_style="cyan"))

            # If reply drafted and user wants to post
            if result.draft_reply and result.action_needed == "REPLY_COMMENT":
                if self.safety.prompt_confirmation(
                    f"Post draft reply comment to PR #{pr_number} on GitHub?",
                    auto_approve=auto_approve,
                ):
                    self.github.post_issue_comment(owner, repo_name, pr_number, result.draft_reply)
                    console.print("[green]Comment successfully posted to PR![/green]")

        return monitor_reports
