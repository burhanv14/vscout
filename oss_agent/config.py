import os
from pathlib import Path
from typing import List, Optional
import yaml
from pydantic import BaseModel, Field


class GitHubConfig(BaseModel):
    username: str = Field(default="")
    gh_path: str = Field(default="", description="Optional path to gh binary")
    prefer_gh_cli: bool = Field(default=True)
    token: Optional[str] = Field(default=None)


class GitConfig(BaseModel):
    author_name: str = Field(default="OSS Contributor")
    author_email: str = Field(default="contributor@example.com")
    git_path: str = Field(default="", description="Optional explicit path to git binary")


class AppConfig(BaseModel):
    tech_stack: List[str] = Field(default_factory=lambda: ["python", "typescript"])
    experience_level: str = Field(default="intermediate")
    preferred_labels: List[str] = Field(
        default_factory=lambda: ["good first issue", "help wanted", "bug", "docs"]
    )
    min_repo_stars: int = Field(default=50)
    max_repo_stars: int = Field(default=25000)
    min_recent_activity_days: int = Field(default=30)
    excluded_repos_or_orgs: List[str] = Field(default_factory=list)
    max_candidates_per_run: int = Field(default=5)
    worth_score_threshold: int = Field(default=65)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    git: GitConfig = Field(default_factory=GitConfig)


def load_env_file(filepath: Path) -> None:
    """Manually parse .env file without external dependencies."""
    if not filepath.exists():
        return
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'\"")
            if key not in os.environ:
                os.environ[key] = val


def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ensure_github_token(interactive: bool = True) -> str:
    """Ensure a GitHub token is available for API access.

    Checks (in order): the ``GITHUB_TOKEN`` env var, then ``.env``. If none is
    found and ``interactive`` is true, prompts the user once and writes it to
    ``.env`` so future runs don't ask again. Returns the token (possibly empty).
    """
    token = os.getenv("GITHUB_TOKEN")
    if token:
        return token

    root = get_project_root()
    env_file = root / ".env"

    # Check an existing .env file for a token.
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("GITHUB_TOKEN="):
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    if val:
                        os.environ["GITHUB_TOKEN"] = val
                        return val

    if not interactive:
        return ""

    try:
        token = input(
            "GitHub token not found. "
            "Enter a Personal Access Token (or leave blank to use unauthenticated access): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""

    if token:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        # Preserve any existing .env content, replacing/adding the token line.
        lines = []
        if env_file.exists():
            with open(env_file, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        lines = [l for l in lines if not l.strip().startswith("GITHUB_TOKEN=")]
        lines.append(f"GITHUB_TOKEN={token}")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.environ["GITHUB_TOKEN"] = token
        print(f"Token saved to {env_file}")
    return token


def load_config(config_path: Optional[Path] = None) -> AppConfig:
    root = get_project_root()
    load_env_file(root / ".env")

    if config_path is None:
        config_path = root / "config.yaml"
        if not config_path.exists():
            config_path = root / "config.example.yaml"

    data = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    config = AppConfig(**data)

    # Allow environment variable overrides
    env_token = os.getenv("GITHUB_TOKEN")
    if env_token:
        config.github.token = env_token

    return config
