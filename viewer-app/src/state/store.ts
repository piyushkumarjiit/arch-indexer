import { create } from "zustand";
import {
  getCachedChunk,
  loadPartChunk,
  loadSearchIndex,
  readOptionalOverlays,
} from "../data/chunks";
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

  searchOpen: boolean;
  recentBoardOpen: boolean;

  initFromOverview: (overview: OverviewData) => void;
  refreshOverlays: () => void;
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
  exitFlow: () => void;

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
    const entryNode = chunk?.nodes.find((n) => n.id === entryNodeId);
    if (entryNode && get().level === "overview") {
      await get().drillToPart(targetPartId);
    }
    set({
      activeFlowId: flow.id,
      flowStepIndex: 0,
      activeFlow: flow,
      selectedNodeId: entryNodeId,
      inspectorOpen: true,
    });
  },

  nextFlowStep: () => {
    const { activeFlow, flowStepIndex } = get();
    if (!activeFlow || activeFlow.steps.length === 0) return;
    const nextIdx = Math.min(activeFlow.steps.length - 1, flowStepIndex + 1);
    const stepNodeId = activeFlow.steps[nextIdx]?.node_id ?? null;
    set({
      flowStepIndex: nextIdx,
      selectedNodeId: stepNodeId,
    });
  },

  prevFlowStep: () => {
    const { activeFlow, flowStepIndex } = get();
    if (!activeFlow || activeFlow.steps.length === 0) return;
    const prevIdx = Math.max(0, flowStepIndex - 1);
    const stepNodeId = activeFlow.steps[prevIdx]?.node_id ?? null;
    set({
      flowStepIndex: prevIdx,
      selectedNodeId: stepNodeId,
    });
  },

  exitFlow: () => set({ activeFlowId: null, flowStepIndex: 0, activeFlow: null }),

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
    }
  },
}));
