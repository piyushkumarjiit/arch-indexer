import { useMemo } from "react";
import { useViewerStore } from "../state/store";
import { GraphCanvas } from "./GraphCanvas";
import { buildPartGraph } from "./viewGraphs";

export function PartView() {
  const overview = useViewerStore((s) => s.overview);
  const partId = useViewerStore((s) => s.partId);
  const chunks = useViewerStore((s) => s.chunks);
  const layers = useViewerStore((s) => s.layers);
  const exactOnly = useViewerStore((s) => s.exactOnly);
  const runtime = useViewerStore((s) => s.runtime);
  const dataflow = useViewerStore((s) => s.dataflow);
  const activeFlow = useViewerStore((s) => s.activeFlow);
  const flowStepIndex = useViewerStore((s) => s.flowStepIndex);
  const selectedNodeId = useViewerStore((s) => s.selectedNodeId);
  const selectNode = useViewerStore((s) => s.selectNode);
  const selectEdge = useViewerStore((s) => s.selectEdge);
  const drillToPart = useViewerStore((s) => s.drillToPart);
  const drillToGroup = useViewerStore((s) => s.drillToGroup);

  const graph = useMemo(() => {
    if (!overview || !partId) return { nodes: [], edges: [], foldedCount: 0 };
    return buildPartGraph(partId, {
      overview,
      chunks,
      layers,
      exactOnly,
      runtime,
      dataflow,
      activeFlow,
      flowStepIndex,
    });
  }, [overview, partId, chunks, layers, exactOnly, runtime, dataflow, activeFlow, flowStepIndex]);

  if (!overview || !partId) return null;

  const usedByCount = graph.nodes.filter((n) => n.partition === 0).length;
  const insideCount = graph.nodes.filter((n) => n.partition === 1).length;
  const usesCount = graph.nodes.filter((n) => n.partition === 2).length;

  return (
    <div className="arch-view-shell">
      <div className="arch-band-bar" data-testid="part-bands">
        <span className="arch-band-pill">Used by: {usedByCount}</span>
        <span className="arch-band-pill is-primary">Inside: {insideCount}</span>
        <span className="arch-band-pill">Uses: {usesCount}</span>
      </div>
      <GraphCanvas
        viewKey={`part:${partId}`}
        nodes={graph.nodes}
        edges={graph.edges}
        selectedNodeId={selectedNodeId}
        onSelectNode={(id) => selectNode(id)}
        onSelectEdge={(id) => selectEdge(id)}
        onDrillNode={(node) => {
          if (node.groupId && node.partId) {
            void drillToGroup(node.partId, node.groupId);
          } else if (node.partId && node.partId !== partId) {
            void drillToPart(node.partId);
          }
        }}
      />
    </div>
  );
}
