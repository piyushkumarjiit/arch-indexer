import { readFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { afterEach, describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { readOptionalOverlays } from "../src/data/chunks";
import { capNodesForView, MAX_DRAWN_NODES } from "../src/data/layers";
import type { ChunkFlow, OverviewData, PartChunk } from "../src/types/bundle";
import {
  ChangeBadge,
  collectChangeItems,
  getFileChangeStatus,
  getPartChangeStatus,
} from "../src/ui/ChangeBadges";
import { FlowBanner } from "../src/ui/FlowBanner";
import { LayerToggles } from "../src/ui/LayerToggles";
import { Tier2Banner } from "../src/ui/Tier2Banner";
import {
  buildFileGraph,
  buildGroupGraph,
  buildOverviewGraph,
  buildPartGraph,
  buildSymbolGraph,
} from "../src/views/viewGraphs";

const LARGE_DIR = path.resolve(__dirname, "../fixtures/large");
const FQD_DIR = path.resolve(__dirname, "../fixtures/fqd");

function loadBundle(dir: string): {
  overview: OverviewData;
  chunks: Record<string, PartChunk>;
} {
  const overview = JSON.parse(
    readFileSync(path.join(dir, "overview.json"), "utf-8"),
  ) as OverviewData;
  const chunks: Record<string, PartChunk> = {};
  for (const [, meta] of Object.entries(overview.chunks)) {
    const code = readFileSync(path.join(dir, "chunks", meta.file), "utf-8");
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

describe("300-node cap, view rules, and optional overlays", () => {
  afterEach(() => {
    window.__ARCH_RUNTIME__ = undefined;
    window.__ARCH_DESCRIPTIONS__ = undefined;
    window.__ARCH_DATAFLOW__ = undefined;
  });

  it("highlights the view entities that contain active flow steps at every level", () => {
    const { overview, chunks } = loadBundle(FQD_DIR);
    const flow = chunks["part:server"].flows.find(
      (f) => f.id === "flow:ep:GET:/api/accounts#1",
    )!;
    const flowOpts = {
      overview,
      chunks,
      layers: DEFAULT_LAYERS,
      exactOnly: false,
      runtime: null,
      dataflow: null,
      activeFlow: flow,
      flowStepIndex: 0,
    };

    const overviewNodes = new Map(
      buildOverviewGraph(flowOpts).nodes.map((n) => [n.id, n]),
    );
    expect(overviewNodes.get("part:server")?.highlighted).toBe(true);
    expect(overviewNodes.get("ds:sqlite")?.highlighted).toBe(true);
    expect(overviewNodes.get("part:web")?.dimmed).toBe(true);

    const partNodes = new Map(
      buildPartGraph("part:server", flowOpts).nodes.map((n) => [n.id, n]),
    );
    expect(partNodes.get("mod:server")?.isCurrentStep).toBe(true);
    expect(partNodes.get("ds:sqlite")?.highlighted).toBe(true);
    expect(partNodes.get("ext:http")?.dimmed).toBe(true);

    const groupNodes = new Map(
      buildGroupGraph("part:server", "mod:server", null, flowOpts).nodes.map((n) => [n.id, n]),
    );
    expect(groupNodes.get("file:server/src/app.ts")?.isCurrentStep).toBe(true);
    expect(groupNodes.get("file:server/src/store.ts")?.highlighted).toBe(true);
    expect(groupNodes.get("ds:sqlite")?.highlighted).toBe(true);
    expect(groupNodes.get("file:server/src/upstream.ts")?.dimmed).toBe(true);

    const fileNodes = new Map(
      buildFileGraph("part:server", "mod:server", "server/src/app.ts", flowOpts).nodes.map(
        (n) => [n.id, n],
      ),
    );
    expect(fileNodes.get("ep:GET:/api/accounts")?.isCurrentStep).toBe(true);
    expect(fileNodes.get("sym:server.Store.listAccounts")?.highlighted).toBe(true);
    expect(fileNodes.get("sym:server.fetchProviderQuota")?.dimmed).toBe(true);

    const symbolNodes = new Map(
      buildSymbolGraph("part:server", "ep:GET:/api/accounts", flowOpts).nodes.map((n) => [
        n.id,
        n,
      ]),
    );
    expect(symbolNodes.get("ep:GET:/api/accounts")?.isCurrentStep).toBe(true);
    expect(symbolNodes.get("ds:sqlite")?.highlighted).toBe(true);
    expect(symbolNodes.get("sym:server.fetchProviderQuota")?.dimmed).toBe(true);
  });

  it("maps flow steps through their owning module instead of by substring id", () => {
    const { overview, chunks } = loadBundle(FQD_DIR);
    const chunk = chunks["part:server"];
    overview.groups.push({
      id: "web",
      name: "web",
      part_id: "part:server",
      path: "web",
      language: "typescript",
      tier: "scip",
      counts: { files: 1 },
    });
    chunk.nodes.push({
      id: "ep:web-admin:login",
      kind: "entrypoint",
      label: "login",
      module_id: "mod:server",
      file: "server/src/app.ts",
      line: 1,
      tier: "scip",
      confidence: "exact",
      evidence: [{ file: "server/src/app.ts", line: 1, source: "scip" }],
    });
    const flow: ChunkFlow = {
      id: "flow:login",
      entry_id: "ep:web-admin:login",
      steps: [
        { node_id: "ep:web-admin:login", edge_kind: null, file: "server/src/app.ts", line: 1 },
      ],
      ends_at: "ep:web-admin:login",
      end_reason: "no-boundary-detected",
    };

    const nodes = new Map(
      buildPartGraph("part:server", {
        overview,
        chunks,
        layers: DEFAULT_LAYERS,
        exactOnly: false,
        runtime: null,
        dataflow: null,
        activeFlow: flow,
        flowStepIndex: 0,
      }).nodes.map((n) => [n.id, n]),
    );
    expect(nodes.get("mod:server")?.highlighted).toBe(true);
    expect(nodes.get("web")?.dimmed).toBe(true);
  });

  it("enforces the 300-node hard cap and folds excess nodes into +N", () => {
    const { overview, chunks } = loadBundle(LARGE_DIR);
    const viewOpts = {
      overview,
      chunks,
      layers: DEFAULT_LAYERS,
      exactOnly: false,
      runtime: null,
      dataflow: null,
      activeFlow: null,
      flowStepIndex: 0,
    };

    // Large overview has 320 parts -> capped to 300 (299 kept + "+21" fold node)
    const ovGraph = buildOverviewGraph(viewOpts);
    expect(ovGraph.nodes.length).toBe(MAX_DRAWN_NODES);
    expect(ovGraph.foldedCount).toBe(21);
    const foldNode = ovGraph.nodes.find((n) => n.kind === "fold");
    expect(foldNode).toBeDefined();
    expect(foldNode?.label).toBe("+21");
    expect(foldNode?.foldCount).toBe(21);

    // Large symbol call tree has 331 nodes -> capped to 300 (299 kept + "+32" fold node)
    const symGraph = buildSymbolGraph("part:svc-000", "ep:mega.root", viewOpts);
    expect(symGraph.nodes.length).toBe(MAX_DRAWN_NODES);
    expect(symGraph.foldedCount).toBe(32);
    const symFold = symGraph.nodes.find((n) => n.kind === "fold");
    expect(symFold?.label).toBe("+32");

    // Direct capNodesForView rewires incident edges cleanly without self-loops
    const rawNodes = Array.from({ length: 305 }, (_, i) => ({
      id: `n:${String(i).padStart(3, "0")}`,
      label: `N${i}`,
      kind: "service",
    }));
    const rawEdges = [
      // Give n:000..n:298 higher degree so n:299..n:304 are folded
      ...Array.from({ length: 299 }, (_, i) => ({
        id: `n:000->n:${String(i).padStart(3, "0")}-a`,
        source: "n:000",
        target: `n:${String(i).padStart(3, "0")}`,
        kinds: ["calls"],
        count: 1,
      })),
      ...Array.from({ length: 299 }, (_, i) => ({
        id: `n:001->n:${String(i).padStart(3, "0")}-b`,
        source: "n:001",
        target: `n:${String(i).padStart(3, "0")}`,
        kinds: ["calls"],
        count: 1,
      })),
      { id: "n:000->n:304", source: "n:000", target: "n:304", kinds: ["calls"], count: 1 },
    ];
    const capped = capNodesForView(rawNodes, rawEdges, "test");
    expect(capped.nodes.length).toBe(300);
    expect(capped.foldedCount).toBe(6);
    expect(capped.edges.some((e) => e.target === "fold:test")).toBe(true);
  });

  it("applies group directory chips (>24 files), file symbol cap (<=22), and depth-3 symbol tree", () => {
    const { overview, chunks } = loadBundle(LARGE_DIR);
    const viewOpts = {
      overview,
      chunks,
      layers: DEFAULT_LAYERS,
      exactOnly: false,
      runtime: null,
      dataflow: null,
      activeFlow: null,
      flowStepIndex: 0,
    };

    // Group with 30 files uses directory chips instead of all 30 file cards
    const dirView = buildGroupGraph("part:svc-000", "mod:svc-000", null, viewOpts);
    expect(dirView.nodes.every((n) => n.kind === "dir_chip")).toBe(true);
    expect(dirView.nodes.length).toBe(2);

    // Selecting one directory chip reveals its 15 files directly (<=24)
    const filteredDirView = buildGroupGraph(
      "part:svc-000",
      "mod:svc-000",
      "services/svc_000/handlers",
      viewOpts,
    );
    expect(filteredDirView.nodes.length).toBe(15);
    expect(filteredDirView.nodes.every((n) => n.kind === "file")).toBe(true);

    // File view caps at 22 symbols + "+N more"
    const fileView = buildFileGraph(
      "part:svc-000",
      "mod:svc-000",
      "services/svc_000/index.ts",
      viewOpts,
    );
    const fold = fileView.nodes.find((n) => n.kind === "fold");
    expect(fold?.label).toBe("+309 more");
    expect(fileView.nodes.length).toBeLessThanOrEqual(MAX_DRAWN_NODES);

    // Depth-3 call tree truncates a 5-hop chain at depth 3
    const fqd = loadBundle(FQD_DIR);
    const chainChunk: PartChunk = {
      schema_version: "2.0",
      part_id: "part:chain",
      files: [],
      nodes: [0, 1, 2, 3, 4, 5].map((i) => ({
        id: `sym:c${i}`,
        kind: "service",
        label: `c${i}`,
        module_id: "mod:chain",
        file: "c.ts",
        line: i + 1,
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "c.ts", line: i + 1, source: "scip" }],
      })),
      edges: [0, 1, 2, 3, 4].map((i) => ({
        source: `sym:c${i}`,
        target: `sym:c${i + 1}`,
        kind: "calls",
        confidence: "exact",
        evidence: [{ file: "c.ts", line: i + 1, source: "scip" }],
      })),
      flows: [],
    };
    const chainView = buildSymbolGraph("part:chain", "sym:c0", {
      overview: fqd.overview,
      chunks: { "part:chain": chainChunk },
      layers: DEFAULT_LAYERS,
      exactOnly: false,
      runtime: null,
      dataflow: null,
      activeFlow: null,
      flowStepIndex: 0,
    });
    const ids = chainView.nodes.map((n) => n.id);
    expect(ids).toEqual(["sym:c0", "sym:c1", "sym:c2", "sym:c3"]);
  });

  it("handles null optional globals cleanly and keeps Runtime layer toggle hidden", () => {
    window.__ARCH_RUNTIME__ = null;
    window.__ARCH_DESCRIPTIONS__ = null;
    window.__ARCH_DATAFLOW__ = null;

    const overlays = readOptionalOverlays();
    expect(overlays).toEqual({ descriptions: null, runtime: null, dataflow: null });

    const hiddenHtml = renderToStaticMarkup(
      createElement(LayerToggles, {
        layers: DEFAULT_LAYERS,
        hasRuntimeOverlay: false,
        exactOnly: false,
        onToggleLayer: () => {},
        onToggleExactOnly: () => {},
      }),
    );
    expect(hiddenHtml).toContain('data-layer="runtime"');
    expect(hiddenHtml).toContain("display:none");

    const visibleHtml = renderToStaticMarkup(
      createElement(LayerToggles, {
        layers: { ...DEFAULT_LAYERS, runtime: true },
        hasRuntimeOverlay: true,
        exactOnly: false,
        onToggleLayer: () => {},
        onToggleExactOnly: () => {},
      }),
    );
    expect(visibleHtml).toContain('data-layer="runtime"');
    expect(visibleHtml).not.toContain("display:none");
  });

  it("renders Tier2Banner, FlowBanner partial end_reason, and ChangeBadges from delta", () => {
    const { overview, chunks } = loadBundle(FQD_DIR);

    const tier2Html = renderToStaticMarkup(
      createElement(Tier2Banner, { tier2Modules: overview.tier_summary.tier2_modules }),
    );
    expect(tier2Html).toContain("1 module indexed at Tier 2: edges may be incomplete");

    const partialFlow = chunks["part:server"].flows.find(
      (f) => f.end_reason === "no-boundary-detected",
    )!;
    const flowHtml = renderToStaticMarkup(
      createElement(FlowBanner, {
        flow: partialFlow,
        stepIndex: partialFlow.steps.length - 1,
        onPrev: () => {},
        onNext: () => {},
        onExit: () => {},
      }),
    );
    expect(flowHtml).toContain("step 2 of 2");
    expect(flowHtml).toContain("no-boundary-detected");

    expect(getFileChangeStatus("server/src/app.ts", overview.delta)).toBe("changed");
    expect(getFileChangeStatus("web/src/components/UsagePanel.tsx", overview.delta)).toBe("new");
    expect(getPartChangeStatus("part:server", overview)).toBe("changed");
    expect(getPartChangeStatus("part:web", overview)).toBe("new");
    expect(collectChangeItems(overview, chunks).length).toBe(2);

    const badgeHtml = renderToStaticMarkup(createElement(ChangeBadge, { status: "new" }));
    expect(badgeHtml).toContain("new");
  });
});
