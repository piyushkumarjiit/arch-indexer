import { readFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DescriptionsData, OverviewData, PartChunk } from "../src/types/bundle";
import { FlowBanner } from "../src/ui/FlowBanner";
import { Inspector } from "../src/ui/Inspector";
import { GraphCanvas } from "../src/views/GraphCanvas";
import { buildOverviewGraph } from "../src/views/viewGraphs";

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

describe("Slice 5 descriptions, stale badges, and deterministic cards", () => {
  it("marks stale description entries on cards and in Inspector", async () => {
    const { overview, chunks } = loadFqd();
    const descriptions: DescriptionsData = {
      schema_version: "1.0",
      writer_model: "claude-sonnet-4-5",
      entries: {
        "part:server": {
          summary: "Serves quota API and sync loop.",
          details: "Manages accounts and SQLite persistence.",
          citations: ["server/src/app.ts:1"],
          span_hash: "abc",
          verified_by: {
            judge_model: "gemini-2.5-pro",
            verdict: "supported",
            at_commit: "c373aec",
          },
          status: "stale",
        },
      },
    };

    const ovGraph = buildOverviewGraph({
      overview,
      chunks,
      layers: DEFAULT_LAYERS,
      exactOnly: false,
      runtime: null,
      dataflow: null,
      descriptions,
      activeFlow: null,
      flowStepIndex: 0,
    });
    const serverNode = ovGraph.nodes.find((n) => n.id === "part:server")!;
    expect(serverNode.descriptionStale).toBe(true);
    expect(serverNode.descriptionSummary).toBe("Serves quota API and sync loop.");

    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root!.render(
        <GraphCanvas
          viewKey="overview-stale"
          nodes={ovGraph.nodes}
          edges={ovGraph.edges}
          selectedNodeId="part:server"
          onSelectNode={() => {}}
          onSelectEdge={() => {}}
          onDrillNode={() => {}}
        />,
      );
    });
    await vi.waitFor(() => {
      expect(container!.querySelector(".arch-node-card.is-desc-stale")).not.toBeNull();
    });
    expect(container!.querySelector(".arch-node-card.is-desc-stale")?.textContent).toContain("stale");

    const inspHtml = renderToStaticMarkup(
      <Inspector
        open
        overview={overview}
        chunks={chunks}
        selectedNode={serverNode}
        selectedEdge={null}
        visibleEdges={[]}
        descriptions={descriptions}
        runtime={null}
        dataflow={null}
        onClose={() => {}}
        onSelectNode={() => {}}
        onStartFlow={() => {}}
      />,
    );
    expect(inspHtml).toContain("is-stale");
    expect(inspHtml).toContain("supported (gemini-2.5-pro)");
    expect(inspHtml).toContain("server/src/app.ts:1");
  });

  it("renders FlowBanner step narration and deterministic file/endpoint cards", () => {
    const { overview, chunks } = loadFqd();
    const flow = chunks["part:server"].flows[0];
    const bannerHtml = renderToStaticMarkup(
      <FlowBanner
        flow={flow}
        stepIndex={0}
        flowTitle="Fetch account list"
        flowDescription={{
          summary: "Lists accounts.",
          details: "",
          citations: ["server/src/app.ts:34"],
          span_hash: "h",
          verified_by: null,
          status: "fresh",
          title: "Fetch account list",
          steps: [
            {
              node_id: flow.steps[0].node_id,
              narration: "Receives GET request for accounts.",
              citation: "server/src/app.ts:34",
            },
          ],
        }}
        onPrev={() => {}}
        onNext={() => {}}
        onExit={() => {}}
      />,
    );
    expect(bannerHtml).toContain("Receives GET request for accounts.");

    const fileInspHtml = renderToStaticMarkup(
      <Inspector
        open
        overview={overview}
        chunks={chunks}
        selectedNode={{
          id: "file:server/src/app.ts",
          label: "app.ts",
          kind: "file",
          filePath: "server/src/app.ts",
          partId: "part:server",
          groupId: "mod:server",
          drillable: true,
        }}
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
    expect(fileInspHtml).toContain('data-testid="file-card"');

    const epInspHtml = renderToStaticMarkup(
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
    expect(epInspHtml).toContain('data-testid="endpoint-card"');
  });
});
