import { useState } from "react";
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

function buildEditorHref(ev: Evidence, template?: string): string | null {
  if (!template) return null;
  return template
    .replace("{abs_path}", ev.file)
    .replace("{file}", ev.file)
    .replace("{line}", String(ev.line));
}

export function Inspector({
  open,
  overview,
  chunks,
  selectedNode,
  selectedEdge,
  visibleEdges,
  descriptions,
  runtime,
  dataflow,
  onClose,
  onSelectNode,
  onStartFlow,
}: {
  open: boolean;
  overview: OverviewData | null;
  chunks: Record<string, PartChunk>;
  selectedNode: ViewNodeData | null;
  selectedEdge: ViewEdgeData | null;
  visibleEdges: ViewEdgeData[];
  descriptions: DescriptionsData | null;
  runtime: RuntimeOverlayData | null;
  dataflow: DataflowData | null;
  onClose: () => void;
  onSelectNode: (nodeId: string) => void;
  onStartFlow: (flowId: string, partId?: string) => void;
}) {
  const [copiedCitation, setCopiedCitation] = useState<string | null>(null);

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

  if (selectedEdge && !selectedNode) {
    return (
      <aside className="arch-inspector" aria-label="Edge inspector">
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
            <div className="arch-detail-label">Kinds & Confidence</div>
            <div className="arch-tag-row">
              {selectedEdge.kinds.map((k) => (
                <span key={k} className="arch-badge">{k}</span>
              ))}
              {selectedEdge.confidence && (
                <span className={`arch-badge arch-badge--${selectedEdge.confidence}`}>
                  {selectedEdge.confidence === "heuristic" ? "approx (heuristic)" : selectedEdge.confidence}
                </span>
              )}
            </div>
          </div>
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

  if (!selectedNode) return null;

  const desc = descriptions?.entries?.[selectedNode.id] ?? null;
  const runtimeMetric = runtime?.node_metrics?.[selectedNode.id] ?? selectedNode.metrics ?? null;

  // Locate chunk node for docstring if available
  let chunkNode: ChunkNode | undefined;
  for (const c of Object.values(chunks)) {
    const found = c.nodes.find((n) => n.id === selectedNode.id);
    if (found) {
      chunkNode = found;
      break;
    }
  }

  const incoming = visibleEdges.filter((e) => e.target === selectedNode.id);
  const outgoing = visibleEdges.filter((e) => e.source === selectedNode.id);

  // Flows passing through this node
  const flowsThrough: Array<{ id: string; partId?: string; label: string; endReason: string }> = [];
  if (overview) {
    const addedFlowIds = new Set<string>();
    for (const chunk of Object.values(chunks)) {
      for (const fl of chunk.flows) {
        if (
          fl.entry_id === selectedNode.id ||
          fl.ends_at === selectedNode.id ||
          fl.steps.some((s) => s.node_id === selectedNode.id)
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
      if (fi.entry_id === selectedNode.id || fi.ends_at === selectedNode.id || fi.part_id === selectedNode.id) {
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
        d.entry_id === selectedNode.id ||
        d.source_fqname === selectedNode.id ||
        d.sink_boundary_id === selectedNode.id,
    ) ?? [];

  const fileChange = getFileChangeStatus(
    selectedNode.filePath ?? selectedNode.evidence?.[0]?.file,
    overview?.delta,
  );
  const part = selectedNode.partId
    ? overview?.parts.find((p) => p.id === selectedNode.partId)
    : undefined;
  const group = selectedNode.groupId
    ? overview?.groups.find((g) => g.id === selectedNode.groupId)
    : undefined;

  return (
    <aside className="arch-inspector" aria-label="Node inspector" data-testid="inspector">
      <div className="arch-inspector-header">
        <strong>Inspector</strong>
        <button type="button" className="arch-icon-btn" onClick={onClose} aria-label="Close inspector">
          &times;
        </button>
      </div>
      <div className="arch-inspector-body">
        <div className="arch-detail-block">
          <div className="arch-inspector-title">{selectedNode.label}</div>
          <div className="arch-detail-mono">{selectedNode.id}</div>
          <div className="arch-tag-row">
            <span className="arch-badge">{selectedNode.kind}</span>
            {selectedNode.tier && (
              <span className={`arch-badge arch-badge--tier-${selectedNode.tier}`}>
                {selectedNode.tier}
              </span>
            )}
            {selectedNode.confidence && (
              <span className={`arch-badge arch-badge--${selectedNode.confidence}`}>
                {selectedNode.confidence === "heuristic"
                  ? "approx (heuristic)"
                  : selectedNode.confidence}
              </span>
            )}
            <ChangeBadge status={selectedNode.changeStatus ?? fileChange} />
          </div>
        </div>

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
          <div className="arch-detail-block">
            <div className="arch-detail-label">Runtime ({runtimeMetric.match_tier})</div>
            <div className="arch-detail-text">
              {runtimeMetric.span_count} spans · {runtimeMetric.error_count} errors · p95{" "}
              {runtimeMetric.p95_ms} ms
            </div>
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
            Evidence ({selectedNode.evidence?.length ?? 0})
          </div>
          <div className="arch-evidence-list">
            {!selectedNode.evidence || selectedNode.evidence.length === 0 ? (
              <span className="arch-detail-muted">No direct evidence</span>
            ) : (
              selectedNode.evidence.map((ev, idx) => {
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
