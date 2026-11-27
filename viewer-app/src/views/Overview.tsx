import { useMemo } from "react";
import { useViewerStore } from "../state/store";
import { GraphCanvas } from "./GraphCanvas";
import { buildOverviewGraph } from "./viewGraphs";

export function OverviewView() {
  const overview = useViewerStore((s) => s.overview);
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
  const drillToPart = useViewerStore((s) => s.drillToPart);
  const startFlow = useViewerStore((s) => s.startFlow);

  const graph = useMemo(() => {
    if (!overview) return { nodes: [], edges: [], foldedCount: 0 };
    return buildOverviewGraph({
      overview,
      chunks,
      layers,
      exactOnly,
      runtime,
      dataflow,
      activeFlow,
      flowStepIndex,
    });
  }, [overview, chunks, layers, exactOnly, runtime, dataflow, activeFlow, flowStepIndex]);

  if (!overview) return null;

  const totalFiles = overview.tier_summary.total_files;

  return (
    <div className="arch-view-shell">
      <div className="arch-summary-panel" data-testid="overview-summary">
        <div className="arch-summary-stat">
          <span className="arch-stat-val">{overview.parts.length}</span>
          <span className="arch-stat-lbl">Parts</span>
        </div>
        <div className="arch-summary-stat">
          <span className="arch-stat-val">{overview.groups.length}</span>
          <span className="arch-stat-lbl">Groups</span>
        </div>
        <div className="arch-summary-stat">
          <span className="arch-stat-val">{overview.boundaries.length}</span>
          <span className="arch-stat-lbl">Boundaries</span>
        </div>
        <div className="arch-summary-stat">
          <span className="arch-stat-val">{totalFiles}</span>
          <span className="arch-stat-lbl">Files</span>
        </div>
        <div className="arch-summary-stat">
          <span className="arch-stat-val">{overview.flow_index.length}</span>
          <span className="arch-stat-lbl">Flows</span>
        </div>
        {graph.foldedCount > 0 && (
          <div className="arch-summary-stat">
            <span className="arch-stat-val">+{graph.foldedCount}</span>
            <span className="arch-stat-lbl">Folded (300 cap)</span>
          </div>
        )}
        {overview.flow_index.length > 0 && (
          <div className="arch-summary-flows">
            <span className="arch-toolbar-label">Flows:</span>
            {overview.flow_index.slice(0, 4).map((fl) => (
              <button
                key={fl.id}
                type="button"
                className="arch-chip"
                onClick={() => void startFlow(fl.id, fl.part_id)}
              >
                {fl.entry_id} ({fl.step_count})
              </button>
            ))}
          </div>
        )}
      </div>

      <GraphCanvas
        viewKey="overview"
        nodes={graph.nodes}
        edges={graph.edges}
        layers={layers}
        selectedNodeId={selectedNodeId}
        selectedEdgeId={selectedEdgeId}
        onSelectNode={(id) => selectNode(id)}
        onSelectEdge={(id) => selectEdge(id)}
        onDrillNode={(node) => {
          if (node.partId) {
            void drillToPart(node.partId);
          }
        }}
      />
    </div>
  );
}
