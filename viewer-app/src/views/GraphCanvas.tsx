import "@xyflow/react/dist/style.css";
import { useEffect, useMemo, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import {
  DEFAULT_NODE_HEIGHT,
  DEFAULT_NODE_WIDTH,
  layoutWithElk,
  type LayoutResult,
} from "../layout/elkWorker";
import { ChangeBadge } from "../ui/ChangeBadges";
import type { ViewEdgeData, ViewNodeData } from "../types/bundle";

function ArchNodeCard({ data, selected }: NodeProps<Node<ViewNodeData & Record<string, unknown>>>) {
  const isFold = data.kind === "fold";
  const classes = [
    "arch-node-card",
    `kind-${data.kind}`,
    selected ? "is-selected" : "",
    data.isCurrentStep ? "is-current-step" : "",
    data.highlighted ? "is-highlighted" : "",
    data.dimmed ? "is-dimmed" : "",
    isFold ? "is-fold" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      className={classes}
      style={{
        width: DEFAULT_NODE_WIDTH,
        minHeight: DEFAULT_NODE_HEIGHT,
        opacity: data.dimmed ? 0.25 : 1,
      }}
    >
      <Handle type="target" position={Position.Left} className="arch-handle" />
      <div className="arch-node-header">
        <span className="arch-node-title" title={data.label}>
          {data.label}
        </span>
        <div className="arch-node-badges">
          {data.badge && <span className="arch-badge">{data.badge}</span>}
          {data.confidence === "heuristic" && (
            <span className="arch-badge arch-badge--heuristic">approx</span>
          )}
          <ChangeBadge status={data.changeStatus} />
        </div>
      </div>
      {data.subtitle && (
        <div className="arch-node-sub" title={data.subtitle}>
          {data.subtitle}
        </div>
      )}
      <Handle type="source" position={Position.Right} className="arch-handle" />
    </div>
  );
}

const nodeTypes = {
  archCard: ArchNodeCard,
};

export function GraphCanvas({
  viewKey,
  nodes,
  edges,
  selectedNodeId,
  onSelectNode,
  onSelectEdge,
  onDrillNode,
}: {
  viewKey: string;
  nodes: ViewNodeData[];
  edges: ViewEdgeData[];
  selectedNodeId: string | null;
  onSelectNode: (nodeId: string) => void;
  onSelectEdge: (edgeId: string) => void;
  onDrillNode: (node: ViewNodeData) => void;
}) {
  const [layout, setLayout] = useState<LayoutResult | null>(null);

  useEffect(() => {
    let cancelled = false;
    layoutWithElk(
      nodes.map((n) => ({
        id: n.id,
        width: DEFAULT_NODE_WIDTH,
        height: DEFAULT_NODE_HEIGHT,
        partition: n.partition,
      })),
      edges.map((e) => ({ id: e.id, source: e.source, target: e.target })),
    )
      .then((res) => {
        if (!cancelled) setLayout(res);
      })
      .catch(() => {
        if (!cancelled) {
          // Graceful deterministic grid fallback if layout fails
          setLayout({
            nodes: nodes.map((n, idx) => ({
              id: n.id,
              x: (idx % 4) * (DEFAULT_NODE_WIDTH + 48),
              y: Math.floor(idx / 4) * (DEFAULT_NODE_HEIGHT + 36),
              width: DEFAULT_NODE_WIDTH,
              height: DEFAULT_NODE_HEIGHT,
            })),
            edges: [],
            width: 1000,
            height: 600,
            usedWorker: false,
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [viewKey, nodes, edges]);

  const posMap = useMemo(() => {
    const map = new Map<string, { x: number; y: number }>();
    for (const p of layout?.nodes ?? []) {
      map.set(p.id, { x: p.x, y: p.y });
    }
    return map;
  }, [layout]);

  const rfNodes = useMemo<Array<Node<ViewNodeData & Record<string, unknown>>>>(() => {
    return nodes.map((n, idx) => {
      const pos = posMap.get(n.id) ?? {
        x: (idx % 4) * (DEFAULT_NODE_WIDTH + 40),
        y: Math.floor(idx / 4) * (DEFAULT_NODE_HEIGHT + 32),
      };
      return {
        id: n.id,
        type: "archCard",
        position: pos,
        selected: n.id === selectedNodeId,
        data: { ...n },
      };
    });
  }, [nodes, posMap, selectedNodeId]);

  const rfEdges = useMemo<Edge[]>(() => {
    return edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      label: e.label,
      type: "smoothstep",
      animated: Boolean(e.highlighted || e.runtimeOnly),
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: e.highlighted ? "#06b6d4" : e.runtimeOnly ? "#f43f5e" : "#64748b",
      },
      style: {
        stroke: e.highlighted ? "#06b6d4" : e.runtimeOnly ? "#f43f5e" : "#64748b",
        strokeWidth: e.highlighted ? 2.5 : 1.5,
        strokeDasharray: e.runtimeOnly || e.isDataflow ? "5 4" : undefined,
        opacity: e.dimmed ? 0.25 : 1,
      },
    }));
  }, [edges]);

  if (nodes.length === 0) {
    return (
      <div className="arch-empty-canvas">
        <p>No nodes visible with the active layer/confidence filters.</p>
      </div>
    );
  }

  return (
    <div className="arch-canvas-wrap" data-testid={`view-${viewKey}`}>
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        fitView
        minZoom={0.15}
        maxZoom={2}
        nodesDraggable={false}
        nodesConnectable={false}
        onNodeClick={(_ev, node) => onSelectNode(node.id)}
        onNodeDoubleClick={(_ev, node) => {
          const vn = nodes.find((n) => n.id === node.id);
          if (vn) onDrillNode(vn);
        }}
        onEdgeClick={(_ev, edge) => onSelectEdge(edge.id)}
      >
        <Background color="#1e293b" gap={20} />
        <Controls />
      </ReactFlow>
    </div>
  );
}
