import argparse
import re
import sys
from pathlib import Path
from typing import Optional, Tuple
from rich.console import Console
from rich.table import Table

from oss_agent.brain import LLMBrain
from oss_agent.config import AppConfig, load_config
from oss_agent.github import GitHubClient
from oss_agent.state import StateStore
from oss_agent.stages.stage1_repo_discovery import RepoDiscoveryStage
from oss_agent.stages.stage2_issue_discovery import IssueDiscoveryStage
from oss_agent.stages.stage3_worth_it import WorthItAnalysisStage

console = Console()


def parse_issue_url(url_or_identifier: str) -> Tuple[str, int]:
    """
    Parse repo full name and issue number from:
    - https://github.com/owner/repo/issues/123
    - github.com/owner/repo/issues/123
    - owner/repo#123
    - owner/repo/123
    """
    match = re.search(r"github\.com/([^/]+/[^/]+)/issues/(\d+)", url_or_identifier, re.IGNORECASE)
    if match:
        return match.group(1), int(match.group(2))

    match = re.search(r"^([^/]+/[^/#]+)#(\d+)$", url_or_identifier)
    if match:
        return match.group(1), int(match.group(2))

    match = re.search(r"^([^/]+/[^/]+)/(\d+)$", url_or_identifier)
    if match:
        return match.group(1), int(match.group(2))

    raise ValueError(
        f"Invalid issue URL format: '{url_or_identifier}'. "
        f"Expected e.g. 'https://github.com/owner/repo/issues/123' or 'owner/repo#123'"
    )


class AgentApp:
    """Issue discovery & evaluation agent (Stages 1-3 only).

    Finds and evaluates appropriate open-source issues to work on.
    Does not fork, implement, submit, or monitor contributions.
    """

    def __init__(self, config_path: Optional[Path] = None):
        self.config = load_config(config_path)
        self.state = StateStore()
        self.brain = LLMBrain(self.config)
        self.github = GitHubClient(self.config)

    def cmd_discover(self) -> None:
        console.print("[bold blue]Starting Repository & Issue Discovery (Stage 1-2)...[/bold blue]")
        stage1 = RepoDiscoveryStage(self.config, self.brain, self.github, self.state)
        ranked_repos = stage1.run()
        if not ranked_repos:
            return

        stage2 = IssueDiscoveryStage(self.config, self.github, self.state)
        candidate_issues = stage2.run(ranked_repos)
        if not candidate_issues:
            return

        console.print(
            f"[bold green]Discovery complete.[/bold green] Found {len(candidate_issues)} candidate issues. "
            f"Run 'agent run' to evaluate them, or 'agent analyze <issue-url>' to evaluate a specific one."
        )

    def cmd_analyze(self, issue_url: str) -> None:
        repo_full_name, issue_number = parse_issue_url(issue_url)
        console.print(f"[bold blue]Analyzing {repo_full_name}#{issue_number}...[/bold blue]")

        owner, repo_name = repo_full_name.split("/", 1)
        raw_issue = self.github.get_issue(owner, repo_name, issue_number)
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

        # Stage 3: Worth-it analysis
        stage3 = WorthItAnalysisStage(self.config, self.brain, self.github, self.state)
        triage = stage3.analyze_issue(issue_dict)

        # Present findings
        appropriate = (
            triage.status == "available"
            and triage.worth_score >= self.config.worth_score_threshold
        )
        status_color = "green" if appropriate else "red"

        console.print(f"\n[{status_color}]Verdict: {'APPROPRIATE' if appropriate else 'NOT APPROPRIATE'}[/{status_color}]")
        console.print(f"Repository: [cyan]{repo_full_name}[/cyan]")
        console.print(f"Issue: [cyan]#{issue_number}[/cyan] - {issue_dict['title']}")
        console.print(f"Worth score: [bold]{triage.worth_score}/100[/bold] (threshold: {self.config.worth_score_threshold})")
        console.print(f"Issue status: [bold]{triage.status}[/bold]")
        console.print(f"Reasoning: {triage.reasoning}")
        console.print(f"Issue URL: [cyan]{issue_dict['url']}[/cyan]")

        if appropriate:
            console.print("[bold green]This issue looks appropriate to contribute to.[/bold green]")
        else:
            console.print("[bold yellow]This issue is not appropriate to pursue at this time.[/bold yellow]")

    def cmd_run(self) -> None:
        console.print("[bold blue]Running discovery pipeline (Stage 1 to Stage 3)...[/bold blue]")

        # Progressive discovery: rotate Stage 1's sort/page each round so we surface
        # different valid repos each time, and keep searching until we find repos
        # that actually have unassigned candidate issues.
        max_discovery_rounds = 5
        candidate_issues = []
        ranked_repos = []

        for round_idx in range(1, max_discovery_rounds + 1):
            # 1. Repo Discovery (cursor rotates each call -> different repos per round)
            stage1 = RepoDiscoveryStage(self.config, self.brain, self.github, self.state)
            ranked_repos = stage1.run()
            if not ranked_repos:
                console.print("[yellow]No candidate repositories found this round.[/yellow]")
                break

            # 2. Issue Discovery
            stage2 = IssueDiscoveryStage(self.config, self.github, self.state)
            candidate_issues = stage2.run(ranked_repos)
            if candidate_issues:
                break

            console.print(
                f"[yellow]Round {round_idx}: no unassigned issues found; rotating discovery search...[/yellow]"
            )

        if not candidate_issues:
            console.print("[yellow]No issues found after rotating discovery rounds.[/yellow]")
            return

        # 3. Worth-It Analysis
        stage3 = WorthItAnalysisStage(self.config, self.brain, self.github, self.state)
        qualified_issues = stage3.run(candidate_issues)
        if not qualified_issues:
            console.print("[yellow]No issues passed worth-it triage.[/yellow]")
            return

        # Present shortlist of appropriate issues
        self._render_shortlist_table(qualified_issues)

    def _render_shortlist_table(self, items: list) -> None:
        table = Table(title="[bold green]Appropriate Issues to Contribute To[/bold green]", show_header=True)
        table.add_column("Rank", style="bold cyan", width=5)
        table.add_column("Repository & Issue", style="white")
        table.add_column("Score", style="bold green", width=7)
        table.add_column("Status", style="magenta", width=12)
        table.add_column("URL", style="cyan", no_wrap=True)

        sorted_items = sorted(items, key=lambda x: x.get("worth_score", 0), reverse=True)
        for idx, it in enumerate(sorted_items, 1):
            full_name = it["repo_full_name"]
            number = it["issue_number"]
            table.add_row(
                str(idx),
                f"{full_name} #{number}\n{it['title'][:50]}",
                f"{it.get('worth_score', 0)}/100",
                it.get("status", "available"),
                it.get("url", ""),
            )

        console.print(table)
        console.print(
            f"\n[bold green]{len(items)} appropriate issue(s) identified.[/bold green] "
            f"Review them and run 'agent analyze <issue-url>' to evaluate a specific one."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="OSS-Contributor-Agent: Finds and evaluates appropriate open-source issues (powered by OpenCode CLI)",
    )
    parser.add_argument("--config", "-c", type=Path, default=None, help="Path to config.yaml")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # agent discover
    subparsers.add_parser("discover", help="Discover matching repositories and unassigned candidate issues")

    # agent analyze <issue-url>
    p_analyze = subparsers.add_parser("analyze", help="Evaluate whether a specific issue is appropriate to contribute to")
    p_analyze.add_argument("issue_url", help="GitHub issue URL (e.g. https://github.com/owner/repo/issues/123)")

    # agent run
    subparsers.add_parser("run", help="Run full discovery pipeline (Stages 1-3) and present appropriate issues")

    args = parser.parse_args()
    app = AgentApp(config_path=args.config)

    try:
        if args.command == "discover":
            app.cmd_discover()
        elif args.command == "analyze":
            app.cmd_analyze(args.issue_url)
        elif args.command == "run":
            app.cmd_run()
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
