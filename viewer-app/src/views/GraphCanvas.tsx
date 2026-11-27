import "@xyflow/react/dist/style.css";
import { createContext, useContext, useEffect, useMemo, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ViewportPortal,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { edgeStrokeColor } from "../data/layers";
import {
  DEFAULT_NODE_HEIGHT,
  DEFAULT_NODE_WIDTH,
  layoutWithElk,
  type LayoutResult,
} from "../layout/elkWorker";
import { ChangeBadge } from "../ui/ChangeBadges";
import type { LayerFilter, ViewEdgeData, ViewNodeData } from "../types/bundle";

const DEFAULT_ACTIVE_LAYERS: LayerFilter = {
  control: true,
  data: true,
  integration: true,
  runtime: false,
};

const DrillHandlerContext = createContext<((node: ViewNodeData) => void) | null>(null);

interface SwimlaneBox {
  partition: 0 | 1 | 2;
  label: string;
  left: number;
  top: number;
  width: number;
  height: number;
}

const SWIMLANE_LABELS: Record<0 | 1 | 2, string> = {
  0: "Used by",
  1: "Inside",
  2: "Uses",
};

function computeSwimlanes(
  nodes: ViewNodeData[],
  layout: LayoutResult | null,
): SwimlaneBox[] {
  if (!layout || layout.nodes.length === 0) return [];
  const posById = new Map(layout.nodes.map((p) => [p.id, p]));
  const rawBounds = new Map<
    0 | 1 | 2,
    { minX: number; maxX: number; minY: number; maxY: number }
  >();

  let globalMinY = Infinity;
  let globalMaxY = -Infinity;

  for (const n of nodes) {
    if (n.partition === undefined) continue;
    const pos = posById.get(n.id);
    if (!pos) continue;
    globalMinY = Math.min(globalMinY, pos.y);
    globalMaxY = Math.max(globalMaxY, pos.y + pos.height);
    const cur = rawBounds.get(n.partition);
    if (!cur) {
      rawBounds.set(n.partition, {
        minX: pos.x,
        maxX: pos.x + pos.width,
        minY: pos.y,
        maxY: pos.y + pos.height,
      });
    } else {
      cur.minX = Math.min(cur.minX, pos.x);
      cur.maxX = Math.max(cur.maxX, pos.x + pos.width);
    }
  }

  if (rawBounds.size === 0 || !Number.isFinite(globalMinY)) return [];

  const padX = 18;
  const padTop = 36;
  const padBottom = 18;
  const top = globalMinY - padTop;
  const height = Math.max(120, globalMaxY - globalMinY + padTop + padBottom);

  const partitions = ([0, 1, 2] as const).filter((p) => rawBounds.has(p));
  const padded = partitions.map((p) => {
    const b = rawBounds.get(p)!;
    return {
      partition: p,
      left: b.minX - padX,
      right: b.maxX + padX,
    };
  });

  // Prevent adjacent swimlanes from overlapping horizontally
  for (let i = 0; i < padded.length - 1; i++) {
    const cur = padded[i];
    const nxt = padded[i + 1];
    if (cur.right > nxt.left - 8) {
      const mid = (cur.right + nxt.left) / 2;
      cur.right = mid - 4;
      nxt.left = mid + 4;
    }
  }

  return padded.map((b) => ({
    partition: b.partition,
    label: SWIMLANE_LABELS[b.partition],
    left: b.left,
    top,
    width: Math.max(DEFAULT_NODE_WIDTH, b.right - b.left),
    height,
  }));
}

function ArchNodeCard({ data, selected }: NodeProps<Node<ViewNodeData & Record<string, unknown>>>) {
  const onDrillNode = useContext(DrillHandlerContext);
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
          {data.drillable && onDrillNode && (
            <button
              type="button"
              className="arch-node-drill-btn"
              title="Drill in (Enter or double-click)"
              aria-label={`Drill into ${data.label}`}
              onClick={(ev) => {
                ev.stopPropagation();
                onDrillNode(data);
              }}
            >
              &rarr;
            </button>
          )}
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
  layers = DEFAULT_ACTIVE_LAYERS,
  selectedNodeId,
  selectedEdgeId = null,
  showSwimlanes = false,
  onSelectNode,
  onSelectEdge,
  onDrillNode,
}: {
  viewKey: string;
  nodes: ViewNodeData[];
  edges: ViewEdgeData[];
  layers?: LayerFilter;
  selectedNodeId: string | null;
  selectedEdgeId?: string | null;
  showSwimlanes?: boolean;
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

  const swimlanes = useMemo(
    () => (showSwimlanes ? computeSwimlanes(nodes, layout) : []),
    [showSwimlanes, nodes, layout],
  );

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
    return edges.map((e) => {
      const isSelected = e.id === selectedEdgeId;
      const stroke = isSelected ? "#facc15" : edgeStrokeColor(e, layers);
      return {
        id: e.id,
        source: e.source,
        target: e.target,
        label: e.label,
        type: "smoothstep",
        animated: Boolean(e.highlighted || e.runtimeOnly),
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: stroke,
        },
        style: {
          stroke,
          strokeWidth: e.highlighted || isSelected ? 2.5 : 1.5,
          strokeDasharray: e.runtimeOnly || e.isDataflow ? "5 4" : undefined,
          opacity: e.dimmed ? 0.25 : 1,
        },
      };
    });
  }, [edges, layers, selectedEdgeId]);

  if (nodes.length === 0) {
    return (
      <div className="arch-empty-canvas">
        <p>No nodes visible with the active layer/confidence filters.</p>
      </div>
    );
  }

  return (
    <DrillHandlerContext.Provider value={onDrillNode}>
      <div
        className="arch-canvas-wrap"
        data-testid={`view-${viewKey}`}
        data-layout-ready={layout ? "true" : "false"}
      >
        <ReactFlow
          nodes={rfNodes}
          edges={rfEdges}
          nodeTypes={nodeTypes}
          fitView
          minZoom={0.15}
          maxZoom={2}
          nodesDraggable={false}
          nodesConnectable={false}
          zoomOnDoubleClick={false}
          onNodeClick={(_ev, node) => onSelectNode(node.id)}
          onNodeDoubleClick={(_ev, node) => {
            const vn = nodes.find((n) => n.id === node.id);
            if (vn) onDrillNode(vn);
          }}
          onEdgeClick={(_ev, edge) => onSelectEdge(edge.id)}
        >
          {swimlanes.length > 0 && (
            <ViewportPortal>
              <div className="arch-swimlane-layer" aria-hidden="true">
                {swimlanes.map((lane) => (
                  <div
                    key={lane.partition}
                    className={`arch-swimlane arch-swimlane--p${lane.partition}`}
                    data-testid={`swimlane-p${lane.partition}`}
                    style={{
                      left: lane.left,
                      top: lane.top,
                      width: lane.width,
                      height: lane.height,
                    }}
                  >
                    <span className="arch-swimlane__label">{lane.label}</span>
                  </div>
                ))}
              </div>
            </ViewportPortal>
          )}
          <Background color="#1e293b" gap={20} />
          <Controls />
        </ReactFlow>
      </div>
    </DrillHandlerContext.Provider>
  );
}
