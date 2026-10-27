import argparse
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

from oss_agent.config import AppConfig, load_config
from oss_agent.github import GitHubClient
from oss_agent.state import StateStore
from oss_agent.stages.stage1_repo_discovery import RepoDiscoveryStage
from oss_agent.stages.stage2_issue_discovery import IssueDiscoveryStage
from oss_agent.stages.stage3_worth_it import WorthItAnalysisStage


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

    Note: The previous OpenCode-based reasoning (repo ranking and issue
    triage) has been removed. The CLI now performs data gathering only.
    Reasoning (ranking, triage) is the responsibility of an AI client
    connected via MCP.
    """

    def __init__(self, config_path: Optional[Path] = None):
        self.config = load_config(config_path)
        self.state = StateStore()
        self.github = GitHubClient(self.config)

    def cmd_discover(self) -> None:
        """Run Stage 1-2: discover repositories and candidate issues (data gathering only)."""
        print("Starting Repository & Issue Discovery (Stage 1-2)...")

        stage1 = RepoDiscoveryStage(self.config, self.github, self.state)
        ranked_repos = stage1.run()
        if not ranked_repos:
            print("No candidate repositories found matching criteria.")
            return

        stage2 = IssueDiscoveryStage(self.config, self.github, self.state)
        candidate_issues = stage2.run(ranked_repos)
        if not candidate_issues:
            print("No unassigned candidate issues found.")
            return

        print(f"Discovery complete. Found {len(candidate_issues)} candidate issues.")
        print("Use 'agent run' to gather issue context, or 'agent analyze <issue-url>' to analyze a specific issue.")

    def cmd_analyze(self, issue_url: str) -> None:
        """Run Stage 3 data gathering for a specific issue."""
        repo_full_name, issue_number = parse_issue_url(issue_url)
        print(f"Analyzing {repo_full_name}#{issue_number}...")

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

        # Stage 3: Gather issue context (no LLM triage)
        stage3 = WorthItAnalysisStage(self.config, self.github, self.state)
        analysis = stage3.analyze_issue(issue_dict)

        print(f"\nRepository: {repo_full_name}")
        print(f"Issue: #{issue_number} - {issue_dict['title']}")
        print(f"Issue URL: {issue_dict['url']}")
        print(f"Labels: {', '.join(analysis.labels) or 'none'}")
        print(f"Assignees: {', '.join(analysis.assignees) or 'none'}")
        print(f"Comments: {len(analysis.comments)}")
        print(f"Timeline events: {len(analysis.timeline)}")
        print(f"Linked PRs: {len(analysis.pull_requests)}")
        print(f"Referencing commits: {len(analysis.commits)}")
        print("\nIssue context has been gathered. Use an MCP-connected AI client to perform triage reasoning.")

    def cmd_run(self) -> None:
        """Run full discovery pipeline (Stage 1 to Stage 3, data gathering only)."""
        print("Running discovery pipeline (Stage 1 to Stage 3)...")

        # Progressive discovery: rotate Stage 1's sort/page each round so we surface
        # different valid repos each time, and keep searching until we find repos
        # that actually have unassigned candidate issues.
        max_discovery_rounds = 5
        candidate_issues = []
        ranked_repos = []

        for round_idx in range(1, max_discovery_rounds + 1):
            # 1. Repo Discovery (cursor rotates each call -> different repos per round)
            stage1 = RepoDiscoveryStage(self.config, self.github, self.state)
            ranked_repos = stage1.run()
            if not ranked_repos:
                print("No candidate repositories found this round.")
                break

            # 2. Issue Discovery
            stage2 = IssueDiscoveryStage(self.config, self.github, self.state)
            candidate_issues = stage2.run(ranked_repos)
            if candidate_issues:
                break

            print(f"Round {round_idx}: no unassigned issues found; rotating discovery search...")

        if not candidate_issues:
            print("No issues found after rotating discovery rounds.")
            return

        # 3. Issue Context Gathering
        stage3 = WorthItAnalysisStage(self.config, self.github, self.state)
        analyzed_issues = stage3.run(candidate_issues)
        if not analyzed_issues:
            print("No issues could be analyzed.")
            return

        self._render_shortlist_table(analyzed_issues)

    def _render_shortlist_table(self, items: list) -> None:
        print(f"\n{'='*60}")
        print("Issues analyzed (context gathered for AI-client triage):")
        print(f"{'='*60}")
        for idx, it in enumerate(items, 1):
            full_name = it["repo_full_name"]
            number = it["issue_number"]
            print(f"{idx}. {full_name} #{number} - {it.get('title', '')[:60]}")
            print(f"   URL: {it.get('url', '')}")
            print(f"   Labels: {', '.join(it.get('labels', [])) or 'none'}")
            print(f"   Comments: {len(it.get('comments', []))}, PRs: {len(it.get('pull_requests', []))}, Commits: {len(it.get('commits', []))}")
            print()

        print(f"\n{len(items)} issue(s) analyzed. Review them with an MCP-connected AI client for triage.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="OSS-Contributor-Agent: Finds and evaluates appropriate open-source issues (MCP-enabled)",
    )
    parser.add_argument("--config", "-c", type=Path, default=None, help="Path to config.yaml")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # agent discover
    subparsers.add_parser("discover", help="Discover matching repositories and unassigned candidate issues")

    # agent analyze <issue-url>
    p_analyze = subparsers.add_parser("analyze", help="Gather context for a specific issue (AI-client triage via MCP)")
    p_analyze.add_argument("issue_url", help="GitHub issue URL (e.g. https://github.com/owner/repo/issues/123)")

    # agent run
    subparsers.add_parser("run", help="Run full discovery pipeline (Stages 1-3) and gather issue context")

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
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
