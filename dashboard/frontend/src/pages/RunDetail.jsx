import React, { useState, useEffect, useMemo } from 'react';
import { useParams, Link } from 'react-router-dom';
import { getRun } from '../api';
import StatusBadge from '../components/StatusBadge';
import SummaryCard from '../components/SummaryCard';
import StepCard from '../components/StepCard';
import StepDetailPanel from '../components/StepDetailPanel';

const RunDetail = () => {
  const { runId } = useParams();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedStep, setSelectedStep] = useState(null);

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      setError(null);
      try {
        const result = await getRun(runId);
        setData(result);
      } catch (err) {
        setError('Failed to load run details. Is the API server running?');
        console.error(err);
      } finally {
        setLoading(false);
      }
    };
    fetchData();
  }, [runId]);

  const formatDuration = (minutes) => {
    if (minutes == null) return '-';
    const m = Math.floor(minutes);
    const s = Math.round((minutes - m) * 60);
    return `${m}m ${s}s`;
  };

  const formatSeconds = (seconds) => {
    if (seconds == null || seconds === 0) return '0s';
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  };

  const formatTokens = (tokens) => {
    if (!tokens || tokens === 0) return '0';
    if (tokens >= 1000000) return `${(tokens / 1000000).toFixed(1)}M`;
    if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K`;
    return tokens.toLocaleString();
  };

  const parseModelsUsed = (modelsStr) => {
    if (!modelsStr) return [];
    try {
      return JSON.parse(modelsStr);
    } catch {
      return [modelsStr];
    }
  };

  // Calculate derived stats
  const stats = useMemo(() => {
    if (!data) return null;

    const { steps, classifications } = data;

    // Self-correction rate: steps with retries > 0 AND verdict PROCEED / total steps with retries
    const stepsWithRetries = steps.filter(s => s.retries > 0);
    const selfCorrected = stepsWithRetries.filter(
      s => s.final_verdict?.toUpperCase() === 'PROCEED'
    );
    const selfCorrectionRate = stepsWithRetries.length > 0
      ? (selfCorrected.length / stepsWithRetries.length) * 100
      : null;

    // Time on dead ends: sum duration of architectural steps
    const architecturalSteps = steps.filter(
      s => s.classification?.toLowerCase() === 'architectural'
    );
    const timeOnDeadEnds = architecturalSteps.reduce(
      (sum, s) => sum + (s.duration_seconds || 0), 0
    );
    const hasClassifications = steps.some(s => s.classification);

    // Token usage from run
    const totalInputTokens = run?.total_input_tokens || 0;
    const totalOutputTokens = run?.total_output_tokens || 0;
    const totalCacheReadTokens = run?.total_cache_read_tokens || 0;
    const totalCost = run?.total_cost_usd || 0;
    const hasTokenData = totalInputTokens > 0 || totalOutputTokens > 0;

    return {
      selfCorrectionRate,
      timeOnDeadEnds,
      hasClassifications,
      architectural: classifications?.architectural || 0,
      implementation: classifications?.implementation || 0,
      cleanPass: classifications?.clean_pass || 0,
      totalInputTokens,
      totalOutputTokens,
      totalCacheReadTokens,
      totalCost,
      hasTokenData,
    };
  }, [data]);

  // Group failures by category
  const failuresByCategory = useMemo(() => {
    if (!data?.failures) return [];
    const grouped = {};
    data.failures.forEach(f => {
      const cat = f.category || 'Unknown';
      if (!grouped[cat]) {
        grouped[cat] = { category: cat, count: 0, buildPhases: new Set() };
      }
      grouped[cat].count++;
      if (f.build_phase) grouped[cat].buildPhases.add(f.build_phase);
    });
    return Object.values(grouped).sort((a, b) => b.count - a.count);
  }, [data]);

  // Check if has supabase issues
  const hasSupabaseIssues = useMemo(() => {
    if (!data?.run) return false;
    const r = data.run;
    return (r.rls_issues || 0) + (r.migration_issues || 0) +
           (r.edge_function_issues || 0) + (r.auth_issues || 0) > 0;
  }, [data]);

  if (loading) {
    return <div className="page loading">Loading run details...</div>;
  }

  if (error) {
    return (
      <div className="page">
        <Link to="/" className="back-link">← Back to runs</Link>
        <div className="error-banner">{error}</div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="page">
        <Link to="/" className="back-link">← Back to runs</Link>
        <div className="error-banner">Run not found</div>
      </div>
    );
  }

  const { run, steps, failures } = data;
  const modelsUsed = parseModelsUsed(run.models_used);

  return (
    <div className="run-detail-page">
      {/* Navigation */}
      <nav className="page-nav">
        <Link to="/" className="nav-link">Runs</Link>
        <Link to="/patterns" className="nav-link">Patterns</Link>
        <span className="nav-current">{runId}</span>
      </nav>

      {/* Header Section */}
      <header className="run-header">
        <div className="run-header-top">
          <h1 className="run-id">{run.run_id}</h1>
          <StatusBadge status={run.status} />
          <span className="run-duration">{formatDuration(run.duration_minutes)}</span>
        </div>
        <div className="run-prompt">{run.prompt || 'No prompt available'}</div>
        <div className="run-meta">
          <span className="meta-item">
            <strong>Planner:</strong> {run.planner || '-'}
          </span>
          <span className="meta-divider">|</span>
          <span className="meta-item">
            <strong>Implementer:</strong> {run.implementer || '-'}
          </span>
          <span className="meta-divider">|</span>
          <span className="meta-item">
            <strong>Verifier:</strong> {run.verifier || '-'}
          </span>
        </div>
        {modelsUsed.length > 0 && (
          <div className="run-models">
            <strong>Models:</strong> {modelsUsed.join(', ')}
          </div>
        )}
        {(run.events_may_be_truncated || run.steps_without_events > 0) && (
          <div className="warning-banner">
            ⚠️ Event data incomplete: {run.event_coverage || `${run.events_count} events`}
            {run.steps_without_events > 0 && (
              <span> — {run.steps_without_events} step(s) have no event data (events capped at 1000)</span>
            )}
          </div>
        )}
      </header>

      {/* Summary Cards */}
      <div className="summary-cards">
        <SummaryCard
          title="Architectural Failures"
          value={stats.architectural}
          accent="red"
        />
        <SummaryCard
          title="Implementation Struggles"
          value={stats.implementation}
          accent="yellow"
        />
        <SummaryCard
          title="Clean Passes"
          value={stats.cleanPass}
          accent="green"
        />
        <SummaryCard
          title="Self-Correction Rate"
          value={stats.selfCorrectionRate != null
            ? `${stats.selfCorrectionRate.toFixed(0)}%`
            : 'N/A'}
          accent="blue"
          subtitle={stats.selfCorrectionRate != null ? 'of retried steps recovered' : 'no retries'}
        />
        <SummaryCard
          title="Time on Dead Ends"
          value={stats.hasClassifications
            ? formatSeconds(stats.timeOnDeadEnds)
            : 'Pending'}
          accent="gray"
          subtitle={stats.hasClassifications ? 'in architectural failures' : 'awaiting classification'}
        />
        {stats.hasTokenData && (
          <>
            <SummaryCard
              title="Total Tokens"
              value={formatTokens(stats.totalInputTokens + stats.totalOutputTokens)}
              accent="purple"
              subtitle={`${formatTokens(stats.totalInputTokens)} in / ${formatTokens(stats.totalOutputTokens)} out`}
            />
            <SummaryCard
              title="API Cost"
              value={`$${stats.totalCost.toFixed(2)}`}
              accent="purple"
              subtitle={stats.totalCacheReadTokens > 0 ? `${formatTokens(stats.totalCacheReadTokens)} cache hits` : null}
            />
          </>
        )}
      </div>

      {/* Step Timeline */}
      <section className="section">
        <h2 className="section-title">Step Timeline ({steps.length} steps)</h2>
        <div className="step-timeline">
          {steps.map(step => (
            <React.Fragment key={step.id}>
              <StepCard
                step={step}
                isSelected={selectedStep === step.step_number}
                onClick={() => {
                  console.log('Clicked step:', step.step_number, 'current selected:', selectedStep);
                  setSelectedStep(
                    selectedStep === step.step_number ? null : step.step_number
                  );
                }}
              />
              {/* Show detail panel inline below the selected step */}
              {selectedStep === step.step_number && (
                <StepDetailPanel
                  runId={runId}
                  stepNumber={selectedStep}
                  onClose={() => setSelectedStep(null)}
                />
              )}
            </React.Fragment>
          ))}
        </div>
      </section>

      {/* Failure Breakdown */}
      {failuresByCategory.length > 0 && (
        <section className="section">
          <h2 className="section-title">Failure Breakdown</h2>
          <table className="breakdown-table">
            <thead>
              <tr>
                <th>Category</th>
                <th>Count</th>
                <th>Build Phases</th>
              </tr>
            </thead>
            <tbody>
              {failuresByCategory.map((f, i) => (
                <tr key={i}>
                  <td>{f.category}</td>
                  <td>{f.count}</td>
                  <td>{Array.from(f.buildPhases).join(', ') || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {/* Supabase Issues */}
      {hasSupabaseIssues && (
        <section className="section">
          <h2 className="section-title">Supabase Issues</h2>
          <div className="supabase-issues">
            {run.rls_issues > 0 && (
              <span className="issue-badge">RLS Issues: {run.rls_issues}</span>
            )}
            {run.migration_issues > 0 && (
              <span className="issue-badge">Migration Issues: {run.migration_issues}</span>
            )}
            {run.edge_function_issues > 0 && (
              <span className="issue-badge">Edge Function Issues: {run.edge_function_issues}</span>
            )}
            {run.auth_issues > 0 && (
              <span className="issue-badge">Auth Issues: {run.auth_issues}</span>
            )}
          </div>
        </section>
      )}
    </div>
  );
};

export default RunDetail;
