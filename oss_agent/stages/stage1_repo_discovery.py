from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from oss_agent.config import AppConfig, get_project_root
from oss_agent.github import GitHubClient
from oss_agent.state import StateStore

logger = logging.getLogger("oss_agent.stage1")
console = None  # Console output handled by CLI/MCP layer


class RepoDiscoveryStage:
    """Stage 1: Discover GitHub repositories matching tech stack and health metrics.

    This stage performs data gathering only (search + filter). Ranking and
    recommendation decisions are left to the connected AI client via MCP tools,
    which can apply its own reasoning to the returned repository data.
    """

    # Valid GitHub search/repositories sort values. Rotating through them surfaces
    # a different slice of the candidate pool on every run instead of always page-1
    # of the same "updated" sort.
    DISCOVERY_SORTS = ["updated", "stars", "forks", "help-wanted-issues"]
    MAX_DISCOVERY_PAGES = 10
    # Max page/sort combos to try in a single run before giving up.
    MAX_DISCOVERY_ATTEMPTS = 20

    def __init__(self, config: AppConfig, github: GitHubClient, state: StateStore):
        self.config = config
        self.github = github
        self.state = state
        self.root = get_project_root()

    def run(self, max_repos_to_rank: int = 6) -> List[Dict[str, Any]]:
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
            logger.warning("No candidate repositories found matching criteria.")
            return []

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

        ranked_list = []
        for r in filtered:
            full_name = r["full_name"]
            ranked_list.append({
                "repo_full_name": full_name,
                "stars": r.get("stargazers_count", 0),
                "language": r.get("language", ""),
                "open_issues": r.get("open_issues_count", 0),
                "description": r.get("description", ""),
                "updated_at": r.get("updated_at", ""),
                "pushed_at": r.get("pushed_at", ""),
                "forks": r.get("forks_count", 0),
            })

        self.state.save_checkpoint("stage1_repos", {"repos": ranked_list})
        logger.info(f"Discovered {len(ranked_list)} candidate repositories.")
        return ranked_list
