# Claude × Cursor Orchestrator

Observation framework that coordinates **Claude Code** and **Cursor Agents** to build Supabase apps while logging everything.

## Purpose

This is a measurement tool. It answers the question:  **when AI agents are given a task to build an app with Supabase and zero hand-holding, where do they still get things wrong?**

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
  - resolution_count — max 5 resolution actions (retry, search, diagnostic) per step                                         
  - Replan — separate from resolution budget, runs after step passes 

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
