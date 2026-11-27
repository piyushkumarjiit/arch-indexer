import type { ChunkFlow, EndReason } from "../types/bundle";

const END_REASON_LABELS: Record<EndReason, string> = {
  boundary: "reached boundary",
  resource: "reached shared resource",
  "no-boundary-detected": "no-boundary-detected",
  "depth-cap": "depth-cap",
  "no-outgoing-edges": "no-outgoing-edges",
};

export function FlowBanner({
  flow,
  stepIndex,
  flowTitle,
  followStep = true,
  onToggleFollowStep,
  onPrev,
  onNext,
  onExit,
}: {
  flow: ChunkFlow | null;
  stepIndex: number;
  flowTitle?: string;
  followStep?: boolean;
  onToggleFollowStep?: () => void;
  onPrev: () => void;
  onNext: () => void;
  onExit: () => void;
}) {
  if (!flow || flow.steps.length === 0) return null;

  const total = flow.steps.length;
  const clampedIdx = Math.max(0, Math.min(total - 1, stepIndex));
  const step = flow.steps[clampedIdx];
  const isFinal = clampedIdx === total - 1;
  const isPartial = flow.end_reason !== "boundary" && flow.end_reason !== "resource";

  return (
    <div className="arch-flow-banner" role="region" aria-label="Flow walkthrough" data-testid="flow-banner">
      <div className="arch-flow-info">
        <span className="arch-badge arch-badge--flow">
          step {clampedIdx + 1} of {total}
        </span>
        {flowTitle && <span className="arch-flow-title">{flowTitle}</span>}
        <span className="arch-flow-node">{step.node_id}</span>
        {step.edge_kind && (
          <span className="arch-flow-edge">via {step.edge_kind}</span>
        )}
        {step.file && (
          <span className="arch-flow-loc">
            {step.file}:{step.line}
          </span>
        )}
        {isFinal && isPartial && (
          <span className="arch-badge arch-badge--partial" data-testid="flow-end-reason">
            {END_REASON_LABELS[flow.end_reason] ?? flow.end_reason}
          </span>
        )}
      </div>
      <div className="arch-flow-actions">
        {onToggleFollowStep && (
          <label
            className="arch-flow-follow"
            title="Auto-drill to Symbol view as you step through the flow"
          >
            <input
              type="checkbox"
              data-testid="flow-follow-toggle"
              checked={followStep}
              onChange={() => onToggleFollowStep()}
            />
            <span>Follow step</span>
          </label>
        )}
        <button
          type="button"
          className="arch-btn"
          onClick={onPrev}
          disabled={clampedIdx === 0}
        >
          &larr; Prev
        </button>
        <button
          type="button"
          className="arch-btn arch-btn--primary"
          onClick={onNext}
          disabled={clampedIdx >= total - 1}
        >
          Next &rarr;
        </button>
        <button type="button" className="arch-btn" onClick={onExit}>
          Exit
        </button>
      </div>
    </div>
  );
}
