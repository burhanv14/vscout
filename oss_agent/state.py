import json
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional

from oss_agent.config import get_project_root


class StateStore:
    """SQLite-backed persistent and resumable state store under state/pipeline.db."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            state_dir = get_project_root() / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            db_path = state_dir / "pipeline.db"
        self.db_path = db_path
        self._init_tables()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self) -> None:
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS repos (
                    repo_full_name TEXT PRIMARY KEY,
                    stars INTEGER,
                    language TEXT,
                    health_score INTEGER,
                    fit_score INTEGER,
                    recommendation TEXT,
                    evaluation_json TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS issues (
                    id TEXT PRIMARY KEY, -- repo_full_name#issue_number
                    repo_full_name TEXT,
                    issue_number INTEGER,
                    title TEXT,
                    author TEXT,
                    url TEXT,
                    labels_json TEXT,
                    status TEXT, -- available, claimed, pr_exists, already_fixed, stale, etc.
                    worth_score INTEGER,
                    difficulty TEXT,
                    estimated_hours REAL,
                    risks_json TEXT,
                    reasoning TEXT,
                    policy_json TEXT,
                    decision TEXT, -- CONTRIBUTE, SKIP, NEEDS_HUMAN
                    workspace_dir TEXT,
                    branch_name TEXT,
                    diff_summary TEXT,
                    pr_url TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS pr_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_full_name TEXT,
                    issue_number INTEGER,
                    pr_url TEXT,
                    submitted_date TEXT, -- YYYY-MM-DD
                    submitted_at TEXT
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    stage_name TEXT PRIMARY KEY,
                    payload_json TEXT,
                    updated_at TEXT
                );
                """
            )

    def save_repo_evaluation(
        self,
        repo_full_name: str,
        stars: int,
        language: str,
        health_score: int,
        fit_score: int,
        recommendation: str,
        eval_dict: Dict[str, Any],
    ) -> None:
        now = datetime.utcnow().isoformat() + "Z"
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO repos (repo_full_name, stars, language, health_score, fit_score, recommendation, evaluation_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_full_name) DO UPDATE SET
                    stars=excluded.stars,
                    language=excluded.language,
                    health_score=excluded.health_score,
                    fit_score=excluded.fit_score,
                    recommendation=excluded.recommendation,
                    evaluation_json=excluded.evaluation_json,
                    updated_at=excluded.updated_at
                """,
                (
                    repo_full_name,
                    stars,
                    language,
                    health_score,
                    fit_score,
                    recommendation,
                    json.dumps(eval_dict),
                    now,
                ),
            )

    def save_issue_triage(
        self,
        repo_full_name: str,
        issue_number: int,
        title: str,
        author: str,
        url: str,
        labels: List[str],
        status: str,
        worth_score: int,
        difficulty: str,
        estimated_hours: float,
        risks: List[str],
        reasoning: str,
    ) -> None:
        issue_id = f"{repo_full_name}#{issue_number}"
        now = datetime.utcnow().isoformat() + "Z"
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO issues (id, repo_full_name, issue_number, title, author, url, labels_json, status, worth_score, difficulty, estimated_hours, risks_json, reasoning, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    author=excluded.author,
                    url=excluded.url,
                    labels_json=excluded.labels_json,
                    status=excluded.status,
                    worth_score=excluded.worth_score,
                    difficulty=excluded.difficulty,
                    estimated_hours=excluded.estimated_hours,
                    risks_json=excluded.risks_json,
                    reasoning=excluded.reasoning,
                    updated_at=excluded.updated_at
                """,
                (
                    issue_id,
                    repo_full_name,
                    issue_number,
                    title,
                    author,
                    url,
                    json.dumps(labels),
                    status,
                    worth_score,
                    difficulty,
                    estimated_hours,
                    json.dumps(risks),
                    reasoning,
                    now,
                ),
            )

    def save_policy_and_decision(
        self,
        repo_full_name: str,
        issue_number: int,
        policy_dict: Dict[str, Any],
        decision: str,
    ) -> None:
        issue_id = f"{repo_full_name}#{issue_number}"
        now = datetime.utcnow().isoformat() + "Z"
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE issues SET
                    policy_json = ?,
                    decision = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(policy_dict), decision, now, issue_id),
            )

    def update_preparation_state(
        self,
        repo_full_name: str,
        issue_number: int,
        workspace_dir: str,
        branch_name: str,
        diff_summary: str,
    ) -> None:
        issue_id = f"{repo_full_name}#{issue_number}"
        now = datetime.utcnow().isoformat() + "Z"
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE issues SET
                    workspace_dir = ?,
                    branch_name = ?,
                    diff_summary = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (workspace_dir, branch_name, diff_summary, now, issue_id),
            )

    def record_pr_submission(
        self,
        repo_full_name: str,
        issue_number: int,
        pr_url: str,
    ) -> None:
        now = datetime.utcnow()
        today = now.strftime("%Y-%m-%d")
        iso_now = now.isoformat() + "Z"
        issue_id = f"{repo_full_name}#{issue_number}"
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO pr_submissions (repo_full_name, issue_number, pr_url, submitted_date, submitted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (repo_full_name, issue_number, pr_url, today, iso_now),
            )
            conn.execute(
                """
                UPDATE issues SET pr_url = ?, updated_at = ? WHERE id = ?
                """,
                (pr_url, iso_now, issue_id),
            )

    def get_pr_count_for_today(self) -> int:
        today = date.today().strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM pr_submissions WHERE submitted_date = ?",
                (today,),
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def get_issue(self, repo_full_name: str, issue_number: int) -> Optional[Dict[str, Any]]:
        issue_id = f"{repo_full_name}#{issue_number}"
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            return dict(row) if row else None

    def get_top_contribute_issues(self, min_score: int = 60, limit: int = 10) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM issues
                WHERE (decision = 'CONTRIBUTE' OR decision = 'NEEDS_HUMAN' OR decision IS NULL)
                  AND worth_score >= ?
                ORDER BY worth_score DESC
                LIMIT ?
                """,
                (min_score, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def save_checkpoint(self, stage_name: str, payload: Dict[str, Any]) -> None:
        now = datetime.utcnow().isoformat() + "Z"
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints (stage_name, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(stage_name) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (stage_name, json.dumps(payload), now),
            )

    def get_checkpoint(self, stage_name: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT payload_json FROM checkpoints WHERE stage_name = ?", (stage_name,)
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None
