import argparse
import re
import sys
from pathlib import Path
from typing import Optional, Tuple
from rich.console import Console

from oss_agent.brain import LLMBrain
from oss_agent.config import AppConfig, load_config
from oss_agent.github import GitHubClient
from oss_agent.safety import SafetyGuard
from oss_agent.sandbox import SandboxRunner
from oss_agent.state import StateStore
from oss_agent.stages.stage1_repo_discovery import RepoDiscoveryStage
from oss_agent.stages.stage2_issue_discovery import IssueDiscoveryStage
from oss_agent.stages.stage3_worth_it import WorthItAnalysisStage
from oss_agent.stages.stage4_policy_check import PolicyCheckStage
from oss_agent.stages.stage5_decision import DecisionStage
from oss_agent.stages.stage6_prepare import PrepareStage
from oss_agent.stages.stage7_submit import SubmitStage
from oss_agent.stages.stage8_monitor import MonitorStage

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
    def __init__(self, config_path: Optional[Path] = None, auto_approve: bool = False):
        self.config = load_config(config_path)
        self.auto_approve = auto_approve
        self.state = StateStore()
        self.safety = SafetyGuard(self.config, self.state)
        self.brain = LLMBrain(self.config)
        self.github = GitHubClient(self.config)
        self.sandbox = SandboxRunner(self.config)

    def cmd_discover(self) -> None:
        console.print("[bold blue]Starting Repository & Issue Discovery...[/bold blue]")
        stage1 = RepoDiscoveryStage(self.config, self.brain, self.github, self.state)
        ranked_repos = stage1.run()
        if not ranked_repos:
            return

        stage2 = IssueDiscoveryStage(self.config, self.github, self.state)
        candidate_issues = stage2.run(ranked_repos)
        if not candidate_issues:
            return

        console.print(f"[bold green]Discovery complete. Run 'agent run' or 'agent analyze <issue-url>' to evaluate.[/bold green]")

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
            "body": raw_issue.get("body", "") or "",
            "created_at": raw_issue.get("created_at", ""),
            "updated_at": raw_issue.get("updated_at", ""),
        }

        # Stage 3: Worth-it analysis
        stage3 = WorthItAnalysisStage(self.config, self.brain, self.github, self.state)
        triage = stage3.analyze_issue(issue_dict)
        enriched = {**issue_dict, **triage.model_dump()}

        # Stage 4: Policy check
        stage4 = PolicyCheckStage(self.config, self.brain, self.github, self.state, self.safety)
        policy = stage4.check_repo_policy(repo_full_name, issue_number)
        audited = {**enriched, "policy": policy.model_dump()}

        # Stage 5: Decision & Report
        stage5 = DecisionStage(self.config, self.brain, self.state)
        shortlist = stage5.run([audited])
        console.print(f"[bold green]Analysis complete for {repo_full_name}#{issue_number}.[/bold green]")

    def cmd_prepare(self, issue_url: str) -> None:
        repo_full_name, issue_number = parse_issue_url(issue_url)
        stage6 = PrepareStage(
            self.config, self.brain, self.github, self.state, self.safety, self.sandbox
        )
        res = stage6.prepare_issue(repo_full_name, issue_number, auto_approve=self.auto_approve)
        if res.get("status") == "prepared":
            console.print(f"[bold green]Preparation succeeded![/bold green] Ready to submit PR.")
        else:
            console.print(f"[yellow]Preparation stopped: {res.get('reason')}[/yellow]")

    def cmd_submit(self, issue_url: str) -> None:
        repo_full_name, issue_number = parse_issue_url(issue_url)
        stage7 = SubmitStage(self.config, self.github, self.state, self.safety)
        res = stage7.submit_pr(repo_full_name, issue_number, auto_approve=self.auto_approve)
        if res.get("status") == "submitted":
            console.print(f"[bold green]PR Live on GitHub:[/bold green] {res.get('pr_url')}")
        else:
            console.print(f"[yellow]Submission stopped: {res.get('reason')}[/yellow]")

    def cmd_monitor(self) -> None:
        stage8 = MonitorStage(self.config, self.brain, self.github, self.state, self.safety)
        stage8.run(auto_approve=self.auto_approve)

    def cmd_run(self) -> None:
        console.print("[bold blue]Running full autonomous pipeline (Stage 1 to Stage 5)...[/bold blue]")

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
            return

        # 4. Policy Check
        stage4 = PolicyCheckStage(self.config, self.brain, self.github, self.state, self.safety)
        audited_issues = stage4.run(qualified_issues)
        if not audited_issues:
            return

        # 5. Decision & Shortlist
        stage5 = DecisionStage(self.config, self.brain, self.state)
        shortlist = stage5.run(audited_issues)
        if not shortlist:
            console.print("[yellow]No issues cleared all contribution filters.[/yellow]")
            return

        # Human Approval Gate before Stage 6 & 7
        top_pick = shortlist[0]
        top_url = top_pick["url"]
        console.print(f"\n[bold green]Top Opportunity:[/bold green] {top_pick['repo_full_name']}#{top_pick['issue_number']}")
        console.print(f"Report: [dim]{top_pick.get('report_path')}[/dim]")

        if self.config.mode in ("prepare", "submit"):
            if self.safety.prompt_confirmation(
                f"Proceed to PREPARE code fix for {top_pick['repo_full_name']}#{top_pick['issue_number']}?",
                auto_approve=self.auto_approve,
            ):
                self.cmd_prepare(top_url)
                if self.config.mode == "submit":
                    if self.safety.prompt_confirmation(
                        f"Proceed to SUBMIT Pull Request for {top_pick['repo_full_name']}#{top_pick['issue_number']}?",
                        auto_approve=self.auto_approve,
                    ):
                        self.cmd_submit(top_url)
        else:
            console.print(
                f"[bold cyan]Mode is 'suggest'.[/bold cyan] Review the report in `reports/` and run:\n"
                f"  agent prepare {top_url}\n"
                f"when you are ready to prepare the contribution."
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="OSS-Contributor-Agent: Autonomous GitHub Contributor powered by OpenCode CLI",
    )
    parser.add_argument("--config", "-c", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument("--yes", "-y", action="store_true", help="Auto-approve non-critical prompts")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # agent discover
    subparsers.add_parser("discover", help="Discover matching repositories and unassigned candidate issues")

    # agent analyze <issue-url>
    p_analyze = subparsers.add_parser("analyze", help="Perform worth-it and policy analysis on a single issue")
    p_analyze.add_argument("issue_url", help="GitHub issue URL (e.g. https://github.com/owner/repo/issues/123)")

    # agent prepare <issue-url>
    p_prepare = subparsers.add_parser("prepare", help="Fork, clone, implement fix + tests, and self-review")
    p_prepare.add_argument("issue_url", help="GitHub issue URL")

    # agent submit <issue-url>
    p_submit = subparsers.add_parser("submit", help="Push branch and submit Pull Request via GitHub")
    p_submit.add_argument("issue_url", help="GitHub issue URL")

    # agent monitor
    subparsers.add_parser("monitor", help="Check open PRs for reviewer comments and CI status")

    # agent run
    subparsers.add_parser("run", help="Run full pipeline (Stages 1-5) and present shortlisted opportunities")

    args = parser.parse_args()
    app = AgentApp(config_path=args.config, auto_approve=args.yes)

    try:
        if args.command == "discover":
            app.cmd_discover()
        elif args.command == "analyze":
            app.cmd_analyze(args.issue_url)
        elif args.command == "prepare":
            app.cmd_prepare(args.issue_url)
        elif args.command == "submit":
            app.cmd_submit(args.issue_url)
        elif args.command == "monitor":
            app.cmd_monitor()
        elif args.command == "run":
            app.cmd_run()
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
