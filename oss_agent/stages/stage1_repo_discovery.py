from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
from typing import Any, Dict, List
from pydantic import BaseModel
from rich.console import Console

from oss_agent.brain import LLMBrain
from oss_agent.config import AppConfig, get_project_root, render_template
from oss_agent.github import GitHubClient
from oss_agent.state import StateStore

logger = logging.getLogger("oss_agent.stage1")
console = Console()


class RepoRankItem(BaseModel):
    repo_full_name: str
    health_score: int
    fit_score: int
    recommendation: str
    reasons: List[str]
    risks: List[str]


class RepoRankResult(BaseModel):
    ranked_repos: List[RepoRankItem]


class RepoDiscoveryStage:
    """Stage 1: Discover and rank GitHub repositories matching tech stack and health metrics."""

    # Valid GitHub search/repositories sort values. Rotating through them surfaces
    # a different slice of the candidate pool on every run instead of always page-1
    # of the same "updated" sort.
    DISCOVERY_SORTS = ["updated", "stars", "forks", "help-wanted-issues"]
    MAX_DISCOVERY_PAGES = 10
    # Max page/sort combos to try in a single run before giving up.
    MAX_DISCOVERY_ATTEMPTS = 20

    def __init__(self, config: AppConfig, brain: LLMBrain, github: GitHubClient, state: StateStore):
        self.config = config
        self.brain = brain
        self.github = github
        self.state = state
        self.root = get_project_root()

    def run(self, max_repos_to_rank: int = 6) -> List[Dict[str, Any]]:
        console.print("[bold cyan]Stage 1: Repository Discovery[/bold cyan]")
        pushed_after = (datetime.utcnow() - timedelta(days=self.config.min_recent_activity_days)).strftime("%Y-%m-%d")

        # --- Progressive discovery cursor: rotate sort + page for variety ---
        cursor = self.state.get_checkpoint("stage1_cursor") or {}
        page = int(cursor.get("page", 1))
        sort_index = int(cursor.get("sort_index", 0))

        excluded = {e.lower() for e in self.config.excluded_repos_or_orgs}

        # Search across pages/sort orders until we find enough valid candidates,
        # advancing the cursor past empty or unusable pages.
        filtered: List[Dict[str, Any]] = []
        attempts = 0
        while attempts < self.MAX_DISCOVERY_ATTEMPTS and not filtered:
            sort = self.DISCOVERY_SORTS[sort_index % len(self.DISCOVERY_SORTS)]
            candidates = self.github.search_repositories(
                keywords=self.config.tech_stack,
                min_stars=self.config.min_repo_stars,
                max_stars=self.config.max_repo_stars,
                pushed_after_iso=pushed_after,
                limit=self.config.max_candidates_per_run * 3,
                sort=sort,
                page=page,
            )

            for repo in candidates:
                full_name = repo.get("full_name", "")
                if not full_name:
                    continue
                owner = full_name.split("/")[0].lower()
                if full_name.lower() in excluded or owner in excluded:
                    continue
                if repo.get("archived", False) or repo.get("disabled", False):
                    continue
                if repo.get("open_issues_count", 0) < 2:
                    continue
                filtered.append(repo)
                if len(filtered) >= max_repos_to_rank:
                    break

            attempts += 1
            if filtered:
                break  # found enough candidates; stop searching

            # Advance cursor past this (empty/insufficient) page/sort combo
            page += 1
            if page > self.MAX_DISCOVERY_PAGES:
                page = 1
                sort_index += 1

        # Save the cursor so the next run continues from where we left off.
        next_page = page + 1
        next_sort_index = sort_index
        if next_page > self.MAX_DISCOVERY_PAGES:
            next_page = 1
            next_sort_index = (sort_index + 1) % len(self.DISCOVERY_SORTS)
        self.state.save_checkpoint("stage1_cursor", {"page": next_page, "sort_index": next_sort_index})

        if not filtered:
            console.print("[yellow]No candidate repositories found matching criteria.[/yellow]")
            return []

        console.print(f"Found [green]{len(filtered)}[/green] candidate repositories. Evaluating health and fit with OpenCode brain...")

        repos_summary = []
        for r in filtered:
            repos_summary.append({
                "full_name": r["full_name"],
                "stars": r.get("stargazers_count", 0),
                "forks": r.get("forks_count", 0),
                "open_issues": r.get("open_issues_count", 0),
                "language": r.get("language", ""),
                "description": r.get("description", ""),
                "updated_at": r.get("updated_at", ""),
                "pushed_at": r.get("pushed_at", ""),
            })

        prompt_template = (self.root / "prompts" / "repo_ranker.txt").read_text(encoding="utf-8")
        prompt = render_template(
            prompt_template,
            tech_stack=", ".join(self.config.tech_stack),
            experience_level=self.config.experience_level,
            repos_data=json.dumps(repos_summary, indent=2),
        )

        result: RepoRankResult = self.brain.think(prompt, schema=RepoRankResult)  # type: ignore

        ranked_list = []
        for ranked in result.ranked_repos:
            original = next((r for r in filtered if r["full_name"].lower() == ranked.repo_full_name.lower()), None)
            stars = original.get("stargazers_count", 0) if original else 0
            language = original.get("language", "") if original else ""

            self.state.save_repo_evaluation(
                repo_full_name=ranked.repo_full_name,
                stars=stars,
                language=language,
                health_score=ranked.health_score,
                fit_score=ranked.fit_score,
                recommendation=ranked.recommendation,
                eval_dict=ranked.model_dump(),
            )
            ranked_list.append({
                "repo_full_name": ranked.repo_full_name,
                "stars": stars,
                "language": language,
                "health_score": ranked.health_score,
                "fit_score": ranked.fit_score,
                "recommendation": ranked.recommendation,
                "reasons": ranked.reasons,
                "risks": ranked.risks,
            })

        self.state.save_checkpoint("stage1_repos", {"repos": ranked_list})
        console.print(f"[bold green]Ranked {len(ranked_list)} repositories successfully.[/bold green]")
        return ranked_list
