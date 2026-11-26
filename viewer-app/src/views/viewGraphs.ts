import {
  capNodesForView,
  isBoundaryVisibleInLayers,
  isEdgeVisibleInLayers,
  MAX_FILE_SYMBOLS,
  MAX_GROUP_FILES,
  passesConfidenceFilter,
} from "../data/layers";
import {
  getFileChangeStatus,
  getGroupChangeStatus,
  getPartChangeStatus,
} from "../ui/ChangeBadges";
import type {
  ChunkEdge,
  ChunkFlow,
  ChunkNode,
  DataflowData,
  LayerFilter,
  OverviewData,
  OverviewEdge,
  PartChunk,
  RuntimeOverlayData,
  ViewEdgeData,
  ViewNodeData,
} from "../types/bundle";

export interface ViewGraphInput {
  nodes: ViewNodeData[];
  edges: ViewEdgeData[];
  foldedCount: number;
}

export interface ViewBuildOptions {
  overview: OverviewData;
  chunks: Record<string, PartChunk>;
  layers: LayerFilter;
  exactOnly: boolean;
  runtime: RuntimeOverlayData | null;
  dataflow: DataflowData | null;
  activeFlow: ChunkFlow | null;
  flowStepIndex: number;
}

function mergeEdgeIntoMap(
  map: Map<string, ViewEdgeData>,
  edge: ViewEdgeData,
): void {
  const existing = map.get(edge.id);
  if (!existing) {
    map.set(edge.id, {
      ...edge,
      kinds: [...edge.kinds].sort(),
      evidence: edge.evidence ? [...edge.evidence] : [],
    });
    return;
  }
  const kindSet = new Set([...existing.kinds, ...edge.kinds]);
  existing.kinds = Array.from(kindSet).sort();
  existing.count += edge.count;
  if (existing.evidence && edge.evidence) {
    for (const ev of edge.evidence) {
      if (existing.evidence.length >= 3) break;
      existing.evidence.push(ev);
    }
  }
  existing.label =
    existing.count > 1
      ? `${existing.kinds.join(", ")} (${existing.count})`
      : existing.kinds.join(", ");
}

function applyFlowDimming(
  nodes: ViewNodeData[],
  edges: ViewEdgeData[],
  activeFlow: ChunkFlow | null,
  flowStepIndex: number,
): void {
  if (!activeFlow || activeFlow.steps.length === 0) return;
  const stepNodeIds = new Set(activeFlow.steps.map((s) => s.node_id));
  const currentStepId =
    activeFlow.steps[Math.max(0, Math.min(activeFlow.steps.length - 1, flowStepIndex))]?.node_id;

  const stepEdges = new Set<string>();
  for (let i = 0; i < activeFlow.steps.length - 1; i++) {
    stepEdges.add(`${activeFlow.steps[i].node_id}->${activeFlow.steps[i + 1].node_id}`);
  }

  for (const n of nodes) {
    const inFlow =
      stepNodeIds.has(n.id) ||
      (n.partId !== undefined &&
        activeFlow.steps.some((s) => s.node_id === n.id || s.node_id.includes(n.id)));
    n.isCurrentStep = n.id === currentStepId;
    n.highlighted = inFlow;
    n.dimmed = !inFlow;
  }

  for (const e of edges) {
    const inFlow = stepEdges.has(`${e.source}->${e.target}`);
    e.highlighted = inFlow;
    e.dimmed = !inFlow;
  }
}

function appendRuntimeDriftEdges(
  edgeMap: Map<string, ViewEdgeData>,
  visibleNodeIds: Set<string>,
  runtime: RuntimeOverlayData | null,
  layers: LayerFilter,
): void {
  if (!runtime || !layers.runtime) return;
  for (const [key, metric] of Object.entries(runtime.edge_metrics ?? {})) {
    if (metric.static) continue;
    const arrowIdx = key.indexOf("->");
    if (arrowIdx <= 0) continue;
    const source = key.slice(0, arrowIdx);
    const target = key.slice(arrowIdx + 2);
    if (!visibleNodeIds.has(source) || !visibleNodeIds.has(target)) continue;
    mergeEdgeIntoMap(edgeMap, {
      id: `${source}->${target}`,
      source,
      target,
      kinds: ["runtime"],
      label: `runtime (${metric.span_count})`,
      count: metric.span_count,
      confidence: "runtime",
      runtimeOnly: true,
    });
  }
}

export function buildOverviewGraph(opts: ViewBuildOptions): ViewGraphInput {
  const { overview, layers, exactOnly, runtime, dataflow, activeFlow, flowStepIndex } = opts;

  const nodes: ViewNodeData[] = [];
  const nodeIds = new Set<string>();

  for (const part of overview.parts) {
    nodes.push({
      id: part.id,
      label: part.name,
      subtitle: `${part.kind} · ${part.counts.files} files`,
      kind: part.kind,
      badge: part.kind,
      changeStatus: getPartChangeStatus(part.id, overview),
      drillable: true,
      partId: part.id,
      evidence: part.evidence,
      metrics: runtime?.node_metrics?.[part.id] ?? null,
    });
    nodeIds.add(part.id);
  }

  // Filter overview edges by active layers first
  const visibleEdges: ViewEdgeData[] = [];
  const connectedBoundaryIds = new Set<string>();
  const edgeMap = new Map<string, ViewEdgeData>();

  for (const oe of overview.edges as OverviewEdge[]) {
    const kinds = Object.keys(oe.kinds).sort();
    const count = Object.values(oe.kinds).reduce((a, b) => a + b, 0);
    const edge: ViewEdgeData = {
      id: `${oe.source}->${oe.target}`,
      source: oe.source,
      target: oe.target,
      kinds,
      label: count > 1 ? `${kinds.join(", ")} (${count})` : kinds.join(", "),
      count,
      evidence: oe.evidence_sample,
    };
    if (!isEdgeVisibleInLayers(edge, layers)) continue;
    mergeEdgeIntoMap(edgeMap, edge);
    connectedBoundaryIds.add(oe.source);
    connectedBoundaryIds.add(oe.target);
  }

  if (dataflow && layers.data) {
    for (const df of dataflow.edges) {
      if (nodeIds.has(df.entry_id) && overview.boundaries.some((b) => b.id === df.sink_boundary_id)) {
        mergeEdgeIntoMap(edgeMap, {
          id: `${df.entry_id}->${df.sink_boundary_id}`,
          source: df.entry_id,
          target: df.sink_boundary_id,
          kinds: ["dataflow"],
          label: "dataflow",
          count: 1,
          confidence: df.confidence,
          evidence: df.evidence,
          isDataflow: true,
        });
        connectedBoundaryIds.add(df.sink_boundary_id);
      }
    }
  }

  for (const b of overview.boundaries) {
    if (!passesConfidenceFilter(b.confidence, exactOnly)) continue;
    if (!isBoundaryVisibleInLayers(b, layers) && !connectedBoundaryIds.has(b.id)) continue;
    nodes.push({
      id: b.id,
      label: b.label,
      subtitle: `${b.boundary} (${b.kind})`,
      kind: b.boundary,
      badge: b.kind,
      confidence: b.confidence,
      drillable: false,
      evidence: b.evidence,
      metrics: runtime?.node_metrics?.[b.id] ?? null,
    });
    nodeIds.add(b.id);
  }

  appendRuntimeDriftEdges(edgeMap, nodeIds, runtime, layers);

  for (const e of edgeMap.values()) {
    if (nodeIds.has(e.source) && nodeIds.has(e.target)) {
      visibleEdges.push(e);
    }
  }

  applyFlowDimming(nodes, visibleEdges, activeFlow, flowStepIndex);
  const capped = capNodesForView(nodes, visibleEdges, "overview");
  return { nodes: capped.nodes, edges: capped.edges, foldedCount: capped.foldedCount };
}

export function buildPartGraph(
  partId: string,
  opts: ViewBuildOptions,
): ViewGraphInput {
  const { overview, layers, exactOnly, runtime, activeFlow, flowStepIndex } = opts;
  const partGroups = overview.groups.filter((g) => g.part_id === partId);
  const insideGroupIds = new Set(partGroups.map((g) => g.id));

  const nodes: ViewNodeData[] = [];
  const nodeIds = new Set<string>();
  const boundaryMap = new Map(overview.boundaries.map((b) => [b.id, b]));
  const partMap = new Map(overview.parts.map((p) => [p.id, p]));
  const groupMap = new Map(overview.groups.map((g) => [g.id, g]));

  // Inside band (partition 1)
  for (const grp of partGroups) {
    nodes.push({
      id: grp.id,
      label: grp.name,
      subtitle: `${grp.language} · ${grp.counts.files ?? 0} files`,
      kind: "group",
      badge: grp.tier,
      tier: grp.tier,
      changeStatus: getGroupChangeStatus(grp.id, overview),
      drillable: true,
      partition: 1,
      partId,
      groupId: grp.id,
      metrics: runtime?.node_metrics?.[grp.id] ?? null,
    });
    nodeIds.add(grp.id);
  }

  // Map any node/group id to its representative inside/outside entity in Part view
  const resolveEntity = (id: string): string | null => {
    if (insideGroupIds.has(id)) return id;
    if (id === partId) return partGroups[0]?.id ?? partId;
    if (groupMap.has(id)) {
      const g = groupMap.get(id)!;
      return g.part_id === partId ? g.id : g.part_id;
    }
    if (partMap.has(id) || boundaryMap.has(id)) return id;
    return null;
  };

  const ensureExternalNode = (id: string, partition: 0 | 2) => {
    if (nodeIds.has(id)) return;
    const extPart = partMap.get(id);
    if (extPart) {
      nodes.push({
        id: extPart.id,
        label: extPart.name,
        subtitle: `part:${extPart.kind}`,
        kind: extPart.kind,
        badge: partition === 0 ? "used by" : "uses",
        changeStatus: getPartChangeStatus(extPart.id, overview),
        drillable: true,
        partition,
        partId: extPart.id,
        evidence: extPart.evidence,
      });
      nodeIds.add(id);
      return;
    }
    const b = boundaryMap.get(id);
    if (b && passesConfidenceFilter(b.confidence, exactOnly)) {
      nodes.push({
        id: b.id,
        label: b.label,
        subtitle: `${b.boundary} (${b.kind})`,
        kind: b.boundary,
        badge: b.kind,
        confidence: b.confidence,
        drillable: false,
        partition,
        evidence: b.evidence,
      });
      nodeIds.add(id);
    }
  };

  const edgeMap = new Map<string, ViewEdgeData>();
  for (const oe of overview.edges) {
    const kinds = Object.keys(oe.kinds).sort();
    const count = Object.values(oe.kinds).reduce((a, b) => a + b, 0);
    const candidate: ViewEdgeData = {
      id: `${oe.source}->${oe.target}`,
      source: oe.source,
      target: oe.target,
      kinds,
      count,
      evidence: oe.evidence_sample,
    };
    if (!isEdgeVisibleInLayers(candidate, layers)) continue;

    const src = resolveEntity(oe.source);
    const tgt = resolveEntity(oe.target);
    if (!src || !tgt || src === tgt) continue;

    const srcInside = insideGroupIds.has(src) || src === partId;
    const tgtInside = insideGroupIds.has(tgt) || tgt === partId;
    if (!srcInside && !tgtInside) continue;

    if (!srcInside && tgtInside) ensureExternalNode(src, 0); // Used by
    if (srcInside && !tgtInside) ensureExternalNode(tgt, 2); // Uses

    if (nodeIds.has(src) && nodeIds.has(tgt)) {
      mergeEdgeIntoMap(edgeMap, {
        ...candidate,
        id: `${src}->${tgt}`,
        source: src,
        target: tgt,
        label: count > 1 ? `${kinds.join(", ")} (${count})` : kinds.join(", "),
      });
    }
  }

  // Fallback if part had no groups (e.g. an empty/virtual part)
  if (partGroups.length === 0) {
    const part = partMap.get(partId);
    if (part) {
      nodes.push({
        id: part.id,
        label: part.name,
        subtitle: part.kind,
        kind: part.kind,
        partition: 1,
        partId: part.id,
        evidence: part.evidence,
      });
      nodeIds.add(part.id);
    }
  }

  const edges = Array.from(edgeMap.values());
  applyFlowDimming(nodes, edges, activeFlow, flowStepIndex);
  const capped = capNodesForView(nodes, edges, `part:${partId}`);
  return { nodes: capped.nodes, edges: capped.edges, foldedCount: capped.foldedCount };
}

function directoryOf(filePath: string): string {
  const idx = filePath.lastIndexOf("/");
  return idx >= 0 ? filePath.slice(0, idx) : ".";
}

export function buildGroupGraph(
  partId: string,
  groupId: string,
  dirFilter: string | null,
  opts: ViewBuildOptions,
): ViewGraphInput {
  const { overview, chunks, layers, exactOnly, activeFlow, flowStepIndex } = opts;
  const chunk = chunks[partId];
  if (!chunk) {
    return { nodes: [], edges: [], foldedCount: 0 };
  }

  let files = chunk.files.filter((f) => f.module_id === groupId);
  if (dirFilter) {
    files = files.filter((f) => directoryOf(f.path) === dirFilter);
  }

  const useDirChips = !dirFilter && files.length > MAX_GROUP_FILES;
  const nodes: ViewNodeData[] = [];
  const nodeIds = new Set<string>();
  const fileToEntity = new Map<string, string>();

  if (useDirChips) {
    const dirGroups = new Map<string, typeof files>();
    for (const f of files) {
      const dir = directoryOf(f.path);
      const list = dirGroups.get(dir) ?? [];
      list.push(f);
      dirGroups.set(dir, list);
    }
    for (const [dir, dirFiles] of Array.from(dirGroups.entries()).sort((a, b) =>
      a[0].localeCompare(b[0]),
    )) {
      const id = `dir:${dir}`;
      const symCount = dirFiles.reduce((acc, f) => acc + f.symbols.length, 0);
      nodes.push({
        id,
        label: dir.split("/").pop() || dir,
        subtitle: `${dirFiles.length} files · ${symCount} symbols`,
        kind: "dir_chip",
        badge: `${dirFiles.length} files`,
        drillable: true,
        partId,
        groupId,
        filePath: dir,
      });
      nodeIds.add(id);
      for (const f of dirFiles) {
        fileToEntity.set(f.path, id);
      }
    }
  } else {
    for (const f of files) {
      const id = `file:${f.path}`;
      nodes.push({
        id,
        label: f.path.split("/").pop() || f.path,
        subtitle: `${f.symbols.length} symbols · ${f.loc} loc`,
        kind: f.is_infra ? "infra_file" : "file",
        badge: f.tier,
        tier: f.tier,
        changeStatus: getFileChangeStatus(f.path, overview.delta),
        drillable: true,
        partId,
        groupId,
        filePath: f.path,
        evidence: [{ file: f.path, line: 1, source: f.tier }],
      });
      nodeIds.add(id);
      fileToEntity.set(f.path, id);
    }
  }

  const nodeFileLookup = new Map<string, string>();
  for (const n of chunk.nodes) {
    if (n.file && fileToEntity.has(n.file)) {
      nodeFileLookup.set(n.id, fileToEntity.get(n.file)!);
    }
  }

  const boundaryMap = new Map(overview.boundaries.map((b) => [b.id, b]));
  const edgeMap = new Map<string, ViewEdgeData>();

  for (const e of chunk.edges as ChunkEdge[]) {
    if (!passesConfidenceFilter(e.confidence, exactOnly)) continue;
    if (!isEdgeVisibleInLayers(e, layers)) continue;
    const srcEntity = nodeFileLookup.get(e.source);
    const tgtEntity = nodeFileLookup.get(e.target) ?? (boundaryMap.has(e.target) ? e.target : undefined);
    if (!srcEntity || !tgtEntity || srcEntity === tgtEntity) continue;

    if (!nodeIds.has(tgtEntity) && boundaryMap.has(tgtEntity)) {
      const b = boundaryMap.get(tgtEntity)!;
      if (!passesConfidenceFilter(b.confidence, exactOnly)) continue;
      nodes.push({
        id: b.id,
        label: b.label,
        subtitle: `${b.boundary} (${b.kind})`,
        kind: b.boundary,
        badge: b.kind,
        confidence: b.confidence,
        drillable: false,
        evidence: b.evidence,
      });
      nodeIds.add(b.id);
    }

    if (nodeIds.has(srcEntity) && nodeIds.has(tgtEntity)) {
      mergeEdgeIntoMap(edgeMap, {
        id: `${srcEntity}->${tgtEntity}`,
        source: srcEntity,
        target: tgtEntity,
        kinds: [e.kind],
        label: e.kind,
        count: 1,
        confidence: e.confidence,
        evidence: e.evidence,
      });
    }
  }

  const edges = Array.from(edgeMap.values());
  applyFlowDimming(nodes, edges, activeFlow, flowStepIndex);
  const capped = capNodesForView(nodes, edges, `group:${groupId}`);
  return { nodes: capped.nodes, edges: capped.edges, foldedCount: capped.foldedCount };
}

export function buildFileGraph(
  partId: string,
  groupId: string,
  filePath: string,
  opts: ViewBuildOptions,
): ViewGraphInput {
  const { overview, chunks, layers, exactOnly, activeFlow, flowStepIndex } = opts;
  const chunk = chunks[partId];
  if (!chunk) {
    return { nodes: [], edges: [], foldedCount: 0 };
  }

  const fileNodes = chunk.nodes.filter(
    (n) => n.file === filePath && passesConfidenceFilter(n.confidence, exactOnly),
  );
  const fileNodeMap = new Map<string, ChunkNode>(fileNodes.map((n) => [n.id, n]));

  // Include file.symbols rows if any aren't already in chunk.nodes
  const fileRow = chunk.files.find((f) => f.path === filePath);
  if (fileRow) {
    for (const sym of fileRow.symbols) {
      const hasMatch = fileNodes.some((n) => n.symbol === sym.name || n.label === sym.name);
      if (!hasMatch) {
        const synId = `sym:${filePath}:${sym.name}:${sym.line}`;
        const synNode: ChunkNode = {
          id: synId,
          kind: sym.kind,
          label: sym.name,
          module_id: groupId,
          file: filePath,
          line: sym.line,
          symbol: sym.name,
          tier: fileRow.tier,
          confidence: "exact",
          evidence: [{ file: filePath, line: sym.line, source: fileRow.tier }],
        };
        fileNodes.push(synNode);
        fileNodeMap.set(synId, synNode);
      }
    }
  }

  const degree = new Map<string, number>();
  for (const n of fileNodes) degree.set(n.id, 0);

  const relevantEdges = chunk.edges.filter(
    (e) =>
      passesConfidenceFilter(e.confidence, exactOnly) &&
      isEdgeVisibleInLayers(e, layers) &&
      (fileNodeMap.has(e.source) || fileNodeMap.has(e.target)),
  );
  for (const e of relevantEdges) {
    if (degree.has(e.source)) degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
    if (degree.has(e.target)) degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
  }

  const sortedFileNodes = [...fileNodes].sort((a, b) => {
    const degDiff = (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0);
    if (degDiff !== 0) return degDiff;
    return a.id.localeCompare(b.id);
  });

  const keptFileNodes = sortedFileNodes.slice(0, MAX_FILE_SYMBOLS);
  const extraFileNodes = sortedFileNodes.slice(MAX_FILE_SYMBOLS);
  const keptIds = new Set(keptFileNodes.map((n) => n.id));

  const nodes: ViewNodeData[] = keptFileNodes.map((n) => ({
    id: n.id,
    label: n.label,
    subtitle: `${n.kind} · L${n.line}`,
    kind: n.kind,
    badge: n.confidence === "heuristic" ? "approx" : n.tier,
    tier: n.tier,
    confidence: n.confidence,
    changeStatus: getFileChangeStatus(n.file, overview.delta),
    drillable: true,
    partId,
    groupId,
    filePath: n.file,
    symbolId: n.id,
    evidence: n.evidence,
  }));

  const nodeIds = new Set(nodes.map((n) => n.id));
  const foldId = `fold:file:${filePath}`;
  if (extraFileNodes.length > 0) {
    nodes.push({
      id: foldId,
      label: `+${extraFileNodes.length} more`,
      subtitle: `${extraFileNodes.length} additional symbols`,
      kind: "fold",
      badge: `+${extraFileNodes.length}`,
      foldCount: extraFileNodes.length,
      foldedIds: extraFileNodes.map((n) => n.id),
      drillable: false,
    });
    nodeIds.add(foldId);
  }

  const boundaryMap = new Map(overview.boundaries.map((b) => [b.id, b]));
  const chunkNodeMap = new Map(chunk.nodes.map((n) => [n.id, n]));
  const edgeMap = new Map<string, ViewEdgeData>();

  const ensureNeighbor = (id: string) => {
    if (nodeIds.has(id)) return;
    const cn = chunkNodeMap.get(id);
    if (cn && passesConfidenceFilter(cn.confidence, exactOnly)) {
      nodes.push({
        id: cn.id,
        label: cn.label,
        subtitle: `${cn.file}:${cn.line}`,
        kind: cn.kind,
        badge: cn.tier,
        tier: cn.tier,
        confidence: cn.confidence,
        drillable: true,
        partId,
        groupId: cn.module_id,
        filePath: cn.file,
        symbolId: cn.id,
        evidence: cn.evidence,
      });
      nodeIds.add(cn.id);
      return;
    }
    const b = boundaryMap.get(id);
    if (b && passesConfidenceFilter(b.confidence, exactOnly)) {
      nodes.push({
        id: b.id,
        label: b.label,
        subtitle: `${b.boundary} (${b.kind})`,
        kind: b.boundary,
        badge: b.kind,
        confidence: b.confidence,
        drillable: false,
        evidence: b.evidence,
      });
      nodeIds.add(b.id);
    }
  };

  for (const e of relevantEdges) {
    const src = fileNodeMap.has(e.source) ? (keptIds.has(e.source) ? e.source : foldId) : e.source;
    const tgt = fileNodeMap.has(e.target) ? (keptIds.has(e.target) ? e.target : foldId) : e.target;
    if (src === tgt) continue;
    ensureNeighbor(src);
    ensureNeighbor(tgt);
    if (nodeIds.has(src) && nodeIds.has(tgt)) {
      mergeEdgeIntoMap(edgeMap, {
        id: `${src}->${tgt}`,
        source: src,
        target: tgt,
        kinds: [e.kind],
        label: e.kind,
        count: 1,
        confidence: e.confidence,
        evidence: e.evidence,
      });
    }
  }

  const edges = Array.from(edgeMap.values());
  applyFlowDimming(nodes, edges, activeFlow, flowStepIndex);
  const capped = capNodesForView(nodes, edges, `file:${filePath}`);
  return {
    nodes: capped.nodes,
    edges: capped.edges,
    foldedCount: extraFileNodes.length + capped.foldedCount,
  };
}

export function buildSymbolGraph(
  partId: string,
  symbolId: string,
  opts: ViewBuildOptions,
): ViewGraphInput {
  const { overview, chunks, layers, exactOnly, activeFlow, flowStepIndex } = opts;
  const chunk = chunks[partId];
  if (!chunk) {
    return { nodes: [], edges: [], foldedCount: 0 };
  }

  const chunkNodeMap = new Map(chunk.nodes.map((n) => [n.id, n]));
  const boundaryMap = new Map(overview.boundaries.map((b) => [b.id, b]));
  const filteredEdges = chunk.edges.filter(
    (e) => passesConfidenceFilter(e.confidence, exactOnly) && isEdgeVisibleInLayers(e, layers),
  );

  // Depth-3 outgoing BFS from symbolId + 1-hop incoming callers
  const includedIds = new Set<string>([symbolId]);
  const outgoing = new Map<string, ChunkEdge[]>();
  const incoming = new Map<string, ChunkEdge[]>();

  for (const e of filteredEdges) {
    const out = outgoing.get(e.source) ?? [];
    out.push(e);
    outgoing.set(e.source, out);
    const inc = incoming.get(e.target) ?? [];
    inc.push(e);
    incoming.set(e.target, inc);
  }

  // Incoming callers (1 hop)
  for (const e of incoming.get(symbolId) ?? []) {
    includedIds.add(e.source);
  }

  // Outgoing call tree up to depth 3
  const queue: Array<{ id: string; depth: number }> = [{ id: symbolId, depth: 0 }];
  const visitedDepth = new Map<string, number>([[symbolId, 0]]);
  while (queue.length > 0) {
    const cur = queue.shift()!;
    if (cur.depth >= 3) continue;
    const nextEdges = (outgoing.get(cur.id) ?? []).sort((a, b) =>
      a.target.localeCompare(b.target),
    );
    for (const e of nextEdges) {
      includedIds.add(e.target);
      const prevDepth = visitedDepth.get(e.target);
      if (prevDepth === undefined || cur.depth + 1 < prevDepth) {
        visitedDepth.set(e.target, cur.depth + 1);
        queue.push({ id: e.target, depth: cur.depth + 1 });
      }
    }
  }

  const nodes: ViewNodeData[] = [];
  const nodeIds = new Set<string>();
  for (const id of Array.from(includedIds).sort()) {
    const cn = chunkNodeMap.get(id);
    if (cn) {
      nodes.push({
        id: cn.id,
        label: cn.label,
        subtitle: `${cn.file}:${cn.line}`,
        kind: cn.kind,
        badge: cn.id === symbolId ? "root" : cn.tier,
        tier: cn.tier,
        confidence: cn.confidence,
        highlighted: cn.id === symbolId,
        drillable: cn.id !== symbolId,
        partId,
        groupId: cn.module_id,
        filePath: cn.file,
        symbolId: cn.id,
        evidence: cn.evidence,
      });
      nodeIds.add(cn.id);
      continue;
    }
    const b = boundaryMap.get(id);
    if (b) {
      nodes.push({
        id: b.id,
        label: b.label,
        subtitle: `${b.boundary} (${b.kind})`,
        kind: b.boundary,
        badge: b.kind,
        confidence: b.confidence,
        drillable: false,
        evidence: b.evidence,
      });
      nodeIds.add(b.id);
    }
  }

  const edgeMap = new Map<string, ViewEdgeData>();
  for (const e of filteredEdges) {
    if (nodeIds.has(e.source) && nodeIds.has(e.target)) {
      mergeEdgeIntoMap(edgeMap, {
        id: `${e.source}->${e.target}`,
        source: e.source,
        target: e.target,
        kinds: [e.kind],
        label: e.kind,
        count: 1,
        confidence: e.confidence,
        evidence: e.evidence,
      });
    }
  }

  const edges = Array.from(edgeMap.values());
  applyFlowDimming(nodes, edges, activeFlow, flowStepIndex);
  const capped = capNodesForView(nodes, edges, `symbol:${symbolId}`);
  return { nodes: capped.nodes, edges: capped.edges, foldedCount: capped.foldedCount };
}
