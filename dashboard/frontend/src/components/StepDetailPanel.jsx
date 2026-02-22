import React, { useState, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import { getStepDetail } from '../api';

const StepDetailPanel = ({ runId, stepNumber, onClose }) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showRaw, setShowRaw] = useState(false);

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      try {
        const result = await getStepDetail(runId, stepNumber);
        setData(result);
      } catch (err) {
        console.error('Failed to fetch step detail:', err);
      } finally {
        setLoading(false);
      }
    };
    fetchData();
  }, [runId, stepNumber]);

  if (loading) {
    return <div className="step-detail-panel loading">Loading step details...</div>;
  }

  if (!data) {
    return <div className="step-detail-panel error">Failed to load step details</div>;
  }

  const { step, failures, web_searches } = data;

  const formatDuration = (seconds) => {
    if (seconds == null) return '-';
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  };

  const formatTokens = (tokens) => {
    if (!tokens || tokens === 0) return '-';
    if (tokens >= 1000000) return `${(tokens / 1000000).toFixed(1)}M`;
    if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K`;
    return tokens.toLocaleString();
  };

  const parseJSON = (str) => {
    if (!str) return null;
    try {
      return JSON.parse(str);
    } catch {
      return null;
    }
  };

  const resolutionActions = parseJSON(step.resolution_actions);
  const errorCategories = parseJSON(step.error_categories);


  return (
    <div className="step-detail-panel">
      <div className="step-detail-header">
        <h3>Step {step.step_number} Details</h3>
        <button className="close-btn" onClick={onClose}>×</button>
      </div>

      <div className="step-detail-content">
        {/* Basic Info */}
        <div className="detail-section">
          <h4>Step Information</h4>
          <div className="detail-grid">
            <div className="detail-item">
              <span className="detail-label">Build Phase</span>
              <span className="detail-value">{step.build_phase || '-'}</span>
            </div>
            <div className="detail-item">
              <span className="detail-label">Phase</span>
              <span className="detail-value">{step.phase || '-'}</span>
            </div>
            <div className="detail-item">
              <span className="detail-label">Tool</span>
              <span className="detail-value">{step.tool || '-'}</span>
            </div>
            <div className="detail-item">
              <span className="detail-label">Verdict</span>
              <span className={`detail-value verdict-${step.final_verdict?.toLowerCase()}`}>
                {step.final_verdict || '-'}
              </span>
            </div>
            <div className="detail-item">
              <span className="detail-label">Attempts</span>
              <span className="detail-value">{step.attempts ?? '-'}</span>
            </div>
            <div className="detail-item">
              <span className="detail-label">Retries</span>
              <span className="detail-value">{step.retries ?? '-'}</span>
            </div>
            <div className="detail-item">
              <span className="detail-label">Duration</span>
              <span className="detail-value">{formatDuration(step.duration_seconds)}</span>
            </div>
            {(step.input_tokens > 0 || step.output_tokens > 0) && (
              <>
                <div className="detail-item">
                  <span className="detail-label">Tokens</span>
                  <span className="detail-value">
                    {formatTokens(step.input_tokens)} in / {formatTokens(step.output_tokens)} out
                  </span>
                </div>
                {step.cost_usd > 0 && (
                  <div className="detail-item">
                    <span className="detail-label">Cost</span>
                    <span className="detail-value">${step.cost_usd.toFixed(4)}</span>
                  </div>
                )}
              </>
            )}
          </div>
        </div>

        {/* Classification */}
        {step.classification && (
          <div className="detail-section">
            <h4>Classification</h4>
            <div className={`classification-display cls-${step.classification.toLowerCase()}`}>
              <span className="classification-label">{step.classification}</span>
              {step.classification_confidence != null && (
                <div className="confidence-bar-container">
                  <div className="confidence-label">
                    Confidence: {(step.classification_confidence * 100).toFixed(0)}%
                  </div>
                  <div className="confidence-bar">
                    <div
                      className="confidence-fill"
                      style={{ width: `${step.classification_confidence * 100}%` }}
                    />
                  </div>
                </div>
              )}
            </div>
            {step.classification_reasoning && (
              <div className="classification-reasoning">
                <strong>Reasoning:</strong>
                <div className="markdown-content">
                  <ReactMarkdown>{step.classification_reasoning}</ReactMarkdown>
                </div>
              </div>
            )}
            {step.classification_evidence && (
              <div className="classification-evidence">
                <strong>Evidence:</strong>
                <div className="markdown-content">
                  <ReactMarkdown>{step.classification_evidence}</ReactMarkdown>
                </div>
              </div>
            )}
            <div className="observable-patterns">
              <span className={`pattern-pill ${step.approach_changed ? 'yes' : 'no'}`}>
                Approach Changed: {step.approach_changed ? 'Yes' : 'No'}
              </span>
              <span className={`pattern-pill ${step.same_file_repeated ? 'yes' : 'no'}`}>
                Same File Repeated: {step.same_file_repeated ? 'Yes' : 'No'}
              </span>
              <span className={`pattern-pill ${step.error_category_stable ? 'yes' : 'no'}`}>
                Error Category Stable: {step.error_category_stable ? 'Yes' : 'No'}
              </span>
            </div>
          </div>
        )}

        {/* Errors */}
        {failures && failures.length > 0 && (
          <div className="detail-section">
            <h4>Errors ({failures.length})</h4>
            <div className="errors-list">
              {failures.map((f, i) => (
                <div key={i} className="error-item">
                  <span className="error-category">{f.category || 'Unknown'}</span>
                  <div className="error-message-markdown">
                    <ReactMarkdown>{f.error || ''}</ReactMarkdown>
                  </div>
                  {f.exit_code != null && (
                    <span className="error-exit-code">Exit code: {f.exit_code}</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Resolution Actions */}
        {resolutionActions && resolutionActions.length > 0 && (
          <div className="detail-section">
            <h4>Resolution Actions</h4>
            <ul className="resolution-actions">
              {resolutionActions.map((action, i) => (
                <li key={i}>
                  <div className="markdown-content inline">
                    <ReactMarkdown>{action}</ReactMarkdown>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Error Categories */}
        {errorCategories && errorCategories.length > 0 && (
          <div className="detail-section">
            <h4>Error Categories</h4>
            <div className="error-categories">
              {errorCategories.map((cat, i) => (
                <span key={i} className="error-category-tag">{cat}</span>
              ))}
            </div>
          </div>
        )}

        {/* No Events Warning */}
        {!step.has_events && (
          <div className="detail-section">
            <div className="no-events-warning">
              <strong>⚠️ No event data available</strong>
              <p>
                Events were capped at 1000 and exhausted before this step.
                Tool calls, file modifications, and approach changes cannot be verified.
              </p>
            </div>
          </div>
        )}

        {/* Web Searches */}
        {web_searches && web_searches.length > 0 && (
          <div className="detail-section">
            <h4>Web Searches ({web_searches.length})</h4>
            <ul className="web-searches">
              {web_searches.map((ws, i) => (
                <li key={i}>
                  <span className="search-query">"{ws.query}"</span>
                  <span className="search-count">×{ws.count}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Raw Data (Collapsible) */}
        <div className="detail-section">
          <button
            className="collapse-toggle"
            onClick={() => setShowRaw(!showRaw)}
          >
            {showRaw ? '▼' : '▶'} Raw Data
          </button>
          {showRaw && (
            <div className="raw-data">
              <h5>Errors Summary</h5>
              <div className="markdown-content">
                <ReactMarkdown>{step.errors_summary || 'No errors summary'}</ReactMarkdown>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default StepDetailPanel;
