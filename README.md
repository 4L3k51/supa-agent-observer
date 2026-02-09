# Claude × Cursor Orchestrator

Observation framework that coordinates **Claude Code** (planner/verifier) and **Cursor Agent** (implementer) to build apps while logging every token, tool call, and web search to **Supabase**.

## Purpose

This is a measurement tool. It answers the question: **given clear, well-specified instructions, where do AI coding agents still get things wrong?**

By running the same build prompt multiple times and logging every step, tool call, verification verdict, and smoke test result, you get a structured dataset that shows:

- **Knowledge gaps** — which tasks consistently fail? If RLS policy steps fail 8 out of 10 runs with similar errors, that's a gap in training data or documentation, not randomness
- **Recovery effectiveness** — when the verifier triggers a web search, do the findings actually help the retry succeed? If searches for "supabase realtime" never lead to passing retries, the available docs aren't good enough
- **Tool comparison** — Claude Code and Cursor implement the same steps with the same instructions. You can compare which tool uses more Bash calls, reads more files before editing, or recovers better from failures
- **Ground truth** — the smoke test phase runs the finished app. Either the server starts or it doesn't, either the tests pass or they don't. This separates "code that looks right" from "code that works"

The actionable output: identify where documentation, examples, or training data need improvement so these tools build correctly. The human prompt is held constant so you're measuring agent capability, not user skill.

## How It Works

```
You: "Build a Supabase todo app with auth"
                    │
                    ▼
        ┌───────────────────────┐
        │  Python Orchestrator  │
        └───────┬───────┬───────┘
                │       │
      ┌─────────▼─┐   ┌─▼──────────┐
      │Claude Code │   │  Cursor    │
      │  Plans &   │──▶│  Agent     │
      │  Verifies  │◀──│ Implements │
      └─────────┬──┘   └──┬────────┘
                │          │
        ┌───────▼──────────▼───────┐
        │   Supabase (JSONB logs)  │
        │   Every event captured   │
        └──────────────────────────┘
```

**One prompt in → fully built project + complete observation dataset out.**

For each step, the orchestrator runs:

1. **Plan** — Claude Code generates a step-by-step implementation plan. Each step is tagged with a `build_phase` (`setup`, `schema`, `backend`, `frontend`, `testing`, `deployment`)
2. **Implement** — Cursor Agent builds each step (`--force` auto-applies changes)
3. **Verify** — Claude Code reads the actual project files and verifies Cursor's work
4. **Resolve** — Based on the verification verdict, the orchestrator picks a resolution strategy:

| Verdict | Strategy | What happens |
|---------|----------|--------------|
| `PROCEED` | Continue | Move to the next step |
| `RETRY` | Retry | Append issues to the prompt, re-run Cursor |
| `WEB_SEARCH` | Research | Claude Code searches the web for docs/examples, appends findings, then retries |
| `RUN_DIAGNOSTIC` | Diagnose | Run an allowlisted command (`npx tsc --noEmit`, `npm test`, etc.), append output, then retry |
| `SKIP` | Skip | Mark the step as skipped with a reason and move on |
| `MODIFY_PLAN` | Continue | Log the issue and continue with best effort |

5. **Log** — Every token, tool call, file write, bash command, web search, and error → Supabase

The verifier decides which strategy to use by returning a `RECOMMENDATION` and an optional `RESOLUTION` JSON payload (e.g. `{"query": "supabase RLS policy syntax"}` for web search, or `{"command": "npx tsc --noEmit"}` for diagnostics). The orchestrator executes it mechanically.

Two separate counters control the loop per step:
- **`retry_count`** — capped by `--max-retries` (default 2). Only incremented on actual Cursor retries
- **`resolution_count`** — capped by `MAX_RESOLUTIONS_PER_STEP` (default 5). Incremented on every resolution action (retry, search, diagnostic). This prevents infinite loops where the verifier alternates between search and retry

## What Gets Logged

Everything is stored as JSONB in Supabase and queryable with SQL:

- **Runs & steps**: run metadata + step records (phase, tool, build_phase, duration, timestamps)
- **Commands executed**: shell commands run per step (also included in EXIT_ERROR for debugging)
- **Process output**: stdout/stderr + exit code per step
- **Tool calls/events**: tool events (Read, Write, Edit, Bash, WebSearch, WebFetch) stored in `orchestrator_events`
- **Verification verdicts**: PASS/FAIL/PARTIAL in parsed_result with reasoning
- **Normalized errors**:
  - `PARSED_ERROR`: explicit errors from AI output
  - `EXIT_ERROR`: non-zero exit code + stderr tail + commands_run
- **Timing**: duration per step

## Quick Start

### 1. Install CLIs

```bash
# Claude Code
npm install -g @anthropic-ai/claude-code

# Cursor Agent
curl https://cursor.com/install -fsSL | bash
agent login
```

### 2. Install Python dependencies

```bash
pip install python-dotenv supabase
```

### 3. Set up Supabase

- Create a project at [supabase.com](https://supabase.com)
- Run `migration.sql` in the SQL Editor
- Copy your project URL and **service_role** key

### 4. Configure environment

```bash
cp .env.example .env
# Edit .env with your Supabase credentials
```

### 5. Preflight check

```bash
python preflight.py
```

### 6. Run

```bash
# Simple test
python orchestrator.py "Create a simple Node.js hello world project"

# Real build with runtime testing
python orchestrator.py \
  --supabase-url "https://yourproject.supabase.co" \
  --supabase-anon-key "your-anon-key" \
  --supabase-service-key "your-service-key" \
  --supabase-db-url "postgresql://..." \
  --supabase-project-ref "yourproject" \
  "Build a Supabase todo app with email auth, RLS policies per user, and a React frontend"
```

## CLI Arguments

### Tool Selection

The orchestrator supports **any combination** of Claude Code and Cursor Agent for all three roles: planning, implementation, and verification.

| Argument | Default | Description |
|----------|---------|-------------|
| `--planner` | `claude` | Tool for planning (`claude` or `cursor`) |
| `--implementer` | `cursor` | Tool for implementation (`claude` or `cursor`) |
| `--verifier` | `claude` | Tool for verification, testing, and analysis (`claude` or `cursor`) |

**Example configurations:**

| Configuration | Command | Use Case |
|---------------|---------|----------|
| Claude plans, Cursor implements, Claude verifies | (default) | Compare both agents on their strengths |
| All Claude | `--planner claude --implementer claude --verifier claude` | Baseline without Cursor |
| All Cursor | `--planner cursor --implementer cursor --verifier cursor` | Baseline without Claude |
| Cursor only with GPT-5.2 | `--planner cursor --implementer cursor --verifier cursor --cursor-model gpt-5.2` | Test GPT-5.2 end-to-end |

```bash
# Default: Claude plans, Cursor implements, Claude verifies
python orchestrator.py "Build X"

# All Claude (no Cursor)
python orchestrator.py --planner claude --implementer claude --verifier claude "Build X"

# All Cursor (no Claude Code at all)
python orchestrator.py --planner cursor --implementer cursor --verifier cursor "Build X"

# Cursor with GPT-5.2 for everything
python orchestrator.py --planner cursor --implementer cursor --verifier cursor --cursor-model gpt-5.2 "Build X"

# Claude plans, Cursor implements with GPT-5.2, Claude verifies
python orchestrator.py --implementer cursor --cursor-model gpt-5.2 "Build X"
```

This flexibility lets you run the same prompt with different configurations to compare agent performance.

### Model Selection

| Argument | Description |
|----------|-------------|
| `--claude-model` | Model for Claude Code (planner/verifier) |
| `--cursor-model` | Model for Cursor Agent (implementer) |

**Claude Code models:**
- Aliases: `sonnet`, `opus`, `haiku`
- Full names: `claude-sonnet-4-5-20250929`, `claude-opus-4-6`, etc.

**Cursor Agent models** (run `agent --help` for your available models):

| Slug | Model |
|------|-------|
| `sonnet-4.5` | Claude 4.5 Sonnet |
| `opus-4.6` | Claude 4.6 Opus |
| `opus-4.6-thinking` | Claude 4.6 Opus (Thinking) |
| `sonnet-4.5-thinking` | Claude 4.5 Sonnet (Thinking) |
| `gpt-5.2` | GPT-5.2 |
| `gemini-3-pro` | Gemini 3 Pro |
| `auto` | Auto-select |

```bash
python orchestrator.py \
  --claude-model sonnet \
  --cursor-model sonnet-4.5 \
  "Build X"
```

### Supabase Target (Runtime Testing)

These enable migration execution, RLS testing, and Edge Function deployment against a live Supabase project:

| Argument | Description |
|----------|-------------|
| `--supabase-url` | Project REST API URL |
| `--supabase-anon-key` | Anonymous key |
| `--supabase-service-key` | Service role key (for admin operations) |
| `--supabase-db-url` | Postgres connection string (for migrations) |
| `--supabase-project-ref` | Project ref (enables `supabase` CLI linking) |

### Other Options

| Argument | Default | Description |
|----------|---------|-------------|
| `--max-retries` | `2` | Max retries per step before giving up |
| `--skip-smoke-test` | `false` | Skip the final smoke test phase |
| `--resume` | - | Resume a previous run by run_id |
| `--start-step` | `1` | Start from a specific step (with `--resume`) |
| `--list-runs` | - | List all previous runs |

## Querying Logs

```sql
-- Run overview
SELECT * FROM orchestrator_run_summary;

-- All errors
SELECT * FROM orchestrator_errors;

-- Tool usage breakdown
SELECT * FROM orchestrator_tool_usage;

-- Verification verdicts
SELECT id, step_number, phase, tool, parsed_result, duration_seconds
FROM orchestrator_steps
WHERE tool = 'claude_code' AND phase = 'verify';

-- Steps by build phase
SELECT step_number, phase, tool, build_phase, duration_seconds
FROM orchestrator_steps
WHERE build_phase IS NOT NULL
ORDER BY step_number;

-- Research actions (web searches triggered by verification)
SELECT step_number, parsed_result, duration_seconds
FROM orchestrator_steps
WHERE phase = 'research';

-- Diagnostic runs
SELECT step_number, prompt_sent AS command, parsed_result, exit_code
FROM orchestrator_steps
WHERE phase = 'diagnostic';

-- Web searches
SELECT id, step_id, event_data->'message'->'content' as content
FROM orchestrator_events
WHERE event_data->>'type' = 'assistant'
AND (event_data->'message'->'content')::text LIKE '%WebSearch%';

-- Bash commands from Cursor Agent
SELECT id, step_id,
  event_data->'tool_call'->'shellToolCall'->'args'->>'command' as command
FROM orchestrator_events
WHERE event_data->>'type' = 'tool_call'
AND (event_data->'tool_call')::text LIKE '%shellToolCall%';
```

## Files

| File | Purpose |
|------|---------|
| `orchestrator.py` | Main loop — plans, implements, verifies, resolves (retry/search/diagnose/skip) |
| `storage.py` | Supabase storage layer |
| `analyzer.py` | Post-run analysis — errors, tool usage, timeline |
| `preflight.py` | Pre-run check — verifies CLIs and connections |
| `migration.sql` | Supabase schema — tables, indexes, views |

## Known Issues

- **Cursor Agent hanging**: The `-p` mode sometimes doesn't release the terminal. The orchestrator kills it after 2 minutes of idle time.
- **Verification is file-based**: Claude Code verifies by reading files, not by running the app. Runtime errors (like React version conflicts) aren't caught.
- **Context limits**: For complex multi-step builds, later steps may lack full context. The orchestrator passes a summary of completed steps.

## License

MIT
