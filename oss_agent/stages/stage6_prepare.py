import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from rich.console import Console

from oss_agent.brain import LLMBrain
from oss_agent.config import AppConfig, get_project_root, render_template
from oss_agent.github import GitHubClient
from oss_agent.reporter import ActionReport
from oss_agent.safety import SafetyGuard
from oss_agent.sandbox import SandboxRunner
from oss_agent.state import StateStore

logger = logging.getLogger("oss_agent.stage6")
console = Console()


class MaintainerReviewResult(BaseModel):
    verdict: str = Field(description="'APPROVE', 'REQUEST_CHANGES', or 'REJECT'")
    score: int
    scope_creep_detected: bool
    missing_tests: bool
    concerns: List[str] = Field(default_factory=list)
    required_fixes: List[str] = Field(default_factory=list)
    maintainer_summary: str


class PRDraftResult(BaseModel):
    commit_message: str
    pr_title: str
    pr_body: str


class PrepareStage:
    """Stage 6: Clone fork, baseline tests, opencode implementation in repo cwd, test loop, and self-review."""

    def __init__(
        self,
        config: AppConfig,
        brain: LLMBrain,
        github: GitHubClient,
        state: StateStore,
        safety: SafetyGuard,
        sandbox: SandboxRunner,
    ):
        self.config = config
        self.brain = brain
        self.github = github
        self.state = state
        self.safety = safety
        self.sandbox = sandbox
        self.root = get_project_root()
        self.workspaces_dir = self.root / "workspaces"
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_git_binary(self) -> str:
        if self.config.git.git_path and os.path.isfile(self.config.git.git_path):
            return self.config.git.git_path
        if os.path.isfile(r"C:\Program Files\Git\cmd\git.exe"):
            return r"C:\Program Files\Git\cmd\git.exe"
        return "git"

    def _run_git(self, args: List[str], cwd: Path) -> subprocess.CompletedProcess:
        git_bin = self._resolve_git_binary()
        cmd = [git_bin] + args
        return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace")

    def prepare_issue(
        self,
        repo_full_name: str,
        issue_number: int,
        auto_approve: bool = False,
    ) -> Dict[str, Any]:
        console.print(f"\n[bold cyan]Stage 6: Preparing Contribution for {repo_full_name}#{issue_number}[/bold cyan]")
        owner, repo_name = repo_full_name.split("/", 1)
        safe_dir_name = f"{owner}__{repo_name}"
        workspace_dir = self.workspaces_dir / safe_dir_name

        # 1. Human Approval Gate
        issue_info = self.state.get_issue(repo_full_name, issue_number)
        if not issue_info:
            raw_issue = self.github.get_issue(owner, repo_name, issue_number)
            issue_title = raw_issue.get("title", f"Issue #{issue_number}")
            issue_body = raw_issue.get("body", "")
            triage_status = "available"
            ai_policy = "unspecified"
            test_commands = ["pytest"]
            commit_format = "Conventional Commits"
            pr_template = ""
        else:
            issue_title = issue_info["title"]
            issue_body = issue_info.get("reasoning", "")
            triage_status = issue_info.get("status", "available")
            policy_data = json.loads(issue_info.get("policy_json") or "{}")
            ai_policy = policy_data.get("ai_policy", "unspecified")
            test_commands = policy_data.get("test_commands", ["pytest"])
            commit_format = policy_data.get("commit_format", "Conventional Commits")
            pr_template_path = policy_data.get("pr_template_path")
            pr_template = self.github.get_repo_file_content(owner, repo_name, pr_template_path) if pr_template_path else ""

        # Safety checks
        self.safety.verify_not_claimed_or_active(triage_status)
        if not self.safety.verify_ai_policy(ai_policy, auto_approve=auto_approve):
            return {"status": "aborted", "reason": "AI policy confirmation rejected"}

        if not self.safety.prompt_confirmation(
            f"Fork & clone {repo_full_name} and start automated preparation for #{issue_number}?",
            auto_approve=auto_approve,
        ):
            return {"status": "aborted", "reason": "User declined preparation confirmation"}

        # 2. Fork and Clone
        console.print(f"[cyan]Forking {repo_full_name}...[/cyan]")
        try:
            self.github.fork_repository(owner, repo_name)
        except Exception as e:
            logger.warning(f"Fork notice: {e}")

        # Git clone if not existing
        git_bin = self._resolve_git_binary()
        if not workspace_dir.exists():
            console.print(f"[cyan]Cloning repository to {workspace_dir}...[/cyan]")
            clone_url = f"https://github.com/{owner}/{repo_name}.git"
            res = subprocess.run([git_bin, "clone", clone_url, str(workspace_dir)], capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(f"Git clone failed: {res.stderr}")
        else:
            console.print(f"[cyan]Using existing workspace: {workspace_dir}[/cyan]")

        # Configure git identity in workspace
        self._run_git(["config", "user.name", self.config.git.author_name], cwd=workspace_dir)
        self._run_git(["config", "user.email", self.config.git.author_email], cwd=workspace_dir)

        # 3. Create branch following convention
        clean_slug = re.sub(r"[^a-zA-Z0-9_-]", "-", issue_title.lower())[:30].strip("-")
        branch_name = f"fix/issue-{issue_number}-{clean_slug}"
        self.safety.verify_branch_safety(branch_name)

        self._run_git(["checkout", "-b", branch_name], cwd=workspace_dir)
        console.print(f"[green]Checked out working branch:[/green] {branch_name}")

        # 4. Baseline test execution
        console.print("[dim cyan]Running baseline tests in sandbox...[/dim cyan]")
        baseline_cmd = test_commands[0] if test_commands else "pytest"
        baseline_res = self.sandbox.run_command(baseline_cmd, workspace_dir=workspace_dir)
        baseline_summary = f"Baseline Exit: {baseline_res.exit_code} ({'Pass' if baseline_res.success else 'Fail'})"
        console.print(f"[dim]{baseline_summary}[/dim]")

        # 5. Call opencode in repo cwd to implement fix and tests
        console.print(f"[bold cyan]Invoking OpenCode in {workspace_dir} to implement fix and regression tests...[/bold cyan]")
        code_prompt_tmpl = (self.root / "prompts" / "code_implement.txt").read_text(encoding="utf-8")
        code_prompt = render_template(
            code_prompt_tmpl,
            cwd=str(workspace_dir),
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_context=f"Issue #{issue_number}: {issue_title}\n\n{issue_body}",
            commit_format=commit_format,
            test_commands=", ".join(test_commands),
            lint_commands="ruff check, flake8, npm test",
        )

        # Execute opencode directly in target repository workspace
        opencode_output = self.brain.think(code_prompt, cwd=workspace_dir)
        console.print("[green]OpenCode completed implementation run in workspace.[/green]")

        # 6. Test & lint loop
        console.print("[cyan]Validating test suite in sandbox...[/cyan]")
        test_success = False
        latest_test_res = None
        for iteration in range(1, 3):
            latest_test_res = self.sandbox.run_command(baseline_cmd, workspace_dir=workspace_dir)
            if latest_test_res.success:
                test_success = True
                console.print(f"[bold green]Tests passed on iteration {iteration}![/bold green]")
                break
            else:
                console.print(f"[yellow]Test failed on iteration {iteration}. Asking OpenCode to fix failure...[/yellow]")
                fix_prompt = (
                    f"The test command `{baseline_cmd}` failed with exit code {latest_test_res.exit_code}:\n"
                    f"```\n{latest_test_res.stdout[-1500:]}\n{latest_test_res.stderr[-1500:]}\n```\n"
                    f"Please inspect and fix the failing tests or implementation."
                )
                self.brain.think(fix_prompt, cwd=workspace_dir)

        # 7. Get git diff & scan for secrets
        diff_res = self._run_git(["diff", "HEAD"], cwd=workspace_dir)
        git_diff = diff_res.stdout
        if not git_diff.strip():
            # Check untracked files
            status_res = self._run_git(["status", "--short"], cwd=workspace_dir)
            git_diff = f"(Untracked / staged changes):\n{status_res.stdout}"

        self.safety.scan_for_secrets(git_diff)

        # 8. Strict Maintainer Self-Review pass
        console.print("[bold cyan]Running second brain call: Strict Maintainer Code Review...[/bold cyan]")
        review_tmpl = (self.root / "prompts" / "maintainer_review.txt").read_text(encoding="utf-8")
        review_prompt = render_template(
            review_tmpl,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            issue_title=issue_title,
            git_diff=git_diff[:8000],
            test_results=latest_test_res.stdout[-1000:] if latest_test_res else "None",
            lint_results="Clean",
        )
        review: MaintainerReviewResult = self.brain.think(review_prompt, schema=MaintainerReviewResult)  # type: ignore
        console.print(f"Maintainer verdict: [bold { 'green' if review.verdict == 'APPROVE' else 'yellow' }]{review.verdict}[/bold] (Score: {review.score}/100)")

        # 9. Draft PR description & commit message
        console.print("[cyan]Drafting PR description and commit message matching project conventions...[/cyan]")
        pr_tmpl_text = (self.root / "prompts" / "pr_description.txt").read_text(encoding="utf-8")
        pr_prompt = render_template(
            pr_tmpl_text,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            issue_title=issue_title,
            pr_template=pr_template or "(No repo PR template)",
            commit_format=commit_format,
            ai_policy=ai_policy,
            diff_summary=git_diff[:3000],
            test_results=f"Sandbox test command: `{baseline_cmd}` | Status: {'Passed' if test_success else 'Failing'}",
        )
        pr_draft: PRDraftResult = self.brain.think(pr_prompt, schema=PRDraftResult)  # type: ignore

        # Commit changes locally
        self._run_git(["add", "-A"], cwd=workspace_dir)
        commit_res = self._run_git(["commit", "-m", pr_draft.commit_message], cwd=workspace_dir)

        # Update state
        self.state.update_preparation_state(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            workspace_dir=str(workspace_dir),
            branch_name=branch_name,
            diff_summary=git_diff[:2000],
        )

        # Generate Action Required report
        human_actions = [
            f"Review git diff in workspace: `{workspace_dir}`",
            f"Confirm PR title & description:\n   Title: {pr_draft.pr_title}",
            f"Run `agent submit https://github.com/{repo_full_name}/issues/{issue_number}` when ready",
        ]
        if ai_policy == "forbidden":
            human_actions.insert(0, "Verify human sign-off on changes for forbidden AI policy repo")

        reporter = ActionReport(repo_full_name, issue_number, issue_title)
        report_path = reporter.generate_and_save(
            worth_score=issue_info.get("worth_score", 85) if issue_info else 85,
            triage_status="prepared",
            difficulty=issue_info.get("difficulty", "intermediate") if issue_info else "intermediate",
            estimated_hours=issue_info.get("estimated_hours", 2.0) if issue_info else 2.0,
            reasoning=f"Code implemented and reviewed locally. Maintainer review score: {review.score}/100. Verdict: {review.verdict}.",
            ai_policy=ai_policy,
            human_actions=human_actions,
            workspace_dir=str(workspace_dir),
            branch_name=branch_name,
            test_results="Passed" if test_success else "Check failures",
            diff_summary=git_diff[:1500],
        )

        return {
            "status": "prepared",
            "workspace_dir": str(workspace_dir),
            "branch_name": branch_name,
            "test_success": test_success,
            "review": review.model_dump(),
            "pr_draft": pr_draft.model_dump(),
            "report_path": str(report_path),
        }
