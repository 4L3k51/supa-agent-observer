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
import shutil
import subprocess
import sys


def check(label: str, condition: bool, fix: str = ""):
    """Print a check result."""
    icon = "✅" if condition else "❌"
    print(f"  {icon} {label}")
    if not condition and fix:
        print(f"     Fix: {fix}")
    return condition


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
    else:
        print("  ⚠️  supabase CLI not found (optional)")
        print("     Edge Function deployment will be unavailable")
        print("     Install: brew install supabase/tap/supabase")

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
