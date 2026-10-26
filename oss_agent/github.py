import json
import logging
import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional
import httpx

from oss_agent.config import AppConfig

logger = logging.getLogger("oss_agent.github")


class GitHubAPIError(Exception):
    pass


class GitHubClient:
    """
    Unified GitHub access layer.
    Prefers `gh` CLI if available and authenticated; seamlessly falls back
    to GitHub REST/GraphQL API via token with exponential backoff on rate limits.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.gh_bin = self._find_gh_binary()
        self.token = config.github.token or os.getenv("GITHUB_TOKEN")
        self.use_gh = bool(self.gh_bin and config.github.prefer_gh_cli and self._check_gh_auth())
        self.http_client = httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "OSS-Contributor-Agent/0.1.0",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
            timeout=30.0,
        )

    def _find_gh_binary(self) -> Optional[str]:
        if self.config.github.gh_path and os.path.isfile(self.config.github.gh_path):
            return self.config.github.gh_path
        which_path = shutil.which("gh")
        if which_path:
            return which_path
        # Check standard Windows paths
        common_paths = [
            r"C:\Program Files\GitHub CLI\bin\gh.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\GitHub CLI\bin\gh.exe"),
        ]
        for p in common_paths:
            if os.path.isfile(p):
                return p
        return None

    def _check_gh_auth(self) -> bool:
        if not self.gh_bin:
            return False
        try:
            res = subprocess.run(
                [self.gh_bin, "auth", "status"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return res.returncode == 0
        except Exception:
            return False

    def _backoff_sleep(self, attempt: int, reset_time: Optional[int] = None) -> None:
        if reset_time:
            sleep_sec = max(1, reset_time - int(time.time()))
            logger.warning(f"GitHub Rate Limit hit. Backing off for {sleep_sec}s until reset.")
            time.sleep(min(sleep_sec, 60))
        else:
            delay = min(2**attempt * 2, 30)
            logger.warning(f"GitHub request backoff: sleeping {delay}s...")
            time.sleep(delay)

    def _request_api(
        self, method: str, endpoint: str, params: Optional[dict] = None, json_data: Optional[dict] = None
    ) -> Any:
        url = endpoint if endpoint.startswith("http") else endpoint.lstrip("/")
        max_attempts = 4

        for attempt in range(1, max_attempts + 1):
            try:
                res = self.http_client.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                )
                if res.status_code in (403, 429):
                    # Rate limit or secondary rate limit
                    reset_header = res.headers.get("x-ratelimit-reset")
                    reset_ts = int(reset_header) if reset_header and reset_header.isdigit() else None
                    if attempt < max_attempts:
                        self._backoff_sleep(attempt, reset_ts)
                        continue
                    res.raise_for_status()

                if res.status_code >= 400:
                    res.raise_for_status()

                if res.status_code == 204:
                    return None
                return res.json()
            except httpx.HTTPStatusError as err:
                if attempt == max_attempts:
                    raise GitHubAPIError(f"GitHub API Error {err.response.status_code}: {err.response.text}") from err
                self._backoff_sleep(attempt)
            except httpx.RequestError as req_err:
                if attempt == max_attempts:
                    raise GitHubAPIError(f"GitHub Network Error: {req_err}") from req_err
                self._backoff_sleep(attempt)

        raise GitHubAPIError("Max retries exceeded while calling GitHub API")

    def _run_gh_cli(self, args: list[str]) -> Any:
        assert self.gh_bin is not None
        cmd = [self.gh_bin] + args
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if res.returncode == 0:
                stdout = res.stdout.strip()
                if not stdout:
                    return None
                try:
                    return json.loads(stdout)
                except json.JSONDecodeError:
                    return stdout
            stderr = res.stderr.lower()
            if "rate limit" in stderr or "secondary rate" in stderr:
                if attempt < max_attempts:
                    self._backoff_sleep(attempt)
                    continue
            raise GitHubAPIError(f"gh CLI error (exit {res.returncode}): {res.stderr.strip()}")

    def search_repositories(
        self,
        keywords: List[str],
        min_stars: int = 50,
        max_stars: int = 25000,
        pushed_after_iso: Optional[str] = None,
        limit: int = 15,
        sort: str = "updated",
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """Search GitHub for active, non-archived repositories matching tech stack."""
        q_parts = ["is:public", "archived:false", f"stars:{min_stars}..{max_stars}"]
        if pushed_after_iso:
            q_parts.append(f"pushed:>={pushed_after_iso}")

        # Add language or keyword filter
        if keywords:
            langs = " ".join(f"language:{k}" for k in keywords[:2])
            q_parts.append(f"({langs})")

        query = " ".join(q_parts)

        if self.use_gh:
            try:
                res = self._run_gh_cli(
                    ["api", "search/repositories", "-F", f"q={query}", "-F", f"per_page={limit}", "-F", f"sort={sort}", "-F", f"page={page}"]
                )
                if isinstance(res, dict) and "items" in res:
                    return res["items"]
            except Exception as gh_err:
                logger.warning(f"gh search failed, falling back to REST: {gh_err}")

        data = self._request_api(
            "GET",
            "search/repositories",
            params={"q": query, "per_page": limit, "sort": sort, "page": page},
        )
        return data.get("items", []) if isinstance(data, dict) else []

    def get_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        if self.use_gh:
            try:
                return self._run_gh_cli(["api", f"repos/{owner}/{repo}"])
            except Exception:
                pass
        return self._request_api("GET", f"repos/{owner}/{repo}")

    def _fetch_issues_page(
        self,
        owner: str,
        repo: str,
        label: Optional[str],
        state: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        """Fetch a single page of issues, optionally filtered by one label."""
        params: dict = {"state": state, "per_page": limit, "sort": "updated", "direction": "desc"}
        if label:
            params["labels"] = label

        if self.use_gh:
            try:
                gh_args = ["api", f"repos/{owner}/{repo}/issues", "-F", f"state={state}", "-F", f"per_page={limit}"]
                if label:
                    gh_args.extend(["-F", f"labels={label}"])
                res = self._run_gh_cli(gh_args)
                if isinstance(res, list):
                    return [item for item in res if "pull_request" not in item]
            except Exception:
                pass

        data = self._request_api("GET", f"repos/{owner}/{repo}/issues", params=params)
        if isinstance(data, list):
            return [item for item in data if "pull_request" not in item]
        return []

    def get_issues(
        self,
        owner: str,
        repo: str,
        labels: Optional[List[str]] = None,
        state: str = "open",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Fetch candidate issues.

        GitHub's REST `labels` filter uses AND semantics (issue must have ALL
        labels). To match the intent of "any of these preferred labels", we
        issue one request per label when multiple are supplied and merge the
        results (OR semantics), deduplicating by issue number.
        """
        if not labels:
            return self._fetch_issues_page(owner, repo, None, state, limit)

        if len(labels) == 1:
            return self._fetch_issues_page(owner, repo, labels[0], state, limit)

        # OR semantics: one call per preferred label, merge + dedupe
        merged: List[Dict[str, Any]] = []
        seen_numbers: set = set()
        per_label_limit = max(1, limit // len(labels)) + 1
        for label in labels:
            try:
                page = self._fetch_issues_page(owner, repo, label, state, per_label_limit)
            except Exception as exc:
                logger.warning(f"Failed to fetch issues for {owner}/{repo} with label '{label}': {exc}")
                continue
            for item in page:
                number = item.get("number")
                if number is not None and number in seen_numbers:
                    continue
                if number is not None:
                    seen_numbers.add(number)
                merged.append(item)
                if len(merged) >= limit:
                    break
            if len(merged) >= limit:
                break

        merged.sort(key=lambda it: it.get("updated_at", ""), reverse=True)
        return merged[:limit]

    def get_issue(self, owner: str, repo: str, issue_number: int) -> Dict[str, Any]:
        return self._request_api("GET", f"repos/{owner}/{repo}/issues/{issue_number}")

    def get_issue_comments(self, owner: str, repo: str, issue_number: int, limit: int = 50) -> List[Dict[str, Any]]:
        return self._request_api("GET", f"repos/{owner}/{repo}/issues/{issue_number}/comments", params={"per_page": limit})

    def get_issue_timeline(self, owner: str, repo: str, issue_number: int, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch timeline events (cross-references, mentions, linked PRs)."""
        headers = {"Accept": "application/vnd.github.mockingbird-preview+json"}
        try:
            return self._request_api("GET", f"repos/{owner}/{repo}/issues/{issue_number}/timeline", params={"per_page": limit})
        except Exception:
            return []

    def search_commits_for_issue(self, owner: str, repo: str, issue_number: int) -> List[Dict[str, Any]]:
        """Search commits referencing this issue."""
        query = f"repo:{owner}/{repo} {issue_number}"
        try:
            res = self._request_api("GET", "search/commits", params={"q": query, "per_page": 5})
            return res.get("items", []) if isinstance(res, dict) else []
        except Exception:
            return []

    def search_pull_requests_for_issue(self, owner: str, repo: str, issue_number: int) -> List[Dict[str, Any]]:
        """Search open/closed PRs referencing this issue."""
        query = f"repo:{owner}/{repo} is:pr {issue_number}"
        try:
            res = self._request_api("GET", "search/issues", params={"q": query, "per_page": 10})
            return res.get("items", []) if isinstance(res, dict) else []
        except Exception:
            return []


