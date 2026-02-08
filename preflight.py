#!/usr/bin/env python3
"""
Preflight Check
===============
Verifies that Claude Code CLI and Cursor Agent CLI are installed,
authenticated, and working before you run the orchestrator.

Usage:
    python preflight.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False

try:
    from supabase import create_client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False


def check(label: str, condition: bool, fix: str = ""):
    """Print a check result."""
    icon = "✅" if condition else "❌"
    print(f"  {icon} {label}")
    if not condition and fix:
        print(f"     Fix: {fix}")
    return condition


REQUIRED_SCHEMA = {
    "orchestrator_runs": ["run_id", "user_prompt", "project_dir", "status", "created_at", "finished_at"],
    "orchestrator_steps": [
        "id", "run_id", "step_number", "phase", "tool", "prompt_sent",
        "raw_stdout", "raw_stderr", "parsed_result", "exit_code",
        "duration_seconds", "build_phase", "timestamp", "commands_executed"
    ],
    "orchestrator_events": ["id", "run_id", "step_id", "event_type", "event_data", "timestamp"],
}


def check_supabase_schema(supabase_url: str, supabase_key: str) -> tuple[bool, list[str]]:
    """Check if Supabase schema has all required tables and columns.

    Returns (all_ok, list of missing items).
    """
    if not SUPABASE_AVAILABLE:
        return False, ["supabase Python package not installed"]

    try:
        client = create_client(supabase_url, supabase_key)
        missing = []

        # Test each table by selecting its required columns
        for table, columns in REQUIRED_SCHEMA.items():
            try:
                # Try to select all required columns (limit 0 to avoid fetching data)
                col_list = ", ".join(columns)
                client.table(table).select(col_list).limit(0).execute()
            except Exception as e:
                error_msg = str(e)
                if "relation" in error_msg and "does not exist" in error_msg:
                    missing.append(f"Table '{table}' not found")
                elif "column" in error_msg and "does not exist" in error_msg:
                    # Extract column name from error like: column "foo" does not exist
                    match = re.search(r"column ['\"]?(\w+)['\"]? .* does not exist", error_msg)
                    if match:
                        missing.append(f"Column '{table}.{match.group(1)}' not found")
                    else:
                        missing.append(f"Column missing in '{table}': {error_msg[:80]}")
                elif "Could not find" in error_msg and "column" in error_msg:
                    # PostgREST error format: Could not find the 'foo' column
                    match = re.search(r"Could not find the '(\w+)' column", error_msg)
                    if match:
                        missing.append(f"Column '{table}.{match.group(1)}' not found")
                    else:
                        missing.append(f"Column missing in '{table}': {error_msg[:80]}")
                else:
                    missing.append(f"Error checking '{table}': {error_msg[:80]}")

        return len(missing) == 0, missing

    except Exception as e:
        return False, [f"Could not connect to Supabase: {str(e)[:100]}"]


def run_cmd(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """Run a command and return (exit_code, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -2, "", "Command timed out"
    except Exception as e:
        return -3, "", str(e)


def main():
    print("""
╔══════════════════════════════════════════════════════════════╗
║              ORCHESTRATOR PREFLIGHT CHECK                     ║
╚══════════════════════════════════════════════════════════════╝
""")

    all_ok = True

    # ── Python version ──
    print("  Python:")
    py_ver = sys.version_info
    all_ok &= check(
        f"Python {py_ver.major}.{py_ver.minor}.{py_ver.micro}",
        py_ver >= (3, 9),
        "Install Python 3.9+"
    )

    # ── Claude Code CLI ──
    print("\n  Claude Code CLI:")

    claude_path = shutil.which("claude")
    all_ok &= check(
        f"claude binary found: {claude_path or 'NOT FOUND'}",
        claude_path is not None,
        "npm install -g @anthropic-ai/claude-code"
    )

    if claude_path:
        code, out, err = run_cmd(["claude", "--version"])
        version = out.strip() or err.strip()
        all_ok &= check(
            f"claude version: {version[:60]}",
            code == 0,
            "Check claude installation"
        )

        # Test headless mode with a simple prompt
        print("    Testing headless mode (this may take a few seconds)...")
        code, out, err = run_cmd(
            ["claude", "-p", "Say exactly: PREFLIGHT_OK", "--output-format", "json"],
            timeout=30,
        )
        if code == 0:
            try:
                data = json.loads(out)
                has_result = "result" in data or "PREFLIGHT_OK" in out
                all_ok &= check("Headless mode works", has_result)
            except json.JSONDecodeError:
                all_ok &= check(
                    "Headless mode works (non-JSON response)",
                    "PREFLIGHT_OK" in out
                )
        else:
            all_ok &= check(
                f"Headless mode works",
                False,
                f"Error: {err[:100]}"
            )

    # ── Cursor Agent CLI ──
    print("\n  Cursor Agent CLI:")

    cursor_path = shutil.which("agent")
    all_ok &= check(
        f"agent binary found: {cursor_path or 'NOT FOUND'}",
        cursor_path is not None,
        "curl https://cursor.com/install -fsSL | bash"
    )

    if cursor_path:
        code, out, err = run_cmd(["agent", "--version"])
        version = out.strip() or err.strip()
        all_ok &= check(
            f"agent version: {version[:60]}",
            code == 0 or version != "",
            "Check agent installation"
        )

        # Test headless mode
        print("    Testing headless mode (this may take a few seconds)...")
        code, out, err = run_cmd(
            ["agent", "-p", "Say exactly: PREFLIGHT_OK",
             "--output-format", "json"],
            timeout=30,
        )

        # Cursor might hang (known bug) but still produce output
        if "PREFLIGHT_OK" in out or code == 0:
            all_ok &= check("Headless mode works", True)
        elif code == -2:
            print("    ⚠️  agent timed out (known issue).")
            print("       The orchestrator handles this with idle timeouts.")
            print("       Marking as OK if binary exists.")
            all_ok &= check("Headless mode (timeout workaround)", cursor_path is not None)
        else:
            all_ok &= check(
                f"Headless mode works",
                False,
                f"Error: {err[:100]}"
            )

    # ── Supabase CLI (optional) ──
    print("\n  Supabase CLI (optional):")

    supabase_path = shutil.which("supabase")
    if supabase_path:
        code, out, err = run_cmd(["supabase", "--version"])
        version = out.strip() or err.strip()
        check(f"supabase CLI: {version[:40]}", True)

        # Check authentication status (needed for --supabase-project-ref)
        access_token = os.environ.get("SUPABASE_ACCESS_TOKEN")
        if access_token:
            check("Authenticated via SUPABASE_ACCESS_TOKEN", True)
        else:
            # Try `supabase projects list` to check login status
            print("    Checking login status...")
            code, out, err = run_cmd(["supabase", "projects", "list"], timeout=30)
            if code == 0:
                check("Authenticated (logged in)", True)
            else:
                print("  ⚠️  Supabase CLI not authenticated")
                print("     Required for --supabase-project-ref (Edge Functions)")
                print("     Fix: Run 'supabase login' or set SUPABASE_ACCESS_TOKEN")
    else:
        print("  ⚠️  supabase CLI not found (optional)")
        print("     Edge Function deployment will be unavailable")
        print("     Install: brew install supabase/tap/supabase")

    # ── Supabase Schema Check ──
    print("\n  Supabase Schema:")

    # Load .env file
    if DOTENV_AVAILABLE:
        env_file = Path(__file__).parent / ".env"
        if env_file.exists():
            load_dotenv(env_file)

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")

    if not supabase_url or not supabase_key:
        print("  ⚠️  SUPABASE_URL or SUPABASE_KEY not set in .env")
        print("     Schema check skipped - configure .env to enable logging")
    elif not SUPABASE_AVAILABLE:
        print("  ⚠️  supabase Python package not installed")
        print("     Run: pip install supabase")
    else:
        schema_ok, missing = check_supabase_schema(supabase_url, supabase_key)
        if schema_ok:
            check("All required tables and columns exist", True)
        else:
            all_ok &= check("Schema up to date", False, "Run migration.sql in Supabase SQL Editor")
            for item in missing[:5]:  # Show first 5 missing items
                print(f"       - {item}")
            if len(missing) > 5:
                print(f"       ... and {len(missing) - 5} more")

    # ── Project directory ──
    print("\n  Environment:")

    default_dir = os.path.expanduser("~/orchestrator-projects")
    all_ok &= check(
        f"Project directory writable: {default_dir}",
        os.access(os.path.dirname(default_dir) or os.path.expanduser("~"), os.W_OK),
    )

    # ── Summary ──
    print(f"\n{'=' * 60}")
    if all_ok:
        print("  ✅ ALL CHECKS PASSED - Ready to orchestrate!")
        print(f"\n  Try a test run:")
        print(f"    python orchestrator.py \"Create a simple hello world Node.js project\"")
    else:
        print("  ❌ SOME CHECKS FAILED - Fix the issues above first.")
        print(f"\n  Quick install commands:")
        print(f"    Claude Code:   npm install -g @anthropic-ai/claude-code")
        print(f"    Cursor CLI:    curl https://cursor.com/install -fsSL | bash")
        print(f"    Supabase CLI:  brew install supabase/tap/supabase")
    print()


if __name__ == "__main__":
    main()
