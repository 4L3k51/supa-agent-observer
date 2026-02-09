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

# Project output directory relative to this script's location
SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_PROJECT_DIR = SCRIPT_DIR / "projects"
DEFAULT_PROJECT_DIR.mkdir(parents=True, exist_ok=True)

# Timeouts (seconds) - generous because these agents can be slow
CLAUDE_CODE_TIMEOUT = 600   # 10 min for planning/verification
CURSOR_TIMEOUT = 900        # 15 min for implementation (can be complex)
CURSOR_IDLE_TIMEOUT = 120   # Kill cursor if no output for 2 min (hanging bug workaround)
MAX_RESOLUTIONS_PER_STEP = 5  # total resolution actions (search, diagnostic, retry) before giving up

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
        "--permission-mode", "bypassPermissions",
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
        "--sandbox", "enabled",
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
- Tag each step with a build phase: setup, schema, backend, frontend, testing, or deployment

IDEMPOTENT DDL (critical for retry safety):
- All SQL migrations MUST be idempotent so they can be safely re-run:
  - Use CREATE TABLE IF NOT EXISTS (not plain CREATE TABLE)
  - Use CREATE INDEX IF NOT EXISTS
  - Use DROP POLICY IF EXISTS before CREATE POLICY
  - Use CREATE OR REPLACE FUNCTION for functions
  - Use CREATE OR REPLACE VIEW for views
- This ensures migrations succeed even if partially applied in a previous attempt

TABLE PERMISSIONS (critical for RLS to work):
- After creating tables, ALWAYS add GRANT statements for the authenticated role:
  - GRANT ALL ON tablename TO authenticated;
  - GRANT USAGE ON SEQUENCE tablename_id_seq TO authenticated;
- Without GRANTs, the PostgREST API returns PGRST301 "permission denied" errors
- The service_role key bypasses GRANTs, so missing GRANTs only show up at runtime

AVOID DUPLICATE STEPS:
- Before finalizing the plan, review each step and remove any that duplicate work from earlier steps
- If step N will naturally be completed as part of step M, merge them into a single step
- Common duplicates to avoid: "install dependencies" repeated, "create component" then "add component to page" when the first step already does both

FORMAT your response as:
STEP 1: [title]
PHASE: [one of: setup, schema, backend, frontend, testing, deployment]
[detailed instruction for the implementer, 2-5 sentences]

STEP 2: [title]
PHASE: [one of: setup, schema, backend, frontend, testing, deployment]
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

RECOMMENDATION: PROCEED | RETRY | MODIFY_PLAN | WEB_SEARCH | RUN_DIAGNOSTIC | SKIP
- PROCEED: step is good, continue.
- RETRY: explain what needs to be fixed; implementer will retry.
- MODIFY_PLAN: explain what should change in the plan.
- WEB_SEARCH: look up something (e.g. docs, syntax). Follow with RESOLUTION line.
- RUN_DIAGNOSTIC: run a command to diagnose (e.g. npm test, npx tsc --noEmit, python -m pytest). Follow with RESOLUTION line.
- SKIP: skip this step (e.g. not applicable). Follow with RESOLUTION line.

Only include a RESOLUTION line when the recommendation requires parameters. PROCEED and RETRY do not need one.

RESOLUTION (single line, valid JSON, only when needed):
- WEB_SEARCH → {"query": "..."}
- RUN_DIAGNOSTIC → {"command": "...", "reason": "..."}
- SKIP → {"reason": "..."}
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

SMOKE_TEST_SYSTEM_PROMPT = """\
You are a SMOKE TESTER in an automated coding pipeline. Your job is to verify
that the completed project actually runs.

RULES:
- Look at the project files to determine what kind of project this is (Node.js, Python, etc.)
- Run the build command first (npm run build, next build, etc.) to catch TypeScript/compilation errors
- Find and run the appropriate start command (npm start, npm run dev, python main.py, etc.)
- If a test suite exists (npm test, pytest, etc.), run it
- Let the app start, wait a few seconds, then check if it's still running or crashed
- If there's a web server, try to reach its health endpoint or main page with curl
- If Supabase credentials are provided:
  - Test authentication flow (sign up, sign in)
  - Make authenticated API requests to verify the full stack works
  - If Storage is used: try uploading a small test file and verify it succeeds
- Kill any long-running processes after testing (don't leave servers running)
- Report what worked and what didn't

FORMAT your response as:
BUILD_SUCCEEDS: YES | NO | N/A
APP_STARTS: YES | NO | N/A
TESTS_PASS: YES | NO | N/A | NO_TESTS
AUTH_WORKS: YES | NO | N/A
STORAGE_WORKS: YES | NO | N/A
ERRORS (if any):
- [error 1]
- [error 2]

SUMMARY: [1-3 sentence assessment of whether this project actually works]
"""

SMOKE_TEST_PROMPT_TEMPLATE = """\
The project is complete. Verify it actually runs.

PROJECT GOAL: {user_prompt}

{credentials_section}

STEPS COMPLETED:
{completed_steps}

Check the project files, determine how to start it, run it, and report the results.
If there are tests, run them too.
{auth_instructions}
"""

MIGRATION_EXEC_SYSTEM_PROMPT = """\
You are executing database migrations for a Supabase project.

RULES:
- Find all SQL migration files in the project (supabase/migrations/*.sql, migrations/*.sql, schema.sql, etc.)
- Execute them against the database using psql with the provided connection string
- Run: psql "$DATABASE_URL" -f <migration_file>
- Execute migrations in order (by filename timestamp if present)
- Report which migrations were executed and any errors

IDEMPOTENCY HANDLING (critical for retries):
- If a migration fails with "already exists" errors (e.g., "relation X already exists",
  "constraint X already exists", "index X already exists"), these indicate partial
  completion from a previous attempt.
- After running migrations, verify the actual database state using: psql "$DATABASE_URL" -c "\\dt"
- If ALL failures are "already exists" AND the expected tables/objects exist, report STATUS: SUCCESS
- Only report STATUS: FAILED for actual failures (syntax errors, permission errors, etc.)

FORMAT your response as:
MIGRATIONS_FOUND: [number]
MIGRATIONS_EXECUTED: [number]
STATUS: SUCCESS | FAILED
ERRORS (if any):
- [error 1]
- [error 2]

SUMMARY: [1-2 sentence assessment]
"""

MIGRATION_EXEC_PROMPT_TEMPLATE = """\
Execute the database migrations for this step against the real Supabase database.

STEP: {step_number} - {step_title}

DATABASE_URL: {db_url}

Find all migration files created or modified in this step and execute them using:
  psql "$DATABASE_URL" -f <migration_file>

Report the results. If you encounter "already exists" errors but the schema looks correct, that's OK.
"""

RLS_TEST_SYSTEM_PROMPT = """\
You are testing Row Level Security policies on a live Supabase database.

CRITICAL: The service_role key bypasses BOTH RLS policies AND table GRANTs.
You MUST test data access using the anon key + user JWT to catch missing GRANTs.

RULES:
- Create a test user using the Supabase Auth API (service_role key for admin endpoint only)
- Sign in as that user to get a JWT
- For ALL data access tests, use: anon key + user JWT (Authorization: Bearer <jwt>)
- NEVER use the service_role key for data access tests - it hides permission issues
- Verify that:
  1. Authenticated users can access tables they should (catches missing GRANT TO authenticated)
  2. Authenticated users can only see their own data (catches missing RLS policies)
  3. Unauthenticated requests (anon key only, no JWT) are properly blocked
- Watch for PGRST301 errors ("permission denied" or "No suitable key") - these indicate missing GRANTs
- If triggers exist: after INSERT/UPDATE, check if expected side effects occurred (e.g., updated_at changed, audit log created)
- Clean up the test user when done

TEST PATTERN (use curl):
  # Create user (service_role OK here - it's an admin operation)
  curl -X POST "$URL/auth/v1/admin/users" -H "apikey: $SERVICE_KEY" -H "Authorization: Bearer $SERVICE_KEY" ...

  # Sign in to get JWT
  curl -X POST "$URL/auth/v1/token?grant_type=password" -H "apikey: $ANON_KEY" ...

  # Data access - MUST use anon key + JWT, NOT service key
  curl "$URL/rest/v1/todos" -H "apikey: $ANON_KEY" -H "Authorization: Bearer $USER_JWT"

FORMAT your response as:
TEST_USER_CREATED: YES | NO
TESTS_RUN: [number]
TESTS_PASSED: [number]
STATUS: SUCCESS | FAILED
RLS_ENFORCED: YES | NO | PARTIAL
GRANTS_VALID: YES | NO
TRIGGERS_WORK: YES | NO | N/A
ERRORS (if any):
- [error 1]
- [error 2]

SUMMARY: [1-2 sentence assessment of RLS security and table permissions]
"""

RLS_TEST_PROMPT_TEMPLATE = """\
Test the Row Level Security policies created in this step against the live Supabase database.

STEP: {step_number} - {step_title}

SUPABASE_URL: {supabase_url}
SUPABASE_ANON_KEY: {supabase_anon_key}
SUPABASE_SERVICE_KEY: {supabase_service_key}

CRITICAL: Use anon key + user JWT for data access tests, NOT service_role key.
The service_role key bypasses GRANTs and will hide permission issues like PGRST301.

Test plan:
1. Create a test user via Auth API (service_role key OK for admin endpoint):
   curl -X POST "{supabase_url}/auth/v1/admin/users" \\
     -H "apikey: $SERVICE_KEY" -H "Authorization: Bearer $SERVICE_KEY" \\
     -H "Content-Type: application/json" \\
     -d '{{"email":"test@example.com","password":"testpass123","email_confirm":true}}'

2. Sign in as that user to get a JWT:
   curl -X POST "{supabase_url}/auth/v1/token?grant_type=password" \\
     -H "apikey: $ANON_KEY" -H "Content-Type: application/json" \\
     -d '{{"email":"test@example.com","password":"testpass123"}}'

3. Make data requests using anon key + JWT (NOT service key):
   curl "{supabase_url}/rest/v1/TABLE_NAME" \\
     -H "apikey: $ANON_KEY" -H "Authorization: Bearer $USER_JWT"

4. Verify:
   - Authenticated access works (no PGRST301 errors = GRANTs are correct)
   - Users only see their own data (RLS policies work)
   - Unauthenticated access is blocked

5. Clean up the test user

Report whether RLS is enforced AND whether table GRANTs are correct.
"""

EDGE_FUNCTION_DEPLOY_SYSTEM_PROMPT = """\
You are deploying and testing Supabase Edge Functions.

RULES:
- Find all Edge Functions in the project (supabase/functions/*/index.ts)
- Deploy each function using: supabase functions deploy <function_name>
- After deployment, test each function with curl
- For functions requiring auth, use the service_role key
- Report deployment status and invocation results

FORMAT your response as:
FUNCTIONS_FOUND: [number]
FUNCTIONS_DEPLOYED: [number]
FUNCTIONS_TESTED: [number]
STATUS: SUCCESS | FAILED
ERRORS (if any):
- [error 1]
- [error 2]

SUMMARY: [1-2 sentence assessment]
"""

EDGE_FUNCTION_DEPLOY_PROMPT_TEMPLATE = """\
Deploy and test the Edge Functions created in this step.

STEP: {step_number} - {step_title}

SUPABASE_URL: {supabase_url}
SUPABASE_ANON_KEY: {supabase_anon_key}
SUPABASE_SERVICE_KEY: {supabase_service_key}

Steps:
1. Find Edge Functions in supabase/functions/
2. Deploy each with: supabase functions deploy <name>
3. Test each with curl:
   curl -X POST "{supabase_url}/functions/v1/<name>" \\
     -H "Authorization: Bearer {supabase_service_key}" \\
     -H "Content-Type: application/json" \\
     -d '{{}}'
4. Verify 2xx response

Report which functions were deployed and tested.
"""

APPROACH_ANALYSIS_SYSTEM_PROMPT = """\
You are an APPROACH ANALYZER in an automated coding pipeline. Your job is to examine
the completed project and document what technical choices were made.

DO NOT judge whether choices are good or bad — just observe and report what was chosen.

ANALYSIS STEPS:
1. Read package.json, requirements.txt, Cargo.toml, go.mod, or equivalent dependency files
2. Check git log for initial commits that might reveal scaffolding (create-next-app, etc.)
3. Look at lock files (package-lock.json, yarn.lock, pnpm-lock.yaml) for dependency versions
4. Examine project structure patterns to identify frameworks and starters
5. Find Supabase integration code and identify which client library is used
6. Identify authentication pattern (client-side, SSR, middleware, etc.)
7. Check for ORM usage vs raw SQL

OUTPUT FORMAT (must be valid JSON):
```json
{
  "dependencies": {
    "runtime": ["package@version", ...],
    "dev": ["package@version", ...],
    "source_file": "package.json"
  },
  "scaffolding": {
    "tool": "create-next-app | create-vite | manual | etc",
    "template": "template name if any",
    "evidence": "how you determined this"
  },
  "framework": {
    "name": "Next.js | React | Vue | Svelte | etc",
    "version": "version",
    "rendering": "CSR | SSR | SSG | hybrid"
  },
  "supabase_integration": {
    "client_library": "@supabase/supabase-js | @supabase/ssr | raw fetch | none",
    "version": "version if applicable",
    "auth_pattern": "client-side | SSR | middleware | server-actions | none",
    "database_access": "supabase-js | prisma | drizzle | raw SQL | none",
    "realtime_used": true | false,
    "edge_functions_used": true | false,
    "storage_used": true | false
  },
  "architecture": {
    "api_pattern": "REST | tRPC | GraphQL | server-actions | direct-db",
    "state_management": "React context | Redux | Zustand | none | etc",
    "styling": "Tailwind | CSS Modules | styled-components | etc",
    "testing": "Jest | Vitest | Playwright | none | etc"
  },
  "file_structure": {
    "src_directory": true | false,
    "app_router": true | false,
    "pages_router": true | false,
    "monorepo": true | false
  }
}
```

If a field cannot be determined, use null. Be precise about versions when available.
"""

APPROACH_ANALYSIS_PROMPT_TEMPLATE = """\
Analyze the technical approach used in this completed project.

PROJECT GOAL: {user_prompt}

Examine the project files and document:
1. What dependencies were installed (read package.json, requirements.txt, etc.)
2. What scaffolding or starter template was used (check git history, project structure)
3. What framework and architecture patterns were chosen
4. How Supabase was integrated (which client library, auth pattern, etc.)

Output ONLY the JSON object as specified. No commentary before or after.
"""


# ─────────────────────────────────────────────
# Plan Parser
# ─────────────────────────────────────────────

def strip_markdown(text: str) -> str:
    """Strip common markdown formatting from text."""
    import re
    # Remove bold/italic markers (but not underscores within words like TEST_USER)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # **bold**
    text = re.sub(r'(?<!\w)\*([^*]+)\*(?!\w)', r'\1', text)  # *italic* (not mid-word)
    text = re.sub(r'(?<!\w)__([^_]+)__(?!\w)', r'\1', text)  # __bold__ (not mid-word)
    # Skip _italic_ - too likely to match SNAKE_CASE identifiers
    text = re.sub(r'`([^`]+)`', r'\1', text)        # `code`
    return text


def parse_plan(plan_text: str) -> list[dict]:
    """Parse the planner's output into structured steps."""
    steps = []
    current_step = None
    current_body = []

    for line in plan_text.split("\n"):
        stripped = strip_markdown(line.strip())

        # Match "STEP N:" pattern
        if stripped.upper().startswith("STEP ") and ":" in stripped:
            # Save previous step
            if current_step is not None:
                steps.append({
                    "number": current_step["number"],
                    "title": current_step["title"],
                    "instructions": "\n".join(current_body).strip(),
                    "build_phase": current_step.get("build_phase"),
                })

            # Parse new step
            parts = stripped.split(":", 1)
            try:
                step_num = int(parts[0].upper().replace("STEP", "").strip())
            except ValueError:
                continue
            title = parts[1].strip() if len(parts) > 1 else ""
            current_step = {"number": step_num, "title": title, "build_phase": None}
            current_body = []

        elif stripped.upper().startswith("TOTAL_STEPS:"):
            continue  # Skip this meta line
        elif current_step is not None:
            if stripped.upper().startswith("PHASE:"):
                phase_val = stripped.split(":", 1)[1].strip().lower()
                valid_phases = {"setup", "schema", "backend", "frontend", "testing", "deployment"}
                if phase_val in valid_phases:
                    current_step["build_phase"] = phase_val
            else:
                current_body.append(line)

    # Don't forget the last step
    if current_step is not None:
        steps.append({
            "number": current_step["number"],
            "title": current_step["title"],
            "instructions": "\n".join(current_body).strip(),
            "build_phase": current_step.get("build_phase"),
        })

    return steps


RECOMMENDATION_KEYWORDS = ["WEB_SEARCH", "RUN_DIAGNOSTIC", "SKIP", "RETRY", "MODIFY_PLAN"]


def extract_normalized_errors(
    phase: str,
    tool: str,
    exit_code: int,
    stderr: str,
    parsed_result: str,
    parsed_errors: list[str],
) -> list[dict]:
    """Extract and normalize errors from step outputs.

    Returns a list of error records:
    [{"type": "RLS_ERROR", "message": "...", "source": "stderr", "phase": "...", "tool": "..."}, ...]
    """
    errors = []

    # Helper to classify error type based on message content
    def classify_error(msg: str) -> str:
        msg_lower = msg.lower()
        if any(k in msg_lower for k in ["pgrst", "permission denied", "rls", "policy"]):
            return "RLS_ERROR"
        if any(k in msg_lower for k in ["grant", "role", "authenticated"]):
            return "GRANT_ERROR"
        if any(k in msg_lower for k in ["syntax error", "parse error", "unexpected token"]):
            return "SYNTAX_ERROR"
        if any(k in msg_lower for k in ["cannot find module", "module not found", "no such file"]):
            return "IMPORT_ERROR"
        if any(k in msg_lower for k in ["type error", "typescript", "is not assignable"]):
            return "TYPE_ERROR"
        if any(k in msg_lower for k in ["build failed", "compilation failed", "esbuild"]):
            return "BUILD_ERROR"
        if any(k in msg_lower for k in ["timeout", "timed out"]):
            return "TIMEOUT_ERROR"
        if any(k in msg_lower for k in ["connection refused", "econnrefused", "network"]):
            return "NETWORK_ERROR"
        if any(k in msg_lower for k in ["already exists", "duplicate"]):
            return "DUPLICATE_ERROR"
        if any(k in msg_lower for k in ["not found", "404", "does not exist"]):
            return "NOT_FOUND_ERROR"
        if any(k in msg_lower for k in ["unauthorized", "401", "403", "forbidden"]):
            return "AUTH_ERROR"
        if any(k in msg_lower for k in ["500", "internal server", "failed"]):
            return "SERVER_ERROR"
        return "UNKNOWN_ERROR"

    # Helper to extract file/line from error message
    def extract_location(msg: str) -> dict:
        location = {}
        # Pattern: /path/to/file.ts:42:10 or file.ts(42,10)
        import re
        # Node/TS style: /path/file.ts:42:10
        match = re.search(r'([/\w.-]+\.[a-z]+):(\d+)(?::(\d+))?', msg)
        if match:
            location["file"] = match.group(1)
            location["line"] = int(match.group(2))
            if match.group(3):
                location["column"] = int(match.group(3))
        # Postgres style: at line 42
        if not location:
            match = re.search(r'at line (\d+)', msg, re.IGNORECASE)
            if match:
                location["line"] = int(match.group(1))
        # Error code extraction (PGRST301, TS2345, etc.)
        code_match = re.search(r'\b(PGRST\d+|TS\d+|E\d+)\b', msg)
        if code_match:
            location["code"] = code_match.group(1)
        return location

    # Process stderr if present
    if stderr and stderr.strip():
        for line in stderr.split("\n"):
            line = line.strip()
            if line and any(k in line.lower() for k in ["error", "fail", "exception", "pgrst"]):
                error = {
                    "type": classify_error(line),
                    "message": line[:500],  # Truncate long messages
                    "source": "stderr",
                    "phase": phase,
                    "tool": tool,
                }
                error.update(extract_location(line))
                errors.append(error)

    # Process parsed errors from AI output
    for err_msg in parsed_errors:
        error = {
            "type": classify_error(err_msg),
            "message": err_msg[:500],
            "source": "parsed",
            "phase": phase,
            "tool": tool,
        }
        error.update(extract_location(err_msg))
        errors.append(error)

    # If exit_code != 0 but no errors found, add a generic one
    if exit_code != 0 and not errors:
        errors.append({
            "type": "EXIT_ERROR",
            "message": f"Process exited with code {exit_code}",
            "source": "exit_code",
            "phase": phase,
            "tool": tool,
            "code": str(exit_code),
        })

    # Deduplicate by message
    seen = set()
    unique_errors = []
    for err in errors:
        if err["message"] not in seen:
            seen.add(err["message"])
            unique_errors.append(err)

    return unique_errors


def extract_commands_from_events(events: list[dict]) -> list[dict]:
    """Extract executed shell commands from CLI result events.

    Returns a list of command records:
    [{"command": "npm install", "tool": "Bash"}, ...]
    """
    commands = []

    for event in events:
        event_type = event.get("type", "")

        # Claude Code format: tool_use or tool_call events
        if event_type in ("tool_use", "tool_call"):
            tool_name = event.get("name", "") or event.get("tool", "")
            tool_input = event.get("input", {}) or event.get("args", {})

            if tool_name == "Bash":
                cmd = tool_input.get("command", "")
                if cmd:
                    commands.append({"command": cmd, "tool": "Bash"})

            # Also check nested tool_call object
            nested = event.get("tool_call", {})
            if nested:
                if "terminalToolCall" in nested:
                    cmd = nested["terminalToolCall"].get("args", {}).get("command", "")
                    if cmd:
                        commands.append({"command": cmd, "tool": "terminal"})

        # Cursor format: may have tool_call at top level with different structure
        tool_call = event.get("tool_call", {})
        if tool_call and event_type not in ("tool_use", "tool_call"):
            if "terminalToolCall" in tool_call:
                cmd = tool_call["terminalToolCall"].get("args", {}).get("command", "")
                if cmd:
                    commands.append({"command": cmd, "tool": "terminal"})

    return commands


def parse_verification(verify_text: str) -> dict:
    """Parse the verifier's output into structured result."""
    result = {
        "status": "UNKNOWN",
        "issues": [],
        "summary": "",
        "recommendation": "PROCEED",
        "resolution": {},
    }

    for line in verify_text.split("\n"):
        stripped = strip_markdown(line.strip())
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
            result["recommendation"] = "PROCEED"
            for keyword in RECOMMENDATION_KEYWORDS:
                if keyword in rec:
                    result["recommendation"] = keyword
                    break

        elif upper.startswith("RESOLUTION:"):
            payload = stripped.split(":", 1)[1].strip()
            try:
                result["resolution"] = json.loads(payload)
            except json.JSONDecodeError:
                result["resolution"] = {"raw": payload}

        elif stripped.startswith("- ") and result["status"] != "PASS":
            result["issues"].append(stripped[2:])

    return result


def parse_smoke_test(smoke_text: str) -> dict:
    """Parse the smoke tester's output into structured result."""
    result = {
        "build_succeeds": "UNKNOWN",
        "app_starts": "UNKNOWN",
        "tests_pass": "UNKNOWN",
        "auth_works": "UNKNOWN",
        "storage_works": "UNKNOWN",
        "errors": [],
        "summary": "",
    }

    for line in smoke_text.split("\n"):
        stripped = strip_markdown(line.strip())
        upper = stripped.upper()

        if upper.startswith("BUILD_SUCCEEDS:"):
            val = stripped.split(":", 1)[1].strip().upper()
            if "YES" in val:
                result["build_succeeds"] = "YES"
            elif "NO" in val and "N/A" not in val:
                result["build_succeeds"] = "NO"
            elif "N/A" in val:
                result["build_succeeds"] = "N/A"

        elif upper.startswith("APP_STARTS:"):
            val = stripped.split(":", 1)[1].strip().upper()
            if "YES" in val:
                result["app_starts"] = "YES"
            elif "NO" in val and "N/A" not in val:
                result["app_starts"] = "NO"
            elif "N/A" in val:
                result["app_starts"] = "N/A"

        elif upper.startswith("TESTS_PASS:"):
            val = stripped.split(":", 1)[1].strip().upper()
            if "YES" in val:
                result["tests_pass"] = "YES"
            elif "NO_TESTS" in val:
                result["tests_pass"] = "NO_TESTS"
            elif "NO" in val and "N/A" not in val:
                result["tests_pass"] = "NO"
            elif "N/A" in val:
                result["tests_pass"] = "N/A"

        elif upper.startswith("AUTH_WORKS:"):
            val = stripped.split(":", 1)[1].strip().upper()
            if "YES" in val:
                result["auth_works"] = "YES"
            elif "NO" in val and "N/A" not in val:
                result["auth_works"] = "NO"
            elif "N/A" in val:
                result["auth_works"] = "N/A"

        elif upper.startswith("STORAGE_WORKS:"):
            val = stripped.split(":", 1)[1].strip().upper()
            if "YES" in val:
                result["storage_works"] = "YES"
            elif "NO" in val and "N/A" not in val:
                result["storage_works"] = "NO"
            elif "N/A" in val:
                result["storage_works"] = "N/A"

        elif upper.startswith("SUMMARY:"):
            result["summary"] = stripped.split(":", 1)[1].strip()

        elif stripped.startswith("- ") and result["app_starts"] != "UNKNOWN":
            result["errors"].append(stripped[2:])

    return result


def parse_approach_analysis(analysis_text: str) -> dict:
    """Parse the approach analyzer's JSON output."""
    result = {
        "dependencies": None,
        "scaffolding": None,
        "framework": None,
        "supabase_integration": None,
        "architecture": None,
        "file_structure": None,
        "parse_error": None,
    }

    # Try to extract JSON from the response (may be wrapped in markdown code blocks)
    text = analysis_text.strip()

    # Remove markdown code block if present
    if "```json" in text:
        start = text.find("```json") + 7
        end = text.find("```", start)
        if end > start:
            text = text[start:end].strip()
    elif "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end > start:
            text = text[start:end].strip()

    # Try to find JSON object boundaries
    if "{" in text:
        start = text.find("{")
        # Find matching closing brace
        depth = 0
        end = start
        for i, char in enumerate(text[start:], start):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        text = text[start:end]

    try:
        parsed = json.loads(text)
        # Merge parsed fields into result
        for key in result:
            if key != "parse_error" and key in parsed:
                result[key] = parsed[key]
    except json.JSONDecodeError as e:
        result["parse_error"] = f"Failed to parse JSON: {str(e)[:100]}"

    return result


def parse_migration_result(migration_text: str) -> dict:
    """Parse the migration executor's output."""
    result = {
        "migrations_found": 0,
        "migrations_executed": 0,
        "status": "UNKNOWN",
        "errors": [],
        "summary": "",
    }

    for line in migration_text.split("\n"):
        stripped = strip_markdown(line.strip())
        upper = stripped.upper()

        if upper.startswith("MIGRATIONS_FOUND:"):
            try:
                result["migrations_found"] = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif upper.startswith("MIGRATIONS_EXECUTED:"):
            try:
                result["migrations_executed"] = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif upper.startswith("STATUS:"):
            val = stripped.split(":", 1)[1].strip().upper()
            result["status"] = "SUCCESS" if "SUCCESS" in val else "FAILED"
        elif upper.startswith("SUMMARY:"):
            result["summary"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("- "):
            result["errors"].append(stripped[2:])

    return result


def parse_rls_test_result(rls_text: str) -> dict:
    """Parse the RLS tester's output."""
    result = {
        "test_user_created": "UNKNOWN",
        "tests_run": 0,
        "tests_passed": 0,
        "status": "UNKNOWN",
        "rls_enforced": "UNKNOWN",
        "grants_valid": "UNKNOWN",
        "triggers_work": "UNKNOWN",
        "errors": [],
        "summary": "",
    }

    for line in rls_text.split("\n"):
        stripped = strip_markdown(line.strip())
        upper = stripped.upper()

        if upper.startswith("TEST_USER_CREATED:"):
            val = stripped.split(":", 1)[1].strip().upper()
            result["test_user_created"] = "YES" if "YES" in val else "NO"
        elif upper.startswith("TESTS_RUN:"):
            try:
                result["tests_run"] = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif upper.startswith("TESTS_PASSED:"):
            try:
                result["tests_passed"] = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif upper.startswith("STATUS:"):
            val = stripped.split(":", 1)[1].strip().upper()
            result["status"] = "SUCCESS" if "SUCCESS" in val else "FAILED"
        elif upper.startswith("RLS_ENFORCED:"):
            val = stripped.split(":", 1)[1].strip().upper()
            if "YES" in val:
                result["rls_enforced"] = "YES"
            elif "PARTIAL" in val:
                result["rls_enforced"] = "PARTIAL"
            elif "NO" in val:
                result["rls_enforced"] = "NO"
        elif upper.startswith("GRANTS_VALID:"):
            val = stripped.split(":", 1)[1].strip().upper()
            result["grants_valid"] = "YES" if "YES" in val else "NO"
        elif upper.startswith("TRIGGERS_WORK:"):
            val = stripped.split(":", 1)[1].strip().upper()
            if "YES" in val:
                result["triggers_work"] = "YES"
            elif "NO" in val and "N/A" not in val:
                result["triggers_work"] = "NO"
            elif "N/A" in val:
                result["triggers_work"] = "N/A"
        elif upper.startswith("SUMMARY:"):
            result["summary"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("- "):
            result["errors"].append(stripped[2:])

    return result


def parse_edge_function_result(ef_text: str) -> dict:
    """Parse the Edge Function deployer's output."""
    result = {
        "functions_found": 0,
        "functions_deployed": 0,
        "functions_tested": 0,
        "status": "UNKNOWN",
        "errors": [],
        "summary": "",
    }

    for line in ef_text.split("\n"):
        stripped = strip_markdown(line.strip())
        upper = stripped.upper()

        if upper.startswith("FUNCTIONS_FOUND:"):
            try:
                result["functions_found"] = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif upper.startswith("FUNCTIONS_DEPLOYED:"):
            try:
                result["functions_deployed"] = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif upper.startswith("FUNCTIONS_TESTED:"):
            try:
                result["functions_tested"] = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif upper.startswith("STATUS:"):
            val = stripped.split(":", 1)[1].strip().upper()
            result["status"] = "SUCCESS" if "SUCCESS" in val else "FAILED"
        elif upper.startswith("SUMMARY:"):
            result["summary"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("- "):
            result["errors"].append(stripped[2:])

    return result


def redact_credentials(text: str, credentials: dict) -> str:
    """Redact credential values from text for safe logging."""
    redacted = text
    for key, value in credentials.items():
        if value:
            redacted = redacted.replace(value, "***REDACTED***")
    return redacted


def check_psql_available() -> bool:
    """Check if psql is available in the system PATH."""
    try:
        result = subprocess.run(
            ["psql", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_supabase_cli_available() -> bool:
    """Check if supabase CLI is available in the system PATH."""
    try:
        result = subprocess.run(
            ["supabase", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def write_env_local(
    project_dir: str,
    supabase_url: Optional[str],
    supabase_anon_key: Optional[str],
    supabase_service_key: Optional[str],
    supabase_db_url: Optional[str],
    silent: bool = False,
) -> bool:
    """Write .env.local with Supabase credentials. Returns True if written."""
    if not (supabase_url or supabase_anon_key):
        return False

    env_local_path = os.path.join(project_dir, ".env.local")
    env_lines = []
    if supabase_url:
        env_lines.append(f"NEXT_PUBLIC_SUPABASE_URL={supabase_url}")
        env_lines.append(f"VITE_SUPABASE_URL={supabase_url}")
        env_lines.append(f"SUPABASE_URL={supabase_url}")
    if supabase_anon_key:
        env_lines.append(f"NEXT_PUBLIC_SUPABASE_ANON_KEY={supabase_anon_key}")
        env_lines.append(f"VITE_SUPABASE_ANON_KEY={supabase_anon_key}")
        env_lines.append(f"SUPABASE_ANON_KEY={supabase_anon_key}")
    if supabase_service_key:
        env_lines.append(f"SUPABASE_SERVICE_ROLE_KEY={supabase_service_key}")
    if supabase_db_url:
        env_lines.append(f"DATABASE_URL={supabase_db_url}")

    with open(env_local_path, "w") as f:
        f.write("\n".join(env_lines) + "\n")

    if not silent:
        print(f"  Created .env.local with target Supabase credentials")
    return True


# ─────────────────────────────────────────────
# Logging helpers
# ─────────────────────────────────────────────

def log_step(store: SupabaseStorage, run_id: str, step_number: int,
             phase: str, tool: str, prompt: str, result: CLIResult,
             build_phase: Optional[str] = None,
             commands_executed: Optional[list] = None,
             credentials_to_redact: Optional[dict] = None,
             parsed_errors: Optional[list] = None) -> int:
    """Log a step to storage. Returns step ID.

    If credentials_to_redact is provided, all text fields will be redacted.
    If parsed_errors is provided, they will be included in normalized errors.
    """
    if commands_executed is None and result.events:
        commands_executed = extract_commands_from_events(result.events)

    # Extract normalized errors
    errors_normalized = extract_normalized_errors(
        phase=phase,
        tool=tool,
        exit_code=result.exit_code,
        stderr=result.stderr or "",
        parsed_result=result.text_result or "",
        parsed_errors=parsed_errors or [],
    )

    # Redact credentials from all text fields
    prompt_redacted = prompt
    stdout_redacted = result.stdout
    stderr_redacted = result.stderr
    parsed_redacted = result.text_result
    events_redacted = result.events

    if credentials_to_redact:
        prompt_redacted = redact_credentials(prompt, credentials_to_redact)
        stdout_redacted = redact_credentials(result.stdout, credentials_to_redact) if result.stdout else None
        stderr_redacted = redact_credentials(result.stderr, credentials_to_redact) if result.stderr else None
        parsed_redacted = redact_credentials(result.text_result, credentials_to_redact) if result.text_result else None

        # Redact credentials from events (convert to JSON string, redact, parse back)
        if result.events:
            import json
            events_json = json.dumps(result.events)
            events_json_redacted = redact_credentials(events_json, credentials_to_redact)
            events_redacted = json.loads(events_json_redacted)

        # Redact credentials from normalized errors too
        if errors_normalized:
            import json
            errors_json = json.dumps(errors_normalized)
            errors_json_redacted = redact_credentials(errors_json, credentials_to_redact)
            errors_normalized = json.loads(errors_json_redacted)

    step_id = store.log_step(
        run_id=run_id,
        step_number=step_number,
        phase=phase,
        tool=tool,
        prompt_sent=prompt_redacted,
        raw_stdout=stdout_redacted,
        raw_stderr=stderr_redacted,
        parsed_result=parsed_redacted,
        exit_code=result.exit_code,
        duration_seconds=result.duration,
        build_phase=build_phase,
        commands_executed=commands_executed,
        errors_normalized=errors_normalized if errors_normalized else None,
    )

    # Batch insert events for performance - step must exist first (FK constraint)
    if events_redacted and step_id:
        store.log_events_batch(run_id, step_id, events_redacted)

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
    skip_smoke_test: bool = False,
    target_supabase_url: Optional[str] = None,
    target_supabase_anon_key: Optional[str] = None,
    target_supabase_service_key: Optional[str] = None,
    target_supabase_db_url: Optional[str] = None,
    target_supabase_project_ref: Optional[str] = None,
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

    # Write target Supabase credentials to .env.local if provided
    write_env_local(
        project_dir,
        target_supabase_url,
        target_supabase_anon_key,
        target_supabase_service_key,
        target_supabase_db_url,
    )

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

    # Link Supabase project if ref provided
    supabase_cli_available = False
    if target_supabase_project_ref:
        if check_supabase_cli_available():
            supabase_cli_available = True
            print(f"\n  ▶ Linking Supabase project: {target_supabase_project_ref}")
            link_result = subprocess.run(
                ["supabase", "link", "--project-ref", target_supabase_project_ref],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if link_result.returncode == 0:
                print(f"  ✓ Supabase project linked")
            else:
                print(f"  ⚠️  Supabase link failed: {link_result.stderr[:200]}")
                print(f"     Edge Function deployment will be skipped")
                supabase_cli_available = False
        else:
            print(f"\n  ⚠️  Supabase CLI not found - skipping project link")
            print(f"     Install with: brew install supabase/tap/supabase")

    # ── Phase 2: Implementation Loop ──────────────
    start = (start_from_step or 1) - 1
    completed_descriptions = []

    # Track psql availability for migration execution (checked once on first use)
    psql_checked = False
    psql_available = False

    # Credentials dict for redaction in logs (only secrets, not URLs/refs)
    credentials_to_redact = {
        "supabase_anon_key": target_supabase_anon_key,
        "supabase_service_key": target_supabase_service_key,
        "supabase_db_url": target_supabase_db_url,  # Contains password
        "logging_supabase_key": os.environ.get("SUPABASE_KEY"),  # Logging credentials
    }

    for idx, step in enumerate(steps[start:], start=start):
        step_num = step["number"]
        retry_count = 0
        resolution_count = 0

        print(f"\n{'=' * 60}")
        print(f"  STEP {step_num}/{len(steps)}: {step['title']}")
        print(f"{'=' * 60}")

        while resolution_count < MAX_RESOLUTIONS_PER_STEP:
            # ── 2a: Implement with Cursor ──────────
            print(f"\n  ▶ Implementing (retry {retry_count}, resolution {resolution_count + 1}/{MAX_RESOLUTIONS_PER_STEP})...")
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
                     impl_prompt, impl_result, build_phase=step.get("build_phase"))

            # Re-write .env.local after Cursor (it often overwrites with placeholders)
            write_env_local(
                project_dir,
                target_supabase_url,
                target_supabase_anon_key,
                target_supabase_service_key,
                target_supabase_db_url,
                silent=True,  # Don't print every time
            )

            if impl_result.exit_code != 0 and not impl_result.text_result:
                print(f"\n  ⚠️  Cursor failed (exit code {impl_result.exit_code})")
                if impl_result.killed_idle:
                    print("     (Killed due to idle timeout - likely the hanging bug)")
                    if impl_result.events:
                        print("     But we got output, so checking the work anyway...")
                    else:
                        retry_count += 1
                        resolution_count += 1
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
                     verify_prompt, verify_result, build_phase=step.get("build_phase"))

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
                # ── Runtime execution for schema steps ──────
                is_schema_step = step.get("build_phase") == "schema"
                has_runtime_creds = target_supabase_db_url and target_supabase_service_key

                if is_schema_step and has_runtime_creds:
                    # Check psql availability (once per run)
                    if not psql_checked:
                        psql_checked = True
                        psql_available = check_psql_available()
                        if psql_available:
                            print(f"\n  ✓ psql is available for migration execution")
                        else:
                            print(f"\n  ⚠️  psql not found - skipping migration execution")
                            print(f"     Install PostgreSQL client to enable runtime migration testing")

                    if psql_available:
                        # ── Run migration_exec ──────
                        print(f"\n  ▶ Executing migrations...")
                        print(f"  {'─' * 50}")

                        migration_prompt = MIGRATION_EXEC_PROMPT_TEMPLATE.format(
                            step_number=step_num,
                            step_title=step["title"],
                            db_url=target_supabase_db_url,
                        )

                        migration_result = run_claude_code(
                            prompt=migration_prompt,
                            working_dir=project_dir,
                            system_prompt=MIGRATION_EXEC_SYSTEM_PROMPT,
                        )

                        # Log with redacted credentials
                        redacted_migration_prompt = redact_credentials(
                            migration_prompt, credentials_to_redact
                        )
                        log_step(store, run_id, step_num, "migration_exec", "claude_code",
                                 redacted_migration_prompt, migration_result, build_phase="schema")

                        migration = parse_migration_result(migration_result.text_result)

                        mig_emoji = "✅" if migration["status"] == "SUCCESS" else "❌"
                        print(f"\n  {mig_emoji} Migration: {migration['status']}")
                        print(f"     Found: {migration['migrations_found']}, Executed: {migration['migrations_executed']}")
                        if migration["errors"]:
                            print(f"     Errors:")
                            for err in migration["errors"]:
                                print(f"       • {err}")

                        if migration["status"] == "FAILED":
                            resolution_count += 1
                            if resolution_count < MAX_RESOLUTIONS_PER_STEP:
                                print(f"\n  🔄 Migration failed, retrying step...")
                                step["instructions"] += (
                                    f"\n\nMIGRATION EXECUTION FAILED:\n"
                                    + "\n".join(f"- {e}" for e in migration["errors"])
                                    + f"\n\nFix the migration SQL so it executes successfully against Postgres."
                                )
                                continue  # Re-enter loop, retry implementation
                            else:
                                print(f"\n  ❌ Max resolutions reached. Continuing with migration errors.")
                                completed_descriptions.append(
                                    f"Step {step_num} ({step['title']}): Completed with migration errors"
                                )
                                break

                        # ── Run rls_test if migration succeeded ──────
                        if target_supabase_url and target_supabase_anon_key:
                            print(f"\n  ▶ Testing RLS policies...")
                            print(f"  {'─' * 50}")

                            rls_prompt = RLS_TEST_PROMPT_TEMPLATE.format(
                                step_number=step_num,
                                step_title=step["title"],
                                supabase_url=target_supabase_url,
                                supabase_anon_key=target_supabase_anon_key,
                                supabase_service_key=target_supabase_service_key,
                            )

                            rls_result = run_claude_code(
                                prompt=rls_prompt,
                                working_dir=project_dir,
                                system_prompt=RLS_TEST_SYSTEM_PROMPT,
                            )

                            # Log with redacted credentials
                            redacted_rls_prompt = redact_credentials(
                                rls_prompt, credentials_to_redact
                            )
                            log_step(store, run_id, step_num, "rls_test", "claude_code",
                                     redacted_rls_prompt, rls_result, build_phase="schema")

                            rls = parse_rls_test_result(rls_result.text_result)

                            rls_emoji = {"YES": "✅", "NO": "❌", "PARTIAL": "⚠️", "UNKNOWN": "❓"}.get(
                                rls["rls_enforced"], "❓"
                            )
                            grants_emoji = {"YES": "✅", "NO": "❌", "UNKNOWN": "❓"}.get(
                                rls["grants_valid"], "❓"
                            )
                            triggers_emoji = {"YES": "✅", "NO": "❌", "N/A": "⏭", "UNKNOWN": "❓"}.get(
                                rls["triggers_work"], "❓"
                            )
                            print(f"\n  {rls_emoji} RLS enforced: {rls['rls_enforced']}")
                            print(f"  {grants_emoji} GRANTs valid: {rls['grants_valid']}")
                            print(f"  {triggers_emoji} Triggers work: {rls['triggers_work']}")
                            print(f"     Tests: {rls['tests_passed']}/{rls['tests_run']} passed")
                            if rls["errors"]:
                                print(f"     Errors:")
                                for err in rls["errors"]:
                                    print(f"       • {err}")

                            # Fail if RLS not enforced OR grants are missing
                            rls_failed = rls["status"] == "FAILED" or rls["rls_enforced"] == "NO"
                            grants_failed = rls["grants_valid"] == "NO"

                            if rls_failed or grants_failed:
                                resolution_count += 1
                                if resolution_count < MAX_RESOLUTIONS_PER_STEP:
                                    failure_reason = []
                                    if rls_failed:
                                        failure_reason.append(f"RLS enforcement: {rls['rls_enforced']}")
                                    if grants_failed:
                                        failure_reason.append("Missing GRANT statements (PGRST301)")
                                    print(f"\n  🔄 RLS/GRANT test failed, retrying step...")
                                    step["instructions"] += (
                                        f"\n\nRLS RUNTIME TEST FAILED:\n"
                                        + "\n".join(f"- {e}" for e in rls["errors"])
                                        + f"\n\nIssues: {', '.join(failure_reason)}"
                                        + f"\n\nFix the RLS policies and ensure GRANT statements exist. "
                                        f"Example: GRANT ALL ON tablename TO authenticated;"
                                    )
                                    continue  # Re-enter loop, retry implementation
                                else:
                                    print(f"\n  ❌ Max resolutions reached. Continuing with RLS/GRANT issues.")
                                    completed_descriptions.append(
                                        f"Step {step_num} ({step['title']}): Completed with RLS/GRANT issues"
                                    )
                                    break

                # ── Run Edge Function deployment for backend steps ──────
                is_backend_step = step.get("build_phase") == "backend"
                has_function_creds = (
                    supabase_cli_available
                    and target_supabase_url
                    and target_supabase_service_key
                )

                if is_backend_step and has_function_creds:
                    print(f"\n  ▶ Deploying Edge Functions...")
                    print(f"  {'─' * 50}")

                    ef_prompt = EDGE_FUNCTION_DEPLOY_PROMPT_TEMPLATE.format(
                        step_number=step_num,
                        step_title=step["title"],
                        supabase_url=target_supabase_url,
                        supabase_anon_key=target_supabase_anon_key or "",
                        supabase_service_key=target_supabase_service_key,
                    )

                    ef_result = run_claude_code(
                        prompt=ef_prompt,
                        working_dir=project_dir,
                        system_prompt=EDGE_FUNCTION_DEPLOY_SYSTEM_PROMPT,
                    )

                    redacted_ef_prompt = redact_credentials(
                        ef_prompt, credentials_to_redact
                    )
                    log_step(store, run_id, step_num, "edge_function_deploy", "claude_code",
                             redacted_ef_prompt, ef_result, build_phase=step.get("build_phase"))

                    ef = parse_edge_function_result(ef_result.text_result)

                    ef_emoji = "✅" if ef["status"] == "SUCCESS" else "❌"
                    print(f"\n  {ef_emoji} Edge Functions: {ef['status']}")
                    print(f"     Found: {ef['functions_found']}, Deployed: {ef['functions_deployed']}, Tested: {ef['functions_tested']}")
                    if ef["errors"]:
                        print(f"     Errors:")
                        for err in ef["errors"]:
                            print(f"       • {err}")

                    if ef["status"] == "FAILED":
                        resolution_count += 1
                        if resolution_count < MAX_RESOLUTIONS_PER_STEP:
                            print(f"\n  🔄 Edge Function deployment failed, retrying step...")
                            step["instructions"] += (
                                f"\n\nEDGE FUNCTION DEPLOYMENT FAILED:\n"
                                + "\n".join(f"- {e}" for e in ef["errors"])
                                + f"\n\nFix the Edge Function code so it deploys and returns 2xx."
                            )
                            continue
                        else:
                            print(f"\n  ❌ Max resolutions reached. Continuing with Edge Function issues.")
                            completed_descriptions.append(
                                f"Step {step_num} ({step['title']}): Completed with Edge Function issues"
                            )
                            break

                # Step fully passed (including runtime if applicable)
                completed_descriptions.append(
                    f"Step {step_num} ({step['title']}): Completed"
                )
                break

            elif verification["recommendation"] == "RETRY":
                retry_count += 1
                resolution_count += 1
                if retry_count <= max_retries and resolution_count < MAX_RESOLUTIONS_PER_STEP:
                    print(f"\n  🔄 Retrying step {step_num} (attempt {retry_count + 1})...")
                    step["instructions"] += (
                        f"\n\nPREVIOUS ATTEMPT ISSUES (fix these):\n"
                        + "\n".join(f"- {i}" for i in verification["issues"])
                    )
                else:
                    if retry_count > max_retries:
                        print(f"\n  ❌ Max retries reached for step {step_num}. Continuing anyway.")
                    else:
                        print(f"\n  ❌ Max resolutions reached for step {step_num}. Continuing anyway.")
                    completed_descriptions.append(
                        f"Step {step_num} ({step['title']}): Completed with issues ({retry_count} retries, {resolution_count} resolutions)"
                    )
                    break

            elif verification["recommendation"] == "MODIFY_PLAN":
                print(f"\n  📝 Plan modification requested. Continuing with best effort.")
                completed_descriptions.append(
                    f"Step {step_num} ({step['title']}): Needs attention"
                )
                break

            elif verification["recommendation"] == "WEB_SEARCH":
                resolution = verification.get("resolution", {})
                query = resolution.get("query", " ".join(verification["issues"]))
                print(f"\n  🔍 Searching: {query}")

                search_prompt = (
                    f"Search the web for: {query}\n\n"
                    f"Context: This is for step {step_num} of a project.\n"
                    f"Step goal: {step['title']}\n"
                    f"Issues encountered: {verification['issues']}\n\n"
                    f"Return concise, actionable findings. Include code examples if relevant."
                )
                search_result = run_claude_code(
                    prompt=search_prompt,
                    working_dir=project_dir,
                    system_prompt="You are a research assistant. Search the web and return "
                    "concise, actionable technical findings. Focus on code examples and correct API usage.",
                )

                log_step(store, run_id, step_num, "research", "claude_code",
                        search_prompt, search_result, build_phase=step.get("build_phase"))

                findings = search_result.text_result[:2000] if search_result.text_result else "No results found."
                step["instructions"] += (
                    f"\n\nPREVIOUS ATTEMPT ISSUES (fix these):\n"
                    + "\n".join(f"- {i}" for i in verification["issues"])
                    + f"\n\nRESEARCH FINDINGS (use this information):\n{findings}"
                )
                resolution_count += 1
                if resolution_count >= MAX_RESOLUTIONS_PER_STEP:
                    print(f"\n  ❌ Max resolutions reached for step {step_num}. Continuing anyway.")
                    completed_descriptions.append(
                        f"Step {step_num} ({step['title']}): Completed with issues ({retry_count} retries, {resolution_count} resolutions)"
                    )
                    break

            elif verification["recommendation"] == "RUN_DIAGNOSTIC":
                resolution = verification.get("resolution", {})
                command = resolution.get("command", "")
                reason = resolution.get("reason", "Verifier requested diagnostic")

                if command:
                    print(f"\n  🩺 Running diagnostic: {command}")
                    print(f"     Reason: {reason}")

                    diag_prompt = (
                        f"Run this command and return the full output:\n\n"
                        f"  {command}\n\n"
                        f"Do not modify any files. Only run the command and report what it outputs."
                    )
                    diag_result = run_claude_code(
                        prompt=diag_prompt,
                        working_dir=project_dir,
                        system_prompt="You are a diagnostic assistant. Run the requested command "
                        "using the Bash tool and return its complete output. Do not modify any "
                        "files or fix any issues — only run the command and report the results.",
                    )

                    log_step(store, run_id, step_num, "diagnostic", "claude_code",
                             diag_prompt, diag_result, build_phase=step.get("build_phase"))

                    diag_output = diag_result.text_result[:2000] if diag_result.text_result else "No output."
                    step["instructions"] += (
                        f"\n\nPREVIOUS ATTEMPT ISSUES (fix these):\n"
                        + "\n".join(f"- {i}" for i in verification["issues"])
                        + f"\n\nDIAGNOSTIC OUTPUT ({command}):\n{diag_output}"
                    )
                else:
                    print(f"\n  ⚠️  No diagnostic command provided")
                    step["instructions"] += (
                        f"\n\nPREVIOUS ATTEMPT ISSUES (fix these):\n"
                        + "\n".join(f"- {i}" for i in verification["issues"])
                    )

                resolution_count += 1
                if resolution_count >= MAX_RESOLUTIONS_PER_STEP:
                    print(f"\n  ❌ Max resolutions reached for step {step_num}. Continuing anyway.")
                    completed_descriptions.append(
                        f"Step {step_num} ({step['title']}): Completed with issues ({retry_count} retries, {resolution_count} resolutions)"
                    )
                    break

            elif verification["recommendation"] == "SKIP":
                reason = verification.get("resolution", {}).get("reason", "Verifier recommended skipping")
                print(f"\n  ⏭  Skipping step {step_num}: {reason}")
                completed_descriptions.append(
                    f"Step {step_num} ({step['title']}): SKIPPED - {reason}"
                )
                break

            else:
                completed_descriptions.append(
                    f"Step {step_num} ({step['title']}): Completed (unverified)"
                )
                break

    if not skip_smoke_test:
        # ── Phase 3: Smoke Test ───────────────────────
        print(f"\n{'=' * 60}")
        print("  PHASE 3: SMOKE TEST (Claude Code)")
        print(f"{'=' * 60}")

        # Build credentials section for smoke test
        credentials_section = ""
        auth_instructions = ""
        if target_supabase_url and target_supabase_anon_key:
            credentials_section = f"""SUPABASE_URL: {target_supabase_url}
SUPABASE_ANON_KEY: {target_supabase_anon_key}"""
            if target_supabase_service_key:
                credentials_section += f"\nSUPABASE_SERVICE_KEY: {target_supabase_service_key}"
            auth_instructions = """
If the app has authentication:
1. Create a test user using the service_role key
2. Sign in as that user
3. Verify authenticated features work
4. Clean up the test user"""

        smoke_prompt = SMOKE_TEST_PROMPT_TEMPLATE.format(
            user_prompt=user_prompt,
            credentials_section=credentials_section,
            completed_steps="\n".join(completed_descriptions) if completed_descriptions else "None",
            auth_instructions=auth_instructions,
        )

        smoke_result = run_claude_code(
            prompt=smoke_prompt,
            working_dir=project_dir,
            system_prompt=SMOKE_TEST_SYSTEM_PROMPT,
        )

        # Log with redacted credentials
        redacted_smoke_prompt = redact_credentials(smoke_prompt, credentials_to_redact)
        log_step(store, run_id, len(steps) + 1, "smoke_test", "claude_code",
                 redacted_smoke_prompt, smoke_result)

        smoke = parse_smoke_test(smoke_result.text_result)

        app_emoji = {"YES": "✅", "NO": "❌", "N/A": "⏭"}.get(smoke["app_starts"], "❓")
        test_emoji = {"YES": "✅", "NO": "❌", "N/A": "⏭", "NO_TESTS": "⏭"}.get(smoke["tests_pass"], "❓")
        build_emoji = {"YES": "✅", "NO": "❌", "N/A": "⏭"}.get(smoke["build_succeeds"], "❓")
        auth_emoji = {"YES": "✅", "NO": "❌", "N/A": "⏭"}.get(smoke["auth_works"], "❓")
        storage_emoji = {"YES": "✅", "NO": "❌", "N/A": "⏭"}.get(smoke["storage_works"], "❓")

        print(f"\n  {build_emoji} Build succeeds: {smoke['build_succeeds']}")
        print(f"  {app_emoji} App starts: {smoke['app_starts']}")
        print(f"  {test_emoji} Tests pass: {smoke['tests_pass']}")
        print(f"  {auth_emoji} Auth works: {smoke['auth_works']}")
        print(f"  {storage_emoji} Storage works: {smoke['storage_works']}")
        if smoke["errors"]:
            print(f"  Errors:")
            for err in smoke["errors"]:
                print(f"    • {err}")
        print(f"  Summary: {smoke['summary']}")

        if smoke["build_succeeds"] == "NO":
            run_final_status = "completed_build_failing"
        elif smoke["app_starts"] == "NO":
            run_final_status = "completed_failing"
        elif smoke["tests_pass"] == "NO":
            run_final_status = "completed_tests_failing"
        elif smoke["auth_works"] == "NO":
            run_final_status = "completed_auth_failing"
        else:
            run_final_status = "completed"
    else:
        print(f"\n  ⏭  Smoke test skipped (--skip-smoke-test)")
        run_final_status = "completed"

    # ── Phase 4: Approach Analysis ───────────────────────
    print(f"\n{'=' * 60}")
    print("  PHASE 4: APPROACH ANALYSIS (Claude Code)")
    print(f"{'=' * 60}")

    approach_prompt = APPROACH_ANALYSIS_PROMPT_TEMPLATE.format(
        user_prompt=user_prompt,
    )

    approach_result = run_claude_code(
        prompt=approach_prompt,
        working_dir=project_dir,
        system_prompt=APPROACH_ANALYSIS_SYSTEM_PROMPT,
    )

    log_step(store, run_id, len(steps) + 2, "approach_analysis", "claude_code",
             approach_prompt, approach_result)

    approach = parse_approach_analysis(approach_result.text_result)

    if approach["parse_error"]:
        print(f"\n  ⚠️  Could not parse approach analysis: {approach['parse_error']}")
    else:
        print("\n  Analysis complete. Findings:")
        if approach["framework"]:
            fw = approach["framework"]
            print(f"    Framework: {fw.get('name', 'unknown')} {fw.get('version', '')}")
        if approach["scaffolding"]:
            sc = approach["scaffolding"]
            print(f"    Scaffolding: {sc.get('tool', 'unknown')}")
        if approach["supabase_integration"]:
            sb = approach["supabase_integration"]
            print(f"    Supabase client: {sb.get('client_library', 'unknown')}")
            print(f"    Auth pattern: {sb.get('auth_pattern', 'unknown')}")
        if approach["architecture"]:
            arch = approach["architecture"]
            print(f"    API pattern: {arch.get('api_pattern', 'unknown')}")
            print(f"    Styling: {arch.get('styling', 'unknown')}")

    # ── Phase 5: Completion ───────────────────────
    print(f"\n{'=' * 60}")
    print("  PHASE 5: COMPLETE")
    print(f"{'=' * 60}")

    store.finish_run(run_id, status=run_final_status)

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
    parser.add_argument("--skip-smoke-test", action="store_true",
                        help="Skip the smoke test phase")
    parser.add_argument("--supabase-url", default=None,
                        help="Target Supabase project URL (REST API) for runtime testing")
    parser.add_argument("--supabase-anon-key", default=None,
                        help="Target Supabase anon key for runtime testing")
    parser.add_argument("--supabase-service-key", default=None,
                        help="Target Supabase service_role key (for auth and admin operations)")
    parser.add_argument("--supabase-db-url", default=None,
                        help="Target Supabase Postgres connection string (for migrations)")
    parser.add_argument("--supabase-project-ref", default=None,
                        help="Supabase project ref for CLI operations (enables Edge Function deployment)")
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
        project_dir = str(DEFAULT_PROJECT_DIR / f"{timestamp}_{slug}")

    run_orchestration(
        user_prompt=args.prompt,
        project_dir=project_dir,
        max_retries=args.max_retries,
        resume_run_id=args.resume,
        start_from_step=args.start_step,
        skip_smoke_test=args.skip_smoke_test,
        target_supabase_url=args.supabase_url,
        target_supabase_anon_key=args.supabase_anon_key,
        target_supabase_service_key=args.supabase_service_key,
        target_supabase_db_url=args.supabase_db_url,
        target_supabase_project_ref=args.supabase_project_ref,
    )


if __name__ == "__main__":
    if "--test-parse-verification" in sys.argv:
        text = """STATUS: FAIL
ISSUES:
- RLS policy syntax is wrong
SUMMARY: Bad policy
RECOMMENDATION: WEB_SEARCH
RESOLUTION: {"query": "supabase RLS policy syntax"}"""
        result = parse_verification(text)
        assert result["recommendation"] == "WEB_SEARCH"
        assert result["resolution"]["query"] == "supabase RLS policy syntax"
        print("PASS:", result)

        # Test RUN_DIAGNOSTIC parsing
        text2 = """STATUS: FAIL
ISSUES:
- Type errors in auth module
SUMMARY: TypeScript compilation fails
RECOMMENDATION: RUN_DIAGNOSTIC
RESOLUTION: {"command": "npx tsc --noEmit", "reason": "check type errors"}"""
        result2 = parse_verification(text2)
        assert result2["recommendation"] == "RUN_DIAGNOSTIC"
        assert result2["resolution"]["command"] == "npx tsc --noEmit"
        assert result2["resolution"]["reason"] == "check type errors"
        print("PASS:", result2)

        # Test smoke test parsing
        text3 = """APP_STARTS: YES
TESTS_PASS: NO
ERRORS:
- 2 of 5 tests failed
- Missing DATABASE_URL
SUMMARY: App starts but tests fail."""
        result3 = parse_smoke_test(text3)
        assert result3["app_starts"] == "YES"
        assert result3["tests_pass"] == "NO"
        assert len(result3["errors"]) == 2
        assert "DATABASE_URL" in result3["errors"][1]
        print("PASS:", result3)

        # Test migration result parsing
        text4 = """MIGRATIONS_FOUND: 3
MIGRATIONS_EXECUTED: 3
STATUS: SUCCESS
ERRORS:
SUMMARY: All migrations executed successfully."""
        result4 = parse_migration_result(text4)
        assert result4["migrations_found"] == 3
        assert result4["migrations_executed"] == 3
        assert result4["status"] == "SUCCESS"
        assert result4["errors"] == []
        print("PASS:", result4)

        # Test migration failure parsing
        text4b = """MIGRATIONS_FOUND: 2
MIGRATIONS_EXECUTED: 1
STATUS: FAILED
ERRORS:
- relation "users" already exists
- permission denied for schema public
SUMMARY: Migration failed on second file."""
        result4b = parse_migration_result(text4b)
        assert result4b["status"] == "FAILED"
        assert len(result4b["errors"]) == 2
        assert "already exists" in result4b["errors"][0]
        print("PASS:", result4b)

        # Test RLS test result parsing
        text5 = """TEST_USER_CREATED: YES
TESTS_RUN: 5
TESTS_PASSED: 4
STATUS: FAILED
RLS_ENFORCED: PARTIAL
GRANTS_VALID: NO
ERRORS:
- todos table allows public read
- PGRST301: permission denied for table todos
SUMMARY: RLS mostly enforced, but missing GRANT TO authenticated."""
        result5 = parse_rls_test_result(text5)
        assert result5["test_user_created"] == "YES"
        assert result5["tests_run"] == 5
        assert result5["tests_passed"] == 4
        assert result5["status"] == "FAILED"
        assert result5["rls_enforced"] == "PARTIAL"
        assert result5["grants_valid"] == "NO"
        assert "todos table allows public read" in result5["errors"][0]
        print("PASS:", result5)

        # Test Edge Function result parsing
        text_ef = """FUNCTIONS_FOUND: 2
FUNCTIONS_DEPLOYED: 2
FUNCTIONS_TESTED: 1
STATUS: FAILED
ERRORS:
- hello-world returned 500 Internal Server Error
SUMMARY: Deployed but one function crashes."""
        result_ef = parse_edge_function_result(text_ef)
        assert result_ef["functions_found"] == 2
        assert result_ef["functions_deployed"] == 2
        assert result_ef["functions_tested"] == 1
        assert result_ef["status"] == "FAILED"
        assert "500" in result_ef["errors"][0]
        print("PASS:", result_ef)

        # Test smoke test with auth
        text6 = """APP_STARTS: YES
TESTS_PASS: YES
AUTH_WORKS: NO
ERRORS:
- Sign-in returns 400 Bad Request
SUMMARY: App works but auth is broken."""
        result6 = parse_smoke_test(text6)
        assert result6["app_starts"] == "YES"
        assert result6["tests_pass"] == "YES"
        assert result6["auth_works"] == "NO"
        assert "400" in result6["errors"][0]
        print("PASS:", result6)

        # Test credential redaction
        test_creds = {
            "supabase_url": "https://abc.supabase.co",
            "supabase_anon_key": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        }
        test_text = "URL: https://abc.supabase.co, KEY: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        redacted = redact_credentials(test_text, test_creds)
        assert "https://abc.supabase.co" not in redacted
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted
        assert "***REDACTED***" in redacted
        print("PASS: credential redaction works")

        # Test markdown stripping in smoke test parser
        text7 = """**APP_STARTS:** YES
**TESTS_PASS:** NO
**AUTH_WORKS:** N/A
**ERRORS:**
- Server crashed on startup
**SUMMARY:** App fails to start due to missing config."""
        result7 = parse_smoke_test(text7)
        assert result7["app_starts"] == "YES"
        assert result7["tests_pass"] == "NO"
        assert result7["auth_works"] == "N/A"
        print("PASS:", result7)

        sys.exit(0)
    main()
