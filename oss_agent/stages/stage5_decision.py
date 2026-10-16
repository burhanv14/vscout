import json
import logging
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table

from oss_agent.brain import LLMBrain
from oss_agent.config import AppConfig, get_project_root, render_template
from oss_agent.reporter import ActionReport
from oss_agent.state import StateStore

logger = logging.getLogger("oss_agent.stage5")
console = Console()


class DecisionResult(BaseModel):
    decision: Literal["CONTRIBUTE", "SKIP", "NEEDS_HUMAN"]
    reasoning: str
    action_required_for_human: List[str] = Field(default_factory=list)
    draft_assignment_comment: Optional[str] = None


class DecisionStage:
    """Stage 5: Synthesize analysis into CONTRIBUTE / SKIP / NEEDS_HUMAN and present shortlist."""

    def __init__(self, config: AppConfig, brain: LLMBrain, state: StateStore):
        self.config = config
        self.brain = brain
        self.state = state
        self.root = get_project_root()

    def run(self, issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        console.print("[bold cyan]Stage 5: Contribution Decision & Shortlist Synthesis[/bold cyan]")
        shortlisted = []

        template = (self.root / "prompts" / "decision.txt").read_text(encoding="utf-8")

        for item in issues:
            full_name = item["repo_full_name"]
            issue_number = item["issue_number"]
            policy = item.get("policy", {})

            prompt = render_template(
                template,
                repo_full_name=full_name,
                issue_number=issue_number,
                issue_title=item["title"],
                worth_score=item.get("worth_score", 0),
                triage_status=item.get("status", "unknown"),
                difficulty=item.get("difficulty", "intermediate"),
                estimated_hours=item.get("estimated_hours", 2.0),
                ai_policy=policy.get("ai_policy", "unspecified"),
                requires_assignment=policy.get("requires_issue_assignment_first", False),
                requires_cla_or_dco=policy.get("requires_cla_or_dco", False),
                risks=", ".join(item.get("risks", [])),
                policy_human_steps=", ".join(policy.get("steps_for_human", [])),
            )

            result: DecisionResult = self.brain.think(prompt, schema=DecisionResult)  # type: ignore

            # If AI policy is forbidden, force decision to NEEDS_HUMAN per user override requirement
            if policy.get("ai_policy") == "forbidden" and result.decision == "CONTRIBUTE":
                result.decision = "NEEDS_HUMAN"
                if "Approve AI contribution override" not in result.action_required_for_human:
                    result.action_required_for_human.insert(0, "Approve AI contribution override")

            # Merge policy human actions
            all_human_actions = list(dict.fromkeys(result.action_required_for_human + policy.get("steps_for_human", [])))

            # Generate markdown report
            reporter = ActionReport(full_name, issue_number, item["title"])
            report_file = reporter.generate_and_save(
                worth_score=item.get("worth_score", 0),
                triage_status=item.get("status", "available"),
                difficulty=item.get("difficulty", "intermediate"),
                estimated_hours=item.get("estimated_hours", 2.0),
                reasoning=result.reasoning,
                ai_policy=policy.get("ai_policy", "unspecified"),
                human_actions=all_human_actions,
                draft_assignment_comment=result.draft_assignment_comment,
            )

            self.state.save_policy_and_decision(
                repo_full_name=full_name,
                issue_number=issue_number,
                policy_dict=policy,
                decision=result.decision,
            )

            if result.decision != "SKIP":
                shortlisted.append({
                    **item,
                    "decision": result.decision,
                    "decision_reasoning": result.reasoning,
                    "human_actions": all_human_actions,
                    "report_path": str(report_file),
                })

        # Render terminal shortlist
        self._render_shortlist_table(shortlisted)
        self.state.save_checkpoint("stage5_shortlist", {"shortlisted": shortlisted})
        return shortlisted

    def _render_shortlist_table(self, items: List[Dict[str, Any]]) -> None:
        table = Table(title="[bold green]Shortlisted Contribution Opportunities[/bold green]", show_header=True)
        table.add_column("Rank", style="bold cyan", width=5)
        table.add_column("Repository & Issue", style="white")
        table.add_column("Score", style="bold green", width=7)
        table.add_column("Decision", style="magenta", width=14)
        table.add_column("Effort", style="yellow", width=8)
        table.add_column("AI Policy", style="cyan", width=14)

        sorted_items = sorted(items, key=lambda x: x.get("worth_score", 0), reverse=True)
        for idx, it in enumerate(sorted_items, 1):
            policy = it.get("policy", {})
            ai_pol = policy.get("ai_policy", "unspecified")
            ai_style = "[bold red]forbidden[/bold red]" if ai_pol == "forbidden" else ai_pol
            table.add_row(
                str(idx),
                f"{it['repo_full_name']} #{it['issue_number']}\n{it['title'][:50]}",
                f"{it.get('worth_score', 0)}/100",
                it.get("decision", ""),
                f"~{it.get('estimated_hours', 0)}h",
                ai_style,
            )

        console.print(table)
