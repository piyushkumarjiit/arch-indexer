import { useMemo } from "react";
import { useViewerStore } from "../state/store";
import { GraphCanvas } from "./GraphCanvas";
import { buildGroupGraph } from "./viewGraphs";

export function GroupView() {
  const overview = useViewerStore((s) => s.overview);
  const partId = useViewerStore((s) => s.partId);
  const groupId = useViewerStore((s) => s.groupId);
  const dirFilter = useViewerStore((s) => s.dirFilter);
  const chunks = useViewerStore((s) => s.chunks);
  const loadingPartId = useViewerStore((s) => s.loadingPartId);
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
  const drillToGroup = useViewerStore((s) => s.drillToGroup);
  const drillToFile = useViewerStore((s) => s.drillToFile);

  const graph = useMemo(() => {
    if (!overview || !partId || !groupId) return { nodes: [], edges: [], foldedCount: 0 };
    return buildGroupGraph(partId, groupId, dirFilter, {
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
    dirFilter,
    chunks,
    layers,
    exactOnly,
    runtime,
    dataflow,
    activeFlow,
    flowStepIndex,
  ]);

  if (!overview || !partId || !groupId) return null;
  if (loadingPartId === partId && !chunks[partId]) {
    return <div className="arch-empty-canvas">Loading part chunk...</div>;
  }

  return (
    <div className="arch-view-shell">
      {dirFilter && (
        <div className="arch-band-bar">
          <span>Directory: {dirFilter}</span>
          <button
            type="button"
            className="arch-mini-btn"
            onClick={() => void drillToGroup(partId, groupId, null)}
          >
            All directories
          </button>
        </div>
      )}
      <GraphCanvas
        viewKey={`group:${groupId}:${dirFilter ?? "all"}`}
        nodes={graph.nodes}
        edges={graph.edges}
        layers={layers}
        selectedNodeId={selectedNodeId}
        selectedEdgeId={selectedEdgeId}
        onSelectNode={(id) => selectNode(id)}
        onSelectEdge={(id) => selectEdge(id)}
        onDrillNode={(node) => {
          if (node.kind === "dir_chip" && node.filePath) {
            void drillToGroup(partId, groupId, node.filePath);
          } else if ((node.kind === "file" || node.kind === "infra_file") && node.filePath) {
            void drillToFile(partId, groupId, node.filePath);
          }
        }}
      />
    </div>
  );
}
