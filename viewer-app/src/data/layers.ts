import type {
  Confidence,
  LayerFilter,
  LayerName,
  OverviewBoundary,
  ViewEdgeData,
  ViewNodeData,
} from "../types/bundle";

export const MAX_DRAWN_NODES = 300;
export const MAX_GROUP_FILES = 24;
export const MAX_FILE_SYMBOLS = 22;

const CONTROL_EDGE_KINDS = new Set(["calls", "registers", "boots", "implements"]);
const DATA_EDGE_KINDS = new Set(["reads", "writes", "publishes", "dataflow"]);
const INTEGRATION_EDGE_KINDS = new Set(["http_request"]);

export function edgeLayersForEdge(edge: {
  kinds?: string[];
  kind?: string;
  source?: string;
  target?: string;
  runtimeOnly?: boolean;
}): LayerName[] {
  if (edge.runtimeOnly) return ["runtime"];
  const kinds = edge.kinds ?? (edge.kind ? [edge.kind] : []);
  const layers = new Set<LayerName>();

  for (const kind of kinds) {
    if (CONTROL_EDGE_KINDS.has(kind)) layers.add("control");
    if (DATA_EDGE_KINDS.has(kind)) layers.add("data");
    if (INTEGRATION_EDGE_KINDS.has(kind)) layers.add("integration");
    if (kind === "runtime") layers.add("runtime");
  }

  const endpoints = [edge.source ?? "", edge.target ?? ""];
  for (const ep of endpoints) {
    if (ep.startsWith("res:") || ep.startsWith("ds:")) layers.add("data");
    if (ep.startsWith("svc:") || ep.startsWith("ext:")) layers.add("integration");
  }

  if (layers.size === 0) {
    layers.add("control");
  }
  return Array.from(layers);
}

export function isEdgeVisibleInLayers(
  edge: {
    kinds?: string[];
    kind?: string;
    source?: string;
    target?: string;
    runtimeOnly?: boolean;
  },
  layers: LayerFilter,
): boolean {
  const edgeLayers = edgeLayersForEdge(edge);
  return edgeLayers.some((layer) => layers[layer]);
}

export function isBoundaryVisibleInLayers(
  boundary: Pick<OverviewBoundary, "boundary" | "id">,
  layers: LayerFilter,
): boolean {
  if (boundary.boundary === "datastore" || boundary.boundary === "resource" || boundary.id.startsWith("ds:") || boundary.id.startsWith("res:")) {
    return layers.data;
  }
  if (boundary.boundary === "sink" || boundary.boundary === "sidecar" || boundary.id.startsWith("ext:") || boundary.id.startsWith("svc:")) {
    return layers.integration;
  }
  return true;
}

export function passesConfidenceFilter(
  confidence: Confidence | null | undefined,
  exactOnly: boolean,
): boolean {
  if (!exactOnly) return true;
  if (!confidence) return true;
  return confidence === "exact";
}

export interface CappedGraphResult {
  nodes: ViewNodeData[];
  edges: ViewEdgeData[];
  foldedCount: number;
}

/**
 * Enforce the 300-node hard cap per view. When nodes exceed maxNodes (default 300),
 * keep the highest-degree (then deterministic id-sorted) maxNodes - 1 nodes and
 * fold the remaining N nodes into an aggregate "+N" node, rewriting incident edges.
 */
export function capNodesForView(
  nodes: ViewNodeData[],
  edges: ViewEdgeData[],
  viewKey: string,
  maxNodes: number = MAX_DRAWN_NODES,
): CappedGraphResult {
  if (nodes.length <= maxNodes) {
    const nodeIds = new Set(nodes.map((n) => n.id));
    return {
      nodes: [...nodes].sort((a, b) => a.id.localeCompare(b.id)),
      edges: edges
        .filter((e) => nodeIds.has(e.source) && nodeIds.has(e.target))
        .sort((a, b) => a.id.localeCompare(b.id)),
      foldedCount: 0,
    };
  }

  const degree = new Map<string, number>();
  for (const n of nodes) degree.set(n.id, 0);
  for (const e of edges) {
    if (degree.has(e.source)) degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
    if (degree.has(e.target)) degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
  }

  const ranked = [...nodes].sort((a, b) => {
    const aPinned = a.highlighted || a.isCurrentStep ? 1 : 0;
    const bPinned = b.highlighted || b.isCurrentStep ? 1 : 0;
    if (aPinned !== bPinned) return bPinned - aPinned;
    const degDiff = (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0);
    if (degDiff !== 0) return degDiff;
    return a.id.localeCompare(b.id);
  });

  const keepLimit = Math.max(1, maxNodes - 1);
  const kept = ranked.slice(0, keepLimit);
  const folded = ranked.slice(keepLimit);
  const keptIds = new Set(kept.map((n) => n.id));
  const foldId = `fold:${viewKey}`;

  const foldNode: ViewNodeData = {
    id: foldId,
    label: `+${folded.length}`,
    subtitle: `${folded.length} more nodes`,
    kind: "fold",
    badge: `+${folded.length}`,
    foldCount: folded.length,
    foldedIds: folded.map((n) => n.id),
    drillable: false,
    partition: 1,
  };

  const mergedEdges = new Map<string, ViewEdgeData>();
  for (const e of edges) {
    const src = keptIds.has(e.source) ? e.source : foldId;
    const tgt = keptIds.has(e.target) ? e.target : foldId;
    if (src === tgt) continue;
    const key = `${src}->${tgt}`;
    const existing = mergedEdges.get(key);
    if (!existing) {
      mergedEdges.set(key, {
        ...e,
        id: key,
        source: src,
        target: tgt,
        kinds: [...e.kinds],
        evidence: e.evidence ? [...e.evidence] : [],
      });
    } else {
      const kindSet = new Set([...existing.kinds, ...e.kinds]);
      existing.kinds = Array.from(kindSet).sort();
      existing.count += e.count;
    }
  }

  return {
    nodes: [...kept, foldNode].sort((a, b) => a.id.localeCompare(b.id)),
    edges: Array.from(mergedEdges.values()).sort((a, b) => a.id.localeCompare(b.id)),
    foldedCount: folded.length,
  };
}
