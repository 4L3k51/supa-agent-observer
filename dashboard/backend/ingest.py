"""Report ingestion system for the analysis dashboard."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .db import get_db, run_exists

# Reports directory relative to project root
REPORTS_DIR = Path(__file__).parent.parent.parent / "reports"


def _safe_int(value) -> Optional[int]:
    """Safely convert a value to int, returning None if not possible."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def get_raw_steps_for_step_number(raw_data: dict, step_number: int) -> list[dict]:
    """
    Extract all raw_data.steps entries for a given step_number.

    Args:
        raw_data: The raw_data section from the report
        step_number: The step number to filter by

    Returns:
        List of raw step dicts matching the step_number
    """
    steps = raw_data.get("steps", [])
    # Coerce to int for consistent comparison (JSON may have int or string)
    target = int(step_number) if step_number is not None else None
    # raw_data.steps uses "step_number" field, not "step"
    return [s for s in steps if _safe_int(s.get("step_number")) == target]


def _delete_run_data(conn: sqlite3.Connection, run_id: str) -> None:
    """Delete all data for a given run_id from all tables."""
    cursor = conn.cursor()
    cursor.execute("DELETE FROM web_searches WHERE run_id = ?", (run_id,))
    cursor.execute("DELETE FROM failures WHERE run_id = ?", (run_id,))
    cursor.execute("DELETE FROM steps WHERE run_id = ?", (run_id,))
    cursor.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))


def _extract_phase_from_raw_steps(raw_steps: list[dict]) -> Optional[str]:
    """
    Extract phase information from raw steps.

    Priority: If any raw step has a runtime test phase, use that.
    Otherwise use the last entry's phase.
    """
    if not raw_steps:
        return None

    phases = [s.get("phase") for s in raw_steps if s.get("phase")]
    if not phases:
        return None

    # Runtime test phases take priority - if any step has one, use it
    runtime_test_phases = {
        "smoke_test", "browser_test", "browser_test_gen",
        "browser_test_fix", "browser_test_fix_verify", "approach_analysis"
    }

    for phase in phases:
        if phase in runtime_test_phases:
            return phase

    # Otherwise return the last phase (most recent)
    return phases[-1]


def _extract_tool_from_raw_steps(raw_steps: list[dict]) -> Optional[str]:
    """Extract tool information from raw steps."""
    if not raw_steps:
        return None

    tools = [s.get("tool") for s in raw_steps if s.get("tool")]
    if not tools:
        return None

    # Return the most common tool, or the first one if tied
    from collections import Counter
    tool_counts = Counter(tools)
    return tool_counts.most_common(1)[0][0] if tool_counts else None


def _is_false_positive_failure(failure: dict) -> bool:
    """
    Check if a failure entry is actually a false positive (success mistakenly labeled as failure).

    The analyzer sometimes includes success messages in failures.details.
    The "other" category is a catch-all from regex that doesn't match real error patterns.
    Filter out entries that are clearly not failures.
    """
    error = (failure.get("error") or "").strip().lower()
    category = (failure.get("category") or "").strip().lower()
    exit_code = failure.get("exit_code")

    # Success indicators that should not be in failures
    success_indicators = [
        "step is complete",
        "status: pass",
        "status: proceed",
        "successfully",
        "completed successfully",
        "all tests passed",
        "all tests pass",
        "build successful",
        "build_succeeds: yes",
        "build_succeeds:yes",
        "all done",
        "everything looks good",
        "passed:",
        "tests pass",
        "looks good",
        "completed without",
    ]

    # Category "other" with exit_code 0 or None is almost always a false positive
    if category == "other" or not category:
        # If exit_code is 0 or None, it's likely not a real failure
        if exit_code == 0 or exit_code is None:
            return True
        # Even with exit_code, check for success indicators
        for indicator in success_indicators:
            if indicator in error:
                return True

    # For any category, filter out clear success messages
    for indicator in success_indicators:
        if indicator in error:
            # Only filter if there's no clear failure indicator
            if "fail" not in error and "error" not in error and "exception" not in error:
                return True

    # If error message is very short and looks like a pass
    if len(error) < 100 and any(word in error for word in ["pass", "complete", "success", "proceed", "done"]):
        if "fail" not in error and "error" not in error and "exception" not in error:
            return True

    return False


def _get_failures_for_step(failures_details: list[dict], step_number: int) -> list[dict]:
    """Get all failure details for a specific step number, filtering out false positives."""
    target = int(step_number) if step_number is not None else None
    return [
        f for f in failures_details
        if _safe_int(f.get("step")) == target and not _is_false_positive_failure(f)
    ]


def _extract_web_searches_from_events(events: list[dict]) -> list[dict]:
    """
    Extract web search queries and results from raw_data.events.

    WebSearch tool invocations are in event_type="assistant" events,
    inside event_data.message.content[] as items with type="tool_use"
    and name="WebSearch". The query is in input.query.

    Results are in event_type="user" events, inside
    event_data.message.content[] as items with type="tool_result".
    Match results to queries using tool_use_id.
    """
    # First pass: collect all WebSearch tool_use blocks with their IDs
    tool_uses = {}  # tool_use_id -> {step_id, query, timestamp, results, full_text_result}

    for e in events:
        event_data = e.get("event_data", {})
        if isinstance(event_data, str):
            try:
                event_data = json.loads(event_data)
            except:
                continue

        # Look for WebSearch in assistant events
        if e.get("event_type") == "assistant":
            message = event_data.get("message", {})
            content = message.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if (isinstance(item, dict) and
                        item.get("type") == "tool_use" and
                        item.get("name") == "WebSearch"):
                        tool_id = item.get("id")
                        query = item.get("input", {}).get("query", "")
                        if tool_id and query:
                            tool_uses[tool_id] = {
                                "step_id": e.get("step_id"),
                                "query": query,
                                "timestamp": e.get("timestamp"),
                                "results": [],
                                "full_text_result": None,
                            }

    # Second pass: match tool_result blocks to tool_use blocks
    for e in events:
        event_data = e.get("event_data", {})
        if isinstance(event_data, str):
            try:
                event_data = json.loads(event_data)
            except:
                continue

        # Look for tool_result in user events
        if e.get("event_type") == "user":
            message = event_data.get("message", {})
            content = message.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        tool_use_id = item.get("tool_use_id")
                        if tool_use_id and tool_use_id in tool_uses:
                            # Get the full text result
                            result_content = item.get("content", "")
                            if isinstance(result_content, list):
                                # Content might be a list of text blocks
                                result_content = "\n".join(
                                    c.get("text", str(c)) if isinstance(c, dict) else str(c)
                                    for c in result_content
                                )
                            tool_uses[tool_use_id]["full_text_result"] = result_content

            # Also check for tool_use_result at event level (structured data)
            tool_use_result = event_data.get("tool_use_result", {})
            if not isinstance(tool_use_result, dict):
                continue
            if tool_use_result:
                tool_use_id = tool_use_result.get("tool_use_id")
                if tool_use_id and tool_use_id in tool_uses:
                    results = tool_use_result.get("results", [])
                    if isinstance(results, list):
                        for r in results:
                            if isinstance(r, dict):
                                url = r.get("url", "")
                                title = r.get("title", "")
                                if url:
                                    tool_uses[tool_use_id]["results"].append({
                                        "url": url,
                                        "title": title,
                                    })

    # Also check usage stats for implicit web_search_requests (server-side searches)
    for e in events:
        event_data = e.get("event_data", {})
        if isinstance(event_data, str):
            try:
                event_data = json.loads(event_data)
            except:
                continue

        if e.get("event_type") == "result":
            usage = event_data.get("usage", {})
            server_tool_use = usage.get("server_tool_use", {})
            web_requests = server_tool_use.get("web_search_requests", 0)
            if web_requests > 0:
                # Create a synthetic entry for implicit searches
                tool_uses[f"implicit_{e.get('step_id')}_{e.get('timestamp')}"] = {
                    "step_id": e.get("step_id"),
                    "query": "(implicit web search)",
                    "timestamp": e.get("timestamp"),
                    "results": [],
                    "full_text_result": None,
                    "count": web_requests,
                }

    return list(tool_uses.values())


def _ingest_single_report(conn: sqlite3.Connection, report_path: Path) -> str:
    """
    Ingest a single report file.

    Returns:
        The run_id of the ingested report

    Raises:
        Exception if the report is malformed
    """
    with open(report_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    run_id = data["run_id"]
    cursor = conn.cursor()

    # Extract summary data
    summary = data.get("summary", {})
    tools_config = data.get("tools_config", {})
    supabase_specific = data.get("supabase_specific", {})
    token_usage = data.get("token_usage", {})
    raw_data = data.get("raw_data", {})

    # Check events count and potential truncation
    events = raw_data.get("events", [])
    events_count = len(events)
    # Flag if events might be truncated (1000 is a common API limit)
    events_may_be_truncated = events_count >= 1000 or events_count == 0

    # Build mapping from step_number to list of step_ids from raw_data.steps
    # raw_data.steps has: {"id": step_id, "step_number": step_number, "phase": phase, ...}
    # Multiple raw_data.steps can share the same step_number (implement/verify cycles)
    raw_steps_list = raw_data.get("steps", [])
    step_number_to_ids: dict[int, set[int]] = {}
    for rs in raw_steps_list:
        step_num = _safe_int(rs.get("step_number"))
        step_id = _safe_int(rs.get("id"))
        if step_num is not None and step_id is not None:
            if step_num not in step_number_to_ids:
                step_number_to_ids[step_num] = set()
            step_number_to_ids[step_num].add(step_id)

    # Collect step_ids that have events (coerce to int for consistent comparison)
    step_ids_with_events: set[int] = set()
    for e in events:
        step_id = _safe_int(e.get("step_id"))
        if step_id is not None:
            step_ids_with_events.add(step_id)

    # For each step_outcome (by step_number), check if ANY of its step_ids have events
    step_outcomes = data.get("step_outcomes", [])
    step_number_has_events: dict[int, bool] = {}
    for step_outcome in step_outcomes:
        step_number = _safe_int(step_outcome.get("step"))
        if step_number is None:
            continue
        # Get all step_ids for this step_number
        ids_for_step = step_number_to_ids.get(step_number, set())
        # Check if any of these ids have events
        has_events = bool(ids_for_step & step_ids_with_events)
        step_number_has_events[step_number] = has_events

    # Count steps with/without events
    total_steps = len(step_outcomes)
    steps_with_events = sum(1 for has in step_number_has_events.values() if has)
    steps_without_events = total_steps - steps_with_events

    # Build event coverage string
    if total_steps > 0:
        event_coverage = f"{steps_with_events}/{total_steps} steps have events"
    else:
        event_coverage = "No steps"

    # Current timestamp for ingestion
    ingested_at = datetime.now(timezone.utc).isoformat()

    # Insert into runs table
    cursor.execute("""
        INSERT INTO runs (
            run_id, generated_at, prompt, status, duration_minutes,
            total_steps, passed_steps, failed_steps, total_retries,
            replan_checkpoints, replans_triggered, success_rate,
            planner, implementer, verifier, models_used,
            rls_issues, migration_issues, edge_function_issues, auth_issues,
            total_input_tokens, total_output_tokens, total_cache_read_tokens,
            total_cache_creation_tokens, total_cost_usd,
            events_count, events_may_be_truncated,
            steps_with_events, steps_without_events, event_coverage,
            ingested_at, classified_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        run_id,
        data.get("generated_at"),
        summary.get("prompt"),
        summary.get("status"),
        summary.get("duration_minutes"),
        summary.get("total_steps"),
        summary.get("passed_steps"),
        summary.get("failed_steps"),
        summary.get("total_retries"),
        summary.get("replan_checkpoints"),
        summary.get("replans_triggered"),
        summary.get("success_rate"),
        tools_config.get("planner"),
        tools_config.get("implementer"),
        tools_config.get("verifier"),
        json.dumps(tools_config.get("models_used")) if tools_config.get("models_used") else None,
        supabase_specific.get("rls_issues", 0),
        supabase_specific.get("migration_issues", 0),
        supabase_specific.get("edge_function_issues", 0),
        supabase_specific.get("auth_issues", 0),
        token_usage.get("total_input_tokens", 0),
        token_usage.get("total_output_tokens", 0),
        token_usage.get("total_cache_read_tokens", 0),
        token_usage.get("total_cache_creation_tokens", 0),
        token_usage.get("total_cost_usd", 0),
        events_count,
        events_may_be_truncated,
        steps_with_events,
        steps_without_events,
        event_coverage,
        ingested_at,
        None  # classified_at
    ))

    # Process step_outcomes (step_outcomes was already loaded above for event coverage)
    failures_section = data.get("failures", {})
    failures_details = failures_section.get("details", [])

    for step_outcome in step_outcomes:
        step_number = _safe_int(step_outcome.get("step"))
        if step_number is None:
            continue
        step_id = f"{run_id}_{step_number}"

        # Get raw steps for this step number
        raw_steps_for_step = get_raw_steps_for_step_number(raw_data, step_number)

        # Extract phase, tool, and parsed_result from raw steps
        phase = _extract_phase_from_raw_steps(raw_steps_for_step)
        tool = _extract_tool_from_raw_steps(raw_steps_for_step)
        # For parsed_result, concatenate all results from raw steps (plan, replan, etc.)
        parsed_results = [s.get("parsed_result") for s in raw_steps_for_step if s.get("parsed_result")]
        parsed_result = "\n\n---\n\n".join(parsed_results) if parsed_results else None

        # Get failures for this step
        step_failures = _get_failures_for_step(failures_details, step_number)

        # Extract error categories
        error_categories = list(set(f.get("category") for f in step_failures if f.get("category")))
        error_categories_json = json.dumps(error_categories) if error_categories else None

        # Build errors summary (truncate to 1000 chars)
        error_messages = [f.get("error", "") for f in step_failures if f.get("error")]
        errors_summary = " | ".join(error_messages)[:1000] if error_messages else None

        # Resolution actions
        resolution_actions = step_outcome.get("resolution_actions")
        resolution_actions_json = json.dumps(resolution_actions) if resolution_actions else None

        # Check if this step has event data (using pre-computed mapping)
        has_events = step_number_has_events.get(step_number, False)

        cursor.execute("""
            INSERT INTO steps (
                id, run_id, step_number, build_phase, phase, tool,
                final_verdict, attempts, retries, duration_seconds,
                resolution_actions, error_categories, errors_summary,
                classification, classification_confidence,
                classification_reasoning, classification_evidence,
                approach_changed, same_file_repeated, error_category_stable,
                input_tokens, output_tokens, cost_usd, has_events, parsed_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            step_id,
            run_id,
            step_number,
            step_outcome.get("build_phase"),
            phase,
            tool,
            step_outcome.get("final_verdict"),
            step_outcome.get("attempts"),
            step_outcome.get("retries"),
            step_outcome.get("duration_seconds"),
            resolution_actions_json,
            error_categories_json,
            errors_summary,
            None,  # classification
            None,  # classification_confidence
            None,  # classification_reasoning
            None,  # classification_evidence
            None,  # approach_changed (NULL - we don't know without events)
            None,  # same_file_repeated (NULL - we don't know without events)
            None,  # error_category_stable (NULL - we don't know without events)
            step_outcome.get("input_tokens", 0),
            step_outcome.get("output_tokens", 0),
            step_outcome.get("cost_usd", 0),
            has_events,
            parsed_result
        ))

    # Insert failures (filter out false positives)
    for failure in failures_details:
        if _is_false_positive_failure(failure):
            continue  # Skip false positive failures

        cursor.execute("""
            INSERT INTO failures (
                run_id, step_number, build_phase, phase, category, error, exit_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id,
            failure.get("step"),
            failure.get("build_phase"),
            failure.get("phase"),
            failure.get("category"),
            failure.get("error"),
            failure.get("exit_code")
        ))

    # Extract web searches directly from raw_data.events
    # (don't rely on top-level web_searches which may be empty in old reports)
    web_searches = _extract_web_searches_from_events(events)
    for ws in web_searches:
        results_json = json.dumps(ws.get("results", [])) if ws.get("results") else None
        cursor.execute("""
            INSERT INTO web_searches (
                run_id, step_id, query, count, timestamp, results, result_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id,
            ws.get("step_id"),
            ws.get("query"),
            ws.get("count"),
            ws.get("timestamp"),
            results_json,
            ws.get("full_text_result")
        ))

    return run_id


def ingest_reports(force: bool = False) -> dict[str, int]:
    """
    Ingest all *_full.json reports from the reports directory.

    Args:
        force: If True, delete existing data and re-ingest.
               If False, skip reports that are already in the DB.

    Returns:
        Dict with counts: {"ingested": N, "skipped": N, "errors": N}
    """
    result = {"ingested": 0, "skipped": 0, "errors": 0}

    # Check if reports directory exists
    if not REPORTS_DIR.exists():
        print(f"Reports directory not found: {REPORTS_DIR}")
        return result

    # Find all *_full.json files
    report_files = list(REPORTS_DIR.glob("*_full.json"))

    if not report_files:
        print(f"No *_full.json files found in {REPORTS_DIR}")
        return result

    print(f"Found {len(report_files)} report file(s)")

    for report_path in report_files:
        try:
            # First, peek at the run_id without fully parsing
            with open(report_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            run_id = data.get("run_id")
            if not run_id:
                print(f"Warning: No run_id in {report_path.name}, skipping")
                result["errors"] += 1
                continue

            # Check if already exists
            if not force and run_exists(run_id):
                print(f"Skipping {run_id} (already exists)")
                result["skipped"] += 1
                continue

            # Ingest the report
            with get_db() as conn:
                if force and run_exists(run_id):
                    print(f"Deleting existing data for {run_id}")
                    _delete_run_data(conn, run_id)

                _ingest_single_report(conn, report_path)
                conn.commit()

            print(f"Ingested {run_id}")
            result["ingested"] += 1

        except json.JSONDecodeError as e:
            print(f"Warning: Invalid JSON in {report_path.name}: {e}")
            result["errors"] += 1
        except KeyError as e:
            print(f"Warning: Missing required field in {report_path.name}: {e}")
            result["errors"] += 1
        except Exception as e:
            print(f"Warning: Error processing {report_path.name}: {e}")
            result["errors"] += 1

    return result
