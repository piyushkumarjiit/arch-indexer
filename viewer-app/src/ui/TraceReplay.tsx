import type { RuntimeTrace, RuntimeTraceSpan } from "../types/bundle";

export function sortTraceSpans(spans: RuntimeTraceSpan[]): RuntimeTraceSpan[] {
  return [...spans].sort(
    (a, b) => a.start_ns - b.start_ns || a.span_id.localeCompare(b.span_id),
  );
}

export function TraceReplay({
  trace,
  spanIndex,
  onPrev,
  onNext,
  onExit,
  onFocusNode,
}: {
  trace: RuntimeTrace | null;
  spanIndex: number;
  onPrev: () => void;
  onNext: () => void;
  onExit: () => void;
  onFocusNode?: (nodeId: string) => void;
}) {
  if (!trace || trace.spans.length === 0) return null;

  const sortedSpans = sortTraceSpans(trace.spans);
  const total = sortedSpans.length;
  const clampedIdx = Math.max(0, Math.min(total - 1, spanIndex));
  const span = sortedSpans[clampedIdx];
  const durMs = (span.dur_ns / 1_000_000).toFixed(2);
  const isError = span.status === "ERROR";

  return (
    <div
      className="arch-trace-replay"
      role="region"
      aria-label="Trace replay"
      data-testid="trace-replay-banner"
    >
      <div className="arch-trace-replay__info">
        <span className="arch-badge arch-badge--layer-runtime" data-testid="trace-replay-step">
          span {clampedIdx + 1} of {total}
        </span>
        <span className="arch-trace-replay__name">{span.name}</span>
        {span.node_id && (
          <button
            type="button"
            className="arch-link-btn arch-trace-replay__node"
            data-testid="trace-replay-node"
            onClick={() => onFocusNode?.(span.node_id!)}
          >
            {span.node_id}
          </button>
        )}
        <span className="arch-badge">{durMs} ms</span>
        <span className={`arch-badge ${isError ? "arch-badge--rt-error" : ""}`}>
          {span.status}
        </span>
        <span className="arch-badge arch-badge--layer-runtime" data-testid="trace-replay-tier">
          {span.match_tier}
        </span>
        {span.exception_type && (
          <span className="arch-badge arch-badge--rt-error">{span.exception_type}</span>
        )}
      </div>
      <div className="arch-trace-replay__actions">
        <button
          type="button"
          className="arch-btn"
          data-testid="trace-replay-prev"
          onClick={onPrev}
          disabled={clampedIdx === 0}
        >
          &larr; Prev
        </button>
        <button
          type="button"
          className="arch-btn arch-btn--primary"
          data-testid="trace-replay-next"
          onClick={onNext}
          disabled={clampedIdx >= total - 1}
        >
          Next &rarr;
        </button>
        <button
          type="button"
          className="arch-btn"
          data-testid="trace-replay-exit"
          onClick={onExit}
        >
          Exit
        </button>
      </div>
    </div>
  );
}
