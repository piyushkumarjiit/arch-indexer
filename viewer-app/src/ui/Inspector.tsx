import { useMemo, useState } from "react";
import { layerForEdgeKind } from "../data/layers";
import type {
  ChunkNode,
  DataflowData,
  DescriptionsData,
  Evidence,
  FlowIndexEntry,
  OverviewData,
  PartChunk,
  RuntimeOverlayData,
  ViewEdgeData,
  ViewNodeData,
} from "../types/bundle";
import { ChangeBadge, getFileChangeStatus } from "./ChangeBadges";

function formatEvidenceCitation(ev: Evidence): string {
  return `${ev.file}:${ev.line}`;
}

export const ALLOWED_EDITOR_URL_SCHEMES = new Set([
  "vscode",
  "vscode-insiders",
  "cursor",
  "idea",
  "jetbrains",
  "zed",
  "subl",
  "http",
  "https",
  "file",
]);

const CONTROL_CHAR_RE = /[\u0000-\u001f\u007f]/;
const URL_SCHEME_RE = /^([A-Za-z][A-Za-z0-9+.-]*):/;

function hasAllowedEditorScheme(url: string): boolean {
  if (CONTROL_CHAR_RE.test(url)) return false;
  const match = URL_SCHEME_RE.exec(url);
  if (!match) return false;
  return ALLOWED_EDITOR_URL_SCHEMES.has(match[1].toLowerCase());
}

export function buildEditorHref(ev: Evidence, template?: string): string | null {
  if (!template) return null;
  const trimmed = template.trim();
  if (!trimmed || !hasAllowedEditorScheme(trimmed)) return null;
  const href = trimmed.replaceAll(/\{(abs_path|file|line)\}/g, (token) =>
    token === "{line}" ? String(ev.line) : ev.file,
  );
  if (!hasAllowedEditorScheme(href)) return null;
  return href;
}

function resolveFallbackNode(
  nodeId: string | null,
  overview: OverviewData | null,
  chunks: Record<string, PartChunk>,
): ViewNodeData | null {
  if (!nodeId || !overview) return null;
  const part = overview.parts.find((p) => p.id === nodeId);
  if (part) {
    return {
      id: part.id,
      label: part.name,
      kind: part.kind,
      partId: part.id,
      drillable: true,
      evidence: part.evidence,
    };
  }
  const grp = overview.groups.find((g) => g.id === nodeId);
  if (grp) {
    return {
      id: grp.id,
      label: grp.name,
      kind: "group",
      tier: grp.tier,
      partId: grp.part_id,
      groupId: grp.id,
      drillable: true,
    };
  }
  const boundary = overview.boundaries.find((b) => b.id === nodeId);
  if (boundary) {
    return {
      id: boundary.id,
      label: boundary.label,
      kind: boundary.boundary,
      badge: boundary.kind,
      confidence: boundary.confidence,
      drillable: false,
      evidence: boundary.evidence,
    };
  }
  for (const [pid, chunk] of Object.entries(chunks)) {
    const cn = chunk.nodes.find((n) => n.id === nodeId);
    if (cn) {
      return {
        id: cn.id,
        label: cn.label,
        kind: cn.kind,
        badge: cn.tier,
        tier: cn.tier,
        confidence: cn.confidence,
        drillable: true,
        partId: pid,
        groupId: cn.module_id,
        filePath: cn.file,
        symbolId: cn.id,
        evidence: cn.evidence,
      };
    }
  }
  return null;
}

export function Inspector({
  open,
  overview,
  chunks,
  selectedNode,
  selectedNodeId,
  selectedEdge,
  visibleEdges,
  descriptions,
  runtime,
  dataflow,
  onClose,
  onSelectNode,
  onDrillNode,
  onStartFlow,
}: {
  open: boolean;
  overview: OverviewData | null;
  chunks: Record<string, PartChunk>;
  selectedNode: ViewNodeData | null;
  selectedNodeId?: string | null;
  selectedEdge: ViewEdgeData | null;
  visibleEdges: ViewEdgeData[];
  descriptions: DescriptionsData | null;
  runtime: RuntimeOverlayData | null;
  dataflow: DataflowData | null;
  onClose: () => void;
  onSelectNode: (nodeId: string) => void;
  onDrillNode?: (node: ViewNodeData) => void;
  onStartFlow: (flowId: string, partId?: string) => void;
}) {
  const [copiedCitation, setCopiedCitation] = useState<string | null>(null);
  const [foldedFilter, setFoldedFilter] = useState("");

  const resolvedNode = useMemo(
    () => selectedNode ?? resolveFallbackNode(selectedNodeId ?? null, overview, chunks),
    [selectedNode, selectedNodeId, overview, chunks],
  );

  const foldedItems = useMemo(() => {
    const ids = resolvedNode?.foldedIds ?? [];
    if (ids.length === 0) return [];
    const labelById = new Map<string, string>();
    if (overview) {
      for (const p of overview.parts) labelById.set(p.id, p.name);
      for (const g of overview.groups) labelById.set(g.id, g.name);
      for (const b of overview.boundaries) labelById.set(b.id, b.label);
    }
    for (const c of Object.values(chunks)) {
      for (const n of c.nodes) labelById.set(n.id, n.label);
    }
    const q = foldedFilter.trim().toLowerCase();
    return ids
      .map((id) => ({ id, label: labelById.get(id) ?? id }))
      .filter((item) => !q || item.label.toLowerCase().includes(q) || item.id.toLowerCase().includes(q));
  }, [resolvedNode?.foldedIds, overview, chunks, foldedFilter]);

  if (!open) return null;

  const copyCitation = async (text: string) => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      }
      setCopiedCitation(text);
      setTimeout(() => setCopiedCitation((cur) => (cur === text ? null : cur)), 1500);
    } catch {
      setCopiedCitation(text);
    }
  };

  if (selectedEdge && !resolvedNode) {
    const kindEntries = Object.entries(
      selectedEdge.kindCounts && Object.keys(selectedEdge.kindCounts).length > 0
        ? selectedEdge.kindCounts
        : Object.fromEntries(selectedEdge.kinds.map((k) => [k, selectedEdge.kinds.length === 1 ? selectedEdge.count : 1])),
    ).sort((a, b) => a[0].localeCompare(b[0]));
    const runtimeEdgeKey = selectedEdge.id.endsWith("#runtime")
      ? selectedEdge.id.slice(0, -"#runtime".length)
      : selectedEdge.id;
    const runtimeEdgeMetric = runtime?.edge_metrics?.[runtimeEdgeKey] ?? null;

    return (
      <aside className="arch-inspector" aria-label="Edge inspector" data-testid="edge-inspector">
        <div className="arch-inspector-header">
          <strong>Edge Inspector</strong>
          <button type="button" className="arch-icon-btn" onClick={onClose} aria-label="Close inspector">
            &times;
          </button>
        </div>
        <div className="arch-inspector-body">
          <div className="arch-detail-block">
            <div className="arch-detail-label">Connection</div>
            <div className="arch-detail-mono">
              {selectedEdge.source} &rarr; {selectedEdge.target}
            </div>
          </div>
          <div className="arch-detail-block">
            <div className="arch-detail-label">
              Kinds &amp; Layer Breakdown ({selectedEdge.count} total)
            </div>
            <div className="arch-tag-row">
              {kindEntries.map(([kind, cnt]) => {
                const layer = layerForEdgeKind(kind);
                return (
                  <span key={kind} className={`arch-badge arch-badge--layer-${layer}`}>
                    {kind}: {cnt}
                  </span>
                );
              })}
              {selectedEdge.confidence && (
                <span className={`arch-badge arch-badge--${selectedEdge.confidence}`}>
                  {selectedEdge.confidence === "heuristic" ? "approx (heuristic)" : selectedEdge.confidence}
                </span>
              )}
              {selectedEdge.runtimeOnly && (
                <span className="arch-badge arch-badge--layer-runtime" data-testid="runtime-only-edge-badge">
                  runtime_only
                </span>
              )}
            </div>
          </div>
          {runtimeEdgeMetric && (
            <div className="arch-detail-block" data-testid="edge-runtime-section">
              <div className="arch-detail-label">
                Runtime Edge{" "}
                <span className="arch-badge arch-badge--layer-runtime">
                  {runtimeEdgeMetric.match_tier}
                </span>
              </div>
              <div className="arch-detail-text">
                {runtimeEdgeMetric.span_count} spans &middot; {runtimeEdgeMetric.error_count} errors &middot; p95{" "}
                {runtimeEdgeMetric.p95_ms} ms
              </div>
            </div>
          )}
          {selectedEdge.evidence && selectedEdge.evidence.length > 0 && (
            <div className="arch-detail-block">
              <div className="arch-detail-label">Evidence ({selectedEdge.evidence.length})</div>
              <div className="arch-evidence-list">
                {selectedEdge.evidence.map((ev, idx) => {
                  const cite = formatEvidenceCitation(ev);
                  const href = buildEditorHref(ev, overview?.editor_url);
                  return (
                    <div key={`${cite}-${idx}`} className="arch-evidence-row">
                      {href ? (
                        <a href={href} className="arch-evidence-cite">{cite}</a>
                      ) : (
                        <span className="arch-evidence-cite">{cite}</span>
                      )}
                      <button
                        type="button"
                        className="arch-mini-btn"
                        onClick={() => copyCitation(cite)}
                      >
                        {copiedCitation === cite ? "Copied" : "Copy"}
                      </button>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      </aside>
    );
  }

  if (!resolvedNode) return null;

  const desc = descriptions?.entries?.[resolvedNode.id] ?? null;
  const runtimeMetric = runtime?.node_metrics?.[resolvedNode.id] ?? resolvedNode.metrics ?? null;

  let chunkNode: ChunkNode | undefined;
  for (const c of Object.values(chunks)) {
    const found = c.nodes.find((n) => n.id === resolvedNode.id);
    if (found) {
      chunkNode = found;
      break;
    }
  }

  const incoming = visibleEdges.filter((e) => e.target === resolvedNode.id);
  const outgoing = visibleEdges.filter((e) => e.source === resolvedNode.id);

  const flowsThrough: Array<{ id: string; partId?: string; label: string; endReason: string }> = [];
  if (overview) {
    const addedFlowIds = new Set<string>();
    for (const chunk of Object.values(chunks)) {
      for (const fl of chunk.flows) {
        if (
          fl.entry_id === resolvedNode.id ||
          fl.ends_at === resolvedNode.id ||
          fl.steps.some((s) => s.node_id === resolvedNode.id)
        ) {
          addedFlowIds.add(fl.id);
          flowsThrough.push({
            id: fl.id,
            partId: chunk.part_id,
            label: `${fl.entry_id} (${fl.steps.length} steps)`,
            endReason: fl.end_reason,
          });
        }
      }
    }
    for (const fi of overview.flow_index as FlowIndexEntry[]) {
      if (addedFlowIds.has(fi.id)) continue;
      if (fi.entry_id === resolvedNode.id || fi.ends_at === resolvedNode.id || fi.part_id === resolvedNode.id) {
        flowsThrough.push({
          id: fi.id,
          partId: fi.part_id,
          label: `${fi.entry_id} (${fi.step_count} steps)`,
          endReason: fi.end_reason,
        });
      }
    }
  }

  const relatedDataflow =
    dataflow?.edges?.filter(
      (d) =>
        d.entry_id === resolvedNode.id ||
        d.source_fqname === resolvedNode.id ||
        d.sink_boundary_id === resolvedNode.id,
    ) ?? [];

  const fileChange = getFileChangeStatus(
    resolvedNode.filePath ?? resolvedNode.evidence?.[0]?.file,
    overview?.delta,
  );
  const part = resolvedNode.partId
    ? overview?.parts.find((p) => p.id === resolvedNode.partId)
    : undefined;
  const group = resolvedNode.groupId
    ? overview?.groups.find((g) => g.id === resolvedNode.groupId)
    : undefined;

  return (
    <aside className="arch-inspector" aria-label="Node inspector" data-testid="inspector">
      <div className="arch-inspector-header">
        <strong>Inspector</strong>
        <div className="arch-inspector-header-actions">
          {resolvedNode.drillable && onDrillNode && (
            <button
              type="button"
              className="arch-mini-btn arch-btn--primary"
              data-testid="inspector-drill-btn"
              onClick={() => onDrillNode(resolvedNode)}
            >
              Drill in &rarr;
            </button>
          )}
          <button type="button" className="arch-icon-btn" onClick={onClose} aria-label="Close inspector">
            &times;
          </button>
        </div>
      </div>
      <div className="arch-inspector-body">
        <div className="arch-detail-block">
          <div className="arch-inspector-title">{resolvedNode.label}</div>
          <div className="arch-detail-mono">{resolvedNode.id}</div>
          <div className="arch-tag-row">
            <span className="arch-badge">{resolvedNode.kind}</span>
            {resolvedNode.tier && (
              <span className={`arch-badge arch-badge--tier-${resolvedNode.tier}`}>
                {resolvedNode.tier}
              </span>
            )}
            {resolvedNode.confidence && (
              <span className={`arch-badge arch-badge--${resolvedNode.confidence}`}>
                {resolvedNode.confidence === "heuristic"
                  ? "approx (heuristic)"
                  : resolvedNode.confidence}
              </span>
            )}
            <ChangeBadge status={resolvedNode.changeStatus ?? fileChange} />
          </div>
        </div>

        {resolvedNode.kind === "fold" && resolvedNode.foldedIds && resolvedNode.foldedIds.length > 0 && (
          <div className="arch-detail-block" data-testid="folded-items-section">
            <div className="arch-detail-label">
              Folded Items ({resolvedNode.foldedIds.length})
            </div>
            {resolvedNode.foldedIds.length > 6 && (
              <input
                type="text"
                className="arch-folded-filter"
                placeholder="Filter folded items..."
                value={foldedFilter}
                onChange={(e) => setFoldedFilter(e.target.value)}
              />
            )}
            <div className="arch-folded-list">
              {foldedItems.length === 0 ? (
                <span className="arch-detail-muted">No folded items match</span>
              ) : (
                foldedItems.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className="arch-link-item"
                    onClick={() => onSelectNode(item.id)}
                  >
                    <span className="arch-link-item__label">{item.label}</span>
                    <span className="arch-badge">Jump</span>
                  </button>
                ))
              )}
            </div>
          </div>
        )}

        {(part || group) && (
          <div className="arch-detail-block">
            <div className="arch-detail-label">Part / Group</div>
            <div className="arch-detail-text">
              {part ? part.name : "—"} {group ? `/ ${group.name}` : ""}
            </div>
          </div>
        )}

        {(desc || chunkNode?.docstring) && (
          <div className="arch-detail-block">
            <div className="arch-detail-label">
              Description
              {desc?.status === "stale" && (
                <span className="arch-badge arch-badge--stale" style={{ marginLeft: 6 }}>
                  stale
                </span>
              )}
            </div>
            {desc ? (
              <div className={`arch-description ${desc.status === "stale" ? "is-stale" : ""}`}>
                <p>{desc.summary}</p>
                {desc.details && <p className="arch-detail-muted">{desc.details}</p>}
              </div>
            ) : (
              <pre className="arch-docstring">{chunkNode?.docstring}</pre>
            )}
          </div>
        )}

        {runtimeMetric && (
          <div className="arch-detail-block" data-testid="inspector-runtime-section">
            <div className="arch-detail-label">
              Runtime{" "}
              <span
                className="arch-badge arch-badge--layer-runtime"
                data-testid="inspector-runtime-tier"
              >
                {runtimeMetric.match_tier}
              </span>
            </div>
            <div className="arch-detail-text">
              {runtimeMetric.span_count} spans &middot; {runtimeMetric.error_count} errors &middot; p50{" "}
              {runtimeMetric.p50_ms} ms &middot; p95 {runtimeMetric.p95_ms} ms
            </div>
            {runtimeMetric.top_exceptions && runtimeMetric.top_exceptions.length > 0 && (
              <div className="arch-links-list" data-testid="inspector-top-exceptions">
                {runtimeMetric.top_exceptions.map(([excType, cnt]) => (
                  <div key={excType} className="arch-evidence-row">
                    <span className="arch-detail-mono">{excType}</span>
                    <span className="arch-badge arch-badge--rt-error">{cnt}</span>
                  </div>
                ))}
              </div>
            )}
            {runtimeMetric.error_frames && runtimeMetric.error_frames.length > 0 && (
              <div className="arch-evidence-list" data-testid="inspector-error-frames">
                {runtimeMetric.error_frames.map((fr, idx) => {
                  const ev: Evidence = {
                    file: fr.file,
                    line: fr.line,
                    source: "runtime",
                  };
                  const cite = formatEvidenceCitation(ev);
                  const href = buildEditorHref(ev, overview?.editor_url);
                  return (
                    <div key={`${cite}-${idx}`} className="arch-evidence-row">
                      {href ? (
                        <a href={href} className="arch-evidence-cite">{cite}</a>
                      ) : (
                        <span className="arch-evidence-cite">{cite}</span>
                      )}
                      <button
                        type="button"
                        className="arch-mini-btn"
                        onClick={() => copyCitation(cite)}
                      >
                        {copiedCitation === cite ? "Copied" : "Copy"}
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}

        {relatedDataflow.length > 0 && (
          <div className="arch-detail-block">
            <div className="arch-detail-label">Dataflow Paths ({relatedDataflow.length})</div>
            <div className="arch-links-list">
              {relatedDataflow.map((df, i) => (
                <div key={`${df.entry_id}-${i}`} className="arch-detail-mono">
                  {df.payload_hint || df.source_fqname} &rarr; {df.sink_boundary_id}
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="arch-detail-block">
          <div className="arch-detail-label">Incoming ({incoming.length})</div>
          <div className="arch-links-list">
            {incoming.length === 0 ? (
              <span className="arch-detail-muted">None in current view</span>
            ) : (
              incoming.map((e) => (
                <button
                  key={e.id}
                  type="button"
                  className="arch-link-item"
                  onClick={() => onSelectNode(e.source)}
                >
                  <span>&larr; {e.source}</span>
                  <span className="arch-badge">{e.kinds.join(", ")}</span>
                </button>
              ))
            )}
          </div>
        </div>

        <div className="arch-detail-block">
          <div className="arch-detail-label">Outgoing ({outgoing.length})</div>
          <div className="arch-links-list">
            {outgoing.length === 0 ? (
              <span className="arch-detail-muted">None in current view</span>
            ) : (
              outgoing.map((e) => (
                <button
                  key={e.id}
                  type="button"
                  className="arch-link-item"
                  onClick={() => onSelectNode(e.target)}
                >
                  <span>&rarr; {e.target}</span>
                  <span className="arch-badge">{e.kinds.join(", ")}</span>
                </button>
              ))
            )}
          </div>
        </div>

        {flowsThrough.length > 0 && (
          <div className="arch-detail-block">
            <div className="arch-detail-label">Flows ({flowsThrough.length})</div>
            <div className="arch-links-list">
              {flowsThrough.map((fl) => (
                <button
                  key={fl.id}
                  type="button"
                  className="arch-link-item"
                  onClick={() => onStartFlow(fl.id, fl.partId)}
                >
                  <span>{fl.label}</span>
                  <span className="arch-badge">{fl.endReason}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        <div className="arch-detail-block">
          <div className="arch-detail-label">
            Evidence ({resolvedNode.evidence?.length ?? 0})
          </div>
          <div className="arch-evidence-list">
            {!resolvedNode.evidence || resolvedNode.evidence.length === 0 ? (
              <span className="arch-detail-muted">No direct evidence</span>
            ) : (
              resolvedNode.evidence.map((ev, idx) => {
                const cite = formatEvidenceCitation(ev);
                const href = buildEditorHref(ev, overview?.editor_url);
                return (
                  <div key={`${cite}-${idx}`} className="arch-evidence-row">
                    {href ? (
                      <a href={href} className="arch-evidence-cite">
                        {cite}
                      </a>
                    ) : (
                      <span className="arch-evidence-cite">{cite}</span>
                    )}
                    <button
                      type="button"
                      className="arch-mini-btn"
                      onClick={() => copyCitation(cite)}
                    >
                      {copiedCitation === cite ? "Copied" : "Copy"}
                    </button>
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>
    </aside>
  );
}
