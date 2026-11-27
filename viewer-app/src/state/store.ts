import { create } from "zustand";
import {
  getCachedChunk,
  loadPartChunk,
  loadSearchIndex,
  readOptionalOverlays,
} from "../data/chunks";
import { sortTraceSpans } from "../ui/TraceReplay";
import { buildSymbolGraph, parseFileSymbolId } from "../views/viewGraphs";
import type {
  BreadcrumbItem,
  ChunkFlow,
  DataflowData,
  DescriptionsData,
  LayerFilter,
  LayerName,
  OverviewData,
  PartChunk,
  RuntimeOverlayData,
  SearchTuple,
  ViewLevel,
} from "../types/bundle";

export interface ViewerStoreState {
  overview: OverviewData | null;
  chunks: Record<string, PartChunk>;
  loadingPartId: string | null;
  chunkError: string | null;
  searchIndex: SearchTuple[] | null;
  descriptions: DescriptionsData | null;
  runtime: RuntimeOverlayData | null;
  dataflow: DataflowData | null;

  level: ViewLevel;
  partId: string | null;
  groupId: string | null;
  filePath: string | null;
  symbolId: string | null;
  dirFilter: string | null;
  breadcrumbs: BreadcrumbItem[];

  selectedNodeId: string | null;
  selectedEdgeId: string | null;
  inspectorOpen: boolean;

  layers: LayerFilter;
  exactOnly: boolean;

  activeFlowId: string | null;
  flowStepIndex: number;
  activeFlow: ChunkFlow | null;
  flowFollowStep: boolean;

  activeTraceId: string | null;
  traceSpanIndex: number;

  searchOpen: boolean;
  recentBoardOpen: boolean;

  initFromOverview: (overview: OverviewData) => void;
  refreshOverlays: () => void;
  setRuntimeOverlay: (overlay: RuntimeOverlayData | null) => void;
  ensurePartLoaded: (partId: string) => Promise<PartChunk | null>;
  openSearch: () => Promise<void>;
  closeSearch: () => void;
  toggleRecentBoard: (open?: boolean) => void;

  goToOverview: () => void;
  drillToPart: (partId: string) => Promise<void>;
  drillToGroup: (partId: string, groupId: string, dirFilter?: string | null) => Promise<void>;
  drillToFile: (partId: string, groupId: string, filePath: string) => Promise<void>;
  drillToSymbol: (partId: string, groupId: string, filePath: string, symbolId: string) => Promise<void>;
  goUp: () => void;
  goToBreadcrumb: (index: number) => void;

  selectNode: (nodeId: string | null, updateHash?: boolean) => void;
  selectEdge: (edgeId: string | null) => void;
  closeInspector: () => void;

  toggleLayer: (layer: LayerName) => void;
  setExactOnly: (exactOnly: boolean) => void;

  startFlow: (flowId: string, partIdHint?: string) => Promise<void>;
  nextFlowStep: () => void;
  prevFlowStep: () => void;
  toggleFlowFollowStep: () => void;
  exitFlow: () => void;

  startTrace: (traceId: string) => void;
  nextTraceSpan: () => void;
  prevTraceSpan: () => void;
  exitTrace: () => void;

  navigateToNodeId: (nodeId: string) => Promise<void>;
}

function updateUrlHashForNode(nodeId: string | null): void {
  if (typeof window === "undefined") return;
  const target = nodeId ? `#node=${encodeURIComponent(nodeId)}` : "";
  if (window.location.hash !== target) {
    try {
      if (target) {
        window.history.replaceState(null, "", target);
      } else if (window.location.hash) {
        window.history.replaceState(null, "", window.location.pathname + window.location.search);
      }
    } catch {
      // Ignore history restrictions in sandboxed environments
    }
  }
}

async function followFlowStepToSymbol(
  get: () => ViewerStoreState,
  set: (partial: Partial<ViewerStoreState>) => void,
  flow: ChunkFlow,
  stepIndex: number,
  partIdHint?: string,
): Promise<void> {
  const state = get();
  const { overview } = state;
  if (!overview || flow.steps.length === 0) return;
  const clamped = Math.max(0, Math.min(flow.steps.length - 1, stepIndex));
  const stepNodeId = flow.steps[clamped]?.node_id;
  if (!stepNodeId) return;

  const isStale = (): boolean => {
    const current = get();
    return (
      current.activeFlow !== flow ||
      current.flowStepIndex !== clamped ||
      !current.flowFollowStep
    );
  };

  // If already in Symbol view and the step node is already rendered there, just keep it selected.
  if (state.level === "symbol" && state.partId && state.symbolId) {
    const symGraph = buildSymbolGraph(state.partId, state.symbolId, {
      overview,
      chunks: state.chunks,
      layers: state.layers,
      exactOnly: state.exactOnly,
      runtime: state.runtime,
      dataflow: state.dataflow,
      activeFlow: flow,
      flowStepIndex: clamped,
    });
    if (symGraph.nodes.some((n) => n.id === stepNodeId)) {
      set({ selectedNodeId: stepNodeId, selectedEdgeId: null, inspectorOpen: true });
      return;
    }
  }

  // Pick a chunk symbol to root the Symbol view (if step is a boundary, use prior non-boundary step).
  let anchorId = stepNodeId;
  const isBoundary =
    anchorId.startsWith("ds:") ||
    anchorId.startsWith("ext:") ||
    anchorId.startsWith("svc:") ||
    anchorId.startsWith("res:");
  if (isBoundary) {
    for (let i = clamped - 1; i >= 0; i--) {
      const cand = flow.steps[i]?.node_id;
      if (cand && !cand.startsWith("ds:") && !cand.startsWith("ext:") && !cand.startsWith("svc:") && !cand.startsWith("res:")) {
        anchorId = cand;
        break;
      }
    }
  }

  const flowPartId =
    partIdHint ?? overview.flow_index.find((f) => f.id === flow.id)?.part_id ?? state.partId;
  const searchHit = state.searchIndex?.find((t) => t[0] === anchorId)?.[3];
  const candidateParts = [
    ...(searchHit ? [searchHit] : []),
    ...(flowPartId ? [flowPartId] : []),
    ...Object.keys(state.chunks),
    ...overview.parts.map((p) => p.id),
  ].filter((id, idx, arr) => Boolean(id) && arr.indexOf(id) === idx);

  for (const pid of candidateParts) {
    const chunk = await get().ensurePartLoaded(pid);
    if (isStale()) return;
    if (!chunk) continue;
    const symNode = chunk.nodes.find((n) => n.id === anchorId);
    if (symNode) {
      const grpId = symNode.module_id || overview.groups.find((g) => g.part_id === pid)?.id || "";
      await get().drillToSymbol(pid, grpId, symNode.file, symNode.id);
      if (isStale()) return;
      set({ selectedNodeId: stepNodeId, selectedEdgeId: null, inspectorOpen: true });
      return;
    }
  }
}

export const useViewerStore = create<ViewerStoreState>((set, get) => ({
  overview: null,
  chunks: {},
  loadingPartId: null,
  chunkError: null,
  searchIndex: null,
  descriptions: null,
  runtime: null,
  dataflow: null,

  level: "overview",
  partId: null,
  groupId: null,
  filePath: null,
  symbolId: null,
  dirFilter: null,
  breadcrumbs: [{ level: "overview", id: "overview", label: "Overview" }],

  selectedNodeId: null,
  selectedEdgeId: null,
  inspectorOpen: false,

  layers: {
    control: true,
    data: true,
    integration: true,
    runtime: false,
  },
  exactOnly: false,

  activeFlowId: null,
  flowStepIndex: 0,
  activeFlow: null,
  flowFollowStep: true,

  activeTraceId: null,
  traceSpanIndex: 0,

  searchOpen: false,
  recentBoardOpen: false,

  initFromOverview: (overview) => {
    const overlays = readOptionalOverlays();
    set({
      overview,
      descriptions: overlays.descriptions,
      runtime: overlays.runtime,
      dataflow: overlays.dataflow,
      level: "overview",
      partId: null,
      groupId: null,
      filePath: null,
      symbolId: null,
      dirFilter: null,
      breadcrumbs: [
        { level: "overview", id: "overview", label: overview.repo_name || "Overview" },
      ],
      selectedNodeId: null,
      selectedEdgeId: null,
      inspectorOpen: false,
      activeFlowId: null,
      flowStepIndex: 0,
      activeFlow: null,
      activeTraceId: null,
      traceSpanIndex: 0,
      layers: {
        control: true,
        data: true,
        integration: true,
        runtime: overlays.runtime !== null,
      },
    });
  },

  refreshOverlays: () => {
    const overlays = readOptionalOverlays();
    set((state) => ({
      descriptions: overlays.descriptions,
      runtime: overlays.runtime,
      dataflow: overlays.dataflow,
      layers: {
        ...state.layers,
        runtime: overlays.runtime !== null ? state.layers.runtime : false,
      },
    }));
  },

  setRuntimeOverlay: (overlay) => {
    if (typeof window !== "undefined") {
      window.__ARCH_RUNTIME__ = overlay;
    }
    set((state) => ({
      runtime: overlay,
      activeTraceId: overlay ? state.activeTraceId : null,
      traceSpanIndex: overlay ? state.traceSpanIndex : 0,
      layers: {
        ...state.layers,
        runtime: overlay !== null,
      },
    }));
  },

  ensurePartLoaded: async (partId) => {
    const { overview, chunks } = get();
    if (!overview) return null;
    if (chunks[partId]) return chunks[partId];
    const cached = getCachedChunk(partId);
    if (cached) {
      set((s) => ({ chunks: { ...s.chunks, [partId]: cached } }));
      return cached;
    }
    set({ loadingPartId: partId, chunkError: null });
    try {
      const chunk = await loadPartChunk(partId, overview);
      set((s) => ({
        chunks: { ...s.chunks, [partId]: chunk },
        loadingPartId: s.loadingPartId === partId ? null : s.loadingPartId,
      }));
      return chunk;
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      set((s) => ({
        loadingPartId: s.loadingPartId === partId ? null : s.loadingPartId,
        chunkError: msg,
      }));
      return null;
    }
  },

  openSearch: async () => {
    set({ searchOpen: true });
    if (get().searchIndex === null) {
      try {
        const entries = await loadSearchIndex();
        set({ searchIndex: entries });
      } catch {
        set({ searchIndex: [] });
      }
    }
  },

  closeSearch: () => set({ searchOpen: false }),

  toggleRecentBoard: (open) =>
    set((s) => ({ recentBoardOpen: open !== undefined ? open : !s.recentBoardOpen })),

  goToOverview: () => {
    const repoName = get().overview?.repo_name || "Overview";
    set({
      level: "overview",
      partId: null,
      groupId: null,
      filePath: null,
      symbolId: null,
      dirFilter: null,
      breadcrumbs: [{ level: "overview", id: "overview", label: repoName }],
      selectedEdgeId: null,
    });
  },

  drillToPart: async (partId) => {
    const { overview } = get();
    if (!overview) return;
    const part = overview.parts.find((p) => p.id === partId);
    if (!part) return;
    await get().ensurePartLoaded(partId);
    const repoName = overview.repo_name || "Overview";
    set({
      level: "part",
      partId,
      groupId: null,
      filePath: null,
      symbolId: null,
      dirFilter: null,
      breadcrumbs: [
        { level: "overview", id: "overview", label: repoName },
        { level: "part", id: part.id, label: part.name, partId: part.id },
      ],
      selectedNodeId: partId,
      selectedEdgeId: null,
      inspectorOpen: true,
    });
  },

  drillToGroup: async (partId, groupId, dirFilter = null) => {
    const { overview } = get();
    if (!overview) return;
    const part = overview.parts.find((p) => p.id === partId);
    const group = overview.groups.find((g) => g.id === groupId);
    if (!part || !group) return;
    await get().ensurePartLoaded(partId);
    const repoName = overview.repo_name || "Overview";
    set({
      level: "group",
      partId,
      groupId,
      filePath: null,
      symbolId: null,
      dirFilter,
      breadcrumbs: [
        { level: "overview", id: "overview", label: repoName },
        { level: "part", id: part.id, label: part.name, partId: part.id },
        { level: "group", id: group.id, label: group.name, partId: part.id, groupId: group.id },
      ],
      selectedNodeId: groupId,
      selectedEdgeId: null,
      inspectorOpen: true,
    });
  },

  drillToFile: async (partId, groupId, filePath) => {
    const { overview } = get();
    if (!overview) return;
    const part = overview.parts.find((p) => p.id === partId);
    const group = overview.groups.find((g) => g.id === groupId);
    if (!part || !group) return;
    await get().ensurePartLoaded(partId);
    const repoName = overview.repo_name || "Overview";
    const shortName = filePath.split("/").pop() || filePath;
    set({
      level: "file",
      partId,
      groupId,
      filePath,
      symbolId: null,
      breadcrumbs: [
        { level: "overview", id: "overview", label: repoName },
        { level: "part", id: part.id, label: part.name, partId: part.id },
        { level: "group", id: group.id, label: group.name, partId: part.id, groupId: group.id },
        {
          level: "file",
          id: `file:${filePath}`,
          label: shortName,
          partId: part.id,
          groupId: group.id,
          filePath,
        },
      ],
      selectedNodeId: `file:${filePath}`,
      selectedEdgeId: null,
      inspectorOpen: true,
    });
  },

  drillToSymbol: async (partId, groupId, filePath, symbolId) => {
    const { overview } = get();
    if (!overview) return;
    const part = overview.parts.find((p) => p.id === partId);
    const group = overview.groups.find((g) => g.id === groupId);
    const chunk = await get().ensurePartLoaded(partId);
    if (!part || !group) return;
    const symNode = chunk?.nodes.find((n) => n.id === symbolId);
    const repoName = overview.repo_name || "Overview";
    const shortFile = filePath.split("/").pop() || filePath;
    set({
      level: "symbol",
      partId,
      groupId,
      filePath,
      symbolId,
      breadcrumbs: [
        { level: "overview", id: "overview", label: repoName },
        { level: "part", id: part.id, label: part.name, partId: part.id },
        { level: "group", id: group.id, label: group.name, partId: part.id, groupId: group.id },
        {
          level: "file",
          id: `file:${filePath}`,
          label: shortFile,
          partId: part.id,
          groupId: group.id,
          filePath,
        },
        {
          level: "symbol",
          id: symbolId,
          label: symNode?.label || symbolId,
          partId: part.id,
          groupId: group.id,
          filePath,
          symbolId,
        },
      ],
      selectedNodeId: symbolId,
      selectedEdgeId: null,
      inspectorOpen: true,
    });
  },

  goUp: () => {
    const { breadcrumbs, dirFilter } = get();
    if (dirFilter) {
      set({ dirFilter: null });
      return;
    }
    if (breadcrumbs.length <= 1) return;
    get().goToBreadcrumb(breadcrumbs.length - 2);
  },

  goToBreadcrumb: (index) => {
    const { breadcrumbs } = get();
    const target = breadcrumbs[index];
    if (!target) return;
    const nextCrumbs = breadcrumbs.slice(0, index + 1);
    set({
      level: target.level,
      partId: target.partId ?? null,
      groupId: target.groupId ?? null,
      filePath: target.filePath ?? null,
      symbolId: target.symbolId ?? null,
      dirFilter: null,
      breadcrumbs: nextCrumbs,
      selectedEdgeId: null,
    });
  },

  selectNode: (nodeId, updateHash = true) => {
    set({
      selectedNodeId: nodeId,
      selectedEdgeId: null,
      inspectorOpen: Boolean(nodeId),
    });
    if (updateHash) {
      updateUrlHashForNode(nodeId);
    }
  },

  selectEdge: (edgeId) => {
    set({
      selectedEdgeId: edgeId,
      selectedNodeId: null,
      inspectorOpen: Boolean(edgeId),
    });
  },

  closeInspector: () => set({ inspectorOpen: false }),

  toggleLayer: (layer) =>
    set((s) => ({
      layers: {
        ...s.layers,
        [layer]: !s.layers[layer],
      },
    })),

  setExactOnly: (exactOnly) => set({ exactOnly }),

  startFlow: async (flowId, partIdHint) => {
    const { overview } = get();
    if (!overview) return;
    const flowIdx = overview.flow_index.find((f) => f.id === flowId);
    const targetPartId = partIdHint ?? flowIdx?.part_id;
    if (!targetPartId) return;
    const chunk = await get().ensurePartLoaded(targetPartId);
    const flow = chunk?.flows.find((f) => f.id === flowId) ?? null;
    if (!flow) return;
    const entryNodeId = flow.steps[0]?.node_id ?? flow.entry_id;
    set({
      activeFlowId: flow.id,
      flowStepIndex: 0,
      activeFlow: flow,
      selectedNodeId: entryNodeId,
      selectedEdgeId: null,
      inspectorOpen: true,
    });
    if (get().flowFollowStep) {
      await followFlowStepToSymbol(get, set, flow, 0, targetPartId);
    }
  },

  nextFlowStep: () => {
    const { activeFlow, flowStepIndex, flowFollowStep } = get();
    if (!activeFlow || activeFlow.steps.length === 0) return;
    const nextIdx = Math.min(activeFlow.steps.length - 1, flowStepIndex + 1);
    const stepNodeId = activeFlow.steps[nextIdx]?.node_id ?? null;
    set({
      flowStepIndex: nextIdx,
      selectedNodeId: stepNodeId,
      selectedEdgeId: null,
      inspectorOpen: true,
    });
    if (flowFollowStep) {
      void followFlowStepToSymbol(get, set, activeFlow, nextIdx);
    }
  },

  prevFlowStep: () => {
    const { activeFlow, flowStepIndex, flowFollowStep } = get();
    if (!activeFlow || activeFlow.steps.length === 0) return;
    const prevIdx = Math.max(0, flowStepIndex - 1);
    const stepNodeId = activeFlow.steps[prevIdx]?.node_id ?? null;
    set({
      flowStepIndex: prevIdx,
      selectedNodeId: stepNodeId,
      selectedEdgeId: null,
      inspectorOpen: true,
    });
    if (flowFollowStep) {
      void followFlowStepToSymbol(get, set, activeFlow, prevIdx);
    }
  },

  toggleFlowFollowStep: () => {
    const nextVal = !get().flowFollowStep;
    set({ flowFollowStep: nextVal });
    const { activeFlow, flowStepIndex } = get();
    if (nextVal && activeFlow) {
      void followFlowStepToSymbol(get, set, activeFlow, flowStepIndex);
    }
  },

  exitFlow: () => set({ activeFlowId: null, flowStepIndex: 0, activeFlow: null }),

  startTrace: (traceId) => {
    const { runtime } = get();
    const trace = runtime?.traces.find((t) => t.trace_id === traceId);
    if (!trace) return;
    const sorted = sortTraceSpans(trace.spans);
    const firstNodeId = sorted[0]?.node_id ?? null;
    set((s) => ({
      activeTraceId: traceId,
      traceSpanIndex: 0,
      activeFlowId: null,
      activeFlow: null,
      flowStepIndex: 0,
      selectedNodeId: firstNodeId ?? s.selectedNodeId,
      selectedEdgeId: null,
      inspectorOpen: Boolean(firstNodeId),
      layers: {
        ...s.layers,
        runtime: true,
      },
    }));
  },

  nextTraceSpan: () => {
    const { runtime, activeTraceId, traceSpanIndex } = get();
    const trace = runtime?.traces.find((t) => t.trace_id === activeTraceId);
    if (!trace || trace.spans.length === 0) return;
    const sorted = sortTraceSpans(trace.spans);
    const nextIdx = Math.min(sorted.length - 1, traceSpanIndex + 1);
    const nodeId = sorted[nextIdx]?.node_id ?? null;
    set((s) => ({
      traceSpanIndex: nextIdx,
      selectedNodeId: nodeId ?? s.selectedNodeId,
      selectedEdgeId: null,
      inspectorOpen: Boolean(nodeId) || s.inspectorOpen,
    }));
  },

  prevTraceSpan: () => {
    const { runtime, activeTraceId, traceSpanIndex } = get();
    const trace = runtime?.traces.find((t) => t.trace_id === activeTraceId);
    if (!trace || trace.spans.length === 0) return;
    const sorted = sortTraceSpans(trace.spans);
    const prevIdx = Math.max(0, traceSpanIndex - 1);
    const nodeId = sorted[prevIdx]?.node_id ?? null;
    set((s) => ({
      traceSpanIndex: prevIdx,
      selectedNodeId: nodeId ?? s.selectedNodeId,
      selectedEdgeId: null,
      inspectorOpen: Boolean(nodeId) || s.inspectorOpen,
    }));
  },

  exitTrace: () => set({ activeTraceId: null, traceSpanIndex: 0 }),

  navigateToNodeId: async (nodeId) => {
    const { overview } = get();
    if (!overview) return;

    // 1. Overview-level part
    const part = overview.parts.find((p) => p.id === nodeId);
    if (part) {
      await get().drillToPart(part.id);
      get().selectNode(part.id, true);
      return;
    }

    // 2. Overview-level boundary
    const boundary = overview.boundaries.find((b) => b.id === nodeId);
    if (boundary) {
      get().goToOverview();
      get().selectNode(boundary.id, true);
      return;
    }

    // 3. Overview-level group (mod:*)
    const group = overview.groups.find((g) => g.id === nodeId);
    if (group) {
      await get().drillToGroup(group.part_id, group.id);
      get().selectNode(group.id, true);
      return;
    }

    // 4. File id (`file:<path>`) or symbol/entrypoint inside a part chunk
    let targetPartId: string | null = null;
    if (get().searchIndex) {
      const hit = get().searchIndex?.find((t) => t[0] === nodeId);
      if (hit) targetPartId = hit[3];
    }

    // Check already loaded chunks first, then search index, then remaining parts
    const candidatePartIds = [
      ...(targetPartId ? [targetPartId] : []),
      ...Object.keys(get().chunks),
      ...overview.parts.map((p) => p.id),
    ].filter((id, idx, arr) => Boolean(id) && arr.indexOf(id) === idx);

    for (const pid of candidatePartIds) {
      const chunk = await get().ensurePartLoaded(pid);
      if (!chunk) continue;

      if (nodeId.startsWith("file:")) {
        const fp = nodeId.slice(5);
        const fRow = chunk.files.find((f) => f.path === fp);
        if (fRow) {
          await get().drillToFile(pid, fRow.module_id, fRow.path);
          get().selectNode(nodeId, true);
          return;
        }
      }

      const symNode = chunk.nodes.find((n) => n.id === nodeId);
      if (symNode) {
        const grpId = symNode.module_id || overview.groups.find((g) => g.part_id === pid)?.id || "";
        await get().drillToSymbol(pid, grpId, symNode.file, symNode.id);
        get().selectNode(symNode.id, true);
        return;
      }

      // File-view symbols (including folded items listed in the Inspector) carry an id of
      // the form `sym:<path>:<name>:<line>` and are not chunk graph nodes.
      if (nodeId.startsWith("sym:")) {
        const [fp, name, line] = parseFileSymbolId(nodeId);
        const fRow = chunk.files.find(
          (f) =>
            f.path === fp &&
            f.symbols.some(
              (s) => s.name === name && (!line || s.line === Number(line)),
            ),
        );
        if (fRow) {
          await get().drillToSymbol(pid, fRow.module_id, fRow.path, nodeId);
          get().selectNode(nodeId, true);
          return;
        }
      }
    }
  },
}));
