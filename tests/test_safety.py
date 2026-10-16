from unittest.mock import MagicMock
import pytest
from oss_agent.config import AppConfig
from oss_agent.safety import SafetyGuard, SafetyCheckFailed


def test_safety_daily_pr_limit():
    config = AppConfig()
    config.max_prs_per_day = 2
    mock_state = MagicMock()
    mock_state.get_pr_count_for_today.return_value = 2

    guard = SafetyGuard(config, mock_state)
    with pytest.raises(SafetyCheckFailed) as exc:
        guard.check_pr_limits(open_prs_count=0)
    assert "Daily PR limit reached" in str(exc.value)


def test_safety_concurrent_open_prs_limit():
    config = AppConfig()
    config.max_open_prs_at_once = 2
    mock_state = MagicMock()
    mock_state.get_pr_count_for_today.return_value = 0

    guard = SafetyGuard(config, mock_state)
    with pytest.raises(SafetyCheckFailed) as exc:
        guard.check_pr_limits(open_prs_count=2)
    assert "Maximum open PRs reached" in str(exc.value)


def test_safety_rejects_claimed_issues():
    config = AppConfig()
    mock_state = MagicMock()
    guard = SafetyGuard(config, mock_state)

    with pytest.raises(SafetyCheckFailed):
        guard.verify_not_claimed_or_active("claimed")

    with pytest.raises(SafetyCheckFailed):
        guard.verify_not_claimed_or_active("pr_exists")

    with pytest.raises(SafetyCheckFailed):
        guard.verify_not_claimed_or_active("already_fixed")


def test_safety_branch_protection():
    config = AppConfig()
    mock_state = MagicMock()
    guard = SafetyGuard(config, mock_state)

    with pytest.raises(SafetyCheckFailed):
        guard.verify_branch_safety("main")

    with pytest.raises(SafetyCheckFailed):
        guard.verify_branch_safety("master")

    # Safe branch should pass
    guard.verify_branch_safety("fix/issue-42-fix-null-pointer")


def test_safety_secret_detection():
    config = AppConfig()
    mock_state = MagicMock()
    guard = SafetyGuard(config, mock_state)

    leaked_diff = "+ GITHUB_TOKEN = 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890'"
    with pytest.raises(SafetyCheckFailed) as exc:
        guard.scan_for_secrets(leaked_diff)
    assert "POTENTIAL SECRET DETECTED" in str(exc.value)

    # Clean diff should pass
    clean_diff = "+ def calculate_total(a, b): return a + b"
    guard.scan_for_secrets(clean_diff)
