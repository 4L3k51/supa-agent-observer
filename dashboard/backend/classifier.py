"""AI classifier for step failure analysis using Anthropic API."""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .db import get_db, get_steps_for_run, get_failures_for_run, get_run

# Try to load API key from environment or .env file
def _load_api_key() -> Optional[str]:
    """Load Anthropic API key from environment or .env file."""
    # First check environment
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key

    # Try loading from .env file in project root
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ANTHROPIC_API_KEY="):
                        key = line.split("=", 1)[1].strip()
                        # Remove quotes if present
                        if (key.startswith('"') and key.endswith('"')) or \
                           (key.startswith("'") and key.endswith("'")):
                            key = key[1:-1]
                        return key
        except Exception:
            pass

    return None


ANTHROPIC_API_KEY = _load_api_key()


def _get_anthropic_client():
    """Get Anthropic client if API key is available."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        from anthropic import Anthropic
        return Anthropic(api_key=ANTHROPIC_API_KEY)
    except ImportError:
        print("Warning: anthropic package not installed")
        return None
    except Exception as e:
        print(f"Warning: Failed to create Anthropic client: {e}")
        return None


def _build_classification_prompt(step: dict, failures: list, run: dict) -> str:
    """
    Build the classification prompt for a step.

    Uses available data from the database to provide context for classification.
    """
    # Extract step information
    step_number = step.get("step_number", "?")
    build_phase = step.get("build_phase", "unknown")
    phase = step.get("phase", "unknown")
    tool = step.get("tool", "unknown")
    verdict = step.get("final_verdict", "unknown")
    attempts = step.get("attempts", 1)
    retries = step.get("retries", 0)
    duration = step.get("duration_seconds", 0)
    has_events = step.get("has_events", False)

    # Get errors for this step
    step_failures = [f for f in failures if f.get("step_number") == step_number]

    # Build error context
    error_context = ""
    if step_failures:
        error_lines = []
        for f in step_failures:
            cat = f.get("category", "unknown")
            err = f.get("error", "No error message")
            error_lines.append(f"- Category: {cat}\n  Error: {err}")
        error_context = "\n".join(error_lines)
    else:
        error_context = "No specific error details available"

    # Parse resolution actions and error categories if available
    resolution_actions = []
    if step.get("resolution_actions"):
        try:
            resolution_actions = json.loads(step["resolution_actions"])
        except:
            pass

    error_categories = []
    if step.get("error_categories"):
        try:
            error_categories = json.loads(step["error_categories"])
        except:
            pass

    errors_summary = step.get("errors_summary", "")

    # Build the prompt
    prompt = f"""You are an expert software engineering analyst. Your task is to classify a failed build step as either "architectural" or "implementation" based on the available information.

DEFINITIONS:
- **Architectural failure**: The fundamental approach or design was flawed. The agent was trying to do something that wouldn't work regardless of execution quality. Examples: wrong API being used, misunderstanding of requirements, trying to use deprecated features, fundamentally wrong algorithm choice.

- **Implementation failure**: The approach was correct but there were execution issues. Examples: syntax errors, typos, missing imports, incorrect parameter order, off-by-one errors, race conditions in otherwise correct code.

STEP INFORMATION:
- Step Number: {step_number}
- Build Phase: {build_phase}
- Phase: {phase}
- Tool: {tool}
- Final Verdict: {verdict}
- Attempts: {attempts}
- Retries: {retries}
- Duration: {duration:.1f} seconds

ERROR INFORMATION:
{error_context}

{f"Error Categories: {', '.join(error_categories)}" if error_categories else ""}
{f"Resolution Actions Attempted: {', '.join(resolution_actions)}" if resolution_actions else ""}
{f"Errors Summary: {errors_summary[:500]}" if errors_summary else ""}

RUN CONTEXT:
- Prompt: {run.get('prompt', 'Unknown task')[:200]}
- Overall Status: {run.get('status', 'unknown')}
- Total Retries in Run: {run.get('total_retries', 0)}

Based on this information, classify this failure and provide your analysis.

Respond with a JSON object in this exact format:
{{
  "classification": "architectural" | "implementation" | "ambiguous",
  "confidence": 0.0-1.0,
  "reasoning": "Brief explanation of why this classification was chosen",
  "evidence": "Specific evidence from the error information that supports this classification",
  "approach_changed": true | false,
  "same_file_repeated": true | false,
  "error_category_stable": true | false
}}

Notes on the boolean fields:
- approach_changed: Did the resolution attempts suggest a change in approach (architectural) vs just fixing bugs (implementation)?
- same_file_repeated: Were errors occurring in the same file repeatedly? (suggests implementation issues)
- error_category_stable: Did the error category stay the same across retries? (suggests implementation if yes, architectural if changing)

If you cannot determine the classification with reasonable confidence, use "ambiguous" with a lower confidence score.

{f'''**WARNING: No event-level data is available for this step.**
You can only use the parsed_result and error information. You CANNOT verify:
- What files were modified or read
- What tool calls were made
- The actual approach changes between attempts
Lower your confidence accordingly (max 0.7) and note this limitation in your reasoning.
Set approach_changed, same_file_repeated, and error_category_stable to null since we cannot determine them.''' if not has_events else ''}"""

    return prompt


def _parse_classification_response(response_text: str) -> Optional[dict]:
    """Parse the JSON classification response from the API."""
    try:
        # Try to find JSON in the response
        # First try direct parsing
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            pass

        # Try to extract JSON from markdown code blocks
        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1))

        # Try to find a JSON object anywhere in the text
        json_match = re.search(r'\{[^{}]*"classification"[^{}]*\}', response_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))

        return None
    except Exception as e:
        print(f"Failed to parse classification response: {e}")
        return None


def _update_step_classification(run_id: str, step_number: int, classification_data: dict) -> bool:
    """Update a step's classification in the database."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE steps SET
                    classification = ?,
                    classification_confidence = ?,
                    classification_reasoning = ?,
                    classification_evidence = ?,
                    approach_changed = ?,
                    same_file_repeated = ?,
                    error_category_stable = ?
                WHERE run_id = ? AND step_number = ?
            """, (
                classification_data.get("classification"),
                classification_data.get("confidence"),
                classification_data.get("reasoning"),
                classification_data.get("evidence"),
                classification_data.get("approach_changed"),
                classification_data.get("same_file_repeated"),
                classification_data.get("error_category_stable"),
                run_id,
                step_number
            ))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        print(f"Failed to update step classification: {e}")
        return False


def _mark_clean_pass(run_id: str, step_number: int) -> bool:
    """Mark a step with no retries as clean_pass."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE steps SET
                    classification = 'clean_pass',
                    classification_confidence = 1.0,
                    classification_reasoning = 'Step completed without retries',
                    classification_evidence = 'No retries needed',
                    approach_changed = 0,
                    same_file_repeated = 0,
                    error_category_stable = 1
                WHERE run_id = ? AND step_number = ?
            """, (run_id, step_number))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        print(f"Failed to mark step as clean_pass: {e}")
        return False


def _update_run_classified_at(run_id: str) -> bool:
    """Update the run's classified_at timestamp."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE runs SET classified_at = ?
                WHERE run_id = ?
            """, (datetime.now(timezone.utc).isoformat(), run_id))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        print(f"Failed to update run classified_at: {e}")
        return False


def classify_run(run_id: str) -> dict:
    """
    Classify all steps in a run that need classification.

    Args:
        run_id: The run ID to classify

    Returns:
        dict with counts: {"classified": N, "skipped": N, "errors": N, "no_api_key": bool}
    """
    result = {"classified": 0, "skipped": 0, "errors": 0, "no_api_key": False}

    # Check if API key is available
    client = _get_anthropic_client()
    if not client:
        result["no_api_key"] = True
        print("No Anthropic API key available. Skipping AI classification.")
        # Still mark clean passes even without API key
        run = get_run(run_id)
        if not run:
            print(f"Run {run_id} not found")
            return result

        steps = get_steps_for_run(run_id)
        for step in steps:
            if step.get("retries", 0) == 0 and step.get("classification") is None:
                if _mark_clean_pass(run_id, step["step_number"]):
                    result["classified"] += 1
            else:
                result["skipped"] += 1

        _update_run_classified_at(run_id)
        return result

    # Get run data
    run = get_run(run_id)
    if not run:
        print(f"Run {run_id} not found")
        result["errors"] += 1
        return result

    steps = get_steps_for_run(run_id)
    failures = get_failures_for_run(run_id)

    print(f"Classifying {len(steps)} steps for run {run_id}")

    for step in steps:
        step_number = step.get("step_number")
        retries = step.get("retries", 0)
        existing_classification = step.get("classification")

        # Skip already classified steps
        if existing_classification is not None:
            result["skipped"] += 1
            continue

        # Mark clean passes (no retries)
        if retries == 0:
            if _mark_clean_pass(run_id, step_number):
                result["classified"] += 1
                print(f"  Step {step_number}: clean_pass (no retries)")
            else:
                result["errors"] += 1
            continue

        # Build prompt and call API for steps with retries
        try:
            prompt = _build_classification_prompt(step, failures, run)

            # Call Anthropic API
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )

            # Extract response text
            response_text = message.content[0].text if message.content else ""

            # Parse classification
            classification_data = _parse_classification_response(response_text)

            if classification_data and classification_data.get("classification"):
                # Cap confidence at 0.7 if step has no event data
                has_events = step.get("has_events", False)
                if not has_events:
                    original_conf = classification_data.get("confidence", 0)
                    classification_data["confidence"] = min(original_conf, 0.7)
                    # Ensure observable patterns are NULL when events are missing
                    classification_data["approach_changed"] = None
                    classification_data["same_file_repeated"] = None
                    classification_data["error_category_stable"] = None

                if _update_step_classification(run_id, step_number, classification_data):
                    result["classified"] += 1
                    cls = classification_data.get("classification")
                    conf = classification_data.get("confidence", 0)
                    no_events_note = " (no events)" if not has_events else ""
                    print(f"  Step {step_number}: {cls} (confidence: {conf:.2f}){no_events_note}")
                else:
                    result["errors"] += 1
                    print(f"  Step {step_number}: failed to save classification")
            else:
                result["errors"] += 1
                print(f"  Step {step_number}: failed to parse API response")

            # Rate limiting
            time.sleep(1)

        except Exception as e:
            result["errors"] += 1
            print(f"  Step {step_number}: API error - {e}")

    # Update run's classified_at timestamp
    _update_run_classified_at(run_id)

    return result


def classify_all_runs() -> dict:
    """
    Classify all runs that haven't been classified yet.

    Returns:
        dict with overall counts and per-run results
    """
    from .db import get_all_runs

    result = {
        "total_runs": 0,
        "runs_classified": 0,
        "total_steps_classified": 0,
        "total_errors": 0,
        "no_api_key": False,
        "run_results": []
    }

    runs = get_all_runs()
    unclassified_runs = [r for r in runs if r.get("classified_at") is None]

    result["total_runs"] = len(unclassified_runs)
    print(f"Found {len(unclassified_runs)} runs to classify")

    for run in unclassified_runs:
        run_id = run["run_id"]
        print(f"\nClassifying run: {run_id}")

        run_result = classify_run(run_id)
        result["run_results"].append({
            "run_id": run_id,
            **run_result
        })

        if run_result.get("no_api_key"):
            result["no_api_key"] = True

        if run_result["classified"] > 0 or run_result["skipped"] > 0:
            result["runs_classified"] += 1

        result["total_steps_classified"] += run_result["classified"]
        result["total_errors"] += run_result["errors"]

    return result


def reclassify_run(run_id: str) -> dict:
    """
    Force reclassification of all steps in a run.
    Clears existing classifications first.

    Args:
        run_id: The run ID to reclassify

    Returns:
        Same as classify_run()
    """
    # Clear existing classifications
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE steps SET
                    classification = NULL,
                    classification_confidence = NULL,
                    classification_reasoning = NULL,
                    classification_evidence = NULL,
                    approach_changed = NULL,
                    same_file_repeated = NULL,
                    error_category_stable = NULL
                WHERE run_id = ?
            """, (run_id,))
            cursor.execute("""
                UPDATE runs SET classified_at = NULL
                WHERE run_id = ?
            """, (run_id,))
            conn.commit()
            print(f"Cleared existing classifications for run {run_id}")
    except Exception as e:
        print(f"Failed to clear classifications: {e}")
        return {"classified": 0, "skipped": 0, "errors": 1, "no_api_key": False}

    return classify_run(run_id)
