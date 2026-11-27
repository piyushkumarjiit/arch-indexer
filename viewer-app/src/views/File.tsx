import { useMemo } from "react";
import { useViewerStore } from "../state/store";
import { GraphCanvas } from "./GraphCanvas";
import { buildFileGraph } from "./viewGraphs";

export function FileView() {
  const overview = useViewerStore((s) => s.overview);
  const partId = useViewerStore((s) => s.partId);
  const groupId = useViewerStore((s) => s.groupId);
  const filePath = useViewerStore((s) => s.filePath);
  const chunks = useViewerStore((s) => s.chunks);
  const layers = useViewerStore((s) => s.layers);
  const exactOnly = useViewerStore((s) => s.exactOnly);
  const runtime = useViewerStore((s) => s.runtime);
  const dataflow = useViewerStore((s) => s.dataflow);
  const activeFlow = useViewerStore((s) => s.activeFlow);
  const flowStepIndex = useViewerStore((s) => s.flowStepIndex);
  const selectedNodeId = useViewerStore((s) => s.selectedNodeId);
  const selectedEdgeId = useViewerStore((s) => s.selectedEdgeId);
  const selectNode = useViewerStore((s) => s.selectNode);
  const selectEdge = useViewerStore((s) => s.selectEdge);
  const drillToSymbol = useViewerStore((s) => s.drillToSymbol);

  const graph = useMemo(() => {
    if (!overview || !partId || !groupId || !filePath) {
      return { nodes: [], edges: [], foldedCount: 0 };
    }
    return buildFileGraph(partId, groupId, filePath, {
      overview,
      chunks,
      layers,
      exactOnly,
      runtime,
      dataflow,
      activeFlow,
      flowStepIndex,
    });
  }, [
    overview,
    partId,
    groupId,
    filePath,
    chunks,
    layers,
    exactOnly,
    runtime,
    dataflow,
    activeFlow,
    flowStepIndex,
  ]);

  if (!overview || !partId || !groupId || !filePath) return null;

  return (
    <div className="arch-view-shell">
      <div className="arch-band-bar">
        <span>File: {filePath}</span>
        {graph.foldedCount > 0 && (
          <span className="arch-badge">+{graph.foldedCount} folded</span>
        )}
      </div>
      <GraphCanvas
        viewKey={`file:${filePath}`}
        nodes={graph.nodes}
        edges={graph.edges}
        layers={layers}
        selectedNodeId={selectedNodeId}
        selectedEdgeId={selectedEdgeId}
        onSelectNode={(id) => selectNode(id)}
        onSelectEdge={(id) => selectEdge(id)}
        onDrillNode={(node) => {
          if (node.symbolId && node.filePath) {
            void drillToSymbol(partId, node.groupId ?? groupId, node.filePath, node.symbolId);
          }
        }}
      />
    </div>
  );
}
