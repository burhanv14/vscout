import json
import logging
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field
from rich.console import Console

from oss_agent.brain import LLMBrain
from oss_agent.config import AppConfig, get_project_root, render_template
from oss_agent.github import GitHubClient
from oss_agent.state import StateStore

logger = logging.getLogger("oss_agent.stage3")
console = Console()


class IssueTriageResult(BaseModel):
    status: Literal["available", "claimed", "pr_exists", "already_fixed", "stale", "unclear", "not_wanted"]
    worth_score: int = Field(ge=0, le=100)
    difficulty: str = Field(description="'beginner', 'intermediate', or 'advanced'")
    estimated_hours: float
    risks: List[str] = Field(default_factory=list)
    reasoning: str


class WorthItAnalysisStage:
    """Stage 3: Deep dive into issue context, comments, PR cross-references, and code fixes."""

    def __init__(self, config: AppConfig, brain: LLMBrain, github: GitHubClient, state: StateStore):
        self.config = config
        self.brain = brain
        self.github = github
        self.state = state
        self.root = get_project_root()

    def analyze_issue(self, issue: Dict[str, Any]) -> IssueTriageResult:
        full_name = issue["repo_full_name"]
        owner, repo_name = full_name.split("/", 1)
        issue_number = issue["issue_number"]

        console.print(f"Analyzing [cyan]{full_name}#{issue_number}[/cyan]: {issue['title'][:60]}...")

        # 1. Fetch comments
        comments = self.github.get_issue_comments(owner, repo_name, issue_number)
        formatted_comments = []
        for c in comments:
            formatted_comments.append({
                "author": c.get("user", {}).get("login"),
                "author_association": c.get("author_association"),
                "created_at": c.get("created_at"),
                "body": c.get("body", ""),
            })

        # 2. Fetch timeline cross-references
        timeline = self.github.get_issue_timeline(owner, repo_name, issue_number)
        formatted_timeline = []
        for event in timeline:
            event_type = event.get("event")
            if event_type in ("cross-referenced", "referenced", "connected"):
                source = event.get("source", {}).get("issue", {})
                formatted_timeline.append({
                    "event": event_type,
                    "created_at": event.get("created_at"),
                    "source_title": source.get("title"),
                    "source_url": source.get("html_url"),
                    "is_pr": "pull_request" in source,
                    "pr_state": source.get("state"),
                })

        # 3. Pull requests search for this issue
        prs = self.github.search_pull_requests_for_issue(owner, repo_name, issue_number)
        prs_summary = []
        for p in prs:
            prs_summary.append({
                "number": p.get("number"),
                "title": p.get("title"),
                "state": p.get("state"),
                "created_at": p.get("created_at"),
                "url": p.get("html_url"),
            })

        # 4. Search commits referencing this issue
        commits = self.github.search_commits_for_issue(owner, repo_name, issue_number)
        commits_summary = [
            {"sha": c.get("sha", "")[:8], "message": c.get("commit", {}).get("message", "")}
            for c in commits
        ]

        # Read prompt template
        template = (self.root / "prompts" / "issue_triage.txt").read_text(encoding="utf-8")
        prompt = render_template(
            template,
            repo_full_name=full_name,
            issue_title=issue["title"],
            issue_number=issue_number,
            issue_author=issue.get("author", "unknown"),
            issue_created_at=issue.get("created_at", "unknown"),
            issue_updated_at=issue.get("updated_at", "unknown"),
            issue_labels=", ".join(issue.get("labels", [])),
            issue_assignees="None",
            issue_body=issue.get("body", "(No description provided)"),
            comments_and_timeline=json.dumps({"comments": formatted_comments, "timeline": formatted_timeline}, indent=2),
            linked_prs_and_commits=json.dumps({"pull_requests": prs_summary, "commits": commits_summary}, indent=2),
            code_search_findings=f"Commits found referencing #{issue_number}: {len(commits_summary)}",
            user_tech_stack=", ".join(self.config.tech_stack),
        )

        result: IssueTriageResult = self.brain.think(prompt, schema=IssueTriageResult)  # type: ignore

        self.state.save_issue_triage(
            repo_full_name=full_name,
            issue_number=issue_number,
            title=issue["title"],
            author=issue.get("author", "unknown"),
            url=issue["url"],
            labels=issue.get("labels", []),
            status=result.status,
            worth_score=result.worth_score,
            difficulty=result.difficulty,
            estimated_hours=result.estimated_hours,
            risks=result.risks,
            reasoning=result.reasoning,
        )

        return result

    def run(self, issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        console.print("[bold cyan]Stage 3: Worth-It Analysis & Triage[/bold cyan]")
        qualified_issues = []

        for issue in issues:
            try:
                triage = self.analyze_issue(issue)
                issue_entry = {**issue, **triage.model_dump()}

                if triage.worth_score < self.config.worth_score_threshold:
                    console.print(
                        f"[yellow]Dropped #{issue['issue_number']}: score {triage.worth_score} < threshold {self.config.worth_score_threshold}[/yellow]"
                    )
                    continue

                if triage.status != "available":
                    console.print(
                        f"[yellow]Dropped #{issue['issue_number']}: status '{triage.status}' is not 'available'[/yellow]"
                    )
                    continue

                console.print(
                    f"[green]Qualified #{issue['issue_number']}[/green] (Score: {triage.worth_score}/100, Diff: {triage.difficulty}): {triage.reasoning[:80]}..."
                )
                qualified_issues.append(issue_entry)

            except Exception as e:
                logger.error(f"Error triaging issue #{issue.get('issue_number')}: {e}")

        self.state.save_checkpoint("stage3_qualified_issues", {"issues": qualified_issues})
        console.print(f"[bold green]Stage 3 Complete: {len(qualified_issues)} high-worth candidate issues identified.[/bold green]")
        return qualified_issues
