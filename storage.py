"""
Storage Backend
===============
Stores all orchestration logs in Supabase.

Usage:
    from storage import create_storage

    store = create_storage()  # Uses SUPABASE_URL and SUPABASE_KEY from env
    store.create_run(run_id, prompt, project_dir)
    step_id = store.log_step(...)
    store.log_events_batch(run_id, step_id, events)
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


# ─────────────────────────────────────────────
# Supabase Storage Backend
# ─────────────────────────────────────────────

class SupabaseStorage:
    """
    Stores all logs in Supabase.
    Requires: pip install supabase
    Env vars: SUPABASE_URL, SUPABASE_KEY (use service_role key)
    """

    def __init__(self, url: Optional[str] = None, key: Optional[str] = None):
        self.url = url or os.environ.get("SUPABASE_URL")
        self.key = key or os.environ.get("SUPABASE_KEY")

        if not self.url or not self.key:
            raise ValueError(
                "Supabase credentials required. Set SUPABASE_URL and SUPABASE_KEY "
                "environment variables (or create a .env file), or pass them directly.\n"
                "Use the service_role key from Supabase Dashboard > Settings > API"
            )

        try:
            from supabase import create_client, Client
            self.client: Client = create_client(self.url, self.key)
        except ImportError:
            raise ImportError(
                "supabase-py not installed. Run: pip install supabase"
            )

    def init(self):
        """
        Tables must be created in Supabase beforehand via the SQL migration.
        This just verifies connectivity by doing a simple query.
        """
        try:
            self.client.table("orchestrator_runs").select("run_id").limit(1).execute()
            print("  ✅ Supabase connection verified")
        except Exception as e:
            raise ConnectionError(
                f"Failed to connect to Supabase: {e}\n"
                f"Make sure you've run the migration SQL in your Supabase project."
            )

    def create_run(self, run_id: str, user_prompt: str, project_dir: str) -> None:
        self.client.table("orchestrator_runs").insert({
            "run_id": run_id,
            "user_prompt": user_prompt,
            "project_dir": project_dir,
            "status": "running",
            "created_at": _now(),
        }).execute()

    def update_run_status(self, run_id: str, status: str) -> None:
        self.client.table("orchestrator_runs").update({
            "status": status,
        }).eq("run_id", run_id).execute()

    def finish_run(self, run_id: str, status: str = "completed") -> None:
        self.client.table("orchestrator_runs").update({
            "status": status,
            "finished_at": _now(),
        }).eq("run_id", run_id).execute()

    def log_step(
        self, run_id: str, step_number: int, phase: str, tool: str,
        prompt_sent: str, raw_stdout: str, raw_stderr: str,
        parsed_result: str, exit_code: int, duration_seconds: float,
        build_phase: Optional[str] = None,
        commands_executed: Optional[list] = None,
        errors_normalized: Optional[list] = None,
        skills_info: Optional[dict] = None,
        # Token usage
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        cache_read_tokens: Optional[int] = None,
        cache_creation_tokens: Optional[int] = None,
        cost_usd: Optional[float] = None,
    ) -> int:
        """Log a step and return the step ID. Raises on failure."""
        result = self.client.table("orchestrator_steps").insert({
            "run_id": run_id,
            "step_number": step_number,
            "phase": phase,
            "tool": tool,
            "prompt_sent": prompt_sent,
            "raw_stdout": raw_stdout,
            "raw_stderr": raw_stderr,
            "parsed_result": parsed_result,
            "exit_code": exit_code,
            "duration_seconds": duration_seconds,
            "build_phase": build_phase,
            "commands_executed": commands_executed,
            "errors_normalized": errors_normalized,
            "skills_info": skills_info,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cost_usd": cost_usd,
            "timestamp": _now(),
        }).execute()

        # Extract and validate the returned step ID
        if result.data and len(result.data) > 0:
            step_id = result.data[0].get("id")
            if step_id is not None:
                return int(step_id)

        # If we didn't get a valid ID, raise an error
        raise RuntimeError(
            f"Failed to get step_id from Supabase insert. "
            f"Response: {result.data}"
        )

    def log_event(
        self, run_id: str, step_id: int, event_type: str, event_data: dict | str,
    ) -> None:
        # If event_data is a string, try to parse it as JSON for JSONB storage
        if isinstance(event_data, str):
            try:
                event_data = json.loads(event_data)
            except json.JSONDecodeError:
                event_data = {"raw": event_data}

        self.client.table("orchestrator_events").insert({
            "run_id": run_id,
            "step_id": step_id,
            "event_type": event_type,
            "event_data": event_data,
            "timestamp": _now(),
        }).execute()

    def log_events_batch(
        self, run_id: str, step_id: int, events: list[dict],
    ) -> None:
        """Batch insert events. Step must exist before calling this."""
        if not events:
            return

        # Validate step_id before inserting events
        if step_id is None or step_id <= 0:
            print(f"  ⚠️  Invalid step_id ({step_id}), skipping {len(events)} events")
            return

        rows = []
        ts = _now()
        for event in events:
            rows.append({
                "run_id": run_id,
                "step_id": step_id,
                "event_type": event.get("type", "unknown"),
                # Pass dict directly - supabase-py serializes it for JSONB columns
                "event_data": event,
                "timestamp": ts,
            })

        # Insert in chunks of 500 to avoid payload limits
        chunk_size = 500
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i:i + chunk_size]
            try:
                self.client.table("orchestrator_events").insert(chunk).execute()
            except Exception as e:
                print(f"  ⚠️  Failed to insert events batch: {e}")
                # Continue with remaining chunks

    def get_run(self, run_id: str) -> Optional[dict]:
        result = self.client.table("orchestrator_runs") \
            .select("*").eq("run_id", run_id).execute()
        return result.data[0] if result.data else None

    def list_runs(self) -> list[dict]:
        result = self.client.table("orchestrator_runs") \
            .select("*").order("created_at", desc=True).execute()
        return result.data or []

    def get_steps(self, run_id: str) -> list[dict]:
        result = self.client.table("orchestrator_steps") \
            .select("*").eq("run_id", run_id) \
            .order("step_number").order("id").execute()
        return result.data or []

    def get_events(self, run_id: str, step_id: Optional[int] = None) -> list[dict]:
        """Retrieve all events for a run, paginating to bypass the 1000 row limit."""
        all_events = []
        page_size = 1000
        offset = 0

        while True:
            query = self.client.table("orchestrator_events") \
                .select("*").eq("run_id", run_id)
            if step_id is not None:
                query = query.eq("step_id", step_id)
            result = query.order("id").range(offset, offset + page_size - 1).execute()

            batch = result.data or []
            all_events.extend(batch)

            if len(batch) < page_size:
                break  # Last page
            offset += page_size

        return all_events

    def get_step_events(self, step_id: int) -> list[dict]:
        result = self.client.table("orchestrator_events") \
            .select("*").eq("step_id", step_id).order("id").execute()
        return result.data or []


# ─────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────

def create_storage(
    supabase_url: Optional[str] = None,
    supabase_key: Optional[str] = None,
) -> SupabaseStorage:
    """
    Create the Supabase storage backend.

    Credentials are read from environment variables (SUPABASE_URL, SUPABASE_KEY)
    or can be passed directly.
    """
    store = SupabaseStorage(url=supabase_url, key=supabase_key)
    store.init()
    return store


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
