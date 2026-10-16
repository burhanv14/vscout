from unittest.mock import MagicMock
from oss_agent.config import AppConfig
from oss_agent.stages.stage3_worth_it import WorthItAnalysisStage, IssueTriageResult


def test_worth_it_analysis_available_issue():
    config = AppConfig()
    config.worth_score_threshold = 60

    mock_brain = MagicMock()
    mock_brain.think.return_value = IssueTriageResult(
        status="available",
        worth_score=85,
        difficulty="intermediate",
        estimated_hours=2.0,
        risks=[],
        reasoning="Maintainer confirmed bug, unassigned, no existing PRs.",
    )

    mock_github = MagicMock()
    mock_github.get_issue_comments.return_value = [
        {"user": {"login": "maintainer1"}, "author_association": "MEMBER", "created_at": "2026-03-01T00:00:00Z", "body": "Confirmed! PRs welcome."}
    ]
    mock_github.get_issue_timeline.return_value = []
    mock_github.search_pull_requests_for_issue.return_value = []
    mock_github.search_commits_for_issue.return_value = []

    mock_state = MagicMock()

    stage = WorthItAnalysisStage(config, mock_brain, mock_github, mock_state)
    sample_issue = {
        "repo_full_name": "sample/repo",
        "issue_number": 42,
        "title": "Fix regression in JSON parser",
        "author": "dev1",
        "url": "https://github.com/sample/repo/issues/42",
        "labels": ["bug", "help wanted"],
        "body": "Parser fails on trailing comma",
    }

    triage = stage.analyze_issue(sample_issue)
    assert triage.status == "available"
    assert triage.worth_score == 85
    mock_state.save_issue_triage.assert_called_once()


def test_worth_it_analysis_drops_claimed_issue():
    config = AppConfig()
    config.worth_score_threshold = 60

    mock_brain = MagicMock()
    mock_brain.think.return_value = IssueTriageResult(
        status="claimed",
        worth_score=40,
        difficulty="beginner",
        estimated_hours=1.0,
        risks=["Another contributor actively assigned"],
        reasoning="Contributor commented 'I am working on this' 2 hours ago.",
    )

    mock_github = MagicMock()
    mock_github.get_issue_comments.return_value = []
    mock_github.get_issue_timeline.return_value = []
    mock_github.search_pull_requests_for_issue.return_value = []
    mock_github.search_commits_for_issue.return_value = []
    mock_state = MagicMock()

    stage = WorthItAnalysisStage(config, mock_brain, mock_github, mock_state)
    sample_issue = {
        "repo_full_name": "sample/repo",
        "issue_number": 99,
        "title": "Update README docs",
        "author": "dev1",
        "url": "https://github.com/sample/repo/issues/99",
        "labels": ["docs"],
        "body": "Outdated section",
    }

    qualified = stage.run([sample_issue])
    # Claimed issues must be dropped
    assert len(qualified) == 0


def test_worth_it_analysis_drops_low_score_issue():
    config = AppConfig()
    config.worth_score_threshold = 70

    mock_brain = MagicMock()
    mock_brain.think.return_value = IssueTriageResult(
        status="available",
        worth_score=50, # below threshold 70
        difficulty="advanced",
        estimated_hours=10.0,
        risks=["High complexity", "Breaking API change"],
        reasoning="Too broad and risky for automated contribution.",
    )

    mock_github = MagicMock()
    mock_github.get_issue_comments.return_value = []
    mock_github.get_issue_timeline.return_value = []
    mock_github.search_pull_requests_for_issue.return_value = []
    mock_github.search_commits_for_issue.return_value = []
    mock_state = MagicMock()

    stage = WorthItAnalysisStage(config, mock_brain, mock_github, mock_state)
    sample_issue = {
        "repo_full_name": "sample/repo",
        "issue_number": 105,
        "title": "Rewrite core architecture",
        "author": "dev2",
        "url": "https://github.com/sample/repo/issues/105",
        "labels": ["enhancement"],
        "body": "Complete rewrite proposal",
    }

    qualified = stage.run([sample_issue])
    assert len(qualified) == 0
