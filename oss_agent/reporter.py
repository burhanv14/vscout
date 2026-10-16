from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from oss_agent.config import get_project_root

console = Console()


class ActionReport:
    """Generates and displays 'Steps required from the human' reports."""

    def __init__(self, repo_full_name: str, issue_number: int, issue_title: str):
        self.repo_full_name = repo_full_name
        self.issue_number = issue_number
        self.issue_title = issue_title
        self.root = get_project_root()
        self.reports_dir = self.root / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def generate_and_save(
        self,
        *,
        worth_score: int,
        triage_status: str,
        difficulty: str,
        estimated_hours: float,
        reasoning: str,
        ai_policy: str,
        human_actions: List[str],
        draft_assignment_comment: Optional[str] = None,
        workspace_dir: Optional[str] = None,
        branch_name: Optional[str] = None,
        test_results: Optional[str] = None,
        diff_summary: Optional[str] = None,
        pr_url: Optional[str] = None,
    ) -> Path:
        safe_repo = self.repo_full_name.replace("/", "__")
        filename = f"{safe_repo}_issue_{self.issue_number}.md"
        report_path = self.reports_dir / filename

        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        md_lines = [
            f"# Contribution Triage & Human Action Report",
            f"**Repository:** `{self.repo_full_name}`  ",
            f"**Issue:** [#{self.issue_number} - {self.issue_title}](https://github.com/{self.repo_full_name}/issues/{self.issue_number})  ",
            f"**Date:** {now_str}  ",
            f"**Worth-It Score:** {worth_score}/100 | **Status:** `{triage_status}` | **Difficulty:** `{difficulty}` | **Estimated Effort:** ~{estimated_hours}h  ",
            "",
            "---",
            "",
            "## 🚨 ACTION REQUIRED (you)",
            "",
        ]

        if human_actions:
            for act in human_actions:
                md_lines.append(f"- [ ] **{act}**")
        else:
            md_lines.append("- [x] No immediate manual action required. Ready for automated pipeline execution.")

        if ai_policy == "forbidden":
            md_lines.append("")
            md_lines.append("> ⚠️ **AI Policy Warning**: This repository prohibits AI-generated contributions. Human review, rewriting, or explicit maintainer authorization is mandatory before submitting.")

        if draft_assignment_comment:
            md_lines.append("")
            md_lines.append("### Draft Comment for Issue Assignment")
            md_lines.append("If required to be assigned before coding, copy and paste this comment:")
            md_lines.append("```markdown")
            md_lines.append(draft_assignment_comment)
            md_lines.append("```")

        md_lines.extend([
            "",
            "---",
            "",
            "## Triage Assessment & Reasoning",
            reasoning,
            "",
            "## Repository Policy Summary",
            f"- **AI Contribution Policy:** `{ai_policy}`",
            f"- **External Contributors Allowed:** Yes",
            "",
        ])

        if workspace_dir or branch_name:
            md_lines.extend([
                "## Preparation & Workspace Details",
                f"- **Local Workspace:** `{workspace_dir or 'Not yet prepared'}`",
                f"- **Working Branch:** `{branch_name or 'N/A'}`",
                f"- **Test Execution:** {test_results or 'Pending'}",
                "",
            ])

        if diff_summary:
            md_lines.extend([
                "## Code Changes Diff Summary",
                "```diff",
                diff_summary,
                "```",
                "",
            ])

        if pr_url:
            md_lines.extend([
                "## Pull Request Submitted",
                f"🎉 Pull Request is live: [View PR on GitHub]({pr_url})",
                "",
            ])

        report_path.write_text("\n".join(md_lines), encoding="utf-8")

        # Print highlight in terminal
        self.print_terminal_summary(
            report_path=report_path,
            worth_score=worth_score,
            triage_status=triage_status,
            human_actions=human_actions,
            ai_policy=ai_policy,
            pr_url=pr_url,
        )

        return report_path

    def print_terminal_summary(
        self,
        report_path: Path,
        worth_score: int,
        triage_status: str,
        human_actions: List[str],
        ai_policy: str,
        pr_url: Optional[str] = None,
    ) -> None:
        table = Table(title=f"Assessment: {self.repo_full_name} #{self.issue_number}", show_header=True)
        table.add_column("Property", style="bold cyan")
        table.add_column("Value", style="white")

        table.add_row("Worth Score", f"[bold green]{worth_score}/100[/bold green]" if worth_score >= 70 else f"[yellow]{worth_score}/100[/yellow]")
        table.add_row("Status", triage_status)
        table.add_row("AI Policy", f"[bold red]{ai_policy}[/bold red]" if ai_policy == "forbidden" else ai_policy)
        table.add_row("Report Saved", str(report_path))
        if pr_url:
            table.add_row("PR URL", f"[bold green]{pr_url}[/bold green]")

        console.print(table)

        action_text = ""
        for i, act in enumerate(human_actions, 1):
            action_text += f"[bold yellow]{i}.[/bold yellow] {act}\n"

        if ai_policy == "forbidden":
            action_text += "\n[bold red]⚠️ REPO POLICY FORBIDS AI CONTRIBUTIONS:[/bold red] Human approval & manual review required!\n"

        panel = Panel(
            action_text.strip() or "[green]None - fully automated[/green]",
            title="[bold red]🚨 ACTION REQUIRED (YOU)[/bold red]",
            border_style="red" if human_actions or ai_policy == "forbidden" else "green",
        )
        console.print(panel)
