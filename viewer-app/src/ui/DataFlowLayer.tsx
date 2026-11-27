import { useMemo, useState } from "react";
import type {
  DataflowData,
  DataflowEdge,
  Evidence,
  OverviewData,
  PartChunk,
  ViewEdgeData,
  ViewNodeData,
} from "../types/bundle";

export function readDataflowOverlay(): DataflowData | null {
  if (typeof window === "undefined") return null;
  return window.__ARCH_DATAFLOW__ ?? null;
}

export function formatDataflowEdgeLabel(edge: DataflowEdge): string {
  const hint = (edge.payload_hint || "").trim();
  if (!hint) return "dataflow";
  return hint.length > 40 ? `dataflow: ${hint.slice(0, 37)}...` : `dataflow: ${hint}`;
}

export function resolveDataflowSourceLocation(
  edge: DataflowEdge,
  overview: OverviewData | null,
  chunks: Record<string, PartChunk>,
): { partId?: string; groupId?: string; filePath?: string } {
  const firstFile = edge.path[0]?.file ?? edge.evidence[0]?.file;
  if (overview) {
    const flowEntry = overview.flow_index.find((f) => f.entry_id === edge.entry_id);
    if (flowEntry) {
      const chunk = chunks[flowEntry.part_id];
      const fileRow = firstFile ? chunk?.files.find((f) => f.path === firstFile) : undefined;
      const chunkNode = chunk?.nodes.find(
        (n) => n.id === edge.entry_id || n.symbol === edge.source_fqname,
      );
      return {
        partId: flowEntry.part_id,
        groupId: chunkNode?.module_id ?? fileRow?.module_id,
        filePath: firstFile ?? chunkNode?.file,
      };
    }
  }

  for (const [pid, chunk] of Object.entries(chunks)) {
    const node = chunk.nodes.find(
      (n) => n.id === edge.entry_id || n.symbol === edge.source_fqname,
    );
    if (node) {
      return {
        partId: pid,
        groupId: node.module_id,
        filePath: firstFile ?? node.file,
      };
    }
    if (firstFile) {
      const fileRow = chunk.files.find((f) => f.path === firstFile);
      if (fileRow) {
        return {
          partId: pid,
          groupId: fileRow.module_id,
          filePath: firstFile,
        };
      }
    }
  }

  return { filePath: firstFile };
}

export function matchDataflowForNode(
  dataflow: DataflowData | null,
  node: ViewNodeData,
  overview: OverviewData | null,
  chunks: Record<string, PartChunk>,
): DataflowEdge[] {
  if (!dataflow || !Array.isArray(dataflow.edges)) return [];
  const symPrefix = `sym:${node.id}`;
  return dataflow.edges.filter((df) => {
    if (
      df.entry_id === node.id ||
      df.source_fqname === node.id ||
      `sym:${df.source_fqname}` === node.id ||
      symPrefix === df.source_fqname ||
      df.sink_boundary_id === node.id
    ) {
      return true;
    }
    if (df.path.some((s) => s.fqname === node.id || (s.fqname && `sym:${s.fqname}` === node.id))) {
      return true;
    }
    if (node.kind === "file" && node.filePath) {
      return df.path.some((s) => s.file === node.filePath);
    }
    if (node.kind === "group" && node.groupId) {
      const loc = resolveDataflowSourceLocation(df, overview, chunks);
      return loc.groupId === node.groupId;
    }
    if (node.partId && node.id === node.partId) {
      const loc = resolveDataflowSourceLocation(df, overview, chunks);
      return loc.partId === node.partId;
    }
    return false;
  });
}

export function matchDataflowForEdge(
  dataflow: DataflowData | null,
  edge: ViewEdgeData,
  overview: OverviewData | null,
  chunks: Record<string, PartChunk>,
): DataflowEdge[] {
  if (!dataflow || !Array.isArray(dataflow.edges)) return [];
  if (!edge.isDataflow && !edge.kinds.includes("dataflow")) return [];
  return dataflow.edges.filter((df) => {
    if (df.sink_boundary_id !== edge.target) return false;
    if (
      df.entry_id === edge.source ||
      df.source_fqname === edge.source ||
      `sym:${df.source_fqname}` === edge.source
    ) {
      return true;
    }
    const loc = resolveDataflowSourceLocation(df, overview, chunks);
    return (
      loc.partId === edge.source ||
      loc.groupId === edge.source ||
      (loc.filePath !== undefined && `file:${loc.filePath}` === edge.source)
    );
  });
}

function buildStepHref(file: string, line: number, template?: string): string | null {
  if (!template) return null;
  return template
    .replace("{abs_path}", file)
    .replace("{file}", file)
    .replace("{line}", String(line));
}

export function DataFlowPathsSection({
  edges,
  editorUrl,
  onCopyCitation,
  copiedCitation,
}: {
  edges: DataflowEdge[];
  editorUrl?: string;
  onCopyCitation?: (cite: string) => void;
  copiedCitation?: string | null;
}) {
  if (edges.length === 0) return null;

  return (
    <div className="arch-detail-block" data-testid="dataflow-paths-section">
      <div className="arch-detail-label">Dataflow Paths ({edges.length})</div>
      <div className="arch-dataflow-steps">
        {edges.map((df, idx) => (
          <div key={`${df.entry_id}-${df.sink_boundary_id}-${idx}`} className="arch-dataflow-steps__card">
            <div className="arch-dataflow-steps__summary">
              <span className="arch-dataflow-steps__hint" title={df.payload_hint || df.source_fqname}>
                {df.payload_hint || df.source_fqname} &rarr; {df.sink_boundary_id}
              </span>
              <span className="arch-badge arch-badge--heuristic">
                {df.confidence === "heuristic" ? "approx" : df.confidence}
              </span>
            </div>
            <ol className="arch-dataflow-steps__list">
              {df.path.map((step, sIdx) => {
                const cite = `${step.file}:${step.line}`;
                const href = buildStepHref(step.file, step.line, editorUrl);
                return (
                  <li key={`${cite}-${sIdx}`} className="arch-dataflow-steps__item">
                    <div className="arch-dataflow-steps__row">
                      {href ? (
                        <a href={href} className="arch-dataflow-steps__cite">
                          {cite}
                        </a>
                      ) : (
                        <span className="arch-dataflow-steps__cite">{cite}</span>
                      )}
                      {onCopyCitation && (
                        <button
                          type="button"
                          className="arch-mini-btn"
                          onClick={() => onCopyCitation(cite)}
                        >
                          {copiedCitation === cite ? "Copied" : "Copy"}
                        </button>
                      )}
                    </div>
                    {(step.fqname || step.note) && (
                      <div className="arch-dataflow-steps__note" title={step.note || step.fqname || ""}>
                        {step.fqname ? `${step.fqname}` : ""}
                        {step.fqname && step.note ? " — " : ""}
                        {step.note || ""}
                      </div>
                    )}
                  </li>
                );
              })}
            </ol>
          </div>
        ))}
      </div>
    </div>
  );
}

export function DataFlowLayer({
  dataflow,
  visible,
  onSelectEntry,
}: {
  dataflow: DataflowData | null;
  visible: boolean;
  onSelectEntry: (entryId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  const edges = useMemo(() => dataflow?.edges ?? [], [dataflow]);

  if (!dataflow || edges.length === 0 || !visible) {
    return null;
  }

  return (
    <div className="arch-dataflow-layer" data-testid="dataflow-layer-panel">
      <button
        type="button"
        className={`arch-dataflow-layer__toggle ${expanded ? "arch-dataflow-layer__toggle--active" : ""}`}
        onClick={() => setExpanded((prev) => !prev)}
      >
        <span>Data-Flow ({edges.length})</span>
        <span className="arch-badge">{dataflow.tool}</span>
      </button>

      {expanded && (
        <div className="arch-dataflow-layer__panel" role="region" aria-label="Dataflow edges">
          <div className="arch-dataflow-layer__header">
            <strong>
              Dataflow Paths ({edges.length}) &middot; {dataflow.tool} {dataflow.tool_version}
            </strong>
            <button
              type="button"
              className="arch-icon-btn"
              onClick={() => setExpanded(false)}
              aria-label="Close dataflow panel"
            >
              &times;
            </button>
          </div>
          <div className="arch-dataflow-layer__list">
            {edges.map((df: DataflowEdge, idx: number) => {
              const firstEv: Evidence | undefined = df.evidence[0];
              return (
                <button
                  key={`${df.entry_id}-${df.sink_boundary_id}-${idx}`}
                  type="button"
                  className="arch-dataflow-layer__item"
                  onClick={() => onSelectEntry(df.entry_id)}
                >
                  <span className="arch-dataflow-layer__item-main">
                    <span className="arch-dataflow-layer__item-title">
                      {df.payload_hint || df.source_fqname} &rarr; {df.sink_boundary_id}
                    </span>
                    <span className="arch-dataflow-layer__item-sub">
                      {df.entry_id}
                      {firstEv ? ` · ${firstEv.file}:${firstEv.line}` : ""}
                    </span>
                  </span>
                  <span className="arch-badge">{df.path.length} steps</span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
