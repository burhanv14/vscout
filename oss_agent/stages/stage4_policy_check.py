import json
import logging
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field
from rich.console import Console

from oss_agent.brain import LLMBrain
from oss_agent.config import AppConfig, get_project_root, render_template
from oss_agent.github import GitHubClient
from oss_agent.safety import SafetyGuard
from oss_agent.state import StateStore

logger = logging.getLogger("oss_agent.stage4")
console = Console()


class PolicyCheckResult(BaseModel):
    allows_external_contributors: bool
    ai_policy: Literal["allowed", "disclosure_required", "forbidden", "unspecified"]
    requires_issue_assignment_first: bool
    requires_cla_or_dco: bool
    commit_format: str = Field(default="Conventional Commits")
    branch_naming: str = Field(default="fix/issue-{number}")
    test_commands: List[str] = Field(default_factory=lambda: ["pytest"])
    lint_commands: List[str] = Field(default_factory=list)
    pr_template_path: Optional[str] = None
    steps_for_human: List[str] = Field(default_factory=list)


class PolicyCheckStage:
    """Stage 4: Hard gate contribution policy & compliance inspection."""

    def __init__(
        self,
        config: AppConfig,
        brain: LLMBrain,
        github: GitHubClient,
        state: StateStore,
        safety: SafetyGuard,
    ):
        self.config = config
        self.brain = brain
        self.github = github
        self.state = state
        self.safety = safety
        self.root = get_project_root()

    def check_repo_policy(self, repo_full_name: str, issue_number: int) -> PolicyCheckResult:
        owner, repo_name = repo_full_name.split("/", 1)
        console.print(f"Auditing contribution policies for [cyan]{repo_full_name}[/cyan]...")

        docs = self.github.find_documentation_files(owner, repo_name)

        contributing = (
            docs.get("CONTRIBUTING.md")
            or docs.get("contributing.md")
            or docs.get(".github/CONTRIBUTING.md")
            or docs.get("docs/CONTRIBUTING.md")
            or "(No CONTRIBUTING guide found)"
        )
        coc = (
            docs.get("CODE_OF_CONDUCT.md")
            or docs.get(".github/CODE_OF_CONDUCT.md")
            or "(No Code of Conduct found)"
        )
        pr_tmpl = (
            docs.get(".github/pull_request_template.md")
            or docs.get(".github/PULL_REQUEST_TEMPLATE.md")
            or docs.get(".github/PULL_REQUEST_TEMPLATE/pull_request_template.md")
            or "(No PR template found)"
        )

        other_docs = {k: v for k, v in docs.items() if k not in ("CONTRIBUTING.md", "CODE_OF_CONDUCT.md")}

        template = (self.root / "prompts" / "policy_check.txt").read_text(encoding="utf-8")
        prompt = render_template(
            template,
            repo_full_name=repo_full_name,
            number=issue_number,
            contributing_content=contributing[:6000],
            code_of_conduct_content=coc[:2000],
            pr_template_content=pr_tmpl[:3000],
            github_docs_content=json.dumps({k: v[:1500] for k, v in other_docs.items()}, indent=2),
        )

        result: PolicyCheckResult = self.brain.think(prompt, schema=PolicyCheckResult)  # type: ignore

        # Custom user rule: If AI contributions are forbidden -> DONT stop, rather wait for human approval.
        if result.ai_policy == "forbidden":
            forbidden_notice = "CRITICAL: Repo policy is forbidden for AI contributions. Human approval required before preparing/submitting."
            if forbidden_notice not in result.steps_for_human:
                result.steps_for_human.insert(0, forbidden_notice)

        if result.requires_issue_assignment_first:
            assign_step = f"Comment on issue #{issue_number} requesting assignment before opening PR"
            if assign_step not in result.steps_for_human:
                result.steps_for_human.append(assign_step)

        if result.requires_cla_or_dco:
            cla_step = "Sign repository CLA / verify DCO Signed-off-by on git commits"
            if cla_step not in result.steps_for_human:
                result.steps_for_human.append(cla_step)

        return result

    def run(self, qualified_issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        console.print("[bold cyan]Stage 4: Contribution Policy Audit (Hard Gate)[/bold cyan]")
        audited_issues = []

        for issue in qualified_issues:
            repo_full_name = issue["repo_full_name"]
            issue_number = issue["issue_number"]
            try:
                policy = self.check_repo_policy(repo_full_name, issue_number)

                if not policy.allows_external_contributors:
                    console.print(f"[bold red]Excluded {repo_full_name}#{issue_number}:[/bold red] External contributions not accepted.")
                    continue

                enriched = {**issue, "policy": policy.model_dump()}
                audited_issues.append(enriched)
                console.print(
                    f"[green]Policy check passed for {repo_full_name}#{issue_number}[/green] "
                    f"(AI Policy: {policy.ai_policy}, Assignment First: {policy.requires_issue_assignment_first})"
                )

            except Exception as e:
                logger.error(f"Policy check failed for {repo_full_name}#{issue_number}: {e}")

        self.state.save_checkpoint("stage4_audited_issues", {"issues": audited_issues})
        return audited_issues
