import os
from pathlib import Path
from typing import Any, List, Optional
import yaml
from pydantic import BaseModel, Field


class OpenCodeConfig(BaseModel):
    backend: str = Field(default="cli", description="'cli' or 'server'")
    server_url: str = Field(default="http://localhost:4096")
    model: str = Field(default="", description="Provider/model#variant if set")
    opencode_path: str = Field(default="", description="Optional path to opencode binary")
    timeout_seconds: int = Field(default=180)
    max_retries: int = Field(default=3)


class GitHubConfig(BaseModel):
    username: str = Field(default="")
    gh_path: str = Field(default="", description="Optional path to gh binary")
    prefer_gh_cli: bool = Field(default=True)
    token: Optional[str] = Field(default=None)


class GitConfig(BaseModel):
    author_name: str = Field(default="OSS Contributor")
    author_email: str = Field(default="contributor@example.com")
    git_path: str = Field(default="", description="Optional explicit path to git binary")


class SandboxConfig(BaseModel):
    mode: str = Field(default="auto", description="'auto', 'docker', or 'venv'")
    timeout_seconds: int = Field(default=120)


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
    opencode: OpenCodeConfig = Field(default_factory=OpenCodeConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)


def render_template(template_text: str, **kwargs: Any) -> str:
    """Safely replace {key} placeholders without interfering with JSON curly braces."""
    result = template_text
    for key, val in kwargs.items():
        result = result.replace(f"{{{key}}}", str(val))
    return result


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

    env_server = os.getenv("OPENCODE_SERVER_URL")
    if env_server:
        config.opencode.server_url = env_server

    return config
