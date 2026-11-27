import { readFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { edgeStrokeColor, filterKindsByLayers } from "../src/data/layers";
import { useViewerStore } from "../src/state/store";
import type { OverviewData, PartChunk, ViewNodeData } from "../src/types/bundle";
import { FlowBanner } from "../src/ui/FlowBanner";
import { Inspector } from "../src/ui/Inspector";
import { Tier2Banner } from "../src/ui/Tier2Banner";
import { GraphCanvas } from "../src/views/GraphCanvas";
import { buildGroupGraph, buildOverviewGraph, buildPartGraph } from "../src/views/viewGraphs";

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", ResizeObserverMock);
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const FQD_DIR = path.resolve(__dirname, "../fixtures/fqd");

function loadFqd(): { overview: OverviewData; chunks: Record<string, PartChunk> } {
  const overview = JSON.parse(
    readFileSync(path.join(FQD_DIR, "overview.json"), "utf-8"),
  ) as OverviewData;
  const chunks: Record<string, PartChunk> = {};
  for (const [, meta] of Object.entries(overview.chunks)) {
    const code = readFileSync(path.join(FQD_DIR, "chunks", meta.file), "utf-8");
    const sandbox: { window: Record<string, unknown> } = { window: {} };
    sandbox.window.__ARCH_CHUNK__ = (id: string, payload: PartChunk) => {
      chunks[id] = payload;
    };
    vm.runInNewContext(code, sandbox);
  }
  return { overview, chunks };
}

const DEFAULT_LAYERS = {
  control: true,
  data: true,
  integration: true,
  runtime: false,
};

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(async () => {
  if (root) {
    await act(async () => root?.unmount());
  }
  container?.remove();
  root = null;
  container = null;
});

describe("Slice 4 Neon design decisions (Q1-Q9)", () => {
  it("Q1: renders ELK-derived swimlanes for non-empty Part view bands and omits empty ones", async () => {
    const nodes: ViewNodeData[] = [
      { id: "part:web", label: "web", kind: "ui", partition: 0, drillable: true },
      { id: "mod:server", label: "server", kind: "group", partition: 1, drillable: true },
    ];
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root!.render(
        <GraphCanvas
          viewKey="part:server"
          nodes={nodes}
          edges={[{ id: "part:web->mod:server", source: "part:web", target: "mod:server", kinds: ["http_request"], count: 1 }]}
          showSwimlanes
          selectedNodeId={null}
          onSelectNode={() => {}}
          onSelectEdge={() => {}}
          onDrillNode={() => {}}
        />,
      );
    });

    await vi.waitFor(() => {
      expect(container!.querySelector('[data-testid="swimlane-p0"]')).not.toBeNull();
      expect(container!.querySelector('[data-testid="swimlane-p1"]')).not.toBeNull();
    });
    expect(container!.querySelector('[data-testid="swimlane-p2"]')).toBeNull();
    expect(container!.querySelector('[data-testid="swimlane-p0"]')?.textContent).toContain("Used by");
    expect(container!.querySelector('[data-testid="swimlane-p1"]')?.textContent).toContain("Inside");
  });

  it("Q2: Inspector renders clickable folded item jump links for fold nodes", async () => {
    const { overview, chunks } = loadFqd();
    const onSelectNode = vi.fn();
    const foldNode: ViewNodeData = {
      id: "fold:file:server/src/app.ts",
      label: "+2 more",
      kind: "fold",
      foldCount: 2,
      foldedIds: ["ep:GET:/api/accounts", "sym:server.Store.listAccounts"],
      drillable: false,
    };

    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root!.render(
        <Inspector
          open
          overview={overview}
          chunks={chunks}
          selectedNode={foldNode}
          selectedEdge={null}
          visibleEdges={[]}
          descriptions={null}
          runtime={null}
          dataflow={null}
          onClose={() => {}}
          onSelectNode={onSelectNode}
          onStartFlow={() => {}}
        />,
      );
    });

    const section = container!.querySelector('[data-testid="folded-items-section"]');
    expect(section).not.toBeNull();
    const jumpBtns = section!.querySelectorAll<HTMLButtonElement>("button.arch-link-item");
    expect(jumpBtns.length).toBe(2);
    expect(jumpBtns[0].textContent).toContain("GET /api/accounts");
    await act(async () => {
      jumpBtns[0].click();
    });
    expect(onSelectNode).toHaveBeenCalledWith("ep:GET:/api/accounts");
  });

  it("Q3 & S2#6: filters merged edge kinds/counts by active layers and colors mixed edges slate", () => {
    const { overview, chunks } = loadFqd();
    overview.edges = [
      {
        source: "mod:server",
        target: "ds:sqlite",
        kinds: { calls: 2, reads: 3 },
        evidence_sample: [{ file: "server/src/store.ts", line: 22, source: "scip" }],
      },
    ];

    const bothOn = buildOverviewGraph({
      overview,
      chunks,
      layers: DEFAULT_LAYERS,
      exactOnly: false,
      runtime: null,
      dataflow: null,
      activeFlow: null,
      flowStepIndex: 0,
    });
    const merged = bothOn.edges.find((e) => e.id === "part:server->ds:sqlite")!;
    expect(merged.kinds).toEqual(["calls", "reads"]);
    expect(merged.count).toBe(5);
    expect(merged.kindCounts).toEqual({ calls: 2, reads: 3 });
    expect(edgeStrokeColor(merged, DEFAULT_LAYERS)).toBe("#94a3b8");

    const dataOffLayers = { ...DEFAULT_LAYERS, data: false };
    const filteredView = buildOverviewGraph({
      overview,
      chunks,
      layers: dataOffLayers,
      exactOnly: false,
      runtime: null,
      dataflow: null,
      activeFlow: null,
      flowStepIndex: 0,
    });
    const filteredEdge = filteredView.edges.find((e) => e.id === "part:server->ds:sqlite")!;
    expect(filteredEdge.kinds).toEqual(["calls"]);
    expect(filteredEdge.count).toBe(2);
    expect(filteredEdge.label).toBe("calls (2)");
    expect(edgeStrokeColor(filteredEdge, dataOffLayers)).toBe("#38bdf8");

    const partView = buildPartGraph("part:server", {
      overview,
      chunks,
      layers: dataOffLayers,
      exactOnly: false,
      runtime: null,
      dataflow: null,
      activeFlow: null,
      flowStepIndex: 0,
    });
    expect(partView.edges[0]?.kinds).toEqual(["calls"]);
    expect(filterKindsByLayers({ reads: 2 }, dataOffLayers).kinds).toEqual([]);
  });

  it("Q4: hides boundary nodes when their layer is off and no active edge connects them", () => {
    const { overview, chunks } = loadFqd();
    const noData = buildOverviewGraph({
      overview,
      chunks,
      layers: { ...DEFAULT_LAYERS, data: false },
      exactOnly: false,
      runtime: null,
      dataflow: null,
      activeFlow: null,
      flowStepIndex: 0,
    });
    expect(noData.nodes.some((n) => n.id === "ds:sqlite")).toBe(false);
    expect(noData.nodes.some((n) => n.id === "res:path:usage_db")).toBe(false);
    expect(noData.nodes.some((n) => n.id === "ext:http")).toBe(true);
  });

  it("Q5: FlowBanner has Follow step toggle (default true) and store auto-drills or stays per toggle", async () => {
    const { overview, chunks } = loadFqd();
    useViewerStore.getState().initFromOverview(overview);
    useViewerStore.setState({ chunks, flowFollowStep: true });

    const bannerHtml = renderToStaticMarkup(
      <FlowBanner
        flow={chunks["part:server"].flows[0]}
        stepIndex={0}
        followStep
        onToggleFollowStep={() => {}}
        onPrev={() => {}}
        onNext={() => {}}
        onExit={() => {}}
      />,
    );
    expect(bannerHtml).toContain("Follow step");
    expect(bannerHtml).toContain('data-testid="flow-follow-toggle"');

    // With followStep=true, startFlow drills straight to Symbol view
    await useViewerStore.getState().startFlow("flow:ep:GET:/api/accounts#1", "part:server");
    expect(useViewerStore.getState().level).toBe("symbol");
    expect(useViewerStore.getState().symbolId).toBe("ep:GET:/api/accounts");

    // With followStep=false, staying on Overview keeps level=overview while Inspector resolves chunk node
    useViewerStore.getState().goToOverview();
    useViewerStore.setState({ flowFollowStep: false });
    await useViewerStore.getState().startFlow("flow:ep:GET:/api/accounts#1", "part:server");
    expect(useViewerStore.getState().level).toBe("overview");
    expect(useViewerStore.getState().selectedNodeId).toBe("ep:GET:/api/accounts");

    const inspHtml = renderToStaticMarkup(
      <Inspector
        open
        overview={overview}
        chunks={chunks}
        selectedNode={null}
        selectedNodeId="ep:GET:/api/accounts"
        selectedEdge={null}
        visibleEdges={[]}
        descriptions={null}
        runtime={null}
        dataflow={null}
        onClose={() => {}}
        onSelectNode={() => {}}
        onStartFlow={() => {}}
      />,
    );
    expect(inspHtml).toContain("GET /api/accounts");
    expect(inspHtml).toContain("server/src/app.ts:34");
  });

  it("Q7: Tier2Banner collapses to persistent pill and caps expanded chips at 12 + more", async () => {
    const manyMods = Array.from({ length: 15 }, (_, i) => `mod:m${String(i).padStart(2, "0")}`);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root!.render(<Tier2Banner tier2Modules={manyMods} />);
    });

    const showBtn = Array.from(container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Show modules"),
    )!;
    await act(async () => {
      showBtn.click();
    });
    expect(container.querySelectorAll('[data-testid="tier2-list"] .arch-chip').length).toBe(12);
    expect(container.querySelector('[data-testid="tier2-more"]')?.textContent).toBe("+3 more");

    const collapseBtn = container.querySelector<HTMLButtonElement>('[data-testid="tier2-collapse-btn"]')!;
    await act(async () => {
      collapseBtn.click();
    });
    const pill = container.querySelector<HTMLButtonElement>('[data-testid="tier2-pill"]')!;
    expect(pill).not.toBeNull();
    expect(pill.textContent).toContain("15 Tier-2");

    await act(async () => {
      pill.click();
    });
    expect(container.querySelector('[data-testid="tier2-collapse-btn"]')).not.toBeNull();
  });

  it("Q8: dir_chip subtitle includes full relative directory path", () => {
    const { overview, chunks } = loadFqd();
    const megaChunk = structuredClone(chunks["part:server"]);
    megaChunk.files = Array.from({ length: 26 }, (_, i) => ({
      path: `server/${i < 13 ? "a/service" : "b/service"}/f${i}.ts`,
      module_id: "mod:server",
      language: "typescript",
      tier: "scip",
      loc: 10,
      is_infra: false,
      symbols: [],
    }));
    const grp = buildGroupGraph("part:server", "mod:server", null, {
      overview,
      chunks: { "part:server": megaChunk },
      layers: DEFAULT_LAYERS,
      exactOnly: false,
      runtime: null,
      dataflow: null,
      activeFlow: null,
      flowStepIndex: 0,
    });
    const subtitles = grp.nodes.map((n) => n.subtitle);
    expect(subtitles.some((s) => s?.includes("server/a/service"))).toBe(true);
    expect(subtitles.some((s) => s?.includes("server/b/service"))).toBe(true);
  });
});
