from unittest.mock import MagicMock
from oss_agent.config import AppConfig
from oss_agent.stages.stage3_worth_it import WorthItAnalysisStage, IssueAnalysisData


def test_worth_it_analysis_gathers_issue_context():
    """Verify that analyze_issue gathers all issue context."""
    config = AppConfig()

    mock_github = MagicMock()
    mock_github.get_issue_comments.return_value = [
        {"user": {"login": "maintainer1"}, "author_association": "MEMBER", "created_at": "2026-03-01T00:00:00Z", "body": "Confirmed! PRs welcome."}
    ]
    mock_github.get_issue_timeline.return_value = []
    mock_github.search_pull_requests_for_issue.return_value = []
    mock_github.search_commits_for_issue.return_value = []
    mock_state = MagicMock()

    stage = WorthItAnalysisStage(config, mock_github, mock_state)
    sample_issue = {
        "repo_full_name": "sample/repo",
        "issue_number": 42,
        "title": "Fix regression in JSON parser",
        "author": "dev1",
        "url": "https://github.com/sample/repo/issues/42",
        "labels": ["bug", "help wanted"],
        "body": "Parser fails on trailing comma",
    }

    analysis = stage.analyze_issue(sample_issue)
    assert isinstance(analysis, IssueAnalysisData)
    analysis_dict = analysis.model_dump()
    assert analysis_dict["repo_full_name"] == "sample/repo"
    assert analysis_dict["issue_number"] == 42
    assert analysis_dict["title"] == "Fix regression in JSON parser"
    assert len(analysis_dict["comments"]) == 1
    assert mock_state.save_issue_triage.called


def test_worth_it_analysis_returns_all_issues_no_filtering():
    """Verify that run() returns all issues with context."""
    config = AppConfig()

    mock_github = MagicMock()
    mock_github.get_issue_comments.return_value = []
    mock_github.get_issue_timeline.return_value = []
    mock_github.search_pull_requests_for_issue.return_value = []
    mock_github.search_commits_for_issue.return_value = []
    mock_state = MagicMock()

    stage = WorthItAnalysisStage(config, mock_github, mock_state)
    sample_issues = [
        {
            "repo_full_name": "sample/repo",
            "issue_number": 99,
            "title": "Update README docs",
            "author": "dev1",
            "url": "https://github.com/sample/repo/issues/99",
            "labels": ["docs"],
            "body": "Outdated section",
        },
        {
            "repo_full_name": "sample/repo",
            "issue_number": 105,
            "title": "Rewrite core architecture",
            "author": "dev2",
            "url": "https://github.com/sample/repo/issues/105",
            "labels": ["enhancement"],
            "body": "Complete rewrite proposal",
        },
    ]

    qualified = stage.run(sample_issues)
    # No filtering: all issues should be returned with gathered context
    assert len(qualified) == 2
