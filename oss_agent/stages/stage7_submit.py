import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional
from rich.console import Console

from oss_agent.config import AppConfig, get_project_root
from oss_agent.github import GitHubClient
from oss_agent.reporter import ActionReport
from oss_agent.safety import SafetyGuard
from oss_agent.state import StateStore

logger = logging.getLogger("oss_agent.stage7")
console = Console()


class SubmitStage:
    """Stage 7: Strict submission gate, push to contributor fork, and pull request creation."""

    def __init__(
        self,
        config: AppConfig,
        github: GitHubClient,
        state: StateStore,
        safety: SafetyGuard,
    ):
        self.config = config
        self.github = github
        self.state = state
        self.safety = safety
        self.root = get_project_root()
        self.workspaces_dir = self.root / "workspaces"

    def _resolve_git_binary(self) -> str:
        if self.config.git.git_path and os.path.isfile(self.config.git.git_path):
            return self.config.git.git_path
        if os.path.isfile(r"C:\Program Files\Git\cmd\git.exe"):
            return r"C:\Program Files\Git\cmd\git.exe"
        return "git"

    def _run_git(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        git_bin = self._resolve_git_binary()
        return subprocess.run([git_bin] + args, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace")

    def submit_pr(
        self,
        repo_full_name: str,
        issue_number: int,
        auto_approve: bool = False,
    ) -> Dict[str, Any]:
        console.print(f"\n[bold cyan]Stage 7: Submitting Pull Request for {repo_full_name}#{issue_number}[/bold cyan]")
        owner, repo_name = repo_full_name.split("/", 1)
        safe_dir_name = f"{owner}__{repo_name}"
        workspace_dir = self.workspaces_dir / safe_dir_name

        if not workspace_dir.exists():
            raise RuntimeError(f"Workspace {workspace_dir} not found. Run 'agent prepare' first.")

        # 1. Check open PR limits
        username = self.config.github.username
        open_prs = self.github.get_user_open_prs(username) if username else []
        self.safety.check_pr_limits(open_prs_count=len(open_prs))

        # 2. Identify branch
        branch_res = self._run_git(["branch", "--show-current"], cwd=workspace_dir)
        branch_name = branch_res.stdout.strip()
        if not branch_name:
            raise RuntimeError("Could not determine current git branch in workspace.")

        self.safety.verify_branch_safety(branch_name)

        # 3. Verify AI policy approval
        issue_info = self.state.get_issue(repo_full_name, issue_number)
        policy_data = json.loads(issue_info.get("policy_json") or "{}") if issue_info else {}
        ai_policy = policy_data.get("ai_policy", "unspecified")
        if not self.safety.verify_ai_policy(ai_policy, auto_approve=auto_approve):
            return {"status": "aborted", "reason": "AI policy confirmation rejected"}

        # 4. Explicit Confirmation Gate
        confirm_msg = (
            f"SUBMIT PULL REQUEST: Push branch '{branch_name}' to fork and open PR to {repo_full_name}?"
        )
        if not self.safety.prompt_confirmation(confirm_msg, auto_approve=auto_approve):
            console.print("[yellow]Submission cancelled by user.[/yellow]")
            return {"status": "cancelled", "reason": "User cancelled submission"}

        # 5. Push branch to contributor fork (never force-push)
        console.print(f"[cyan]Pushing branch {branch_name} to fork origin...[/cyan]")
        push_res = self._run_git(["push", "-u", "origin", branch_name], cwd=workspace_dir)
        if push_res.returncode != 0:
            # Check if remote URL needs authentication or personal fork
            raise RuntimeError(f"Git push failed: {push_res.stderr.strip()}")

        # 6. Read commit message / PR title & body
        log_res = self._run_git(["log", "-1", "--pretty=%s%n%n%b"], cwd=workspace_dir)
        commit_text = log_res.stdout.strip()
        lines = commit_text.splitlines()
        pr_title = lines[0] if lines else f"Fix #{issue_number}"
        pr_body = "\n".join(lines[1:]).strip() if len(lines) > 1 else f"Fixes #{issue_number}"

        if not pr_body or f"#{issue_number}" not in pr_body:
            pr_body = f"Fixes #{issue_number}\n\n" + pr_body

        # 7. Create Pull Request
        console.print("[bold cyan]Opening Pull Request via GitHub...[/bold cyan]")
        head_branch = f"{username}:{branch_name}" if username else branch_name
        pr_response = self.github.create_pull_request(
            owner=owner,
            repo=repo_name,
            title=pr_title,
            body=pr_body,
            head=head_branch,
            base="main",
        )

        pr_url = pr_response.get("html_url") or str(pr_response)
        console.print(f"[bold green]Pull Request successfully submitted![/bold green] URL: {pr_url}")

        # 8. Record submission in SQLite and update report
        self.state.record_pr_submission(repo_full_name, issue_number, pr_url)

        reporter = ActionReport(repo_full_name, issue_number, issue_info["title"] if issue_info else "")
        reporter.generate_and_save(
            worth_score=issue_info.get("worth_score", 90) if issue_info else 90,
            triage_status="submitted",
            difficulty=issue_info.get("difficulty", "intermediate") if issue_info else "intermediate",
            estimated_hours=issue_info.get("estimated_hours", 2.0) if issue_info else 2.0,
            reasoning="Pull request submitted to upstream repository.",
            ai_policy=ai_policy,
            human_actions=[
                f"Verify CI test run on GitHub: {pr_url}",
                "Monitor for reviewer comments and requests",
            ],
            workspace_dir=str(workspace_dir),
            branch_name=branch_name,
            pr_url=pr_url,
        )

        return {"status": "submitted", "pr_url": pr_url}
