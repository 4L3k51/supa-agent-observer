# Supa Agent Orchestrator

Multi-agent observation framework that orchestrates Claude Code and Cursor to build Supabase apps, logging every decision and failure to analyze where AI coding agents break down.

## Purpose

This is a measurement tool. It answers the question:  **when AI agents are given a task to build an app with Supabase and minimal hand-holding, where do they still get things wrong?**

By logging every step, tool call, verification verdict, and smoke test result, we get a structured dataset that shows:

- **Knowledge gaps** — which tasks consistently fail? If RLS policy steps fail 8 out of 10 runs with similar errors, that's a gap in training data or documentation, not randomness
- **Recovery effectiveness** — when the verifier triggers a web search, do the findings actually help the retry succeed? If searches for "supabase realtime" never lead to passing retries, the available docs aren't good enough
- **Tool & model comparison** — Different agents and models implement the same prompt, letting us compare which ones use more Bash calls, read more files before editing, or recover better from failures.

The goal output: identify where documentation, examples, or training data need improvement so these tools build correctly.

## The orchestration balancing act

If the orchestration is too thin — no verification, no RLS testing, no replanning — the agents produce code that looks right but doesn't work, and you can't tell where it broke. If the orchestration is too thick — the system prompt includes the exact SQL, the exact config entries, the exact implementation patterns — the agents just follow instructions and you learn nothing about their actual knowledge.

The goal is enough structure that failures are meaningful, not so much that we are hiding them. The orchestration catches failures and gives agents a chance to recover, but it doesn't prevent failures from happening in the first place. Logging captures every step of this — what failed, how the agent tried to recover, and whether it succeeded. That's the dataset.

## How It Works

```
You: "Build a Supabase todo app with auth"                                    
                      │                                                         
                      ▼                                                         
          ┌───────────────────────┐                                             
          │  Python Orchestrator  │                                             
          └───────────┬───────────┘                                             
                      │                                                         
      ┌───────────────┼───────────────┐                                         
      │               │               │                                         
      ▼               ▼               ▼                                         
  ┌────────┐    ┌──────────┐    ┌──────────┐                                    
  │ Agent  │    │  Agent   │    │  Agent   │                                    
  │ Plans  │───▶│Implements│───▶│ Verifies │                                    
  └────────┘    └──────────┘    └────┬─────┘                                    
      ▲                              │                                          
      │         ┌────────────────────┤                                          
      │         ▼                    ▼                                          
      │    [caveats?]            [retry]                                        
      │         │                    │                                          
      │         ▼                    │                                          
      │   ┌──────────┐               │                                          
      └───│ Replans  │◀──────────────┘                                          
          │if needed │                                                          
          └──────────┘                                                          
                  │                                                             
                  ▼                                                             
      ┌───────────────────────┐                                                 
      │  Supabase JSONB logs  │                                                 
      └───────────────────────┘          
```

**One prompt in → fully built project + complete observation dataset out.**

 For each step, the orchestrator runs:                                                                                      
                                                                                                                             
  1. Plan — Agent generates a step-by-step implementation plan tagged with build_phase (setup, schema, backend, frontend,    
  testing, deployment)                                                                                                       
  2. Implement — Agent builds the step                                                                                       
  3. Verify — Agent checks the work and returns a verdict                                                                    
  4. Resolve — Based on the verdict:
```                                                                                         
  ┌────────────────┬─────────────────────────────────────────────────────────┐                                               
  │    Verdict     │                      What happens                       │                                               
  ├────────────────┼─────────────────────────────────────────────────────────┤                                               
  │ PROCEED        │ Run replan checkpoint, then next step                   │                                               
  ├────────────────┼─────────────────────────────────────────────────────────┤                                               
  │ RETRY          │ Append issues, re-run implementation                    │                                               
  ├────────────────┼─────────────────────────────────────────────────────────┤                                               
  │ WEB_SEARCH     │ Search docs, append findings, retry                     │                                               
  ├────────────────┼─────────────────────────────────────────────────────────┤                                               
  │ RUN_DIAGNOSTIC │ Run a command (npx tsc, npm test), append output, retry │                                               
  ├────────────────┼─────────────────────────────────────────────────────────┤                                               
  │ SKIP           │ Skip step with reason                                   │                                               
  ├────────────────┼─────────────────────────────────────────────────────────┤                                               
  │ MODIFY_PLAN    │ Trigger replan checkpoint                               │                                               
  └────────────────┴─────────────────────────────────────────────────────────┘                                               
```
6. Replan Checkpoint — After step completion, evaluate if remaining steps need adjustment. If implementation diverged,     
  regenerate remaining steps. Completed steps stay locked.                                                                   
  7. Log — Everything goes to Supabase

  Loop controls:
  - resolution_count — max 7 resolution actions (retry, search, diagnostic) per step
  - Replan — separate from resolution budget, runs after step passes

## Test Frameworks

After implementation, the orchestrator runs multiple verification layers to catch different failure modes:

| Test Layer | What It Tests | Method |
|------------|---------------|--------|
| **Smoke Test** | Build succeeds, app starts, auth works, storage works | Claude agent runs npm build, starts dev server, tests auth via curl |
| **RLS Tests** | Row Level Security policies enforce correctly | Creates test user, gets JWT, makes authenticated curl requests to verify policies |
| **API Verification** | REST endpoints respond with expected data | Direct curl requests to Supabase REST API |
| **Edge Function Tests** | Edge functions deploy and execute | Deploys via Supabase CLI, tests with curl |
| **Playwright Browser Tests** | E2E user flows work in real browser | Two-user browser contexts verify auth, CRUD, and cross-user realtime sync |

**Test execution flow:**
```
Implementation Complete
        │
        ▼
   Smoke Test ──────▶ Fix Loop (max 2 retries)
        │
        ▼
   RLS Tests ───────▶ Per-step, only on schema changes
        │
        ▼
  Browser Tests ────▶ Fix Loop (max 2 retries)
        │
        ▼
    Results logged to Supabase
```

**Playwright browser tests** verify:
1. Auth redirect — unauthenticated users see login
2. Login flow — user can sign in
3. Create resource — authenticated CRUD works
4. Realtime sync — User B sees User A's changes without refresh (requires two browser contexts)

All test results are logged with pass/fail status, error messages, and duration for analysis.

## Usage

```
python orchestrator.py [prompt] [options]
```

### Positional

| Argument | Description |
|----------|-------------|
| `prompt` | What to build (the project goal) |

### Project

| Argument | Description |
|----------|-------------|
| `--project-dir DIR` | Directory to create project in (default: auto-generated) |

### Resume

| Argument | Description |
|----------|-------------|
| `--resume RUN_ID` | Resume a previous run |
| `--start-step N` | Start from step N (with --resume) |

### Execution

| Argument | Description |
|----------|-------------|
| `--max-retries N` | Max retries per step (default: 2) |
| `--skip-smoke-test` | Skip the smoke test phase |
| `--encourage-web-search` | Encourage agents to use WebSearch proactively |

### Agent Tools

| Argument | Description |
|----------|-------------|
| `--planner {claude,cursor}` | Tool for planning (default: claude) |
| `--implementer {claude,cursor}` | Tool for implementation (default: cursor) |
| `--verifier {claude,cursor}` | Tool for verification (default: claude) |

### Models

| Argument | Description |
|----------|-------------|
| `--claude-model MODEL` | Model for Claude Code |
| `--cursor-model MODEL` | Model for Cursor Agent |

### Skills Injection

Inject phase-specific guidance into implementation steps.

| Argument | Description |
|----------|-------------|
| `--skills-mode {none,passive,on-demand}` | Injection mode (default: none) |
| `--skills-source PATH` | Path to skills directory (default: ./skills) |
| `--skills-filter {all,phase-matched}` | File selection strategy (default: phase-matched) |

**Modes:**
- `none` — No skills injection
- `passive` — Append skill content to system prompt
- `on-demand` — Copy skills directory to project, add prompt hint

**Filters:**
- `phase-matched` — Load `{build_phase}.md` matching the current step's phase
- `all` — Always load `all.md` regardless of phase

**Build phases:** setup, schema, backend, frontend, testing, deployment, fix

### Supabase (for runtime testing)

| Argument | Description |
|----------|-------------|
| `--supabase-url URL` | REST API URL |
| `--supabase-anon-key KEY` | Anon key |
| `--supabase-service-key KEY` | Service role key (for auth/admin) |
| `--supabase-db-url URL` | Postgres connection string (for migrations) |
| `--supabase-project-ref REF` | Project ref (for Edge Function deployment) |

### Other

| Argument | Description |
|----------|-------------|
| `--list-runs` | List all previous runs |

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


## License

MIT
