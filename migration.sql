-- ============================================================
-- Orchestrator Log Tables - Supabase Migration
-- ============================================================
-- Run this in your Supabase SQL Editor (Dashboard > SQL Editor)
-- This creates the tables needed to store orchestration logs.
-- ============================================================

-- Runs: one row per orchestration session
CREATE TABLE IF NOT EXISTS orchestrator_runs (
    run_id TEXT PRIMARY KEY,
    user_prompt TEXT NOT NULL,
    project_dir TEXT NOT NULL,
    status TEXT DEFAULT 'running',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

-- Steps: each plan/implement/verify phase
CREATE TABLE IF NOT EXISTS orchestrator_steps (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES orchestrator_runs(run_id),
    step_number INTEGER NOT NULL,
    phase TEXT NOT NULL,             -- 'plan', 'implement', 'verify', 'replan_checkpoint', 'migration_exec', 'rls_test', 'edge_function_deploy', 'research', 'diagnostic', 'smoke_test', 'approach_analysis'
    tool TEXT NOT NULL,              -- 'claude_code' or 'cursor'
    prompt_sent TEXT,
    raw_stdout TEXT,                 -- full stdout capture
    raw_stderr TEXT,                 -- full stderr capture
    parsed_result TEXT,              -- extracted text result
    exit_code INTEGER,
    duration_seconds DOUBLE PRECISION,
    build_phase TEXT,               -- 'setup', 'schema', 'backend', 'frontend', 'testing', 'deployment'
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Events: every stream-json event (tool calls, file writes, errors, text)
CREATE TABLE IF NOT EXISTS orchestrator_events (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES orchestrator_runs(run_id),
    step_id BIGINT REFERENCES orchestrator_steps(id),
    event_type TEXT,                 -- 'tool_call', 'assistant', 'result', 'system', etc.
    event_data JSONB,               -- raw event as JSON (using JSONB for query power)
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- Indexes for fast queries
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_steps_run_id ON orchestrator_steps(run_id);
CREATE INDEX IF NOT EXISTS idx_steps_phase ON orchestrator_steps(phase);
CREATE INDEX IF NOT EXISTS idx_events_run_id ON orchestrator_events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_step_id ON orchestrator_events(step_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON orchestrator_events(event_type);

-- JSONB index on event_data for querying specific event fields
CREATE INDEX IF NOT EXISTS idx_events_data ON orchestrator_events USING GIN (event_data);

-- ============================================================
-- Row Level Security Configuration
-- ============================================================
-- RLS is enabled with permissive policies that allow all access.
-- This is appropriate for internal logs accessed via service_role key.
--
-- IMPORTANT: Use the service_role key (not anon key) in SUPABASE_KEY
-- to bypass RLS and have full access to these tables.
--
-- For multi-user access with per-user isolation, replace these
-- policies with user-specific ones (e.g., using auth.uid()).
-- ============================================================

ALTER TABLE orchestrator_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE orchestrator_steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE orchestrator_events ENABLE ROW LEVEL SECURITY;

-- Permissive policies for service role access
-- These allow all operations when using service_role key
-- Drop existing policies first to make migration idempotent
DROP POLICY IF EXISTS "Service role full access on runs" ON orchestrator_runs;
DROP POLICY IF EXISTS "Service role full access on steps" ON orchestrator_steps;
DROP POLICY IF EXISTS "Service role full access on events" ON orchestrator_events;

CREATE POLICY "Service role full access on runs"
    ON orchestrator_runs FOR ALL
    USING (true) WITH CHECK (true);

CREATE POLICY "Service role full access on steps"
    ON orchestrator_steps FOR ALL
    USING (true) WITH CHECK (true);

CREATE POLICY "Service role full access on events"
    ON orchestrator_events FOR ALL
    USING (true) WITH CHECK (true);


-- Migration: add build_phase column if not exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'orchestrator_steps' AND column_name = 'build_phase'
    ) THEN
        ALTER TABLE orchestrator_steps ADD COLUMN build_phase TEXT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_steps_build_phase ON orchestrator_steps(build_phase);

-- Migration: add commands_executed column if not exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'orchestrator_steps' AND column_name = 'commands_executed'
    ) THEN
        ALTER TABLE orchestrator_steps ADD COLUMN commands_executed JSONB;
    END IF;
END $$;

-- Index for querying commands
CREATE INDEX IF NOT EXISTS idx_steps_commands ON orchestrator_steps USING GIN (commands_executed);

-- Migration: add errors_normalized column if not exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'orchestrator_steps' AND column_name = 'errors_normalized'
    ) THEN
        ALTER TABLE orchestrator_steps ADD COLUMN errors_normalized JSONB;
    END IF;
END $$;

-- Index for querying errors
CREATE INDEX IF NOT EXISTS idx_steps_errors ON orchestrator_steps USING GIN (errors_normalized);

-- Migration: add skills_info column if not exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'orchestrator_steps' AND column_name = 'skills_info'
    ) THEN
        ALTER TABLE orchestrator_steps ADD COLUMN skills_info JSONB;
    END IF;
END $$;

-- Index for querying skills usage
CREATE INDEX IF NOT EXISTS idx_steps_skills ON orchestrator_steps USING GIN (skills_info);

-- Migration: add token usage columns if not exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'orchestrator_steps' AND column_name = 'input_tokens'
    ) THEN
        ALTER TABLE orchestrator_steps ADD COLUMN input_tokens INTEGER;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'orchestrator_steps' AND column_name = 'output_tokens'
    ) THEN
        ALTER TABLE orchestrator_steps ADD COLUMN output_tokens INTEGER;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'orchestrator_steps' AND column_name = 'cache_read_tokens'
    ) THEN
        ALTER TABLE orchestrator_steps ADD COLUMN cache_read_tokens INTEGER;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'orchestrator_steps' AND column_name = 'cache_creation_tokens'
    ) THEN
        ALTER TABLE orchestrator_steps ADD COLUMN cache_creation_tokens INTEGER;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'orchestrator_steps' AND column_name = 'cost_usd'
    ) THEN
        ALTER TABLE orchestrator_steps ADD COLUMN cost_usd DOUBLE PRECISION;
    END IF;
END $$;


-- ============================================================
-- Useful views for analysis
-- ============================================================

-- Summary view: one row per run with aggregated stats
CREATE OR REPLACE VIEW orchestrator_run_summary AS
SELECT
    r.run_id,
    r.user_prompt,
    r.status,
    r.created_at,
    r.finished_at,
    EXTRACT(EPOCH FROM (r.finished_at - r.created_at)) AS total_seconds,
    COUNT(DISTINCT s.id) AS step_count,
    COUNT(DISTINCT e.id) AS event_count,
    COUNT(DISTINCT s.id) FILTER (WHERE s.exit_code != 0) AS error_count,
    COUNT(DISTINCT s.id) FILTER (WHERE s.phase = 'implement') AS impl_attempts,
    COUNT(DISTINCT s.id) FILTER (WHERE s.phase = 'verify') AS verify_count,
    COUNT(DISTINCT s.id) FILTER (WHERE s.phase = 'replan_checkpoint') AS replan_count,
    SUM(s.duration_seconds) AS total_tool_seconds,
    -- Token usage aggregates
    SUM(s.input_tokens) AS total_input_tokens,
    SUM(s.output_tokens) AS total_output_tokens,
    SUM(s.cache_read_tokens) AS total_cache_read_tokens,
    SUM(s.cache_creation_tokens) AS total_cache_creation_tokens,
    SUM(s.cost_usd) AS total_cost_usd
FROM orchestrator_runs r
LEFT JOIN orchestrator_steps s ON s.run_id = r.run_id
LEFT JOIN orchestrator_events e ON e.run_id = r.run_id
GROUP BY r.run_id;

-- Error view: all errors across all runs
CREATE OR REPLACE VIEW orchestrator_errors AS
SELECT
    s.run_id,
    s.step_number,
    s.phase,
    s.tool,
    s.exit_code,
    s.raw_stderr,
    s.parsed_result,
    s.duration_seconds,
    s.timestamp
FROM orchestrator_steps s
WHERE s.exit_code != 0
   OR (s.raw_stderr IS NOT NULL AND s.raw_stderr != '')
   OR s.parsed_result LIKE '%FAIL%'
ORDER BY s.timestamp DESC;

-- Tool usage view: what tools were called and how often
CREATE OR REPLACE VIEW orchestrator_tool_usage AS
SELECT
    e.run_id,
    e.event_type,
    e.event_data->>'subtype' AS subtype,
    COALESCE(
        e.event_data->'tool_call'->>'name',
        CASE
            WHEN e.event_data->'tool_call' ? 'writeToolCall' THEN 'write_file'
            WHEN e.event_data->'tool_call' ? 'readToolCall' THEN 'read_file'
            WHEN e.event_data->'tool_call' ? 'terminalToolCall' THEN 'terminal'
            ELSE 'unknown'
        END
    ) AS tool_name,
    COUNT(*) AS call_count
FROM orchestrator_events e
WHERE e.event_type = 'tool_call'
GROUP BY e.run_id, e.event_type, subtype, tool_name
ORDER BY call_count DESC;

-- Commands view: what shell commands were executed per step
CREATE OR REPLACE VIEW orchestrator_commands AS
SELECT
    s.run_id,
    s.step_number,
    s.phase,
    s.build_phase,
    s.tool,
    cmd->>'command' AS command,
    cmd->>'tool' AS command_tool,
    s.exit_code,
    s.parsed_result LIKE '%SUCCESS%' AS succeeded,
    s.timestamp
FROM orchestrator_steps s,
     jsonb_array_elements(COALESCE(s.commands_executed, '[]'::jsonb)) AS cmd
ORDER BY s.timestamp, s.step_number;
