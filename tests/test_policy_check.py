from unittest.mock import MagicMock
from oss_agent.config import AppConfig
from oss_agent.stages.stage4_policy_check import PolicyCheckStage, PolicyCheckResult


def test_policy_check_forbidden_ai_waits_for_human_approval():
    config = AppConfig()
    mock_brain = MagicMock()
    mock_brain.think.return_value = PolicyCheckResult(
        allows_external_contributors=True,
        ai_policy="forbidden",
        requires_issue_assignment_first=False,
        requires_cla_or_dco=False,
        commit_format="Conventional Commits",
        branch_naming="fix/issue-{number}",
        test_commands=["pytest"],
        lint_commands=["flake8"],
        steps_for_human=[],
    )

    mock_github = MagicMock()
    mock_github.find_documentation_files.return_value = {
        "CONTRIBUTING.md": "AI generated pull requests are strictly banned in this project."
    }
    mock_state = MagicMock()
    mock_safety = MagicMock()

    stage = PolicyCheckStage(config, mock_brain, mock_github, mock_state, mock_safety)
    policy = stage.check_repo_policy("strict/repo", 50)

    assert policy.ai_policy == "forbidden"
    # Must NOT abort, but rather flag human approval required
    assert any("forbidden" in step.lower() for step in policy.steps_for_human)


def test_policy_check_assignment_and_cla_flags():
    config = AppConfig()
    mock_brain = MagicMock()
    mock_brain.think.return_value = PolicyCheckResult(
        allows_external_contributors=True,
        ai_policy="disclosure_required",
        requires_issue_assignment_first=True,
        requires_cla_or_dco=True,
        commit_format="Conventional Commits",
        branch_naming="fix/issue-{number}",
        test_commands=["pytest"],
        lint_commands=[],
        steps_for_human=[],
    )

    mock_github = MagicMock()
    mock_github.find_documentation_files.return_value = {
        "CONTRIBUTING.md": "Please ask to be assigned first and sign the CLA."
    }
    mock_state = MagicMock()
    mock_safety = MagicMock()

    stage = PolicyCheckStage(config, mock_brain, mock_github, mock_state, mock_safety)
    policy = stage.check_repo_policy("community/repo", 77)

    assert policy.ai_policy == "disclosure_required"
    assert policy.requires_issue_assignment_first is True
    assert policy.requires_cla_or_dco is True
    assert any("assign" in step.lower() for step in policy.steps_for_human)
    assert any("cla" in step.lower() for step in policy.steps_for_human)
