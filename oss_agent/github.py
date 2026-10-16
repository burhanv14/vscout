import base64
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
                res = self._run_gh_cli(["api", "search/repositories", "-F", f"q={query}", "-F", f"per_page={limit}", "-F", "sort=updated"])
                if isinstance(res, dict) and "items" in res:
                    return res["items"]
            except Exception as gh_err:
                logger.warning(f"gh search failed, falling back to REST: {gh_err}")

        data = self._request_api("GET", "search/repositories", params={"q": query, "per_page": limit, "sort": "updated"})
        return data.get("items", []) if isinstance(data, dict) else []

    def get_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        if self.use_gh:
            try:
                return self._run_gh_cli(["api", f"repos/{owner}/{repo}"])
            except Exception:
                pass
        return self._request_api("GET", f"repos/{owner}/{repo}")

    def get_repo_file_content(self, owner: str, repo: str, file_path: str, branch: Optional[str] = None) -> Optional[str]:
        """Fetch raw content of a repo file if present."""
        params = {"ref": branch} if branch else {}
        try:
            data = self._request_api("GET", f"repos/{owner}/{repo}/contents/{file_path}", params=params)
            if isinstance(data, dict) and data.get("encoding") == "base64" and "content" in data:
                return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            return None
        except GitHubAPIError:
            return None

    def find_documentation_files(self, owner: str, repo: str) -> Dict[str, str]:
        """Scan standard locations for CONTRIBUTING, CODE_OF_CONDUCT, templates."""
        candidates = [
            "CONTRIBUTING.md",
            "contributing.md",
            ".github/CONTRIBUTING.md",
            "docs/CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md",
            ".github/CODE_OF_CONDUCT.md",
            ".github/pull_request_template.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/PULL_REQUEST_TEMPLATE/pull_request_template.md",
            ".github/issue_template.md",
            ".github/ISSUE_TEMPLATE.md",
            "SECURITY.md",
            "DCO",
        ]
        docs = {}
        for candidate in candidates:
            content = self.get_repo_file_content(owner, repo, candidate)
            if content:
                docs[candidate] = content
        return docs

    def get_issues(
        self,
        owner: str,
        repo: str,
        labels: Optional[List[str]] = None,
        state: str = "open",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Fetch candidate issues. Filter out Pull Requests and assigned issues."""
        params: dict = {"state": state, "per_page": limit, "sort": "updated", "direction": "desc"}
        if labels:
            params["labels"] = ",".join(labels)

        if self.use_gh:
            try:
                gh_args = ["api", f"repos/{owner}/{repo}/issues", "-F", f"state={state}", "-F", f"per_page={limit}"]
                if labels:
                    gh_args.extend(["-F", f"labels={','.join(labels)}"])
                res = self._run_gh_cli(gh_args)
                if isinstance(res, list):
                    return [item for item in res if "pull_request" not in item]
            except Exception:
                pass

        data = self._request_api("GET", f"repos/{owner}/{repo}/issues", params=params)
        if isinstance(data, list):
            # GitHub Issues endpoint also returns PRs; filter them out
            return [item for item in data if "pull_request" not in item]
        return []

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

    def fork_repository(self, owner: str, repo: str) -> Dict[str, Any]:
        """Fork repository to user's authenticated account."""
        if self.use_gh:
            try:
                res = self._run_gh_cli(["repo", "fork", f"{owner}/{repo}", "--clone=false"])
                return {"success": True, "output": res}
            except Exception as e:
                logger.warning(f"gh repo fork failed: {e}")
        return self._request_api("POST", f"repos/{owner}/{repo}/forks")

    def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> Dict[str, Any]:
        """Open a pull request from user fork/branch to upstream repo."""
        if self.use_gh:
            try:
                args = [
                    "pr", "create",
                    "--repo", f"{owner}/{repo}",
                    "--title", title,
                    "--body", body,
                    "--head", head,
                    "--base", base,
                ]
                res = self._run_gh_cli(args)
                return {"html_url": res if isinstance(res, str) else str(res), "title": title}
            except Exception as e:
                logger.warning(f"gh pr create failed, falling back to API: {e}")

        payload = {"title": title, "body": body, "head": head, "base": base}
        return self._request_api("POST", f"repos/{owner}/{repo}/pulls", json_data=payload)

    def post_issue_comment(self, owner: str, repo: str, issue_number: int, comment: str) -> Dict[str, Any]:
        if self.use_gh:
            try:
                self._run_gh_cli(["issue", "comment", str(issue_number), "--repo", f"{owner}/{repo}", "--body", comment])
                return {"success": True}
            except Exception:
                pass
        return self._request_api("POST", f"repos/{owner}/{repo}/issues/{issue_number}/comments", json_data={"body": comment})

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> Dict[str, Any]:
        return self._request_api("GET", f"repos/{owner}/{repo}/pulls/{pr_number}")

    def get_pull_request_reviews(self, owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        return self._request_api("GET", f"repos/{owner}/{repo}/pulls/{pr_number}/reviews")

    def get_pull_request_comments(self, owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        return self._request_api("GET", f"repos/{owner}/{repo}/pulls/{pr_number}/comments")

    def get_combined_status(self, owner: str, repo: str, commit_sha: str) -> Dict[str, Any]:
        try:
            return self._request_api("GET", f"repos/{owner}/{repo}/commits/{commit_sha}/status")
        except Exception:
            return {"state": "unknown", "statuses": []}

    def get_check_runs(self, owner: str, repo: str, commit_sha: str) -> List[Dict[str, Any]]:
        try:
            res = self._request_api("GET", f"repos/{owner}/{repo}/commits/{commit_sha}/check-runs")
            return res.get("check_runs", []) if isinstance(res, dict) else []
        except Exception:
            return []

    def get_user_open_prs(self, username: str) -> List[Dict[str, Any]]:
        """Get open pull requests authored by username across GitHub."""
        query = f"is:pr is:open author:{username} archived:false"
        try:
            res = self._request_api("GET", "search/issues", params={"q": query, "per_page": 20})
            return res.get("items", []) if isinstance(res, dict) else []
        except Exception:
            return []
