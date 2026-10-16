import os
import re
import sys
from typing import Optional
from rich.console import Console

from oss_agent.config import AppConfig
from oss_agent.state import StateStore

console = Console()


class SafetyCheckFailed(Exception):
    """Raised when an operation violates agent safety policies."""
    pass


class SafetyGuard:
    """Enforces safety gates, rate limits, branch protection, and human approvals."""

    SECRET_PATTERNS = [
        re.compile(r"ghp_[A-Za-z0-9_]{36,}", re.IGNORECASE),
        re.compile(r"github_pat_[A-Za-z0-9_]{50,}", re.IGNORECASE),
        re.compile(r"(?:api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*['\"][A-Za-z0-9_\-\.]{16,}['\"]", re.IGNORECASE),
        re.compile(r"-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----"),
    ]

    def __init__(self, config: AppConfig, state: StateStore):
        self.config = config
        self.state = state

    def prompt_confirmation(self, action_description: str, auto_approve: bool = False) -> bool:
        """Prompt user with interactive y/N confirmation unless auto-approve is specified."""
        if auto_approve or not self.config.require_confirmation:
            console.print(f"[bold cyan][Auto-Approved][/bold cyan] {action_description}")
            return True

        console.print(f"\n[bold yellow]SAFETY CONFIRMATION REQUIRED[/bold yellow]: {action_description}")
        try:
            choice = input("Proceed? [y/N]: ").strip().lower()
            confirmed = choice in ("y", "yes")
            if not confirmed:
                console.print("[red]Action cancelled by user.[/red]")
            return confirmed
        except (KeyboardInterrupt, EOFError):
            console.print("\n[red]Action cancelled by user interrupt.[/red]")
            return False

    def check_pr_limits(self, open_prs_count: int) -> None:
        """Verify daily PR limit and maximum concurrent open PRs."""
        daily_count = self.state.get_pr_count_for_today()
        if daily_count >= self.config.max_prs_per_day:
            raise SafetyCheckFailed(
                f"Daily PR limit reached: {daily_count}/{self.config.max_prs_per_day} PRs submitted today. "
                f"Rate-limiting prevents open-source spam."
            )

        if open_prs_count >= self.config.max_open_prs_at_once:
            raise SafetyCheckFailed(
                f"Maximum open PRs reached: {open_prs_count}/{self.config.max_open_prs_at_once} open PRs currently active. "
                f"Merge or close open PRs before submitting new ones."
            )

    def verify_not_claimed_or_active(self, status: str) -> None:
        """Ensure issue is not claimed or worked on by someone else."""
        if status in ("claimed", "pr_exists", "already_fixed"):
            raise SafetyCheckFailed(
                f"Safety check rejected: Issue status is '{status}'. "
                f"Never open PRs or duplicate effort when someone is actively working on it or a PR exists."
            )

    def verify_ai_policy(self, ai_policy: str, auto_approve: bool = False) -> bool:
        """
        Verify repository AI policy.
        If 'forbidden', prompt human for explicit override approval.
        """
        if ai_policy == "forbidden":
            console.print(
                "\n[bold red]ATTENTION:[/bold red] This repository's contribution policy explicitly [bold red]FORBIDS[/bold red] AI-assisted contributions!"
            )
            return self.prompt_confirmation(
                "Do you want to override and proceed with human approval anyway?",
                auto_approve=auto_approve,
            )
        return True

    def scan_for_secrets(self, content_or_diff: str) -> None:
        """Scan diff or files for accidental secrets before committing or pushing."""
        for pattern in self.SECRET_PATTERNS:
            matches = pattern.findall(content_or_diff)
            if matches:
                raise SafetyCheckFailed(
                    f"POTENTIAL SECRET DETECTED in changes! Aborting commit/push to protect credentials. Match preview: {matches[0][:8]}..."
                )

    def verify_branch_safety(self, branch_name: str, default_branch: str = "main") -> None:
        """Ensure default branches (main/master/develop) are never committed to directly."""
        protected = {"main", "master", "develop", "dev", default_branch.lower()}
        if branch_name.lower().strip() in protected:
            raise SafetyCheckFailed(
                f"Branch safety violation: Cannot target or modify protected default branch '{branch_name}'. "
                f"Always use a dedicated feature or bugfix branch."
            )
