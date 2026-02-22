"""SQLite database layer for the analysis dashboard."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

# Database file location relative to project root
DB_PATH = Path(__file__).parent.parent / "dashboard.db"


def init_db() -> None:
    """Initialize the database and create tables if they don't exist."""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                generated_at TEXT,
                prompt TEXT,
                status TEXT,
                duration_minutes REAL,
                total_steps INTEGER,
                passed_steps INTEGER,
                failed_steps INTEGER,
                total_retries INTEGER,
                replan_checkpoints INTEGER,
                replans_triggered INTEGER,
                success_rate REAL,
                planner TEXT,
                implementer TEXT,
                verifier TEXT,
                models_used TEXT,
                rls_issues INTEGER DEFAULT 0,
                migration_issues INTEGER DEFAULT 0,
                edge_function_issues INTEGER DEFAULT 0,
                auth_issues INTEGER DEFAULT 0,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                total_cache_read_tokens INTEGER DEFAULT 0,
                total_cache_creation_tokens INTEGER DEFAULT 0,
                total_cost_usd REAL DEFAULT 0,
                events_count INTEGER DEFAULT 0,
                events_may_be_truncated BOOLEAN DEFAULT 0,
                steps_with_events INTEGER DEFAULT 0,
                steps_without_events INTEGER DEFAULT 0,
                event_coverage TEXT,
                ingested_at TEXT,
                classified_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS steps (
                id TEXT PRIMARY KEY,
                run_id TEXT REFERENCES runs(run_id),
                step_number INTEGER,
                build_phase TEXT,
                phase TEXT,
                tool TEXT,
                final_verdict TEXT,
                attempts INTEGER,
                retries INTEGER,
                duration_seconds REAL,
                resolution_actions TEXT,
                error_categories TEXT,
                errors_summary TEXT,
                classification TEXT,
                classification_confidence REAL,
                classification_reasoning TEXT,
                classification_evidence TEXT,
                approach_changed BOOLEAN,
                same_file_repeated BOOLEAN,
                error_category_stable BOOLEAN,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                has_events BOOLEAN DEFAULT 0
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT REFERENCES runs(run_id),
                step_number INTEGER,
                build_phase TEXT,
                phase TEXT,
                category TEXT,
                error TEXT,
                exit_code INTEGER
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS web_searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT REFERENCES runs(run_id),
                step_id TEXT,
                query TEXT,
                count INTEGER,
                timestamp TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_type TEXT,
                pattern_key TEXT,
                occurrence_count INTEGER,
                architectural_count INTEGER,
                implementation_count INTEGER,
                example_run_ids TEXT,
                last_updated TEXT
            )
        """)

        conn.commit()


@contextmanager
def get_db():
    """Context manager that yields a database connection with Row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    """Convert a sqlite3.Row to a dict, or return None if row is None."""
    if row is None:
        return None
    return dict(row)


def _rows_to_dicts(rows: list) -> list[dict]:
    """Convert a list of sqlite3.Row objects to a list of dicts."""
    return [dict(row) for row in rows]


def get_all_runs() -> list[dict]:
    """Get all runs from the database."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM runs ORDER BY ingested_at DESC")
        return _rows_to_dicts(cursor.fetchall())


def get_run(run_id: str) -> Optional[dict]:
    """Get a single run by run_id."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        return _row_to_dict(cursor.fetchone())


def get_steps_for_run(run_id: str) -> list[dict]:
    """Get all steps for a given run, ordered by step number."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY step_number",
            (run_id,)
        )
        return _rows_to_dicts(cursor.fetchall())


def get_step_detail(run_id: str, step_number: int) -> Optional[dict]:
    """Get a specific step by run_id and step_number."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM steps WHERE run_id = ? AND step_number = ?",
            (run_id, step_number)
        )
        return _row_to_dict(cursor.fetchone())


def get_failures_for_run(run_id: str) -> list[dict]:
    """Get all failures for a given run."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM failures WHERE run_id = ? ORDER BY step_number",
            (run_id,)
        )
        return _rows_to_dicts(cursor.fetchall())


def get_web_searches_for_run(run_id: str) -> list[dict]:
    """Get all web searches for a given run."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM web_searches WHERE run_id = ? ORDER BY timestamp",
            (run_id,)
        )
        return _rows_to_dicts(cursor.fetchall())


def run_exists(run_id: str) -> bool:
    """Check if a run exists in the database."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM runs WHERE run_id = ? LIMIT 1",
            (run_id,)
        )
        return cursor.fetchone() is not None


def get_classification_summary(run_id: str) -> dict:
    """
    Get a summary of classifications for a run.

    Returns a dict with counts of:
    - architectural
    - implementation
    - clean_pass
    - ambiguous
    - pending (NULL or empty classification)
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                COALESCE(classification, 'pending') as classification,
                COUNT(*) as count
            FROM steps
            WHERE run_id = ?
            GROUP BY COALESCE(classification, 'pending')
        """, (run_id,))

        results = cursor.fetchall()

        summary = {
            "architectural": 0,
            "implementation": 0,
            "clean_pass": 0,
            "ambiguous": 0,
            "pending": 0
        }

        for row in results:
            classification = row["classification"].lower() if row["classification"] else "pending"
            if classification in summary:
                summary[classification] = row["count"]
            elif classification == "" or classification == "pending":
                summary["pending"] += row["count"]
            else:
                # Handle any unexpected classification values
                summary["ambiguous"] += row["count"]

        return summary
