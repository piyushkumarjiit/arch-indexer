import { readFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { edgeStrokeColor } from "../src/data/layers";
import { useViewerStore } from "../src/state/store";
import type {
  OverviewData,
  PartChunk,
  RuntimeOverlayData,
  ViewEdgeData,
  ViewNodeData,
} from "../src/types/bundle";
import { Inspector } from "../src/ui/Inspector";
import {
  isRuntimeOverlayPayload,
  readRuntimeOverlayFile,
  runtimeHeatLevel,
  RuntimeLayerControls,
} from "../src/ui/RuntimeLayer";
import { sortTraceSpans, TraceReplay } from "../src/ui/TraceReplay";
import { GraphCanvas } from "../src/views/GraphCanvas";
import {
  buildGroupGraph,
  buildOverviewGraph,
  buildPartGraph,
  buildSymbolGraph,
} from "../src/views/viewGraphs";

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

const SAMPLE_RUNTIME: RuntimeOverlayData = {
  schema_version: "1.0",
  source: {
    kind: "otlp-json",
    uri_redacted: "fqd_trace.json",
    service_names: ["fqd-server"],
  },
  captured_at: "2026-11-26T12:00:00Z",
  window_s: 0.35,
  static_commit: "c373aec",
  node_metrics: {
    "ep:GET:/api/accounts": {
      span_count: 12,
      error_count: 2,
      p50_ms: 42,
      p95_ms: 310,
      match_tier: "code",
      top_exceptions: [["SqliteError", 2]],
      error_frames: [{ file: "server/src/app.ts", line: 34, fqname: "server.app.getAccounts" }],
    },
    "ds:sqlite": {
      span_count: 12,
      error_count: 0,
      p50_ms: 8,
      p95_ms: 19,
      match_tier: "peer",
    },
  },
  edge_metrics: {
    "ep:GET:/api/accounts->ds:sqlite": {
      span_count: 12,
      error_count: 0,
      p95_ms: 19,
      match_tier: "peer",
      static: false,
    },
  },
  traces: [
    {
      trace_id: "trace000111222333",
      root_node_id: "ep:GET:/api/accounts",
      spans: [
        {
          span_id: "sp2",
          parent_id: "sp1",
          node_id: "ds:sqlite",
          name: "SELECT accounts",
          start_ns: 2000,
          dur_ns: 15000000,
          status: "OK",
          match_tier: "peer",
        },
        {
          span_id: "sp1",
          parent_id: null,
          node_id: "ep:GET:/api/accounts",
          name: "GET /api/accounts",
          start_ns: 1000,
          dur_ns: 45000000,
          status: "ERROR",
          match_tier: "code",
          exception_type: "SqliteError",
        },
      ],
    },
  ],
  unmatched: {
    span_count: 1,
    top_names: [["healthcheck", 1]],
  },
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

describe("Slice 7 Runtime layer and TraceReplay", () => {
  it("computes heat levels and loads runtime-overlay.v1.json via FileReader", async () => {
    expect(runtimeHeatLevel(SAMPLE_RUNTIME.node_metrics["ep:GET:/api/accounts"])).toBe("error");
    expect(
      runtimeHeatLevel({ span_count: 1, error_count: 0, p50_ms: 100, p95_ms: 300, match_tier: "route" }),
    ).toBe("slow");
    expect(runtimeHeatLevel(SAMPLE_RUNTIME.node_metrics["ds:sqlite"])).toBe("ok");

    const file = new File([JSON.stringify(SAMPLE_RUNTIME)], "runtime-overlay.v1.json", {
      type: "application/json",
    });
    const loaded = await readRuntimeOverlayFile(file);
    expect(loaded.source.kind).toBe("otlp-json");
    expect(loaded.node_metrics["ep:GET:/api/accounts"].match_tier).toBe("code");
  });

  it("renders RuntimeLayerControls summary, match_tier badges, and heat classes on GraphCanvas", async () => {
    const { overview } = loadFqd();
    const onStartTrace = vi.fn();
    const controlsHtml = renderToStaticMarkup(
      <RuntimeLayerControls
        runtime={SAMPLE_RUNTIME}
        overview={overview}
        runtimeLayerActive
        activeTraceId={null}
        onLoadRuntime={() => {}}
        onStartTrace={onStartTrace}
        onClearRuntime={() => {}}
      />,
    );
    expect(controlsHtml).toContain("runtime: otlp-json");
    expect(controlsHtml).toContain("fqd-server");
    expect(controlsHtml).toContain("1 drift");

    const nodes: ViewNodeData[] = [
      {
        id: "ep:GET:/api/accounts",
        label: "GET /api/accounts",
        kind: "entrypoint",
        drillable: false,
        metrics: SAMPLE_RUNTIME.node_metrics["ep:GET:/api/accounts"],
      },
      {
        id: "ds:sqlite",
        label: "SQLite",
        kind: "datastore",
        drillable: false,
        metrics: SAMPLE_RUNTIME.node_metrics["ds:sqlite"],
      },
    ];
    const edges: ViewEdgeData[] = [
      {
        id: "ep:GET:/api/accounts->ds:sqlite",
        source: "ep:GET:/api/accounts",
        target: "ds:sqlite",
        kinds: ["runtime"],
        count: 12,
        confidence: "runtime",
        runtimeOnly: true,
      },
    ];
    const runtimeLayers = { control: true, data: true, integration: true, runtime: true };
    expect(edgeStrokeColor(edges[0], runtimeLayers)).toBe("#f43f5e");

    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root!.render(
        <GraphCanvas
          viewKey="rt-test"
          nodes={nodes}
          edges={edges}
          layers={runtimeLayers}
          selectedNodeId={null}
          onSelectNode={() => {}}
          onSelectEdge={() => {}}
          onDrillNode={() => {}}
        />,
      );
    });

    await vi.waitFor(() => {
      expect(container!.querySelectorAll('[data-testid="runtime-tier-badge"]').length).toBe(2);
    });
    expect(container!.querySelector(".arch-node-card--rt-error")).not.toBeNull();
    expect(container!.querySelector(".arch-node-card--rt-ok")).not.toBeNull();
  });

  it("pins top_exceptions, error_frames, match_tier, and runtime_only badge in Inspector", () => {
    const { overview, chunks } = loadFqd();
    const nodeHtml = renderToStaticMarkup(
      <Inspector
        open
        overview={overview}
        chunks={chunks}
        selectedNode={null}
        selectedNodeId="ep:GET:/api/accounts"
        selectedEdge={null}
        visibleEdges={[]}
        descriptions={null}
        runtime={SAMPLE_RUNTIME}
        dataflow={null}
        onClose={() => {}}
        onSelectNode={() => {}}
        onStartFlow={() => {}}
      />,
    );
    expect(nodeHtml).toContain('data-testid="inspector-runtime-tier"');
    expect(nodeHtml).toContain("code");
    expect(nodeHtml).toContain("SqliteError");
    expect(nodeHtml).toContain("server/src/app.ts:34");

    const edgeHtml = renderToStaticMarkup(
      <Inspector
        open
        overview={overview}
        chunks={chunks}
        selectedNode={null}
        selectedEdge={{
          id: "ep:GET:/api/accounts->ds:sqlite",
          source: "ep:GET:/api/accounts",
          target: "ds:sqlite",
          kinds: ["runtime"],
          count: 12,
          confidence: "runtime",
          runtimeOnly: true,
        }}
        visibleEdges={[]}
        descriptions={null}
        runtime={SAMPLE_RUNTIME}
        dataflow={null}
        onClose={() => {}}
        onSelectNode={() => {}}
        onStartFlow={() => {}}
      />,
    );
    expect(edgeHtml).toContain("runtime_only");
    expect(edgeHtml).toContain("12 spans");
  });

  it("projects drift edges and TraceReplay highlights spans in start_ns order across levels", () => {
    const { overview, chunks } = loadFqd();
    const trace = SAMPLE_RUNTIME.traces[0];
    const sorted = sortTraceSpans(trace.spans);
    expect(sorted.map((s) => s.span_id)).toEqual(["sp1", "sp2"]);

    const replayHtml = renderToStaticMarkup(
      <TraceReplay
        trace={trace}
        spanIndex={0}
        onPrev={() => {}}
        onNext={() => {}}
        onExit={() => {}}
      />,
    );
    expect(replayHtml).toContain("span 1 of 2");
    expect(replayHtml).toContain("GET /api/accounts");
    expect(replayHtml).toContain("SqliteError");

    const layers = { control: true, data: true, integration: true, runtime: true };

    // Overview level projects ep:GET:/api/accounts -> part:server and shows drift edge to ds:sqlite
    const ovGraph = buildOverviewGraph({
      overview,
      chunks,
      layers,
      exactOnly: false,
      runtime: SAMPLE_RUNTIME,
      dataflow: null,
      activeFlow: null,
      flowStepIndex: 0,
      activeTrace: trace,
      traceSpanIndex: 0,
    });
    const partServer = ovGraph.nodes.find((n) => n.id === "part:server")!;
    expect(partServer.isCurrentStep).toBe(true);
    expect(partServer.metrics?.span_count).toBe(12);
    expect(
      ovGraph.edges.some((e) => e.source === "part:server" && e.target === "ds:sqlite" && e.runtimeOnly),
    ).toBe(true);

    // Part and Group levels also project trace highlighting + drift edges
    const partGraph = buildPartGraph("part:server", {
      overview,
      chunks,
      layers,
      exactOnly: false,
      runtime: SAMPLE_RUNTIME,
      dataflow: null,
      activeFlow: null,
      flowStepIndex: 0,
      activeTrace: trace,
      traceSpanIndex: 1,
    });
    expect(partGraph.nodes.find((n) => n.id === "ds:sqlite")?.isCurrentStep).toBe(true);

    const groupGraph = buildGroupGraph("part:server", "mod:server", null, {
      overview,
      chunks,
      layers,
      exactOnly: false,
      runtime: SAMPLE_RUNTIME,
      dataflow: null,
      activeFlow: null,
      flowStepIndex: 0,
      activeTrace: trace,
      traceSpanIndex: 0,
    });
    expect(groupGraph.nodes.find((n) => n.id === "file:server/src/app.ts")?.isCurrentStep).toBe(true);

    // Symbol view includes runtime-only drift edges and store trace controls work
    const symGraph = buildSymbolGraph("part:server", "ep:GET:/api/accounts", {
      overview,
      chunks,
      layers,
      exactOnly: false,
      runtime: SAMPLE_RUNTIME,
      dataflow: null,
      activeFlow: null,
      flowStepIndex: 0,
      activeTrace: trace,
      traceSpanIndex: 0,
    });
    expect(
      symGraph.edges.some(
        (e) => e.source === "ep:GET:/api/accounts" && e.target === "ds:sqlite" && e.runtimeOnly,
      ),
    ).toBe(true);

    useViewerStore.getState().initFromOverview(overview);
    useViewerStore.getState().setRuntimeOverlay(SAMPLE_RUNTIME);
    useViewerStore.getState().startTrace(trace.trace_id);
    expect(useViewerStore.getState().traceSpanIndex).toBe(0);
    expect(useViewerStore.getState().selectedNodeId).toBe("ep:GET:/api/accounts");
    useViewerStore.getState().nextTraceSpan();
    expect(useViewerStore.getState().traceSpanIndex).toBe(1);
    expect(useViewerStore.getState().selectedNodeId).toBe("ds:sqlite");
    useViewerStore.getState().exitTrace();
    expect(useViewerStore.getState().activeTraceId).toBeNull();
  });

  it("rejects partial runtime overlay payloads at every ingestion boundary", async () => {
    const partial = { node_metrics: {}, edge_metrics: {} };
    expect(isRuntimeOverlayPayload(partial)).toBe(false);

    const file = new File([JSON.stringify(partial)], "runtime-overlay.v1.json", {
      type: "application/json",
    });
    await expect(readRuntimeOverlayFile(file)).rejects.toThrow(
      "File is not a valid runtime-overlay.v1.json payload",
    );

    expect(isRuntimeOverlayPayload(SAMPLE_RUNTIME)).toBe(true);
    const previous = window.__ARCH_RUNTIME__;
    window.__ARCH_RUNTIME__ = partial as unknown as RuntimeOverlayData;
    useViewerStore.getState().initFromOverview(loadFqd().overview);
    expect(useViewerStore.getState().runtime).toBeNull();
    window.__ARCH_RUNTIME__ = previous;
  });

  it("keeps the runtime layer toggle and clears trace selection on overlay refresh", () => {
    const { overview } = loadFqd();
    const store = () => useViewerStore.getState();
    store().initFromOverview(overview);
    store().setRuntimeOverlay(null);
    store().setRuntimeOverlay(SAMPLE_RUNTIME);
    expect(store().layers.runtime).toBe(true);

    store().toggleLayer("runtime");
    expect(store().layers.runtime).toBe(false);
    store().setRuntimeOverlay(SAMPLE_RUNTIME);
    expect(store().layers.runtime).toBe(false);

    const traceId = SAMPLE_RUNTIME.traces[0].trace_id;
    store().startTrace(traceId);
    expect(store().layers.runtime).toBe(true);
    expect(store().activeTraceId).toBe(traceId);

    store().setRuntimeOverlay({ ...SAMPLE_RUNTIME, traces: [] });
    expect(store().activeTraceId).toBeNull();
    expect(store().traceSpanIndex).toBe(0);
    expect(store().layers.runtime).toBe(true);

    store().setRuntimeOverlay(null);
    expect(store().layers.runtime).toBe(false);
  });

  it("Clear stops live polling", async () => {
    const { overview } = loadFqd();
    const onClear = vi.fn();
    const originalFetch = globalThis.fetch;
    const fetchMock = vi.fn().mockResolvedValue({ ok: false });
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    vi.useFakeTimers();
    try {
      container = document.createElement("div");
      document.body.appendChild(container);
      root = createRoot(container);
      await act(async () => {
        root!.render(
          <RuntimeLayerControls
            runtime={SAMPLE_RUNTIME}
            overview={overview}
            runtimeLayerActive
            activeTraceId={null}
            onLoadRuntime={() => {}}
            onStartTrace={() => {}}
            onClearRuntime={onClear}
          />,
        );
      });

      const toggle = container.querySelector(
        '[data-testid="runtime-live-poll-toggle"]',
      ) as HTMLInputElement;
      await act(async () => {
        toggle.click();
        await Promise.resolve();
      });
      const callsAfterStart = fetchMock.mock.calls.length;
      expect(callsAfterStart).toBeGreaterThan(0);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      expect(fetchMock.mock.calls.length).toBeGreaterThan(callsAfterStart);

      const clear = container.querySelector(
        '[data-testid="runtime-clear-btn"]',
      ) as HTMLButtonElement;
      await act(async () => {
        clear.click();
      });
      expect(onClear).toHaveBeenCalledTimes(1);
      expect(toggle.checked).toBe(false);

      const callsAfterClear = fetchMock.mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15000);
      });
      expect(fetchMock.mock.calls.length).toBe(callsAfterClear);
    } finally {
      vi.useRealTimers();
      globalThis.fetch = originalFetch;
    }
  });

  it("surfaces live poll failures and marks stale overlays", async () => {
    const { overview } = loadFqd();
    const onLoadRuntime = vi.fn();
    const originalFetch = globalThis.fetch;
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("unavailable", { status: 503 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify(SAMPLE_RUNTIME), {
          status: 200,
          headers: { "X-Arch-Overlay-Status": "stale" },
        }),
      );
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    vi.useFakeTimers();
    try {
      container = document.createElement("div");
      document.body.appendChild(container);
      root = createRoot(container);
      await act(async () => {
        root!.render(
          <RuntimeLayerControls
            runtime={SAMPLE_RUNTIME}
            overview={overview}
            runtimeLayerActive
            activeTraceId={null}
            onLoadRuntime={onLoadRuntime}
            onStartTrace={() => {}}
            onClearRuntime={() => {}}
          />,
        );
      });

      const toggle = container.querySelector(
        '[data-testid="runtime-live-poll-toggle"]',
      ) as HTMLInputElement;
      await act(async () => {
        toggle.click();
        await Promise.resolve();
      });
      expect(container.querySelector(".arch-runtime-error")?.textContent).toContain("503");

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      expect(onLoadRuntime).toHaveBeenCalledWith(SAMPLE_RUNTIME);
      expect(container.querySelector('[data-testid="runtime-overlay-stale"]')).not.toBeNull();
      expect(container.querySelector(".arch-runtime-error")?.textContent).toContain("stale");
    } finally {
      vi.useRealTimers();
      globalThis.fetch = originalFetch;
    }
  });

  it("clamps the trace span index when a refreshed trace shrinks", () => {
    const { overview } = loadFqd();
    const store = () => useViewerStore.getState();
    store().initFromOverview(overview);
    store().setRuntimeOverlay(null);
    store().setRuntimeOverlay(SAMPLE_RUNTIME);

    const trace = SAMPLE_RUNTIME.traces[0];
    store().startTrace(trace.trace_id);
    store().nextTraceSpan();
    expect(store().traceSpanIndex).toBe(1);

    store().setRuntimeOverlay({
      ...SAMPLE_RUNTIME,
      traces: [{ ...trace, spans: [trace.spans[0]] }],
    });
    expect(store().activeTraceId).toBe(trace.trace_id);
    expect(store().traceSpanIndex).toBe(0);

    store().setRuntimeOverlay(SAMPLE_RUNTIME);
    store().startTrace(trace.trace_id);
    store().nextTraceSpan();
    expect(store().traceSpanIndex).toBe(1);
    const previous = window.__ARCH_RUNTIME__;
    window.__ARCH_RUNTIME__ = {
      ...SAMPLE_RUNTIME,
      traces: [{ ...trace, spans: [trace.spans[0]] }],
    };
    store().refreshOverlays();
    expect(store().activeTraceId).toBe(trace.trace_id);
    expect(store().traceSpanIndex).toBe(0);
    window.__ARCH_RUNTIME__ = previous;
  });
});
