import { useEffect, useMemo, useRef } from "react";
import { seedCachedChunk } from "./data/chunks";
import { useViewerStore } from "./state/store";
import type { OverviewData, PartChunk } from "./types/bundle";
import {
  ChangeOverlayButton,
  ChangeOverlayPanel,
  collectChangeItems,
} from "./ui/ChangeBadges";
import { FlowBanner } from "./ui/FlowBanner";
import { Inspector } from "./ui/Inspector";
import { LayerToggles } from "./ui/LayerToggles";
import { SearchPalette } from "./ui/SearchPalette";
import { Tier2Banner } from "./ui/Tier2Banner";
import { FileView } from "./views/File";
import { GroupView } from "./views/Group";
import { OverviewView } from "./views/Overview";
import { PartView } from "./views/Part";
import { SymbolView } from "./views/Symbol";
import {
  buildFileGraph,
  buildGroupGraph,
  buildOverviewGraph,
  buildPartGraph,
  buildSymbolGraph,
} from "./views/viewGraphs";

export function readEmbeddedOverview(): OverviewData | null {
  if (typeof window === "undefined") return null;
  if (window.__ARCH_OVERVIEW__ && window.__ARCH_OVERVIEW__.schema_version) {
    return window.__ARCH_OVERVIEW__;
  }
  const el = document.getElementById("arch-overview-data");
  if (!el || !el.textContent) return null;
  try {
    const parsed = JSON.parse(el.textContent) as Partial<OverviewData>;
    if (parsed && typeof parsed === "object" && parsed.schema_version && Array.isArray(parsed.parts)) {
      return parsed as OverviewData;
    }
  } catch {
    // ignore malformed placeholder
  }
  return null;
}

export function parseHashNodeId(hash: string): string | null {
  if (!hash.startsWith("#node=")) return null;
  const raw = hash.slice(6);
  if (!raw) return null;
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

export function App() {
  const overview = useViewerStore((s) => s.overview);
  const chunks = useViewerStore((s) => s.chunks);
  const searchIndex = useViewerStore((s) => s.searchIndex);
  const descriptions = useViewerStore((s) => s.descriptions);
  const runtime = useViewerStore((s) => s.runtime);
  const dataflow = useViewerStore((s) => s.dataflow);
  const level = useViewerStore((s) => s.level);
  const partId = useViewerStore((s) => s.partId);
  const groupId = useViewerStore((s) => s.groupId);
  const filePath = useViewerStore((s) => s.filePath);
  const symbolId = useViewerStore((s) => s.symbolId);
  const dirFilter = useViewerStore((s) => s.dirFilter);
  const breadcrumbs = useViewerStore((s) => s.breadcrumbs);
  const selectedNodeId = useViewerStore((s) => s.selectedNodeId);
  const selectedEdgeId = useViewerStore((s) => s.selectedEdgeId);
  const inspectorOpen = useViewerStore((s) => s.inspectorOpen);
  const layers = useViewerStore((s) => s.layers);
  const exactOnly = useViewerStore((s) => s.exactOnly);
  const activeFlow = useViewerStore((s) => s.activeFlow);
  const flowStepIndex = useViewerStore((s) => s.flowStepIndex);
  const flowFollowStep = useViewerStore((s) => s.flowFollowStep);
  const searchOpen = useViewerStore((s) => s.searchOpen);
  const recentBoardOpen = useViewerStore((s) => s.recentBoardOpen);
  const chunkError = useViewerStore((s) => s.chunkError);

  const initFromOverview = useViewerStore((s) => s.initFromOverview);
  const refreshOverlays = useViewerStore((s) => s.refreshOverlays);
  const openSearch = useViewerStore((s) => s.openSearch);
  const closeSearch = useViewerStore((s) => s.closeSearch);
  const toggleRecentBoard = useViewerStore((s) => s.toggleRecentBoard);
  const goToBreadcrumb = useViewerStore((s) => s.goToBreadcrumb);
  const goUp = useViewerStore((s) => s.goUp);
  const drillToPart = useViewerStore((s) => s.drillToPart);
  const drillToGroup = useViewerStore((s) => s.drillToGroup);
  const drillToFile = useViewerStore((s) => s.drillToFile);
  const drillToSymbol = useViewerStore((s) => s.drillToSymbol);
  const closeInspector = useViewerStore((s) => s.closeInspector);
  const toggleLayer = useViewerStore((s) => s.toggleLayer);
  const setExactOnly = useViewerStore((s) => s.setExactOnly);
  const startFlow = useViewerStore((s) => s.startFlow);
  const nextFlowStep = useViewerStore((s) => s.nextFlowStep);
  const prevFlowStep = useViewerStore((s) => s.prevFlowStep);
  const toggleFlowFollowStep = useViewerStore((s) => s.toggleFlowFollowStep);
  const exitFlow = useViewerStore((s) => s.exitFlow);
  const navigateToNodeId = useViewerStore((s) => s.navigateToNodeId);

  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const initial = readEmbeddedOverview();
    if (initial) {
      initFromOverview(initial);
      const initialNode = parseHashNodeId(window.location.hash);
      if (initialNode) {
        void navigateToNodeId(initialNode);
      }
    } else {
      refreshOverlays();
    }

    const onHashChange = () => {
      const targetId = parseHashNodeId(window.location.hash);
      if (targetId) {
        void navigateToNodeId(targetId);
      }
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [initFromOverview, navigateToNodeId, refreshOverlays]);

  const currentGraph = useMemo(() => {
    if (!overview) return { nodes: [], edges: [], foldedCount: 0 };
    const opts = {
      overview,
      chunks,
      layers,
      exactOnly,
      runtime,
      dataflow,
      activeFlow,
      flowStepIndex,
    };
    switch (level) {
      case "part":
        return partId ? buildPartGraph(partId, opts) : { nodes: [], edges: [], foldedCount: 0 };
      case "group":
        return partId && groupId
          ? buildGroupGraph(partId, groupId, dirFilter, opts)
          : { nodes: [], edges: [], foldedCount: 0 };
      case "file":
        return partId && groupId && filePath
          ? buildFileGraph(partId, groupId, filePath, opts)
          : { nodes: [], edges: [], foldedCount: 0 };
      case "symbol":
        return partId && symbolId
          ? buildSymbolGraph(partId, symbolId, opts)
          : { nodes: [], edges: [], foldedCount: 0 };
      case "overview":
      default:
        return buildOverviewGraph(opts);
    }
  }, [
    overview,
    chunks,
    layers,
    exactOnly,
    runtime,
    dataflow,
    activeFlow,
    flowStepIndex,
    level,
    partId,
    groupId,
    dirFilter,
    filePath,
    symbolId,
  ]);

  const selectedNode = useMemo(
    () => currentGraph.nodes.find((n) => n.id === selectedNodeId) ?? null,
    [currentGraph.nodes, selectedNodeId],
  );
  const selectedEdge = useMemo(
    () => currentGraph.edges.find((e) => e.id === selectedEdgeId) ?? null,
    [currentGraph.edges, selectedEdgeId],
  );

  // Global keyboard shortcuts
  useEffect(() => {
    const onKeyDown = (ev: KeyboardEvent) => {
      const target = ev.target as HTMLElement | null;
      // Checkboxes/radios/buttons keep global shortcuts alive: the flow "Follow step"
      // toggle is a checkbox, and focusing it must not swallow arrow-key flow stepping.
      const nonTextInputType =
        target &&
        target.tagName === "INPUT" &&
        ["checkbox", "radio", "button", "submit", "reset", "range"].includes(
          (target as HTMLInputElement).type,
        );
      const inInput =
        target &&
        !nonTextInputType &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable);

      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") {
        ev.preventDefault();
        void openSearch();
        return;
      }

      if (inInput) return;

      if (ev.key === "/") {
        ev.preventDefault();
        void openSearch();
      } else if (ev.key === "Escape") {
        if (searchOpen) {
          closeSearch();
        } else if (activeFlow) {
          exitFlow();
        } else if (inspectorOpen) {
          closeInspector();
        }
      } else if (ev.key === "Backspace") {
        ev.preventDefault();
        goUp();
      } else if (ev.key === "ArrowLeft" && activeFlow) {
        ev.preventDefault();
        prevFlowStep();
      } else if (ev.key === "ArrowRight" && activeFlow) {
        ev.preventDefault();
        nextFlowStep();
      } else if (ev.key === "Enter" && selectedNode && selectedNode.drillable) {
        ev.preventDefault();
        if (level === "overview" && selectedNode.partId) {
          void drillToPart(selectedNode.partId);
        } else if (level === "part" && selectedNode.groupId && selectedNode.partId) {
          void drillToGroup(selectedNode.partId, selectedNode.groupId);
        } else if (level === "part" && selectedNode.partId && selectedNode.partId !== partId) {
          void drillToPart(selectedNode.partId);
        } else if (level === "group" && partId && groupId) {
          if (selectedNode.kind === "dir_chip" && selectedNode.filePath) {
            void drillToGroup(partId, groupId, selectedNode.filePath);
          } else if (selectedNode.filePath) {
            void drillToFile(partId, groupId, selectedNode.filePath);
          }
        } else if (level === "file" && partId && selectedNode.symbolId && selectedNode.filePath) {
          void drillToSymbol(partId, selectedNode.groupId ?? groupId ?? "", selectedNode.filePath, selectedNode.symbolId);
        } else if (level === "symbol" && partId && selectedNode.symbolId && selectedNode.filePath) {
          void drillToSymbol(partId, selectedNode.groupId ?? groupId ?? "", selectedNode.filePath, selectedNode.symbolId);
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    activeFlow,
    closeInspector,
    closeSearch,
    drillToFile,
    drillToGroup,
    drillToPart,
    drillToSymbol,
    exitFlow,
    goUp,
    groupId,
    inspectorOpen,
    level,
    nextFlowStep,
    openSearch,
    partId,
    prevFlowStep,
    searchOpen,
    selectedNode,
  ]);

  const handleLoadJsonFile = async (file: File) => {
    try {
      const text = await file.text();
      const parsed = JSON.parse(text) as Record<string, unknown>;
      if (parsed.schema_version === "2.0" && Array.isArray(parsed.parts)) {
        initFromOverview(parsed as unknown as OverviewData);
      } else if (parsed.schema_version === "2.0" && typeof parsed.part_id === "string") {
        seedCachedChunk(parsed as unknown as PartChunk);
      } else if (parsed.entries && typeof parsed.entries === "object") {
        window.__ARCH_DESCRIPTIONS__ = parsed as unknown as typeof window.__ARCH_DESCRIPTIONS__;
        refreshOverlays();
      } else if (parsed.node_metrics && parsed.edge_metrics) {
        window.__ARCH_RUNTIME__ = parsed as unknown as typeof window.__ARCH_RUNTIME__;
        refreshOverlays();
      } else if (Array.isArray(parsed.edges) && typeof parsed.tool === "string") {
        window.__ARCH_DATAFLOW__ = parsed as unknown as typeof window.__ARCH_DATAFLOW__;
        refreshOverlays();
      }
    } catch {
      // Ignore malformed dropped files
    }
  };

  const changeItems = useMemo(
    () => collectChangeItems(overview, chunks),
    [overview, chunks],
  );

  const flowTitle = activeFlow
    ? descriptions?.entries?.[activeFlow.id]?.title ?? activeFlow.entry_id
    : undefined;

  return (
    <div
      className="arch-app"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        const files = Array.from(e.dataTransfer.files ?? []);
        for (const f of files) void handleLoadJsonFile(f);
      }}
    >
      <header className="arch-topbar">
        <div className="arch-topbar-left">
          <div className="arch-brand">
            <span>ARCH</span>
            <span className="arch-brand-badge">v2</span>
          </div>
          <span className="arch-repo-name">
            {overview?.repo_name || "No bundle loaded"}
          </span>
        </div>

        <div className="arch-topbar-center">
          <button
            type="button"
            className="arch-search-trigger"
            onClick={() => void openSearch()}
          >
            <span>Search nodes, routes, files...</span>
            <kbd>/</kbd>
          </button>
        </div>

        <div className="arch-topbar-right">
          <ChangeOverlayButton
            count={changeItems.length}
            active={recentBoardOpen}
            onClick={() => toggleRecentBoard()}
          />
          <button
            type="button"
            className="arch-btn"
            onClick={() => fileInputRef.current?.click()}
          >
            Load JSON
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".json,application/json"
            style={{ display: "none" }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void handleLoadJsonFile(f);
            }}
          />
        </div>
      </header>

      <div className="arch-subbar">
        <nav className="arch-breadcrumbs" aria-label="Breadcrumbs">
          {breadcrumbs.map((crumb, idx) => {
            const isLast = idx === breadcrumbs.length - 1;
            return (
              <span key={`${crumb.level}:${crumb.id}`} className="arch-crumb-wrap">
                <button
                  type="button"
                  className={`arch-crumb ${isLast ? "is-active" : ""}`}
                  onClick={() => goToBreadcrumb(idx)}
                >
                  {crumb.label}
                </button>
                {!isLast && <span className="arch-crumb-sep">&rsaquo;</span>}
              </span>
            );
          })}
          <span className="arch-level-badge">{level}</span>
        </nav>

        <LayerToggles
          layers={layers}
          hasRuntimeOverlay={runtime !== null}
          exactOnly={exactOnly}
          onToggleLayer={toggleLayer}
          onToggleExactOnly={setExactOnly}
        />
      </div>

      {overview && (
        <Tier2Banner
          tier2Modules={overview.tier_summary.tier2_modules}
          onSelectModule={(modId) => void navigateToNodeId(modId)}
        />
      )}

      {chunkError && (
        <div className="arch-error-banner" role="alert">
          {chunkError}
        </div>
      )}

      <main className="arch-main">
        {!overview ? (
          <div className="arch-empty-canvas">
            <h2>Architecture Viewer v2</h2>
            <p>Drop or load an overview.json file to explore the architecture bundle.</p>
          </div>
        ) : (
          <>
            {level === "overview" && <OverviewView />}
            {level === "part" && <PartView />}
            {level === "group" && <GroupView />}
            {level === "file" && <FileView />}
            {level === "symbol" && <SymbolView />}
          </>
        )}

        {recentBoardOpen && (
          <ChangeOverlayPanel
            items={changeItems}
            onSelect={(id) => {
              toggleRecentBoard(false);
              void navigateToNodeId(id);
            }}
            onClose={() => toggleRecentBoard(false)}
          />
        )}

        <FlowBanner
          flow={activeFlow}
          stepIndex={flowStepIndex}
          flowTitle={flowTitle}
          followStep={flowFollowStep}
          onToggleFollowStep={toggleFlowFollowStep}
          onPrev={prevFlowStep}
          onNext={nextFlowStep}
          onExit={exitFlow}
        />

        <Inspector
          open={inspectorOpen}
          overview={overview}
          chunks={chunks}
          selectedNode={selectedNode}
          selectedNodeId={selectedNodeId}
          selectedEdge={selectedEdge}
          visibleEdges={currentGraph.edges}
          descriptions={descriptions}
          runtime={runtime}
          dataflow={dataflow}
          onClose={closeInspector}
          onSelectNode={(id) => void navigateToNodeId(id)}
          onDrillNode={(node) => {
            if (level === "overview" && node.partId) {
              void drillToPart(node.partId);
            } else if (level === "part" && node.groupId && node.partId) {
              void drillToGroup(node.partId, node.groupId);
            } else if (level === "part" && node.partId && node.partId !== partId) {
              void drillToPart(node.partId);
            } else if (level === "group" && partId && groupId) {
              if (node.kind === "dir_chip" && node.filePath) {
                void drillToGroup(partId, groupId, node.filePath);
              } else if (node.filePath) {
                void drillToFile(partId, groupId, node.filePath);
              }
            } else if ((level === "file" || level === "symbol") && partId && node.symbolId && node.filePath) {
              void drillToSymbol(partId, node.groupId ?? groupId ?? "", node.filePath, node.symbolId);
            } else {
              void navigateToNodeId(node.id);
            }
          }}
          onStartFlow={(fid, pid) => void startFlow(fid, pid)}
        />
      </main>

      <SearchPalette
        open={searchOpen}
        overview={overview}
        searchIndex={searchIndex}
        onClose={closeSearch}
        onSelectNode={(id) => void navigateToNodeId(id)}
      />
    </div>
  );
}
