import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from pydantic import BaseModel, Field

from oss_agent.brain import LLMBrain, BrainExecutionError
from oss_agent.config import AppConfig


class SampleTriageSchema(BaseModel):
    status: str
    score: int = Field(ge=0, le=100)
    reason: str


@pytest.fixture
def test_config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.opencode.max_retries = 3
    cfg.opencode.timeout_seconds = 5
    return cfg


def test_brain_direct_text_output(test_config: AppConfig):
    brain = LLMBrain(test_config)

    mock_res = subprocess.CompletedProcess(
        args=["opencode", "run", "hello"],
        returncode=0,
        stdout='{"type": "text", "part": {"text": "Hello world from OpenCode!"}}',
        stderr="",
    )

    with patch("subprocess.run", return_value=mock_res):
        out = brain.think("hello")
        assert out == "Hello world from OpenCode!"


def test_brain_schema_validation_success(test_config: AppConfig):
    brain = LLMBrain(test_config)

    mock_res = subprocess.CompletedProcess(
        args=["opencode", "run"],
        returncode=0,
        stdout='```json\n{"status": "available", "score": 95, "reason": "Great bug report"}\n```',
        stderr="",
    )

    with patch("subprocess.run", return_value=mock_res):
        result = brain.think("Analyze issue", schema=SampleTriageSchema)
        assert isinstance(result, SampleTriageSchema)
        assert result.status == "available"
        assert result.score == 95
        assert result.reason == "Great bug report"


def test_brain_retry_on_validation_failure(test_config: AppConfig):
    brain = LLMBrain(test_config)

    # First attempt: invalid JSON
    fail_res_1 = subprocess.CompletedProcess(
        args=["opencode", "run"],
        returncode=0,
        stdout="This is invalid json not matching schema",
        stderr="",
    )
    # Second attempt: valid schema JSON
    success_res_2 = subprocess.CompletedProcess(
        args=["opencode", "run"],
        returncode=0,
        stdout='{"status": "available", "score": 80, "reason": "Fixed schema output"}',
        stderr="",
    )

    with patch("subprocess.run", side_effect=[fail_res_1, success_res_2]) as mock_sub:
        result = brain.think("Triage", schema=SampleTriageSchema)
        assert isinstance(result, SampleTriageSchema)
        assert result.score == 80
        assert mock_sub.call_count == 2
        # Check that retry prompt includes feedback
        second_call_prompt = mock_sub.call_args_list[1][0][0][2]
        assert "[VALIDATION RETRY 1/3]" in second_call_prompt


def test_brain_exhausts_retries_and_raises(test_config: AppConfig):
    brain = LLMBrain(test_config)

    invalid_res = subprocess.CompletedProcess(
        args=["opencode", "run"],
        returncode=0,
        stdout='{"status": "invalid", "score": 999, "reason": "Out of bounds"}',
        stderr="",
    )

    with patch("subprocess.run", return_value=invalid_res) as mock_sub:
        with pytest.raises(BrainExecutionError):
            brain.think("Triage", schema=SampleTriageSchema)
        assert mock_sub.call_count == test_config.opencode.max_retries


def test_brain_timeout_handling(test_config: AppConfig):
    brain = LLMBrain(test_config)

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="opencode", timeout=5)):
        with pytest.raises(BrainExecutionError) as exc_info:
            brain.think("Slow task")
        assert "timed out" in str(exc_info.value)
