# AI Coding Orchestrator

Coordinates **Claude Code** (planner/verifier) and **Cursor Agent** (implementer) to autonomously build projects while capturing every interaction in Supabase for analysis.

## Architecture

```
You (single prompt)
  │
  ▼
┌─────────────────────────────────────────────┐
│           Python Orchestrator               │
│  (subprocess management + raw log capture)  │
└──────────┬──────────────────┬───────────────┘
           │                  │
     ┌─────▼─────┐     ┌─────▼─────┐
     │ Claude Code│     │  Cursor   │
     │    CLI     │     │ Agent CLI │
     │ (plan +    │     │(implement)│
     │  verify)   │     │           │
     └─────┬─────┘     └─────┬─────┘
           │                  │
     ┌─────▼──────────────────▼─────┐
     │   Shared project directory    │
     │   + Supabase log storage      │
     └──────────────────────────────┘
```

**Flow for each step:**
1. **Claude Code** generates the plan (broken into steps)
2. **Cursor Agent** implements each step (`--force` mode, auto-applies changes)
3. **Claude Code** verifies the implementation (inspects actual files, runs checks)
4. If verification fails → retry with enriched prompt (up to N retries)
5. Everything is logged: every token, tool call, file write, error, timing

## Prerequisites

```bash
# Claude Code CLI
npm install -g @anthropic-ai/claude-code

# Cursor Agent CLI
curl https://cursor.com/install -fsSL | bash

# Authenticate both
claude          # Follow browser auth flow
agent           # Follow auth flow

# Python dependencies
pip install -r requirements.txt
```

## Quick Start

### 1. Set up Supabase

1. Create a Supabase project at [supabase.com](https://supabase.com)
2. Go to SQL Editor and run the contents of `migration.sql`
3. Get your project URL and **service role key** (not anon key) from Settings > API
4. Create a `.env` file:

```bash
cp .env.example .env
# Edit .env with your credentials
```

Or export environment variables:
```bash
export SUPABASE_URL="https://your-project.supabase.co"
export SUPABASE_KEY="your-service-role-key"
```

### 2. Verify setup

```bash
python preflight.py
```

### 3. Run it

```bash
python orchestrator.py "Build a Supabase todo app with user auth, RLS policies, and a Next.js frontend"
```

### 4. Analyze what happened

```bash
python analyzer.py <run_id>
```

## Usage

### Running the orchestrator

```bash
# Basic usage
python orchestrator.py "Build a Supabase app with auth and a todo list"

# Custom project directory
python orchestrator.py --project-dir ~/my-supabase-app "Build a Supabase app"

# Specify models
python orchestrator.py --claude-model claude-sonnet-4 --cursor-model sonnet-4 "Build a Supabase app"

# More retries per step
python orchestrator.py --max-retries 3 "Build a Supabase app"

# Resume a failed/interrupted run from step 3
python orchestrator.py --resume abc123 --start-step 3 "Build a Supabase app"

# List all runs
python orchestrator.py --list-runs
```

### Analyzing logs

```bash
# Full analysis
python analyzer.py <run_id>

# Just the errors
python analyzer.py <run_id> --errors

# Tool usage breakdown (files written, commands run)
python analyzer.py <run_id> --tools

# Timeline view
python analyzer.py <run_id> --timeline

# Deep dive into a specific step
python analyzer.py <run_id> --step 3

# Export full report as JSON
python analyzer.py <run_id> --export report

# Compare two runs
python analyzer.py --compare <run_id_1> <run_id_2>
```

### Querying logs in Supabase

The migration includes ready-made views you can query in the Supabase dashboard:

```sql
-- Summary of all runs
SELECT * FROM orchestrator_run_summary;

-- All errors across runs
SELECT * FROM orchestrator_errors;

-- Tool usage breakdown
SELECT * FROM orchestrator_tool_usage;

-- Query specific events using JSONB
SELECT * FROM orchestrator_events
WHERE event_data->>'type' = 'tool_call'
  AND event_data->'tool_call'->>'name' = 'Bash';
```

## What Gets Logged

Everything. The Supabase database captures:

| Table | What |
|-------|------|
| `orchestrator_runs` | Each orchestration run (prompt, status, timing) |
| `orchestrator_steps` | Every phase (plan/implement/verify) with full stdout/stderr |
| `orchestrator_events` | Every stream-json event (tool calls, file writes, errors, text) |

This means you get:
- Every file Claude Code and Cursor read or wrote
- Every terminal command they ran
- Every error message
- Model names and API timing
- Retry attempts and why they happened
- Verification results (PASS/FAIL/PARTIAL)

## Configuration

Edit the constants at the top of `orchestrator.py`:

```python
CLAUDE_CODE_TIMEOUT = 600   # 10 min for planning/verification
CURSOR_TIMEOUT = 900        # 15 min for implementation
CURSOR_IDLE_TIMEOUT = 120   # Kill cursor if no output for 2 min (hanging bug)
```

## Known Issues

### Cursor Agent hanging

The Cursor CLI has a [known bug](https://forum.cursor.com/t/cursor-agent-hanging-for-n-seconds-when-done/130401) where it hangs after completing in `-p` mode. The orchestrator handles this with an idle timeout — if Cursor produces no output for 2 minutes, it kills the process and checks if the work was done anyway.

### Large projects

Both CLIs have context limits. For very large projects, steps that require understanding the full codebase might fail. The retry mechanism helps, but some manual intervention may be needed.

## Project Structure

```
orchestrator/
├── orchestrator.py    # Main orchestration loop
├── storage.py         # Supabase storage backend
├── analyzer.py        # Log analysis tool
├── preflight.py       # Pre-run checks
├── migration.sql      # Supabase table setup
├── requirements.txt   # Python dependencies
├── .env.example       # Environment variable template
└── README.md
```

## Tip: Supabase-Specific Experiments

Good prompts to test:

```bash
# Auth + CRUD
"Build a Supabase todo app with email auth, RLS policies per user, and a React frontend"

# Real-time
"Build a Supabase chat app with real-time subscriptions and presence"

# Edge functions
"Build a Supabase app with an edge function that processes webhooks and stores data"

# Complex schema
"Build a Supabase project management app with teams, projects, tasks, and role-based access"
```
