import { readFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearChunkCache, loadPartChunk } from "../src/data/chunks";
import { useViewerStore } from "../src/state/store";
import type { OverviewData, PartChunk } from "../src/types/bundle";

vi.mock("../src/data/chunks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/data/chunks")>();
  return { ...actual, loadPartChunk: vi.fn() };
});

const FQD_DIR = path.resolve(__dirname, "../fixtures/fqd");
const FLOW_ID = "flow:ep:GET:/api/accounts#1";

function loadFixture(): { overview: OverviewData; chunk: PartChunk } {
  const overview = JSON.parse(
    readFileSync(path.join(FQD_DIR, "overview.json"), "utf-8"),
  ) as OverviewData;
  const meta = overview.chunks["part:server"];
  const code = readFileSync(path.join(FQD_DIR, "chunks", meta.file), "utf-8");
  let chunk: PartChunk | undefined;
  const sandbox: { window: Record<string, unknown> } = { window: {} };
  sandbox.window.__ARCH_CHUNK__ = (_id: string, payload: PartChunk) => {
    chunk = payload;
  };
  vm.runInNewContext(code, sandbox);
  if (!chunk) throw new Error("fixture chunk missing");
  return { overview, chunk };
}

const pendingLoads: Array<{ resolve: (chunk: PartChunk) => void }> = [];

function installColdChunkLoads(): void {
  pendingLoads.length = 0;
  vi.mocked(loadPartChunk).mockImplementation(
    () =>
      new Promise<PartChunk>((resolve) => {
        pendingLoads.push({ resolve });
      }),
  );
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function primeFlow(overview: OverviewData, chunk: PartChunk): void {
  const flow = chunk.flows.find((f) => f.id === FLOW_ID);
  if (!flow) throw new Error("fixture flow missing");
  useViewerStore.getState().initFromOverview(overview);
  clearChunkCache();
  useViewerStore.setState({
    chunks: {},
    activeFlowId: flow.id,
    activeFlow: flow,
    flowStepIndex: 0,
    flowFollowStep: true,
    level: "part",
    partId: "part:server",
    groupId: "mod:server",
    filePath: null,
    symbolId: null,
    selectedNodeId: flow.steps[0].node_id,
    selectedEdgeId: null,
  });
}

afterEach(() => {
  pendingLoads.length = 0;
  vi.mocked(loadPartChunk).mockReset();
  clearChunkCache();
});

describe("Follow step staleness", () => {
  it("keeps the newest step selected when two cold loads finish out of order", async () => {
    const { overview, chunk } = loadFixture();
    primeFlow(overview, chunk);
    installColdChunkLoads();

    useViewerStore.getState().nextFlowStep();
    useViewerStore.getState().nextFlowStep();
    expect(pendingLoads).toHaveLength(2);

    pendingLoads[1].resolve(chunk);
    await flush();
    expect(useViewerStore.getState().selectedNodeId).toBe("ds:sqlite");

    pendingLoads[0].resolve(chunk);
    await flush();

    const state = useViewerStore.getState();
    expect(state.flowStepIndex).toBe(2);
    expect(state.selectedNodeId).toBe("ds:sqlite");
    expect(state.symbolId).toBe("sym:server.Store.listAccounts");
  });

  it("does not navigate when the flow is exited during a cold load", async () => {
    const { overview, chunk } = loadFixture();
    primeFlow(overview, chunk);
    installColdChunkLoads();

    useViewerStore.getState().nextFlowStep();
    useViewerStore.getState().exitFlow();
    pendingLoads[0].resolve(chunk);
    await flush();

    const state = useViewerStore.getState();
    expect(state.activeFlow).toBeNull();
    expect(state.level).toBe("part");
    expect(state.symbolId).toBeNull();
  });

  it("does not navigate when Follow step is turned off during a cold load", async () => {
    const { overview, chunk } = loadFixture();
    primeFlow(overview, chunk);
    installColdChunkLoads();

    useViewerStore.getState().nextFlowStep();
    useViewerStore.getState().toggleFlowFollowStep();
    expect(useViewerStore.getState().flowFollowStep).toBe(false);
    pendingLoads[0].resolve(chunk);
    await flush();

    const state = useViewerStore.getState();
    expect(state.level).toBe("part");
    expect(state.symbolId).toBeNull();
  });
});
