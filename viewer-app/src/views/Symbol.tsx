import { useMemo } from "react";
import { useViewerStore } from "../state/store";
import { GraphCanvas } from "./GraphCanvas";
import { buildSymbolGraph } from "./viewGraphs";

export function SymbolView() {
  const overview = useViewerStore((s) => s.overview);
  const partId = useViewerStore((s) => s.partId);
  const groupId = useViewerStore((s) => s.groupId);
  const symbolId = useViewerStore((s) => s.symbolId);
  const chunks = useViewerStore((s) => s.chunks);
  const layers = useViewerStore((s) => s.layers);
  const exactOnly = useViewerStore((s) => s.exactOnly);
  const runtime = useViewerStore((s) => s.runtime);
  const dataflow = useViewerStore((s) => s.dataflow);
  const activeFlow = useViewerStore((s) => s.activeFlow);
  const flowStepIndex = useViewerStore((s) => s.flowStepIndex);
  const activeTraceId = useViewerStore((s) => s.activeTraceId);
  const traceSpanIndex = useViewerStore((s) => s.traceSpanIndex);
  const selectedNodeId = useViewerStore((s) => s.selectedNodeId);
  const selectedEdgeId = useViewerStore((s) => s.selectedEdgeId);
  const selectNode = useViewerStore((s) => s.selectNode);
  const selectEdge = useViewerStore((s) => s.selectEdge);
  const drillToSymbol = useViewerStore((s) => s.drillToSymbol);
  const startFlow = useViewerStore((s) => s.startFlow);

  const activeTrace = useMemo(
    () => (activeTraceId ? runtime?.traces.find((t) => t.trace_id === activeTraceId) ?? null : null),
    [runtime, activeTraceId],
  );

  const graph = useMemo(() => {
    if (!overview || !partId || !symbolId) return { nodes: [], edges: [], foldedCount: 0 };
    return buildSymbolGraph(partId, symbolId, {
      overview,
      chunks,
      layers,
      exactOnly,
      runtime,
      dataflow,
      activeFlow,
      flowStepIndex,
      activeTrace,
      traceSpanIndex,
    });
  }, [
    overview,
    partId,
    symbolId,
    chunks,
    layers,
    exactOnly,
    runtime,
    dataflow,
    activeFlow,
    flowStepIndex,
    activeTrace,
    traceSpanIndex,
  ]);

  if (!overview || !partId || !symbolId) return null;

  const chunk = chunks[partId];
  const flowsThrough = (chunk?.flows ?? []).filter(
    (f) => f.entry_id === symbolId || f.ends_at === symbolId || f.steps.some((s) => s.node_id === symbolId),
  );

  return (
    <div className="arch-view-shell">
      <div className="arch-band-bar">
        <span>Call tree (depth 3): {symbolId}</span>
        {flowsThrough.length > 0 && (
          <div className="arch-summary-flows">
            <span className="arch-toolbar-label">Flows:</span>
            {flowsThrough.map((fl) => (
              <button
                key={fl.id}
                type="button"
                className="arch-chip"
                onClick={() => void startFlow(fl.id, partId)}
              >
                {fl.entry_id} ({fl.end_reason})
              </button>
            ))}
          </div>
        )}
      </div>
      <GraphCanvas
        viewKey={`symbol:${symbolId}`}
        nodes={graph.nodes}
        edges={graph.edges}
        layers={layers}
        selectedNodeId={selectedNodeId}
        selectedEdgeId={selectedEdgeId}
        onSelectNode={(id) => selectNode(id)}
        onSelectEdge={(id) => selectEdge(id)}
        onDrillNode={(node) => {
          if (node.symbolId && node.filePath) {
            void drillToSymbol(
              partId,
              node.groupId ?? groupId ?? "",
              node.filePath,
              node.symbolId,
            );
          }
        }}
      />
    </div>
  );
}
