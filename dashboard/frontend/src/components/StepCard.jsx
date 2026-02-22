import React from 'react';

const StepCard = ({ step, isSelected, onClick }) => {
  const getClassificationClass = () => {
    switch (step.classification?.toLowerCase()) {
      case 'architectural':
        return 'step-architectural';
      case 'implementation':
        return 'step-implementation';
      case 'clean_pass':
        return 'step-clean';
      default:
        return 'step-pending';
    }
  };

  const getVerdictBadge = () => {
    const verdict = step.final_verdict?.toUpperCase();
    const phase = step.phase?.toLowerCase() || '';

    // Phases that don't produce PROCEED/FAIL verdicts
    const nonVerdictPhases = [
      'smoke_test',
      'browser_test',
      'browser_test_gen',
      'browser_test_fix',
      'browser_test_fix_verify',
      'rls_test',
      'api_verify',
      'approach_analysis',
      'plan',
      'replan_checkpoint',
    ];

    // Check if this is a non-verdict phase (phase can be comma-separated)
    const isNonVerdictPhase = nonVerdictPhases.some(p => phase.includes(p));

    // Determine badge text for non-verdict phases
    const getNonVerdictBadgeText = () => {
      if (phase.includes('replan_checkpoint')) return 'Replan';
      if (phase.includes('plan')) return 'Plan';
      if (phase.includes('approach_analysis')) return 'Analysis';
      return 'Runtime Test';
    };

    switch (verdict) {
      case 'PROCEED':
        return <span className="verdict-badge verdict-proceed">PROCEED</span>;
      case 'FAIL':
        return <span className="verdict-badge verdict-fail">FAIL</span>;
      case 'SKIP':
        return <span className="verdict-badge verdict-skip">SKIP</span>;
      case 'UNKNOWN':
      case '':
      case undefined:
      case null:
      default:
        // For UNKNOWN or missing verdict, check if it's a non-verdict phase
        if (isNonVerdictPhase) {
          return <span className="verdict-badge verdict-test">{getNonVerdictBadgeText()}</span>;
        }
        return <span className="verdict-badge verdict-unknown">{verdict || 'UNKNOWN'}</span>;
    }
  };

  const getClassificationBadge = () => {
    const cls = step.classification?.toLowerCase();
    switch (cls) {
      case 'architectural':
        return <span className="cls-badge cls-architectural">🔴 Architectural</span>;
      case 'implementation':
        return <span className="cls-badge cls-implementation">🟡 Implementation</span>;
      case 'clean_pass':
        return <span className="cls-badge cls-clean">🟢 Clean Pass</span>;
      case 'ambiguous':
        return <span className="cls-badge cls-ambiguous">⚪ Ambiguous</span>;
      default:
        return <span className="cls-badge cls-pending">Pending</span>;
    }
  };

  const formatDuration = (seconds) => {
    if (seconds == null) return '-';
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  };

  const handleClick = (e) => {
    console.log('StepCard clicked:', step.step_number, 'isSelected:', isSelected);
    if (onClick) {
      onClick(e);
    }
  };

  return (
    <div
      className={`step-card ${getClassificationClass()} ${isSelected ? 'step-selected' : ''}`}
      onClick={handleClick}
    >
      <div className="step-card-header">
        <span className="step-number">Step {step.step_number}</span>
        {step.build_phase && <span className="step-phase-label">— {step.build_phase}</span>}
      </div>
      <div className="step-card-badges">
        {step.phase && <span className="phase-badge">{step.phase}</span>}
        {getVerdictBadge()}
        {step.retries > 0 && (
          <span className="retry-badge">{step.retries} {step.retries === 1 ? 'retry' : 'retries'}</span>
        )}
      </div>
      <div className="step-card-footer">
        {getClassificationBadge()}
        <span className="step-duration">{formatDuration(step.duration_seconds)}</span>
      </div>
    </div>
  );
};

export default StepCard;
