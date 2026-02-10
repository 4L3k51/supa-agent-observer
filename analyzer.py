#!/usr/bin/env python3
"""
Orchestrator Log Analyzer
=========================
Analyze the logs from orchestration runs to understand what went well,
what broke, and what patterns emerge.

Usage:
    python analyzer.py <run_id>                  # Full analysis of a run
    python analyzer.py <run_id> --errors         # Show only errors
    python analyzer.py <run_id> --tools          # Show tool usage breakdown
    python analyzer.py <run_id> --timeline       # Show timeline of events
    python analyzer.py <run_id> --step N         # Deep dive on a specific step
    python analyzer.py <run_id> --save-report    # Save full analysis to reports/
    python analyzer.py <run_id> --export report  # Export full report as JSON (legacy)
    python analyzer.py --compare id1 id2         # Compare two runs
"""

import argparse
import json
import os
import re
import sys
import textwrap
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Any

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from storage import SupabaseStorage, create_storage


def get_store() -> SupabaseStorage:
    try:
        return create_storage()
    except Exception as e:
        print(f"❌ Could not initialize storage: {e}")
        sys.exit(1)


# ─────────────────────────────────────────────
# Analysis Functions
# ─────────────────────────────────────────────

def analyze_run(store: SupabaseStorage, run_id: str):
    """Full analysis of an orchestration run."""
    run = store.get_run(run_id)
    if not run:
        print(f"❌ Run '{run_id}' not found.")
        return

    steps = store.get_steps(run_id)
    events = store.get_events(run_id)

    # ── Run Overview ──
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║                    RUN ANALYSIS: {run_id:<25}  ║
╠══════════════════════════════════════════════════════════════╣
║  Prompt:   {str(run.get('user_prompt',''))[:49]:<49}║
║  Status:   {str(run.get('status','')):<49}║
║  Created:  {str(run.get('created_at','')):<49}║
║  Steps:    {len(steps):<49}║
║  Events:   {len(events):<49}║
╚══════════════════════════════════════════════════════════════╝
""")

    # ── Step-by-step breakdown ──
    print("STEP-BY-STEP BREAKDOWN")
    print("=" * 70)

    step_groups = {}
    for s in steps:
        num = s["step_number"]
        if num not in step_groups:
            step_groups[num] = []
        step_groups[num].append(s)

    total_duration = 0
    for step_num in sorted(step_groups.keys()):
        group = step_groups[step_num]
        print(f"\n  Step {step_num}:")

        for s in group:
            phase = s["phase"]
            tool = s["tool"]
            duration = s.get("duration_seconds") or 0
            exit_code = s.get("exit_code", -1)
            total_duration += duration

            status_icon = "✅" if exit_code == 0 else "❌"
            print(f"    {status_icon} [{phase:<10}] via {tool:<12} "
                  f"({duration:.1f}s, exit: {exit_code})")

            stderr = s.get("raw_stderr", "")
            if stderr and len(stderr.strip()) > 0:
                stderr_preview = stderr.strip()[:200]
                print(f"       stderr: {stderr_preview}")

    print(f"\n  Total duration: {total_duration:.1f}s ({total_duration / 60:.1f}min)")

    # ── Event type breakdown ──
    print(f"\nEVENT TYPE BREAKDOWN")
    print("=" * 70)

    event_types = Counter()
    tool_calls = Counter()
    errors = []

    for e in events:
        event_types[e.get("event_type", "unknown")] += 1

        try:
            raw = e.get("event_data", "{}")
            data = json.loads(raw) if isinstance(raw, str) else raw

            if e.get("event_type") == "tool_call":
                tool_name = data.get("tool_call", {}).get("name", "unknown")
                if tool_name == "unknown":
                    tc = data.get("tool_call", {})
                    if "writeToolCall" in tc:
                        tool_name = "write_file"
                    elif "readToolCall" in tc:
                        tool_name = "read_file"
                    elif "terminalToolCall" in tc:
                        tool_name = "terminal"
                tool_calls[tool_name] += 1

            if "error" in str(data).lower() and e.get("event_type") != "system":
                errors.append({
                    "step_id": e.get("step_id"),
                    "type": e.get("event_type"),
                    "data": data,
                })

        except (json.JSONDecodeError, TypeError):
            pass

    for etype, count in event_types.most_common():
        print(f"  {etype:<25} {count:>5}")

    if tool_calls:
        print(f"\nTOOL USAGE")
        print("=" * 70)
        for tool, count in tool_calls.most_common():
            bar = "█" * min(count, 40)
            print(f"  {tool:<25} {count:>4} {bar}")

    if errors:
        print(f"\nERRORS DETECTED: {len(errors)}")
        print("=" * 70)
        for i, err in enumerate(errors[:20]):
            print(f"\n  Error {i + 1} (step_id: {err['step_id']}):")
            preview = json.dumps(err["data"], indent=2)[:300]
            for line in preview.split("\n"):
                print(f"    {line}")

    print()


def show_errors(store: SupabaseStorage, run_id: str):
    """Show only errors and failures from a run."""
    steps = store.get_steps(run_id)
    error_steps = [
        s for s in steps
        if s.get("exit_code", 0) != 0
        or (s.get("raw_stderr", "") or "").strip()
        or "error" in (s.get("parsed_result", "") or "").lower()
        or "FAIL" in (s.get("parsed_result", "") or "").upper()
    ]

    print(f"\n  ERRORS AND FAILURES FOR RUN: {run_id}")
    print("=" * 70)

    if not error_steps:
        print("  ✅ No errors found!")
        return

    for s in error_steps:
        print(f"\n  Step {s['step_number']} [{s['phase']}] via {s['tool']}:")
        print(f"  Exit code: {s.get('exit_code', '?')}")

        stderr = s.get("raw_stderr", "")
        if stderr:
            print(f"  stderr:")
            for line in stderr.strip().split("\n")[:10]:
                print(f"    {line}")

        parsed = s.get("parsed_result", "") or ""
        if "error" in parsed.lower() or "fail" in parsed.lower():
            print(f"  Result excerpt:")
            for line in parsed.split("\n"):
                if "error" in line.lower() or "fail" in line.lower():
                    print(f"    ► {line.strip()}")


def show_tools(store: SupabaseStorage, run_id: str):
    """Show detailed tool usage breakdown."""
    events = store.get_events(run_id)

    print(f"\n  TOOL USAGE FOR RUN: {run_id}")
    print("=" * 70)

    tools = []
    for e in events:
        if e.get("event_type") != "tool_call":
            continue
        try:
            raw = e.get("event_data", "{}")
            data = json.loads(raw) if isinstance(raw, str) else raw
            tools.append(data)
        except (json.JSONDecodeError, TypeError):
            pass

    if not tools:
        print("  No tool calls found.")
        return

    file_writes = []
    file_reads = []
    commands = []
    other = []

    for t in tools:
        tc = t.get("tool_call", {})
        subtype = t.get("subtype", "")

        if subtype != "started":
            continue

        name = tc.get("name", "")

        if name in ("Write", "write_file") or "writeToolCall" in tc:
            path = (tc.get("args", {}).get("path", "")
                    or tc.get("writeToolCall", {}).get("args", {}).get("path", ""))
            file_writes.append(path)
        elif name in ("Read", "read_file") or "readToolCall" in tc:
            path = (tc.get("args", {}).get("path", "")
                    or tc.get("readToolCall", {}).get("args", {}).get("path", ""))
            file_reads.append(path)
        elif name in ("Bash", "terminal") or "terminalToolCall" in tc:
            cmd = (tc.get("args", {}).get("command", "")
                   or tc.get("terminalToolCall", {}).get("args", {}).get("command", ""))
            commands.append(cmd)
        else:
            other.append(name or str(tc)[:60])

    print(f"\n  Files written ({len(file_writes)}):")
    for f in file_writes:
        print(f"    📝 {f}")
    print(f"\n  Files read ({len(file_reads)}):")
    for f in file_reads:
        print(f"    👁  {f}")
    print(f"\n  Commands run ({len(commands)}):")
    for c in commands:
        print(f"    💻 {c[:80]}")
    if other:
        print(f"\n  Other tools ({len(other)}):")
        for o in other:
            print(f"    🔧 {o}")


def show_timeline(store: SupabaseStorage, run_id: str):
    """Show chronological timeline of all events."""
    steps = store.get_steps(run_id)

    print(f"\n  TIMELINE FOR RUN: {run_id}")
    print("=" * 70)

    for s in steps:
        phase_icon = {
            "plan": "📋", "implement": "🔨", "verify": "🔍", "replan": "📝",
        }.get(s["phase"], "❓")

        tool_label = "Claude Code" if s["tool"] == "claude_code" else "Cursor"
        duration = s.get("duration_seconds") or 0
        ts = s.get("timestamp", "?")

        print(f"\n  {str(ts)[:19]}")
        print(f"  {phase_icon} Step {s['step_number']} | {s['phase'].upper()} | {tool_label} | {duration:.1f}s")

        step_events = store.get_step_events(s["id"])

        tool_count = sum(1 for e in step_events if e.get("event_type") == "tool_call")
        text_count = sum(1 for e in step_events if e.get("event_type") == "assistant")
        result_count = sum(1 for e in step_events if e.get("event_type") == "result")

        print(f"    Events: {len(step_events)} total "
              f"({tool_count} tool calls, {text_count} text, {result_count} results)")


def deep_dive_step(store: SupabaseStorage, run_id: str, step_number: int):
    """Deep dive into a specific step."""
    all_steps = store.get_steps(run_id)
    steps = [s for s in all_steps if s["step_number"] == step_number]

    if not steps:
        print(f"❌ No data for step {step_number} in run {run_id}")
        return

    print(f"\n  DEEP DIVE: Step {step_number} of run {run_id}")
    print("=" * 70)

    for s in steps:
        print(f"\n  Phase: {s['phase']} | Tool: {s['tool']}")
        print(f"  Duration: {(s.get('duration_seconds') or 0):.1f}s | Exit: {s.get('exit_code', '?')}")

        print(f"\n  PROMPT SENT:")
        print(f"  {'─' * 60}")
        for line in (s.get("prompt_sent", "") or "").split("\n"):
            print(f"    {line}")

        print(f"\n  PARSED RESULT:")
        print(f"  {'─' * 60}")
        for line in (s.get("parsed_result", "") or "").split("\n"):
            print(f"    {line}")

        stderr = s.get("raw_stderr", "")
        if stderr:
            print(f"\n  STDERR:")
            print(f"  {'─' * 60}")
            for line in stderr.split("\n")[:20]:
                print(f"    {line}")

        events = store.get_step_events(s["id"])

        print(f"\n  RAW EVENTS ({len(events)}):")
        print(f"  {'─' * 60}")
        for e in events:
            try:
                raw = e.get("event_data", "{}")
                data = json.loads(raw) if isinstance(raw, str) else raw
                etype = data.get("type", "?")
                subtype = data.get("subtype", "")
                compact = json.dumps(data, separators=(",", ":"))
                if len(compact) > 120:
                    compact = compact[:120] + "..."
                print(f"    [{etype}:{subtype}] {compact}")
            except (json.JSONDecodeError, TypeError):
                raw = e.get("event_data", "")
                print(f"    [raw] {str(raw)[:120]}")


def export_report(store: SupabaseStorage, run_id: str, output_path: str):
    """Export a complete run report as JSON."""
    run = store.get_run(run_id)
    if not run:
        print(f"❌ Run '{run_id}' not found.")
        return

    steps = store.get_steps(run_id)
    events = store.get_events(run_id)

    report = {
        "run": run,
        "steps": steps,
        "events": [],
        "summary": {
            "total_steps": len(steps),
            "total_events": len(events),
            "phases": dict(Counter(s["phase"] for s in steps)),
            "tools_used": dict(Counter(s["tool"] for s in steps)),
            "exit_codes": dict(Counter(s.get("exit_code", -1) for s in steps)),
        }
    }

    for e in events:
        event_dict = dict(e) if not isinstance(e, dict) else e
        try:
            raw = event_dict.get("event_data", "{}")
            event_dict["parsed_data"] = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            event_dict["parsed_data"] = None
        report["events"].append(event_dict)

    output_file = f"{output_path}_{run_id}.json"
    with open(output_file, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"  ✅ Report exported to: {output_file}")
    print(f"     Steps: {len(steps)}, Events: {len(events)}")


def compare_runs(store: SupabaseStorage, run_id1: str, run_id2: str):
    """Compare two runs side by side."""
    print(f"\n  COMPARISON: {run_id1} vs {run_id2}")
    print("=" * 70)

    for rid in [run_id1, run_id2]:
        run = store.get_run(rid)
        if not run:
            print(f"❌ Run '{rid}' not found.")
            return

        steps = store.get_steps(rid)
        events = store.get_events(rid)

        total_duration = sum(s.get("duration_seconds") or 0 for s in steps)
        error_count = sum(1 for s in steps if s.get("exit_code", 0) != 0)
        impl_steps = [s for s in steps if s["phase"] == "implement"]
        verify_steps = [s for s in steps if s["phase"] == "verify"]

        pass_count = sum(
            1 for s in verify_steps
            if s.get("parsed_result") and "PASS" in (s["parsed_result"] or "").upper()
        )

        print(f"\n  Run: {rid}")
        print(f"    Prompt: {run.get('user_prompt', '?')[:60]}")
        print(f"    Status: {run.get('status', '?')}")
        print(f"    Total duration: {total_duration:.1f}s")
        print(f"    Steps: {len(steps)} ({error_count} errors)")
        print(f"    Events: {len(events)}")
        print(f"    Implementation attempts: {len(impl_steps)}")
        print(f"    Verifications passed: {pass_count}/{len(verify_steps)}")


# ─────────────────────────────────────────────
# Enhanced Report Generation
# ─────────────────────────────────────────────

# Failure category patterns
FAILURE_PATTERNS = {
    "schema_mismatch": [
        r"column .+ does not exist",
        r"could not find .+ in the schema cache",
        r"relation .+ does not exist",
        r"no such column",
    ],
    "rls_policy": [
        r"row.level security",
        r"rls policy",
        r"violates row-level security",
        r"permission denied for table",
    ],
    "migration_error": [
        r"migration failed",
        r"already exists",
        r"duplicate key",
        r"cannot drop .+ because",
    ],
    "type_error": [
        r"type .+ is not assignable",
        r"typescript error",
        r"cannot find name",
        r"property .+ does not exist on type",
    ],
    "missing_dependency": [
        r"module not found",
        r"cannot find module",
        r"no such file or directory",
        r"enoent",
    ],
    "auth_error": [
        r"auth.*error",
        r"invalid.*token",
        r"unauthorized",
        r"authentication failed",
    ],
    "edge_function_error": [
        r"edge function",
        r"function .+ not found",
        r"deno",
    ],
    "realtime_error": [
        r"realtime",
        r"websocket",
        r"subscription",
    ],
}


def categorize_error(error_text: str) -> str:
    """Categorize an error based on pattern matching."""
    if not error_text:
        return "unknown"
    error_lower = error_text.lower()
    for category, patterns in FAILURE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, error_lower):
                return category
    return "other"


def extract_verdict(parsed_result: str) -> str:
    """Extract verdict from parsed result."""
    if not parsed_result:
        return "UNKNOWN"
    upper = parsed_result.upper()
    for verdict in ["PROCEED", "RETRY", "SKIP", "FAIL", "WEB_SEARCH", "RUN_DIAGNOSTIC"]:
        if verdict in upper:
            return verdict
    return "OTHER"


def extract_web_searches(events: list[dict]) -> list[dict]:
    """Extract web search queries from events."""
    searches = []
    for e in events:
        event_data = e.get("event_data", {})
        if isinstance(event_data, str):
            try:
                event_data = json.loads(event_data)
            except:
                continue

        # Check for WebSearch tool calls
        if e.get("event_type") == "tool_call":
            tool_call = event_data.get("tool_call", {})
            tool_name = tool_call.get("name", "")
            if tool_name == "WebSearch":
                args = tool_call.get("args", {})
                query = args.get("query", "")
                if query:
                    searches.append({
                        "step_id": e.get("step_id"),
                        "query": query,
                        "timestamp": e.get("timestamp"),
                    })

        # Also check usage stats for web_search_requests
        if e.get("event_type") == "result":
            usage = event_data.get("usage", {})
            server_tool_use = usage.get("server_tool_use", {})
            web_requests = server_tool_use.get("web_search_requests", 0)
            if web_requests > 0:
                searches.append({
                    "step_id": e.get("step_id"),
                    "query": "(implicit web search)",
                    "count": web_requests,
                    "timestamp": e.get("timestamp"),
                })

    return searches


def generate_full_report(store, run_id: str) -> dict:
    """Generate the complete analysis report."""
    run = store.get_run(run_id)
    if not run:
        return {"error": f"Run '{run_id}' not found"}

    steps = store.get_steps(run_id)
    events = store.get_events(run_id)

    # Calculate duration
    duration_minutes = None
    if run.get("finished_at") and run.get("created_at"):
        try:
            finished = datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00"))
            created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            duration_minutes = (finished - created).total_seconds() / 60
        except:
            pass

    # Group steps by step_number to track retries
    step_groups = defaultdict(list)
    for s in steps:
        step_groups[s["step_number"]].append(s)

    # Analyze each step
    step_outcomes = []
    all_failures = []
    total_retries = 0
    retries_by_phase = defaultdict(int)

    for step_num in sorted(step_groups.keys()):
        group = step_groups[step_num]
        attempts = len([s for s in group if s["phase"] == "implement"])
        verify_steps = [s for s in group if s["phase"] == "verify"]

        # Get final verdict from last verify step
        final_verdict = "UNKNOWN"
        if verify_steps:
            final_verdict = extract_verdict(verify_steps[-1].get("parsed_result", ""))

        # Count retries (attempts > 1 means retries occurred)
        retries = max(0, attempts - 1)
        total_retries += retries

        # Get build phase
        build_phase = None
        for s in group:
            if s.get("build_phase"):
                build_phase = s["build_phase"]
                break

        if retries > 0 and build_phase:
            retries_by_phase[build_phase] += retries

        # Collect resolution actions
        resolution_actions = []
        for s in group:
            verdict = extract_verdict(s.get("parsed_result", ""))
            if verdict in ["RETRY", "WEB_SEARCH", "RUN_DIAGNOSTIC", "SKIP"]:
                resolution_actions.append(verdict)

        # Calculate total duration for this step
        step_duration = sum(s.get("duration_seconds", 0) or 0 for s in group)

        step_outcomes.append({
            "step": step_num,
            "build_phase": build_phase,
            "final_verdict": final_verdict,
            "attempts": attempts,
            "retries": retries,
            "resolution_actions": resolution_actions if resolution_actions else None,
            "duration_seconds": round(step_duration, 2),
        })

        # Collect failures
        for s in group:
            errors = s.get("errors_normalized", []) or []
            stderr = s.get("raw_stderr", "") or ""
            parsed = s.get("parsed_result", "") or ""

            # Check for errors in various places
            error_text = ""
            if errors:
                error_text = json.dumps(errors)
            elif stderr:
                error_text = stderr
            elif "error" in parsed.lower() or "fail" in parsed.lower():
                error_text = parsed

            if error_text or s.get("exit_code", 0) != 0:
                category = categorize_error(error_text)
                all_failures.append({
                    "step": step_num,
                    "build_phase": build_phase,
                    "phase": s["phase"],
                    "category": category,
                    "error": error_text[:500] if error_text else f"Exit code: {s.get('exit_code')}",
                    "exit_code": s.get("exit_code"),
                })

    # Count failures by category
    failures_by_category = Counter(f["category"] for f in all_failures)

    # Extract web searches
    web_searches = extract_web_searches(events)

    # Calculate success metrics
    passed = sum(1 for s in step_outcomes if s["final_verdict"] == "PROCEED")
    failed = sum(1 for s in step_outcomes if s["final_verdict"] in ["FAIL", "SKIP"])
    total = len(step_outcomes)

    # Supabase-specific issues
    supabase_issues = {
        "rls_issues": failures_by_category.get("rls_policy", 0),
        "migration_issues": failures_by_category.get("migration_error", 0),
        "edge_function_issues": failures_by_category.get("edge_function_error", 0),
        "realtime_issues": failures_by_category.get("realtime_error", 0),
        "auth_issues": failures_by_category.get("auth_error", 0),
        "schema_cache_issues": failures_by_category.get("schema_mismatch", 0),
    }

    # Infer tool configuration from steps
    planner_tool = None
    implementer_tool = None
    verifier_tool = None
    for s in steps:
        if s["phase"] == "plan" and not planner_tool:
            planner_tool = s["tool"]
        elif s["phase"] == "implement" and not implementer_tool:
            implementer_tool = s["tool"]
        elif s["phase"] == "verify" and not verifier_tool:
            verifier_tool = s["tool"]

    # Extract models from system init events
    models_used = set()
    for e in events:
        event_data = e.get("event_data", {})
        if isinstance(event_data, str):
            try:
                event_data = json.loads(event_data)
            except:
                continue
        if event_data.get("type") == "system" and event_data.get("subtype") == "init":
            model = event_data.get("model")
            if model:
                models_used.add(model)

    report = {
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(),

        "tools_config": {
            "planner": planner_tool,
            "implementer": implementer_tool,
            "verifier": verifier_tool,
            "models_used": list(models_used),
        },

        "summary": {
            "status": run.get("status"),
            "duration_minutes": round(duration_minutes, 1) if duration_minutes else None,
            "prompt": run.get("user_prompt"),
            "total_steps": total,
            "passed_steps": passed,
            "failed_steps": failed,
            "total_retries": total_retries,
            "success_rate": round(passed / total, 2) if total > 0 else 0,
        },

        "step_outcomes": step_outcomes,

        "failures": {
            "by_category": dict(failures_by_category),
            "details": all_failures[:50],  # Limit to first 50
        },

        "web_searches": web_searches,

        "retries": {
            "total": total_retries,
            "by_build_phase": dict(retries_by_phase),
        },

        "supabase_specific": supabase_issues,

        "human_test": {
            "tested_at": None,
            "auth_works": None,
            "core_features_work": None,
            "realtime_works": None,
            "notes": None,
        },

        "raw_data": {
            "steps": steps,
            "events_count": len(events),
        },
    }

    return report


def generate_failures_report(full_report: dict) -> dict:
    """Extract just failures for easy comparison."""
    return {
        "run_id": full_report["run_id"],
        "generated_at": full_report["generated_at"],
        "summary": {
            "total_steps": full_report["summary"]["total_steps"],
            "failed_steps": full_report["summary"]["failed_steps"],
            "total_retries": full_report["summary"]["total_retries"],
            "success_rate": full_report["summary"]["success_rate"],
        },
        "failures_by_category": full_report["failures"]["by_category"],
        "failure_details": full_report["failures"]["details"],
        "supabase_specific": full_report["supabase_specific"],
        "web_searches": full_report["web_searches"],
    }


def generate_analysis_markdown(full_report: dict) -> str:
    """Generate human-readable markdown analysis."""
    r = full_report
    s = r["summary"]
    tc = r.get("tools_config", {})

    lines = [
        f"# Run Analysis: {r['run_id'][:12]}",
        f"",
        f"**Generated:** {r['generated_at'][:19]}",
        f"",
        f"## Configuration",
        f"",
        f"| Role | Tool |",
        f"|------|------|",
        f"| Planner | {tc.get('planner', 'unknown')} |",
        f"| Implementer | {tc.get('implementer', 'unknown')} |",
        f"| Verifier | {tc.get('verifier', 'unknown')} |",
        f"",
        f"**Models:** {', '.join(tc.get('models_used', [])) or 'unknown'}",
        f"",
        f"## Summary",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Status | {s['status']} |",
        f"| Duration | {s['duration_minutes']} min |" if s['duration_minutes'] else "| Duration | N/A |",
        f"| Steps | {s['total_steps']} total ({s['passed_steps']} passed, {s['failed_steps']} failed) |",
        f"| Success Rate | {s['success_rate'] * 100:.0f}% |",
        f"| Total Retries | {s['total_retries']} |",
        f"",
        f"**Prompt:** {s['prompt'][:200]}{'...' if len(s['prompt'] or '') > 200 else ''}",
        f"",
    ]

    # Step outcomes table
    lines.extend([
        f"## Step Outcomes",
        f"",
        f"| Step | Phase | Verdict | Attempts | Duration |",
        f"|------|-------|---------|----------|----------|",
    ])
    for step in r["step_outcomes"]:
        lines.append(
            f"| {step['step']} | {step['build_phase'] or '-'} | {step['final_verdict']} | "
            f"{step['attempts']} | {step['duration_seconds']:.1f}s |"
        )
    lines.append("")

    # Failure categories
    failures = r["failures"]["by_category"]
    if failures:
        lines.extend([
            f"## Failure Categories",
            f"",
            f"| Category | Count |",
            f"|----------|-------|",
        ])
        for cat, count in sorted(failures.items(), key=lambda x: -x[1]):
            lines.append(f"| {cat} | {count} |")
        lines.append("")

    # Supabase-specific
    sb = r["supabase_specific"]
    sb_issues = [(k, v) for k, v in sb.items() if v > 0]
    if sb_issues:
        lines.extend([
            f"## Supabase-Specific Issues",
            f"",
        ])
        for issue, count in sb_issues:
            lines.append(f"- **{issue.replace('_', ' ').title()}**: {count}")
        lines.append("")

    # Web searches
    searches = r["web_searches"]
    if searches:
        lines.extend([
            f"## Web Searches",
            f"",
            f"| Step | Query |",
            f"|------|-------|",
        ])
        for search in searches[:20]:
            query = search.get("query", "")[:60]
            lines.append(f"| {search.get('step_id', '-')} | {query} |")
        lines.append("")

    # Retries by phase
    retries = r["retries"]["by_build_phase"]
    if retries:
        lines.extend([
            f"## Retries by Build Phase",
            f"",
        ])
        for phase, count in sorted(retries.items(), key=lambda x: -x[1]):
            lines.append(f"- **{phase}**: {count} retries")
        lines.append("")

    # Failure details (top 10)
    details = r["failures"]["details"][:10]
    if details:
        lines.extend([
            f"## Top Failure Details",
            f"",
        ])
        for i, f in enumerate(details, 1):
            error_preview = (f.get("error", "") or "")[:150]
            lines.extend([
                f"### {i}. Step {f['step']} ({f['build_phase'] or 'unknown'} / {f['phase']})",
                f"",
                f"- **Category:** {f['category']}",
                f"- **Error:** `{error_preview}`",
                f"",
            ])

    # Human test placeholder
    lines.extend([
        f"## Human Test Results",
        f"",
        f"_Not yet tested. Update this section after manual testing._",
        f"",
        f"- [ ] Auth works (sign up, sign in, sign out)",
        f"- [ ] Core features work (CRUD operations)",
        f"- [ ] Realtime works (multi-browser sync)",
        f"- [ ] Notes: ",
        f"",
    ])

    return "\n".join(lines)


def save_reports(store, run_id: str, output_dir: str = "reports") -> dict:
    """Generate and save all report files."""
    # Create output directory
    reports_dir = Path(output_dir)
    reports_dir.mkdir(exist_ok=True)

    # Generate reports
    full_report = generate_full_report(store, run_id)
    if "error" in full_report:
        print(f"❌ {full_report['error']}")
        return full_report

    failures_report = generate_failures_report(full_report)
    analysis_md = generate_analysis_markdown(full_report)

    # Create a short run_id prefix for filenames
    short_id = run_id[:12]

    # Save files
    full_path = reports_dir / f"{short_id}_full.json"
    failures_path = reports_dir / f"{short_id}_failures.json"
    analysis_path = reports_dir / f"{short_id}_analysis.md"

    with open(full_path, "w") as f:
        json.dump(full_report, f, indent=2, default=str)

    with open(failures_path, "w") as f:
        json.dump(failures_report, f, indent=2, default=str)

    with open(analysis_path, "w") as f:
        f.write(analysis_md)

    print(f"✅ Saved: {full_path} (full data)")
    print(f"✅ Saved: {failures_path} (failures only)")
    print(f"✅ Saved: {analysis_path} (analysis markdown)")

    # Print summary to terminal
    s = full_report["summary"]
    print(f"\n📊 Quick Summary:")
    print(f"   Status: {s['status']}")
    print(f"   Duration: {s['duration_minutes']} min" if s['duration_minutes'] else "   Duration: N/A")
    print(f"   Success: {s['passed_steps']}/{s['total_steps']} steps ({s['success_rate']*100:.0f}%)")
    print(f"   Retries: {s['total_retries']}")

    return {
        "full": str(full_path),
        "failures": str(failures_path),
        "analysis": str(analysis_path),
    }


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Analyze orchestration run logs from Supabase",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("run_id", nargs="?", help="Run ID to analyze")
    parser.add_argument("--errors", action="store_true", help="Show only errors")
    parser.add_argument("--tools", action="store_true", help="Show tool usage")
    parser.add_argument("--timeline", action="store_true", help="Show timeline")
    parser.add_argument("--step", type=int, help="Deep dive on a specific step")
    parser.add_argument("--save-report", action="store_true", help="Save full analysis to reports/")
    parser.add_argument("--export", metavar="PREFIX", help="Export report as JSON (legacy)")
    parser.add_argument("--compare", nargs=2, metavar="RUN_ID", help="Compare two runs")

    args = parser.parse_args()
    store = get_store()

    if args.compare:
        compare_runs(store, args.compare[0], args.compare[1])
    elif not args.run_id:
        parser.print_help()
        print("\n❌ Provide a run_id or use --compare.")
    elif args.errors:
        show_errors(store, args.run_id)
    elif args.tools:
        show_tools(store, args.run_id)
    elif args.timeline:
        show_timeline(store, args.run_id)
    elif args.step is not None:
        deep_dive_step(store, args.run_id, args.step)
    elif args.save_report:
        save_reports(store, args.run_id)
    elif args.export:
        export_report(store, args.run_id, args.export)
    else:
        analyze_run(store, args.run_id)


if __name__ == "__main__":
    main()
