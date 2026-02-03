#!/usr/bin/env python3
"""
AI Coding Orchestrator
=====================
Coordinates Claude Code (planner/verifier) and Cursor Agent CLI (implementer)
to autonomously build Supabase projects while logging everything.

Architecture:
    Python Orchestrator (this script)
        ├── Claude Code CLI  (planning + verification)
        ├── Cursor Agent CLI (implementation)
        └── Supabase log store (every interaction captured)

Usage:
    python orchestrator.py "Build a Supabase app with auth and a todo list"
    python orchestrator.py --resume <run_id>
    python orchestrator.py --list-runs
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from storage import SupabaseStorage, create_storage


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

DEFAULT_PROJECT_DIR = os.path.expanduser("~/orchestrator-projects")

# Timeouts (seconds) - generous because these agents can be slow
CLAUDE_CODE_TIMEOUT = 600   # 10 min for planning/verification
CURSOR_TIMEOUT = 900        # 15 min for implementation (can be complex)
CURSOR_IDLE_TIMEOUT = 120   # Kill cursor if no output for 2 min (hanging bug workaround)

# CLI commands
CLAUDE_CODE_CMD = "claude"
CURSOR_CMD = "agent"

# Models (adjust to what you have access to)
CLAUDE_CODE_MODEL = None  # None = use default
CURSOR_MODEL = None       # None = use default


# ─────────────────────────────────────────────
# CLI Execution Engine
# ─────────────────────────────────────────────

class CLIResult:
    """Result from running a CLI tool."""
    def __init__(self):
        self.stdout: str = ""
        self.stderr: str = ""
        self.exit_code: int = -1
        self.duration: float = 0.0
        self.events: list[dict] = []       # parsed stream-json events
        self.text_result: str = ""         # extracted final text
        self.timed_out: bool = False
        self.killed_idle: bool = False


def run_claude_code(
    prompt: str,
    working_dir: str,
    system_prompt: Optional[str] = None,
    session_id: Optional[str] = None,
    timeout: int = CLAUDE_CODE_TIMEOUT,
) -> CLIResult:
    """Run Claude Code CLI in headless mode and capture everything."""
    cmd = [
        CLAUDE_CODE_CMD,
        "-p",
        prompt,
        "--output-format", "stream-json",
        "--verbose",  # Required when using --output-format=stream-json with --print
        "--allowedTools", "Bash,Read,Write,Edit,MultiEdit,LS,Glob,WebSearch,WebFetch",
    ]
    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])
    if session_id:
        cmd.extend(["--resume", session_id])
    if CLAUDE_CODE_MODEL:
        cmd.extend(["--model", CLAUDE_CODE_MODEL])

    return _run_cli(cmd, working_dir, timeout, idle_timeout=None)


def run_cursor_agent(
    prompt: str,
    working_dir: str,
    timeout: int = CURSOR_TIMEOUT,
) -> CLIResult:
    """Run Cursor Agent CLI in headless mode and capture everything."""
    cmd = [
        CURSOR_CMD,
        "-p",
        prompt,
        "--force",
        "--output-format", "stream-json",
        "--workspace", working_dir,
    ]
    if CURSOR_MODEL:
        cmd.extend(["--model", CURSOR_MODEL])

    return _run_cli(cmd, working_dir, timeout, idle_timeout=CURSOR_IDLE_TIMEOUT)


def _run_cli(
    cmd: list[str],
    working_dir: str,
    timeout: int,
    idle_timeout: Optional[int] = None,
) -> CLIResult:
    """
    Execute a CLI command, capturing stdout/stderr in real-time.
    Handles the Cursor hanging bug via idle_timeout.
    """
    result = CLIResult()
    start_time = time.time()

    print(f"\n  {'─' * 60}")
    print(f"  Running: {' '.join(cmd[:4])}...")
    print(f"  Working dir: {working_dir}")
    print(f"  Timeout: {timeout}s | Idle timeout: {idle_timeout or 'none'}s")
    print(f"  {'─' * 60}\n")

    import selectors
    sel = None
    proc = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=working_dir,
            text=True,
            preexec_fn=os.setsid,  # New process group for clean killing
        )

        stdout_lines = []
        stderr_lines = []
        last_output_time = time.time()

        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        sel.register(proc.stderr, selectors.EVENT_READ)

        while True:
            # Check timeouts
            elapsed = time.time() - start_time
            if elapsed > timeout:
                print(f"\n  ⏰ Hard timeout ({timeout}s) reached. Killing process.")
                result.timed_out = True
                _kill_process_group(proc)
                break

            if idle_timeout and (time.time() - last_output_time > idle_timeout):
                print(f"\n  ⏰ Idle timeout ({idle_timeout}s) reached. Killing process.")
                result.killed_idle = True
                _kill_process_group(proc)
                break

            # Read available output with 1-second polling
            ready = sel.select(timeout=1.0)
            for key, _ in ready:
                line = key.fileobj.readline()
                if not line:
                    continue

                last_output_time = time.time()

                if key.fileobj == proc.stdout:
                    stdout_lines.append(line)
                    _process_stream_line(line, result)
                else:
                    stderr_lines.append(line)
                    sys.stderr.write(f"  [stderr] {line}")

            # Check if process finished
            if proc.poll() is not None:
                # Drain remaining output
                for line in proc.stdout:
                    stdout_lines.append(line)
                    _process_stream_line(line, result)
                for line in proc.stderr:
                    stderr_lines.append(line)
                break

        result.stdout = "".join(stdout_lines)
        result.stderr = "".join(stderr_lines)
        # Use -1 for killed/None returncode to distinguish from success
        result.exit_code = proc.returncode if proc.returncode is not None else -1
        result.duration = time.time() - start_time

    except FileNotFoundError:
        print(f"\n  ❌ Command not found: {cmd[0]}")
        print(f"     Make sure {cmd[0]} is installed and in your PATH.")
        result.stderr = f"Command not found: {cmd[0]}"
        result.exit_code = 127
        result.duration = time.time() - start_time
    except Exception as e:
        print(f"\n  ❌ Error running command: {e}")
        result.stderr = str(e)
        result.exit_code = 1
        result.duration = time.time() - start_time
    finally:
        if sel is not None:
            sel.close()

    print(f"\n  ✓ Finished in {result.duration:.1f}s (exit code: {result.exit_code})")
    return result


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Safely kill a process and its process group."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        # Process already gone, that's fine
        pass


def _process_stream_line(line: str, result: CLIResult):
    """Parse a stream-json line and extract useful info for real-time display.

    Claude Code stream-json format includes event types:
    - system (init, etc.)
    - assistant (text content, tool_use)
    - result (final result with text)
    - tool_use / tool_result (tool interactions)

    Cursor Agent may use a different format with writeToolCall, readToolCall, etc.
    """
    line = line.strip()
    if not line:
        return

    try:
        event = json.loads(line)
        result.events.append(event)

        event_type = event.get("type", "")
        subtype = event.get("subtype", "")

        # Real-time display of what's happening
        if event_type == "system" and subtype == "init":
            model = event.get("model", "unknown")
            session_id = event.get("session_id", "")
            print(f"  🤖 Model: {model}")
            if session_id:
                print(f"  📎 Session: {session_id}")

        elif event_type == "result":
            # Final result - extract the text
            result.text_result = event.get("result", "")
            duration = event.get("duration_ms", 0) / 1000
            cost = event.get("cost_usd", 0)
            cost_str = f", ${cost:.4f}" if cost else ""
            print(f"  📋 Result received ({duration:.1f}s API time{cost_str})")

        # Claude Code tool events (may use tool_use or tool_call depending on version)
        elif event_type in ("tool_use", "tool_call"):
            tool_name = event.get("name", "") or event.get("tool", "")
            tool_input = event.get("input", {}) or event.get("args", {})

            if tool_name:
                if tool_name in ("Write", "Edit"):
                    path = tool_input.get("file_path", tool_input.get("path", "?"))
                    print(f"  📝 {tool_name}: {path}")
                elif tool_name == "Read":
                    path = tool_input.get("file_path", tool_input.get("path", "?"))
                    print(f"  👁  Read: {path}")
                elif tool_name == "Bash":
                    cmd = tool_input.get("command", "?")
                    print(f"  💻 Bash: {cmd[:80]}")
                else:
                    print(f"  🔧 Tool: {tool_name}")

            # Also handle nested tool_call object (some versions)
            tool = event.get("tool_call", {})
            if tool and not tool_name:
                if "name" in tool:
                    print(f"  🔧 Tool: {tool['name']}")
                # Cursor format - look for common patterns
                elif "writeToolCall" in tool:
                    path = tool["writeToolCall"].get("args", {}).get("path", "?")
                    print(f"  📝 Writing: {path}")
                elif "readToolCall" in tool:
                    path = tool["readToolCall"].get("args", {}).get("path", "?")
                    print(f"  👁  Reading: {path}")
                elif "terminalToolCall" in tool:
                    cmd_text = tool["terminalToolCall"].get("args", {}).get("command", "?")
                    print(f"  💻 Running: {cmd_text[:80]}")

        elif event_type == "assistant":
            # Streaming text content
            message = event.get("message", {})
            content = message.get("content", "")

            # If there's substantial text content, accumulate it
            if isinstance(content, str) and content:
                result.text_result += content
            elif isinstance(content, list):
                # Handle content blocks (text and tool_use)
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        result.text_result += block.get("text", "")

            # Show a dot for progress on streaming
            sys.stdout.write(".")
            sys.stdout.flush()

        elif event_type == "error":
            error_msg = event.get("error", {}).get("message", str(event))
            print(f"  ❌ Error: {error_msg[:100]}")

    except json.JSONDecodeError:
        # Not JSON - just raw text output, store it
        if line:
            result.text_result += line + "\n"
            # Show abbreviated content
            if len(line) > 100:
                print(f"  📄 {line[:100]}...")
            else:
                print(f"  📄 {line}")


# ─────────────────────────────────────────────
# Prompt Templates
# ─────────────────────────────────────────────

PLANNER_SYSTEM_PROMPT = """\
You are the PLANNER in an automated coding pipeline. Your job is to break down
a user's project request into clear, sequential implementation steps.

RULES:
- Output a numbered list of steps (typically 3-10 steps)
- Each step must be concrete and self-contained enough for another AI to implement
- Each step should build on the previous ones
- Include setup steps (init project, install deps, configure supabase, etc.)
- Think about the order: schema first, then backend logic, then frontend
- Be specific about file names, function names, and expected behavior
- Focus on Supabase specifics: schema/migrations, RLS policies, edge functions, auth setup

FORMAT your response as:
STEP 1: [title]
[detailed instruction for the implementer, 2-5 sentences]

STEP 2: [title]
[detailed instruction for the implementer, 2-5 sentences]

... and so on.

End with:
TOTAL_STEPS: [number]
"""

IMPLEMENTER_PROMPT_TEMPLATE = """\
You are implementing step {step_number} of {total_steps} for a Supabase project.

PROJECT GOAL: {user_prompt}

CURRENT STEP: {step_title}
INSTRUCTIONS:
{step_instructions}

PREVIOUS STEPS COMPLETED: {completed_steps}

RULES:
- Implement ONLY this step, do not jump ahead
- Create/modify the actual files in the project directory
- If you need to run commands (npm install, supabase init, etc.), run them
- If you hit an error, try to fix it before giving up
- After implementing, briefly describe what you did and any issues you encountered
"""

VERIFIER_SYSTEM_PROMPT = """\
You are the VERIFIER in an automated coding pipeline. You just received the output
from an implementation step. Your job is to check the work.

RULES:
- Look at the actual files that were created/modified
- Check if the implementation matches what was requested
- Check for obvious errors, missing imports, incorrect Supabase usage
- Run any relevant checks (TypeScript compilation, linting, etc.) if appropriate
- Be specific about any problems you find

FORMAT your response as:
STATUS: PASS | FAIL | PARTIAL

ISSUES (if any):
- [issue 1]
- [issue 2]

SUMMARY: [1-2 sentence assessment]

RECOMMENDATION: PROCEED | RETRY | MODIFY_PLAN
If RETRY, explain what needs to be fixed.
If MODIFY_PLAN, explain what should change.
"""

VERIFIER_PROMPT_TEMPLATE = """\
Verify step {step_number} of {total_steps}.

STEP GOAL: {step_title}
STEP INSTRUCTIONS:
{step_instructions}

IMPLEMENTER OUTPUT:
{implementer_output}

Check the project directory for the actual files. Verify the implementation is correct.
"""


# ─────────────────────────────────────────────
# Plan Parser
# ─────────────────────────────────────────────

def parse_plan(plan_text: str) -> list[dict]:
    """Parse the planner's output into structured steps."""
    steps = []
    current_step = None
    current_body = []

    for line in plan_text.split("\n"):
        stripped = line.strip()

        # Match "STEP N:" pattern
        if stripped.upper().startswith("STEP ") and ":" in stripped:
            # Save previous step
            if current_step is not None:
                steps.append({
                    "number": current_step["number"],
                    "title": current_step["title"],
                    "instructions": "\n".join(current_body).strip(),
                })

            # Parse new step
            parts = stripped.split(":", 1)
            try:
                step_num = int(parts[0].upper().replace("STEP", "").strip())
            except ValueError:
                continue
            title = parts[1].strip() if len(parts) > 1 else ""
            current_step = {"number": step_num, "title": title}
            current_body = []

        elif stripped.upper().startswith("TOTAL_STEPS:"):
            continue  # Skip this meta line
        elif current_step is not None:
            current_body.append(line)

    # Don't forget the last step
    if current_step is not None:
        steps.append({
            "number": current_step["number"],
            "title": current_step["title"],
            "instructions": "\n".join(current_body).strip(),
        })

    return steps


def parse_verification(verify_text: str) -> dict:
    """Parse the verifier's output into structured result."""
    result = {
        "status": "UNKNOWN",
        "issues": [],
        "summary": "",
        "recommendation": "PROCEED",
    }

    for line in verify_text.split("\n"):
        stripped = line.strip()
        upper = stripped.upper()

        if upper.startswith("STATUS:"):
            status = stripped.split(":", 1)[1].strip().upper()
            if "PASS" in status:
                result["status"] = "PASS"
            elif "FAIL" in status:
                result["status"] = "FAIL"
            elif "PARTIAL" in status:
                result["status"] = "PARTIAL"

        elif upper.startswith("SUMMARY:"):
            result["summary"] = stripped.split(":", 1)[1].strip()

        elif upper.startswith("RECOMMENDATION:"):
            rec = stripped.split(":", 1)[1].strip().upper()
            if "RETRY" in rec:
                result["recommendation"] = "RETRY"
            elif "MODIFY" in rec:
                result["recommendation"] = "MODIFY_PLAN"
            else:
                result["recommendation"] = "PROCEED"

        elif stripped.startswith("- ") and result["status"] != "PASS":
            result["issues"].append(stripped[2:])

    return result


# ─────────────────────────────────────────────
# Logging helpers
# ─────────────────────────────────────────────

def log_step(store: SupabaseStorage, run_id: str, step_number: int,
             phase: str, tool: str, prompt: str, result: CLIResult) -> int:
    """Log a step to storage. Returns step ID."""
    step_id = store.log_step(
        run_id=run_id,
        step_number=step_number,
        phase=phase,
        tool=tool,
        prompt_sent=prompt,
        raw_stdout=result.stdout,
        raw_stderr=result.stderr,
        parsed_result=result.text_result,
        exit_code=result.exit_code,
        duration_seconds=result.duration,
    )

    # Batch insert events for performance - step must exist first (FK constraint)
    if result.events and step_id:
        store.log_events_batch(run_id, step_id, result.events)

    return step_id


# ─────────────────────────────────────────────
# Main Orchestration Loop
# ─────────────────────────────────────────────

def run_orchestration(
    user_prompt: str,
    project_dir: str,
    max_retries: int = 2,
    resume_run_id: Optional[str] = None,
    start_from_step: Optional[int] = None,
):
    """
    Main orchestration loop:
    1. Claude Code creates the plan
    2. For each step:
       a. Cursor Agent implements
       b. Claude Code verifies
       c. If FAIL → retry or replan
    3. Store everything in Supabase
    """
    # Initialize storage
    print("  Initializing Supabase storage...")
    store = create_storage()

    os.makedirs(project_dir, exist_ok=True)

    run_id = resume_run_id or str(uuid.uuid4())[:8]

    if not resume_run_id:
        store.create_run(run_id, user_prompt, project_dir)

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║                   AI CODING ORCHESTRATOR                     ║
╠══════════════════════════════════════════════════════════════╣
║  Run ID:   {run_id:<49}║
║  Project:  {project_dir:<49}║
║  Storage:  Supabase{" " * 40}║
╚══════════════════════════════════════════════════════════════╝
""")
    print(f"📋 User prompt: {user_prompt}\n")

    # ── Phase 1: Planning ──────────────────────────
    print("=" * 60)
    print("  PHASE 1: PLANNING (Claude Code)")
    print("=" * 60)

    plan_prompt = f"Create a step-by-step implementation plan for:\n\n{user_prompt}"

    plan_result = run_claude_code(
        prompt=plan_prompt,
        working_dir=project_dir,
        system_prompt=PLANNER_SYSTEM_PROMPT,
    )

    log_step(store, run_id, 0, "plan", "claude_code", plan_prompt, plan_result)

    if plan_result.exit_code != 0 and not plan_result.text_result:
        print(f"\n❌ Planning failed! Exit code: {plan_result.exit_code}")
        print(f"   stderr: {plan_result.stderr[:500]}")
        store.update_run_status(run_id, "failed_planning")
        return

    steps = parse_plan(plan_result.text_result)

    if not steps:
        print("\n❌ Could not parse any steps from the plan.")
        print(f"   Raw output:\n{plan_result.text_result[:1000]}")
        store.update_run_status(run_id, "failed_planning")
        return

    print(f"\n📋 Plan created with {len(steps)} steps:")
    for step in steps:
        print(f"   Step {step['number']}: {step['title']}")

    # ── Phase 2: Implementation Loop ──────────────
    start = (start_from_step or 1) - 1
    completed_descriptions = []

    for idx, step in enumerate(steps[start:], start=start):
        step_num = step["number"]
        retries = 0

        print(f"\n{'=' * 60}")
        print(f"  STEP {step_num}/{len(steps)}: {step['title']}")
        print(f"{'=' * 60}")

        while retries <= max_retries:
            # ── 2a: Implement with Cursor ──────────
            print(f"\n  ▶ Implementing (attempt {retries + 1}/{max_retries + 1})...")
            print(f"  {'─' * 50}")

            impl_prompt = IMPLEMENTER_PROMPT_TEMPLATE.format(
                step_number=step_num,
                total_steps=len(steps),
                user_prompt=user_prompt,
                step_title=step["title"],
                step_instructions=step["instructions"],
                completed_steps="\n".join(completed_descriptions) if completed_descriptions else "None yet",
            )

            impl_result = run_cursor_agent(
                prompt=impl_prompt,
                working_dir=project_dir,
            )

            log_step(store, run_id, step_num, "implement", "cursor",
                     impl_prompt, impl_result)

            if impl_result.exit_code != 0 and not impl_result.text_result:
                print(f"\n  ⚠️  Cursor failed (exit code {impl_result.exit_code})")
                if impl_result.killed_idle:
                    print("     (Killed due to idle timeout - likely the hanging bug)")
                    if impl_result.events:
                        print("     But we got output, so checking the work anyway...")
                    else:
                        retries += 1
                        continue

            # ── 2b: Verify with Claude Code ────────
            print(f"\n  ▶ Verifying...")
            print(f"  {'─' * 50}")

            verify_prompt = VERIFIER_PROMPT_TEMPLATE.format(
                step_number=step_num,
                total_steps=len(steps),
                step_title=step["title"],
                step_instructions=step["instructions"],
                implementer_output=impl_result.text_result[:3000],
            )

            verify_result = run_claude_code(
                prompt=verify_prompt,
                working_dir=project_dir,
                system_prompt=VERIFIER_SYSTEM_PROMPT,
            )

            log_step(store, run_id, step_num, "verify", "claude_code",
                     verify_prompt, verify_result)

            verification = parse_verification(verify_result.text_result)

            # ── 2c: Act on verification ────────────
            status_emoji = {
                "PASS": "✅", "FAIL": "❌", "PARTIAL": "⚠️"
            }.get(verification["status"], "❓")

            print(f"\n  {status_emoji} Verification: {verification['status']}")
            print(f"     Summary: {verification['summary']}")
            if verification["issues"]:
                print(f"     Issues:")
                for issue in verification["issues"]:
                    print(f"       • {issue}")
            print(f"     Recommendation: {verification['recommendation']}")

            if verification["recommendation"] == "PROCEED":
                completed_descriptions.append(
                    f"Step {step_num} ({step['title']}): Completed"
                )
                break

            elif verification["recommendation"] == "RETRY":
                retries += 1
                if retries <= max_retries:
                    print(f"\n  🔄 Retrying step {step_num} (attempt {retries + 1})...")
                    step["instructions"] += (
                        f"\n\nPREVIOUS ATTEMPT ISSUES (fix these):\n"
                        + "\n".join(f"- {i}" for i in verification["issues"])
                    )
                else:
                    print(f"\n  ❌ Max retries reached for step {step_num}. Continuing anyway.")
                    completed_descriptions.append(
                        f"Step {step_num} ({step['title']}): Completed with issues"
                    )
                    break

            elif verification["recommendation"] == "MODIFY_PLAN":
                print(f"\n  📝 Plan modification requested. Continuing with best effort.")
                completed_descriptions.append(
                    f"Step {step_num} ({step['title']}): Needs attention"
                )
                break

            else:
                completed_descriptions.append(
                    f"Step {step_num} ({step['title']}): Completed (unverified)"
                )
                break

    # ── Phase 3: Completion ───────────────────────
    print(f"\n{'=' * 60}")
    print("  ORCHESTRATION COMPLETE")
    print(f"{'=' * 60}")

    store.finish_run(run_id)

    # Print summary
    all_steps = store.get_steps(run_id)
    all_events = store.get_events(run_id)

    print(f"""
  Run ID:     {run_id}
  Steps:      {len(all_steps)} logged
  Events:     {len(all_events)} captured
  Project:    {project_dir}

  To analyze the logs:
    python analyzer.py {run_id}

  To resume from a specific step:
    python orchestrator.py --resume {run_id} --start-step N "{user_prompt}"
""")


# ─────────────────────────────────────────────
# List runs
# ─────────────────────────────────────────────

def list_runs():
    """List all orchestration runs from Supabase."""
    try:
        store = create_storage()
    except Exception as e:
        print(f"❌ Could not initialize storage: {e}")
        return

    runs = store.list_runs()

    if not runs:
        print("No runs found.")
        return

    print(f"\n{'Run ID':<10} {'Status':<20} {'Prompt'}")
    print("─" * 80)
    for run in runs:
        run_id = run.get("run_id", "?")
        prompt = run.get("user_prompt", "?")
        status = run.get("status", "?")
        prompt_short = prompt[:50] + "..." if len(prompt) > 50 else prompt
        print(f"{run_id:<10} {status:<20} {prompt_short}")


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AI Coding Orchestrator - Claude Code + Cursor Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              %(prog)s "Build a Supabase todo app with auth"
              %(prog)s --project-dir ~/my-project "Add real-time subscriptions"
              %(prog)s --resume abc123 --start-step 3 "Build a todo app"
              %(prog)s --list-runs

            Environment:
              SUPABASE_URL   Your Supabase project URL
              SUPABASE_KEY   Your Supabase service_role key (not anon key)

            Create a .env file with these variables or export them.
        """)
    )
    parser.add_argument("prompt", nargs="?", help="What to build")
    parser.add_argument("--project-dir", default=None,
                        help="Project directory (default: auto-created)")
    parser.add_argument("--resume", metavar="RUN_ID",
                        help="Resume a previous run")
    parser.add_argument("--start-step", type=int, default=None,
                        help="Start from this step number (with --resume)")
    parser.add_argument("--max-retries", type=int, default=2,
                        help="Max retries per step (default: 2)")
    parser.add_argument("--list-runs", action="store_true",
                        help="List all runs")
    parser.add_argument("--claude-model", default=None,
                        help="Model for Claude Code")
    parser.add_argument("--cursor-model", default=None,
                        help="Model for Cursor Agent")

    args = parser.parse_args()

    if args.list_runs:
        list_runs()
        return

    if not args.prompt:
        parser.print_help()
        print("\n❌ Please provide a prompt describing what to build.")
        sys.exit(1)

    # Set models if provided
    global CLAUDE_CODE_MODEL, CURSOR_MODEL
    if args.claude_model:
        CLAUDE_CODE_MODEL = args.claude_model
    if args.cursor_model:
        CURSOR_MODEL = args.cursor_model

    # Determine project directory
    if args.project_dir:
        project_dir = os.path.expanduser(args.project_dir)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = args.prompt[:30].lower().replace(" ", "_")
        slug = "".join(c for c in slug if c.isalnum() or c == "_")
        project_dir = os.path.join(DEFAULT_PROJECT_DIR, f"{timestamp}_{slug}")

    run_orchestration(
        user_prompt=args.prompt,
        project_dir=project_dir,
        max_retries=args.max_retries,
        resume_run_id=args.resume,
        start_from_step=args.start_step,
    )


if __name__ == "__main__":
    main()
