import { readFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { describe, expect, it } from "vitest";
import {
  countEdgeCrossings,
  findOverlappingNodePairs,
  layoutWithElk,
} from "../src/layout/elkWorker";
import type { OverviewData, PartChunk } from "../src/types/bundle";
import {
  buildFileGraph,
  buildGroupGraph,
  buildOverviewGraph,
  buildPartGraph,
  buildSymbolGraph,
} from "../src/views/viewGraphs";

const FQD_DIR = path.resolve(__dirname, "../fixtures/fqd");

function loadFqdFixture(): {
  overview: OverviewData;
  chunks: Record<string, PartChunk>;
} {
  const overview = JSON.parse(
    readFileSync(path.join(FQD_DIR, "overview.json"), "utf-8"),
  ) as OverviewData;
  const chunks: Record<string, PartChunk> = {};
  for (const [partId, meta] of Object.entries(overview.chunks)) {
    const code = readFileSync(path.join(FQD_DIR, "chunks", meta.file), "utf-8");
    const sandbox: { window: Record<string, unknown> } = { window: {} };
    sandbox.window.__ARCH_CHUNK__ = (id: string, payload: PartChunk) => {
      chunks[id] = payload;
    };
    vm.runInNewContext(code, sandbox);
    expect(chunks[partId]).toBeDefined();
  }
  return { overview, chunks };
}

const DEFAULT_LAYERS = {
  control: true,
  data: true,
  integration: true,
  runtime: false,
};

describe("ELK layout invariants", () => {
  it("produces deterministic coordinates across repeated runs", async () => {
    const { overview, chunks } = loadFqdFixture();
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

    const ovGraph = buildOverviewGraph(viewOpts);
    const run1 = await layoutWithElk(ovGraph.nodes, ovGraph.edges);
    const run2 = await layoutWithElk(ovGraph.nodes, ovGraph.edges);

    expect(run1.nodes).toEqual(run2.nodes);
    expect(run1.edges).toEqual(run2.edges);

    const partGraph = buildPartGraph("part:server", viewOpts);
    const partRun1 = await layoutWithElk(partGraph.nodes, partGraph.edges);
    const partRun2 = await layoutWithElk(partGraph.nodes, partGraph.edges);

    expect(partRun1.nodes).toEqual(partRun2.nodes);
    expect(partRun1.edges).toEqual(partRun2.edges);
  });

  it("never places overlapping card rectangles on any FQD view level", async () => {
    const { overview, chunks } = loadFqdFixture();
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

    const views = [
      buildOverviewGraph(viewOpts),
      buildPartGraph("part:server", viewOpts),
      buildPartGraph("part:web", viewOpts),
      buildGroupGraph("part:server", "mod:server", null, viewOpts),
      buildFileGraph("part:server", "mod:server", "server/src/app.ts", viewOpts),
      buildSymbolGraph("part:server", "ep:GET:/api/accounts", viewOpts),
    ];

    for (const view of views) {
      expect(view.nodes.length).toBeGreaterThan(0);
      const layout = await layoutWithElk(view.nodes, view.edges);
      expect(findOverlappingNodePairs(layout.nodes)).toEqual([]);
    }
  });

  it("orders Part view Used by / Inside / Uses bands left-to-right", async () => {
    const { overview, chunks } = loadFqdFixture();
    const partGraph = buildPartGraph("part:server", {
      overview,
      chunks,
      layers: DEFAULT_LAYERS,
      exactOnly: false,
      runtime: null,
      dataflow: null,
      activeFlow: null,
      flowStepIndex: 0,
    });

    const usedBy = partGraph.nodes.filter((n) => n.partition === 0).map((n) => n.id);
    const inside = partGraph.nodes.filter((n) => n.partition === 1).map((n) => n.id);
    const uses = partGraph.nodes.filter((n) => n.partition === 2).map((n) => n.id);

    expect(usedBy).toContain("part:web");
    expect(inside).toContain("mod:server");
    expect(uses).toEqual(
      expect.arrayContaining(["part:shared", "ds:sqlite", "ext:http", "res:path:usage_db"]),
    );

    const layout = await layoutWithElk(partGraph.nodes, partGraph.edges);
    const pos = new Map(layout.nodes.map((n) => [n.id, n]));
    expect(pos.get("part:web")!.x).toBeLessThan(pos.get("mod:server")!.x);
    expect(pos.get("mod:server")!.x).toBeLessThan(pos.get("ds:sqlite")!.x);
  });

  it("measures edge crossings on the FQD module/part views", async () => {
    const { overview, chunks } = loadFqdFixture();
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

    const ovGraph = buildOverviewGraph(viewOpts);
    const ovLayout = await layoutWithElk(ovGraph.nodes, ovGraph.edges);
    const ovCrossings = countEdgeCrossings(ovLayout.edges, ovLayout.nodes);
    expect(ovCrossings).toBeLessThanOrEqual(1);

    const modGraph = buildGroupGraph("part:server", "mod:server", null, viewOpts);
    const modLayout = await layoutWithElk(modGraph.nodes, modGraph.edges);
    const modCrossings = countEdgeCrossings(modLayout.edges, modLayout.nodes);
    expect(modCrossings).toBe(0);

    // Direct sanity check of geometric crossing counter on an X-crossing vs parallel edges
    expect(
      countEdgeCrossings([
        {
          id: "e1",
          source: "a",
          target: "b",
          points: [
            { x: 0, y: 0 },
            { x: 100, y: 100 },
          ],
        },
        {
          id: "e2",
          source: "c",
          target: "d",
          points: [
            { x: 0, y: 100 },
            { x: 100, y: 0 },
          ],
        },
      ]),
    ).toBe(1);
  });
});
