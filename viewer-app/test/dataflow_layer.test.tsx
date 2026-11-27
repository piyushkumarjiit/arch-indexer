import { readFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DataflowData, OverviewData, PartChunk } from "../src/types/bundle";
import { DataFlowLayer } from "../src/ui/DataFlowLayer";
import { Inspector } from "../src/ui/Inspector";
import {
  buildFileGraph,
  buildGroupGraph,
  buildOverviewGraph,
  buildPartGraph,
  buildSymbolGraph,
} from "../src/views/viewGraphs";

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

const SAMPLE_DATAFLOW: DataflowData = {
  schema_version: "1.0",
  tool: "opengrep",
  tool_version: "2.1.0",
  edges: [
    {
      entry_id: "ep:GET:/api/accounts",
      source_fqname: "server.app.getAccounts",
      sink_boundary_id: "ds:sqlite",
      kind: "dataflow",
      confidence: "heuristic",
      payload_hint: "req.query.provider",
      evidence: [
        { file: "server/src/app.ts", line: 42, col: 4, source: "sarif", rule_id: "arch-dataflow.typescript.node-sqlite" },
        { file: "server/src/store.ts", line: 22, col: 8, source: "sarif", rule_id: "arch-dataflow.typescript.node-sqlite" },
      ],
      path: [
        { file: "server/src/app.ts", line: 42, col: 4, fqname: "ep:GET:/api/accounts", note: "req.query.provider" },
        { file: "server/src/store.ts", line: 22, col: 8, fqname: "sym:server.Store.listAccounts", note: "db.prepare(...).all(provider)" },
      ],
    },
  ],
};

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

describe("Slice 8 Data-Flow Layer", () => {
  it("projects dataflow edges with payload_hint across Overview, Part, Group, File, and Symbol views", () => {
    const { overview, chunks } = loadFqd();
    const opts = {
      overview,
      chunks,
      layers: DEFAULT_LAYERS,
      exactOnly: false,
      runtime: null,
      dataflow: SAMPLE_DATAFLOW,
      activeFlow: null,
      flowStepIndex: 0,
    };

    const ovGraph = buildOverviewGraph(opts);
    const ovEdge = ovGraph.edges.find((e) => e.id === "part:server->ds:sqlite");
    expect(ovEdge?.kinds).toContain("dataflow");
    expect(ovEdge?.isDataflow).toBe(true);

    const partGraph = buildPartGraph("part:server", opts);
    const partEdge = partGraph.edges.find((e) => e.id === "mod:server->ds:sqlite");
    expect(partEdge?.kinds).toContain("dataflow");

    const groupGraph = buildGroupGraph("part:server", "mod:server", null, opts);
    const groupEdge = groupGraph.edges.find((e) => e.id === "file:server/src/app.ts->ds:sqlite");
    expect(groupEdge?.kinds).toContain("dataflow");

    const fileGraph = buildFileGraph("part:server", "mod:server", "server/src/app.ts", opts);
    const fileEdge = fileGraph.edges.find((e) => e.id === "ep:GET:/api/accounts->ds:sqlite");
    expect(fileEdge?.kinds).toContain("dataflow");
    expect(fileEdge?.label).toContain("req.query.provider");

    const symGraph = buildSymbolGraph("part:server", "ep:GET:/api/accounts", opts);
    const symEdge = symGraph.edges.find((e) => e.id === "ep:GET:/api/accounts->ds:sqlite");
    expect(symEdge?.kinds).toContain("dataflow");
    expect(symEdge?.label).toContain("req.query.provider");

    // Data layer off or exactOnly hides heuristic dataflow edges
    const noData = buildSymbolGraph("part:server", "ep:GET:/api/accounts", {
      ...opts,
      layers: { ...DEFAULT_LAYERS, data: false },
    });
    expect(noData.edges.some((e) => e.kinds.includes("dataflow"))).toBe(false);

    const exactOnly = buildSymbolGraph("part:server", "ep:GET:/api/accounts", {
      ...opts,
      exactOnly: true,
    });
    expect(exactOnly.edges.some((e) => e.kinds.includes("dataflow"))).toBe(false);
  });

  it("renders DataFlowLayer panel and Inspector lists dataflow path steps with file:line", async () => {
    const { overview, chunks } = loadFqd();
    const onSelectEntry = vi.fn();

    // Hidden when null
    expect(
      renderToStaticMarkup(<DataFlowLayer dataflow={null} visible onSelectEntry={onSelectEntry} />),
    ).toBe("");

    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root!.render(
        <div>
          <DataFlowLayer dataflow={SAMPLE_DATAFLOW} visible onSelectEntry={onSelectEntry} />
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
            dataflow={SAMPLE_DATAFLOW}
            onClose={() => {}}
            onSelectNode={() => {}}
            onStartFlow={() => {}}
          />
        </div>,
      );
    });

    const toggle = container.querySelector<HTMLButtonElement>('[data-testid="dataflow-layer-panel"] button');
    expect(toggle?.textContent).toContain("Data-Flow (1)");
    await act(async () => {
      toggle?.click();
    });

    const item = container.querySelector<HTMLButtonElement>(".arch-dataflow-layer__item");
    expect(item?.textContent).toContain("req.query.provider");
    await act(async () => {
      item?.click();
    });
    expect(onSelectEntry).toHaveBeenCalledWith("ep:GET:/api/accounts");

    const stepsSection = container.querySelector('[data-testid="dataflow-paths-section"]');
    expect(stepsSection).not.toBeNull();
    expect(stepsSection?.textContent).toContain("server/src/app.ts:42");
    expect(stepsSection?.textContent).toContain("server/src/store.ts:22");
  });
});
