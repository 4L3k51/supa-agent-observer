import React, { useState, useEffect, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { getRuns, getStats, triggerIngest } from '../api';
import StatusBadge from '../components/StatusBadge';
import ClassificationBar from '../components/ClassificationBar';

const RunList = () => {
  const [runs, setRuns] = useState([]);
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);

  // Filters
  const [statusFilter, setStatusFilter] = useState('all');
  const [hasArchitectural, setHasArchitectural] = useState(false);

  // Sorting
  const [sortField, setSortField] = useState('generated_at');
  const [sortDirection, setSortDirection] = useState('desc');

  const fetchData = async () => {
    try {
      setError(null);
      const [runsData, statsData] = await Promise.all([getRuns(), getStats()]);
      setRuns(runsData);
      setStats(statsData);
    } catch (err) {
      setError('Failed to fetch data. Is the API server running on port 8000?');
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, []);

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      await triggerIngest(false);
      await fetchData();
    } catch (err) {
      setError('Failed to refresh data');
      console.error(err);
    } finally {
      setRefreshing(false);
    }
  };

  const handleSort = (field) => {
    if (sortField === field) {
      setSortDirection(sortDirection === 'asc' ? 'desc' : 'asc');
    } else {
      setSortField(field);
      setSortDirection('desc');
    }
  };

  const filteredAndSortedRuns = useMemo(() => {
    let result = [...runs];

    // Apply status filter
    if (statusFilter !== 'all') {
      result = result.filter(run => run.status?.toLowerCase() === statusFilter);
    }

    // Apply architectural filter
    if (hasArchitectural) {
      result = result.filter(run => run.classifications?.architectural > 0);
    }

    // Sort
    result.sort((a, b) => {
      let aVal = a[sortField];
      let bVal = b[sortField];

      // Handle nested fields
      if (sortField === 'classifications') {
        aVal = a.classifications?.architectural || 0;
        bVal = b.classifications?.architectural || 0;
      }

      // Handle nulls
      if (aVal == null) aVal = '';
      if (bVal == null) bVal = '';

      // Compare
      if (typeof aVal === 'number' && typeof bVal === 'number') {
        return sortDirection === 'asc' ? aVal - bVal : bVal - aVal;
      }

      const comparison = String(aVal).localeCompare(String(bVal));
      return sortDirection === 'asc' ? comparison : -comparison;
    });

    return result;
  }, [runs, statusFilter, hasArchitectural, sortField, sortDirection]);

  const formatDate = (dateStr) => {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  };

  const formatDuration = (minutes) => {
    if (minutes == null) return '-';
    const m = Math.floor(minutes);
    const s = Math.round((minutes - m) * 60);
    return `${m}m ${s}s`;
  };

  const truncate = (str, len) => {
    if (!str) return '-';
    return str.length > len ? str.slice(0, len) + '...' : str;
  };

  const formatTokens = (tokens) => {
    if (!tokens || tokens === 0) return '0';
    if (tokens >= 1000000) return `${(tokens / 1000000).toFixed(1)}M`;
    if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K`;
    return tokens.toLocaleString();
  };

  const formatTools = (run) => {
    const abbrev = (tool) => {
      if (!tool) return '?';
      if (tool.includes('claude') || tool.includes('CC')) return 'CC';
      if (tool.includes('gemini')) return 'G';
      if (tool.includes('gpt')) return 'GPT';
      return tool.slice(0, 2).toUpperCase();
    };
    return `${abbrev(run.planner)}/${abbrev(run.implementer)}/${abbrev(run.verifier)}`;
  };

  const getSuccessRateClass = (rate) => {
    if (rate == null) return '';
    if (rate > 0.8) return 'rate-high';
    if (rate > 0.5) return 'rate-medium';
    return 'rate-low';
  };

  const SortHeader = ({ field, children }) => (
    <th onClick={() => handleSort(field)} className="sortable">
      {children}
      {sortField === field && (
        <span className="sort-indicator">{sortDirection === 'asc' ? ' ▲' : ' ▼'}</span>
      )}
    </th>
  );

  if (loading) {
    return <div className="loading">Loading...</div>;
  }

  return (
    <div className="run-list-page">
      <nav className="page-nav">
        <span className="nav-current">Runs</span>
        <Link to="/patterns" className="nav-link">Patterns</Link>
      </nav>

      <header className="page-header">
        <h1>Orchestrator Dashboard</h1>
        <button
          className="btn btn-primary"
          onClick={handleRefresh}
          disabled={refreshing}
        >
          {refreshing ? 'Refreshing...' : 'Refresh'}
        </button>
      </header>

      {error && <div className="error-banner">{error}</div>}

      {stats && (
        <div className="stats-bar">
          <div className="stat">
            <span className="stat-value">{stats.total_runs}</span>
            <span className="stat-label">Total Runs</span>
          </div>
          <div className="stat">
            <span className="stat-value">{(stats.avg_success_rate * 100).toFixed(1)}%</span>
            <span className="stat-label">Avg Success</span>
          </div>
          <div className="stat">
            <span className="stat-value">{stats.total_failures}</span>
            <span className="stat-label">Total Failures</span>
          </div>
          <div className="stat">
            <span className="stat-value">{stats.total_retries}</span>
            <span className="stat-label">Total Retries</span>
          </div>
          {(stats.total_input_tokens > 0 || stats.total_output_tokens > 0) && (
            <>
              <div className="stat">
                <span className="stat-value">{formatTokens(stats.total_input_tokens + stats.total_output_tokens)}</span>
                <span className="stat-label">Total Tokens</span>
              </div>
              <div className="stat">
                <span className="stat-value">${stats.total_cost_usd?.toFixed(2) || '0.00'}</span>
                <span className="stat-label">Total Cost</span>
              </div>
            </>
          )}
        </div>
      )}

      <div className="filters-bar">
        <div className="filter-group">
          <label>Status:</label>
          <div className="btn-group">
            {['all', 'success', 'failed', 'partial'].map(status => (
              <button
                key={status}
                className={`btn btn-filter ${statusFilter === status ? 'active' : ''}`}
                onClick={() => setStatusFilter(status)}
              >
                {status.charAt(0).toUpperCase() + status.slice(1)}
              </button>
            ))}
          </div>
        </div>
        <div className="filter-group">
          <label>
            <input
              type="checkbox"
              checked={hasArchitectural}
              onChange={(e) => setHasArchitectural(e.target.checked)}
            />
            Has Architectural Failures
          </label>
        </div>
      </div>

      <div className="table-container">
        <table className="runs-table">
          <thead>
            <tr>
              <SortHeader field="run_id">Run ID</SortHeader>
              <SortHeader field="generated_at">Date</SortHeader>
              <SortHeader field="prompt">Prompt</SortHeader>
              <SortHeader field="status">Status</SortHeader>
              <th>Tools</th>
              <SortHeader field="passed_steps">Steps</SortHeader>
              <SortHeader field="success_rate">Success</SortHeader>
              <SortHeader field="total_retries">Retries</SortHeader>
              <SortHeader field="duration_minutes">Duration</SortHeader>
              <SortHeader field="total_input_tokens">Tokens</SortHeader>
              <SortHeader field="total_cost_usd">Cost</SortHeader>
              <SortHeader field="classifications">Classification</SortHeader>
            </tr>
          </thead>
          <tbody>
            {filteredAndSortedRuns.map(run => (
              <tr key={run.run_id}>
                <td>
                  <Link to={`/runs/${run.run_id}`} className="run-link">
                    {truncate(run.run_id, 8)}
                  </Link>
                  {run.steps_without_events > 0 && (
                    <span
                      className="event-coverage-warning"
                      title={run.event_coverage}
                    >
                      ⚠️
                    </span>
                  )}
                </td>
                <td>{formatDate(run.generated_at)}</td>
                <td title={run.prompt}>{truncate(run.prompt, 60)}</td>
                <td><StatusBadge status={run.status} /></td>
                <td className="tools-cell">{formatTools(run)}</td>
                <td>{run.passed_steps}/{run.total_steps}</td>
                <td className={getSuccessRateClass(run.success_rate)}>
                  {run.success_rate != null ? `${(run.success_rate * 100).toFixed(0)}%` : '-'}
                </td>
                <td className={run.total_retries > 5 ? 'retries-high' : ''}>
                  {run.total_retries ?? 0}
                </td>
                <td>{formatDuration(run.duration_minutes)}</td>
                <td className="tokens-cell">
                  {(run.total_input_tokens > 0 || run.total_output_tokens > 0)
                    ? formatTokens((run.total_input_tokens || 0) + (run.total_output_tokens || 0))
                    : '-'}
                </td>
                <td className="cost-cell">
                  {run.total_cost_usd > 0 ? `$${run.total_cost_usd.toFixed(2)}` : '-'}
                </td>
                <td><ClassificationBar classifications={run.classifications} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        {filteredAndSortedRuns.length === 0 && (
          <div className="no-data">No runs match the current filters</div>
        )}
      </div>
    </div>
  );
};

export default RunList;
