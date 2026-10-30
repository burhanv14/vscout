import json
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from oss_agent.config import AppConfig, get_project_root
from oss_agent.github import GitHubClient
from oss_agent.state import StateStore

logger = logging.getLogger("oss_agent.stage3")
console = None  # Console output handled by CLI/MCP layer


class IssueAnalysisData(BaseModel):
    """Structured issue context gathered for AI-client triage reasoning.

    This contains all the raw issue context for triage analysis. The connected
    AI client is responsible for interpreting this data and making the triage
    decision.
    """

    repo_full_name: str
    issue_number: int
    title: str
    author: str
    url: str
    labels: List[str] = Field(default_factory=list)
    assignees: List[str] = Field(default_factory=list)
    body: str = ""
    created_at: str = ""
    updated_at: str = ""
    comments: List[Dict[str, Any]] = Field(default_factory=list)
    timeline: List[Dict[str, Any]] = Field(default_factory=list)
    pull_requests: List[Dict[str, Any]] = Field(default_factory=list)
    commits: List[Dict[str, Any]] = Field(default_factory=list)


class WorthItAnalysisStage:
    """Stage 3: Gather comprehensive issue context for AI-client triage.

    This stage performs data gathering only (fetching issue details, comments,
    timeline, linked PRs, and commits). Triage decisions (worth score, status,
    difficulty) are left to the connected AI client via MCP tools.
    """

    def __init__(self, config: AppConfig, github: GitHubClient, state: StateStore):
        self.config = config
        self.github = github
        self.state = state
        self.root = get_project_root()

    def analyze_issue(self, issue: Dict[str, Any]) -> IssueAnalysisData:
        """Gather all context for an issue.

        Returns structured IssueAnalysisData that the connected AI client can
        use to perform its own worth-it triage reasoning.
        """
        full_name = issue["repo_full_name"]
        owner, repo_name = full_name.split("/", 1)
        issue_number = issue["issue_number"]

        logger.info(f"Gathering context for {full_name}#{issue_number}: {issue['title'][:60]}...")

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

        analysis_data = IssueAnalysisData(
            repo_full_name=full_name,
            issue_number=issue_number,
            title=issue["title"],
            author=issue.get("author", "unknown"),
            url=issue["url"],
            labels=issue.get("labels", []),
            assignees=[a.get("login", "unknown") for a in issue.get("assignees", []) if isinstance(a, dict)],
            body=issue.get("body", "") or "",
            created_at=issue.get("created_at", ""),
            updated_at=issue.get("updated_at", ""),
            comments=formatted_comments,
            timeline=formatted_timeline,
            pull_requests=prs_summary,
            commits=commits_summary,
        )

        # Persist raw analysis data
        self.state.save_issue_triage(
            repo_full_name=full_name,
            issue_number=issue_number,
            title=issue["title"],
            author=issue.get("author", "unknown"),
            url=issue["url"],
            labels=issue.get("labels", []),
            status="gathered",
            worth_score=0,
            difficulty="unknown",
            estimated_hours=0.0,
            risks=[],
            reasoning=json.dumps(analysis_data.model_dump(), default=str),
        )

        return analysis_data

    def run(self, issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Gather context for all candidate issues.

        Returns a list of issue entries with their gathered analysis data.
        No filtering by worth score or status is performed here — those
        decisions belong to the connected AI client.
        """
        logger.info(f"Stage 3: Gathering context for {len(issues)} issues.")
        analyzed_issues = []

        for issue in issues:
            try:
                analysis = self.analyze_issue(issue)
                issue_entry = {**issue, **analysis.model_dump()}
                analyzed_issues.append(issue_entry)
            except Exception as e:
                logger.error(f"Error gathering context for issue #{issue.get('issue_number')}: {e}")

        self.state.save_checkpoint("stage3_analyzed_issues", {"issues": analyzed_issues})
        logger.info(f"Stage 3 Complete: {len(analyzed_issues)} issues analyzed.")
        return analyzed_issues
