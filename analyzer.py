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
    python analyzer.py <run_id> --export report  # Export full report as JSON
    python analyzer.py --compare id1 id2         # Compare two runs
"""

import argparse
import json
import os
import sys
import textwrap
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

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
    parser.add_argument("--export", metavar="PREFIX", help="Export report as JSON")
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
    elif args.export:
        export_report(store, args.run_id, args.export)
    else:
        analyze_run(store, args.run_id)


if __name__ == "__main__":
    main()
