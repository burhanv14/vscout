import logging
from typing import Any, Dict, List
from rich.console import Console

from oss_agent.config import AppConfig
from oss_agent.github import GitHubClient
from oss_agent.state import StateStore

logger = logging.getLogger("oss_agent.stage2")
console = Console()


class IssueDiscoveryStage:
    """Stage 2: Discover unassigned candidate issues with preferred labels across target repos."""

    def __init__(self, config: AppConfig, github: GitHubClient, state: StateStore):
        self.config = config
        self.github = github
        self.state = state

    def run(self, repos: List[Dict[str, Any]], max_issues_per_repo: int = 5) -> List[Dict[str, Any]]:
        console.print("[bold cyan]Stage 2: Issue Discovery[/bold cyan]")
        all_candidate_issues = []

        for repo_info in repos:
            full_name = repo_info.get("repo_full_name") or repo_info.get("full_name")
            if not full_name or "/" not in full_name:
                continue
            owner, repo_name = full_name.split("/", 1)
            console.print(f"Scanning issues for [bold]{full_name}[/bold]...")

            try:
                raw_issues = self.github.get_issues(
                    owner=owner,
                    repo=repo_name,
                    labels=self.config.preferred_labels,
                    limit=max_issues_per_repo * 3,
                )
            except Exception as e:
                logger.warning(f"Failed to fetch issues for {full_name}: {e}")
                continue

            unassigned_count = 0
            for issue in raw_issues:
                # Skip assigned issues
                if issue.get("assignee") or issue.get("assignees"):
                    continue
                # Skip locked issues
                if issue.get("locked"):
                    continue
                # Skip if pull request
                if "pull_request" in issue:
                    continue

                labels = [label["name"] for label in issue.get("labels", []) if isinstance(label, dict)]
                issue_data = {
                    "repo_full_name": full_name,
                    "issue_number": issue["number"],
                    "title": issue["title"],
                    "author": issue.get("user", {}).get("login", "unknown"),
                    "url": issue.get("html_url", f"https://github.com/{full_name}/issues/{issue['number']}"),
                    "labels": labels,
                    "assignees": [a.get("login", "unknown") for a in issue.get("assignees", []) if isinstance(a, dict)],
                    "body": issue.get("body", "") or "",
                    "created_at": issue.get("created_at", ""),
                    "updated_at": issue.get("updated_at", ""),
                }
                all_candidate_issues.append(issue_data)
                unassigned_count += 1
                if unassigned_count >= max_issues_per_repo:
                    break

        console.print(f"Found [bold green]{len(all_candidate_issues)}[/bold green] unassigned candidate issues.")
        self.state.save_checkpoint("stage2_issues", {"issues": all_candidate_issues})
        return all_candidate_issues
