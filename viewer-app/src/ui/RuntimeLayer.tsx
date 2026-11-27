import { useEffect, useRef, useState } from "react";
import type {
  OverviewData,
  PartChunk,
  RuntimeNodeMetric,
  RuntimeOverlayData,
} from "../types/bundle";

export type RuntimeHeatLevel = "none" | "ok" | "slow" | "error";

const SLOW_P95_THRESHOLD_MS = 250;
const TIER_PRECEDENCE: Record<string, number> = {
  code: 0,
  route: 1,
  peer: 2,
  service: 3,
};

export function runtimeHeatLevel(metric: RuntimeNodeMetric | null | undefined): RuntimeHeatLevel {
  if (!metric) return "none";
  if (metric.error_count > 0) return "error";
  if (metric.p95_ms >= SLOW_P95_THRESHOLD_MS) return "slow";
  return "ok";
}

export function isRuntimeOverlayPayload(value: unknown): value is RuntimeOverlayData {
  if (!value || typeof value !== "object") return false;
  const obj = value as Record<string, unknown>;
  return (
    typeof obj.node_metrics === "object" &&
    obj.node_metrics !== null &&
    typeof obj.edge_metrics === "object" &&
    obj.edge_metrics !== null
  );
}

export function readRuntimeOverlayFile(file: File): Promise<RuntimeOverlayData> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const text = typeof reader.result === "string" ? reader.result : "";
        const parsed: unknown = JSON.parse(text);
        if (!isRuntimeOverlayPayload(parsed)) {
          reject(new Error("File is not a valid runtime-overlay.v1.json payload"));
          return;
        }
        resolve(parsed);
      } catch (err) {
        reject(err instanceof Error ? err : new Error(String(err)));
      }
    };
    reader.onerror = () => {
      reject(reader.error ?? new Error("Failed to read runtime overlay file"));
    };
    reader.readAsText(file);
  });
}

function aggregateMetrics(metrics: RuntimeNodeMetric[]): RuntimeNodeMetric | null {
  if (metrics.length === 0) return null;
  let spanCount = 0;
  let errorCount = 0;
  let maxP50 = 0;
  let maxP95 = 0;
  let bestTier = metrics[0].match_tier;
  const excCounts = new Map<string, number>();
  const framesMap = new Map<string, { file: string; line: number; fqname?: string }>();

  for (const m of metrics) {
    spanCount += m.span_count;
    errorCount += m.error_count;
    if (m.p50_ms > maxP50) maxP50 = m.p50_ms;
    if (m.p95_ms > maxP95) maxP95 = m.p95_ms;
    if ((TIER_PRECEDENCE[m.match_tier] ?? 99) < (TIER_PRECEDENCE[bestTier] ?? 99)) {
      bestTier = m.match_tier;
    }
    for (const [etype, cnt] of m.top_exceptions ?? []) {
      excCounts.set(etype, (excCounts.get(etype) ?? 0) + cnt);
    }
    for (const fr of m.error_frames ?? []) {
      const key = `${fr.file}:${fr.line}:${fr.fqname ?? ""}`;
      if (!framesMap.has(key)) framesMap.set(key, fr);
    }
  }

  const out: RuntimeNodeMetric = {
    span_count: spanCount,
    error_count: errorCount,
    p50_ms: maxP50,
    p95_ms: maxP95,
    match_tier: bestTier,
  };
  if (excCounts.size > 0) {
    out.top_exceptions = Array.from(excCounts.entries())
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 5);
  }
  if (framesMap.size > 0) {
    out.error_frames = Array.from(framesMap.values()).slice(0, 5);
  }
  return out;
}

export function projectRuntimeNodeMetrics(
  nodeId: string,
  runtime: RuntimeOverlayData | null,
  overview: OverviewData,
  chunks: Record<string, PartChunk>,
): RuntimeNodeMetric | null {
  if (!runtime) return null;
  const direct = runtime.node_metrics?.[nodeId];
  if (direct) return direct;

  if (nodeId.startsWith("part:")) {
    const groupIds = new Set(
      overview.groups.filter((g) => g.part_id === nodeId).map((g) => g.id),
    );
    const collected: RuntimeNodeMetric[] = [];
    for (const gid of groupIds) {
      if (runtime.node_metrics?.[gid]) collected.push(runtime.node_metrics[gid]);
    }
    const chunk = chunks[nodeId];
    if (chunk) {
      for (const n of chunk.nodes) {
        if (runtime.node_metrics?.[n.id]) collected.push(runtime.node_metrics[n.id]);
      }
    }
    return aggregateMetrics(collected);
  }

  if (nodeId.startsWith("mod:")) {
    const grp = overview.groups.find((g) => g.id === nodeId);
    const chunk = grp ? chunks[grp.part_id] : undefined;
    if (!chunk) return null;
    const collected: RuntimeNodeMetric[] = [];
    for (const n of chunk.nodes) {
      if (n.module_id === nodeId && runtime.node_metrics?.[n.id]) {
        collected.push(runtime.node_metrics[n.id]);
      }
    }
    return aggregateMetrics(collected);
  }

  if (nodeId.startsWith("file:")) {
    const filePath = nodeId.slice(5);
    const collected: RuntimeNodeMetric[] = [];
    for (const chunk of Object.values(chunks)) {
      for (const n of chunk.nodes) {
        if (n.file === filePath && runtime.node_metrics?.[n.id]) {
          collected.push(runtime.node_metrics[n.id]);
        }
      }
    }
    return aggregateMetrics(collected);
  }

  return null;
}

export function isHttpServedOrigin(): boolean {
  if (typeof window === "undefined") return false;
  return window.location.protocol === "http:" || window.location.protocol === "https:";
}

export function RuntimeLayerControls({
  runtime,
  overview,
  runtimeLayerActive,
  activeTraceId,
  onLoadRuntime,
  onStartTrace,
  onClearRuntime,
}: {
  runtime: RuntimeOverlayData | null;
  overview: OverviewData | null;
  runtimeLayerActive: boolean;
  activeTraceId: string | null;
  onLoadRuntime: (overlay: RuntimeOverlayData) => void;
  onStartTrace: (traceId: string) => void;
  onClearRuntime: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [livePoll, setLivePoll] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const canLivePoll = isHttpServedOrigin();

  useEffect(() => {
    if (!livePoll || !canLivePoll) return;
    let active = true;
    const poll = async () => {
      try {
        const resp = await fetch("arch/runtime-overlay.v1.json", { cache: "no-store" });
        if (!resp.ok) return;
        const parsed: unknown = await resp.json();
        if (active && isRuntimeOverlayPayload(parsed)) {
          onLoadRuntime(parsed);
          setLoadError(null);
        }
      } catch {
        // Ignore transient poll failures during live debugging
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [livePoll, canLivePoll, onLoadRuntime]);

  const handleFileChange = async (file: File | undefined) => {
    if (!file) return;
    try {
      const overlay = await readRuntimeOverlayFile(file);
      setLoadError(null);
      onLoadRuntime(overlay);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Failed to load runtime overlay");
    }
  };

  if (!runtime) {
    return (
      <div className="arch-runtime-actions">
        <button
          type="button"
          className="arch-btn arch-runtime-btn"
          data-testid="load-runtime-btn"
          onClick={() => inputRef.current?.click()}
        >
          Load Runtime
        </button>
        {canLivePoll && (
          <label className="arch-runtime-poll-toggle" title="Poll arch/runtime-overlay.v1.json">
            <input
              type="checkbox"
              data-testid="runtime-live-poll-toggle"
              checked={livePoll}
              onChange={(e) => setLivePoll(e.target.checked)}
            />
            <span>Live poll</span>
          </label>
        )}
        <input
          ref={inputRef}
          type="file"
          accept=".json,application/json"
          data-testid="runtime-file-input"
          style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            void handleFileChange(f);
            e.target.value = "";
          }}
        />
        {loadError && <span className="arch-runtime-error">{loadError}</span>}
      </div>
    );
  }

  const matchedNodeCount = Object.keys(runtime.node_metrics ?? {}).length;
  const driftCount = Object.values(runtime.edge_metrics ?? {}).filter((e) => !e.static).length;
  const commitMismatch = Boolean(
    runtime.static_commit &&
      overview?.generated_at_commit &&
      runtime.static_commit !== overview.generated_at_commit,
  );

  return (
    <div className="arch-runtime-bar" data-testid="runtime-overlay-bar">
      <div className="arch-runtime-bar__meta">
        <span className="arch-badge arch-badge--layer-runtime">
          runtime: {runtime.source.kind}
        </span>
        {runtime.source.service_names.length > 0 && (
          <span className="arch-runtime-bar__services">
            {runtime.source.service_names.join(", ")}
          </span>
        )}
        {runtime.captured_at && (
          <span className="arch-runtime-bar__stat">{runtime.captured_at}</span>
        )}
        <span className="arch-runtime-bar__stat">
          {matchedNodeCount} matched &middot; {driftCount} drift &middot;{" "}
          {runtime.unmatched.span_count} unmatched
        </span>
        {commitMismatch && (
          <span className="arch-badge arch-badge--stale" data-testid="runtime-commit-mismatch">
            commit mismatch
          </span>
        )}
        {!runtimeLayerActive && (
          <span className="arch-runtime-bar__hint">(Runtime layer hidden)</span>
        )}
      </div>

      <div className="arch-runtime-bar__controls">
        {runtime.traces.length > 0 && (
          <select
            className="arch-runtime-select"
            aria-label="Replay runtime trace"
            data-testid="trace-replay-select"
            value={activeTraceId ?? ""}
            onChange={(e) => {
              if (e.target.value) onStartTrace(e.target.value);
            }}
          >
            <option value="">Replay trace ({runtime.traces.length})...</option>
            {runtime.traces.map((tr) => {
              const hasErr = tr.spans.some((s) => s.status === "ERROR");
              return (
                <option key={tr.trace_id} value={tr.trace_id}>
                  {hasErr ? "[ERR] " : ""}
                  {tr.trace_id.slice(0, 8)} · {tr.root_node_id || tr.spans[0]?.name || "trace"} (
                  {tr.spans.length} spans)
                </option>
              );
            })}
          </select>
        )}

        {canLivePoll && (
          <label className="arch-runtime-poll-toggle" title="Poll arch/runtime-overlay.v1.json">
            <input
              type="checkbox"
              data-testid="runtime-live-poll-toggle"
              checked={livePoll}
              onChange={(e) => setLivePoll(e.target.checked)}
            />
            <span>Live poll</span>
          </label>
        )}

        <button
          type="button"
          className="arch-mini-btn arch-runtime-btn"
          onClick={() => inputRef.current?.click()}
        >
          Replace JSON
        </button>
        <button
          type="button"
          className="arch-mini-btn arch-runtime-btn"
          data-testid="runtime-clear-btn"
          onClick={onClearRuntime}
        >
          Clear
        </button>
        <input
          ref={inputRef}
          type="file"
          accept=".json,application/json"
          data-testid="runtime-file-input"
          style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            void handleFileChange(f);
            e.target.value = "";
          }}
        />
        {loadError && <span className="arch-runtime-error">{loadError}</span>}
      </div>
    </div>
  );
}
