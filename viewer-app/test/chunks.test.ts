import { readFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { afterEach, describe, expect, it } from "vitest";
import { clearChunkCache, loadPartChunk, loadSearchIndex } from "../src/data/chunks";
import { parseHashNodeId } from "../src/App";
import { useViewerStore } from "../src/state/store";
import type { OverviewData, PartChunk, SearchTuple } from "../src/types/bundle";

const FQD_DIR = path.resolve(__dirname, "../fixtures/fqd");

function readFqdOverview(): OverviewData {
  return JSON.parse(readFileSync(path.join(FQD_DIR, "overview.json"), "utf-8")) as OverviewData;
}

function makeFixtureScriptLoader(baseDir: string) {
  return async (src: string) => {
    const fileName = src.split("/").pop()!;
    const fullPath = path.join(baseDir, "chunks", fileName);
    const code = readFileSync(fullPath, "utf-8");
    const fn = new Function("window", code);
    fn(window);
  };
}

describe("JSONP chunk and search loader", () => {
  afterEach(() => {
    clearChunkCache();
    delete window.__ARCH_CHUNK__;
    delete window.__ARCH_SEARCH__;
  });

  it("loads and resolves the requested part chunk from JSONP", async () => {
    const overview = readFqdOverview();
    const loader = makeFixtureScriptLoader(FQD_DIR);

    const serverChunk = await loadPartChunk("part:server", overview, {
      basePath: "arch/chunks",
      scriptLoader: loader,
    });
    expect(serverChunk.part_id).toBe("part:server");
    expect(serverChunk.schema_version).toBe("2.0");
    expect(serverChunk.nodes.some((n) => n.id === "ep:GET:/api/accounts")).toBe(true);
    expect(serverChunk.flows.some((f) => f.end_reason === "no-boundary-detected")).toBe(true);

    const webChunk = await loadPartChunk("part:web", overview, {
      basePath: "arch/chunks",
      scriptLoader: loader,
    });
    expect(webChunk.part_id).toBe("part:web");
    expect(webChunk.nodes.some((n) => n.id === "fe:web.fetchAccounts")).toBe(true);
  });

  it("caches loaded chunks so subsequent requests do not re-inject scripts", async () => {
    const overview = readFqdOverview();
    let calls = 0;
    const baseLoader = makeFixtureScriptLoader(FQD_DIR);
    const countingLoader = async (src: string) => {
      calls++;
      await baseLoader(src);
    };

    const first = await loadPartChunk("part:server", overview, { scriptLoader: countingLoader });
    const second = await loadPartChunk("part:server", overview, { scriptLoader: countingLoader });
    expect(calls).toBe(1);
    expect(second).toBe(first);
  });

  it("rejects when chunk byte size does not match overview.chunks metadata", async () => {
    const overview = readFqdOverview();
    overview.chunks["part:server"] = {
      ...overview.chunks["part:server"],
      bytes: 12,
    };
    const loader = makeFixtureScriptLoader(FQD_DIR);

    await expect(
      loadPartChunk("part:server", overview, { scriptLoader: loader }),
    ).rejects.toThrow(/byte-size check failed/i);
  });

  it("loads search index JSONP lazily and resolves SearchTuple rows", async () => {
    const loader = makeFixtureScriptLoader(FQD_DIR);
    const entries: SearchTuple[] = await loadSearchIndex({ scriptLoader: loader });
    expect(entries.length).toBeGreaterThanOrEqual(5);
    const accounts = entries.find((e) => e[0] === "ep:GET:/api/accounts");
    expect(accounts).toEqual([
      "ep:GET:/api/accounts",
      "GET /api/accounts",
      "entrypoint",
      "part:server",
      "/api/accounts",
    ]);
  });

  it("parses #node= deep links and navigates to the target node", async () => {
    const overview = readFqdOverview();
    // Seed chunks via vm to simulate script load in store navigation
    const chunks: Record<string, PartChunk> = {};
    for (const [, meta] of Object.entries(overview.chunks)) {
      const code = readFileSync(path.join(FQD_DIR, "chunks", meta.file), "utf-8");
      const sandbox: { window: Record<string, unknown> } = { window: {} };
      sandbox.window.__ARCH_CHUNK__ = (id: string, payload: PartChunk) => {
        chunks[id] = payload;
      };
      vm.runInNewContext(code, sandbox);
    }

    expect(parseHashNodeId("#node=ep%3AGET%3A%2Fapi%2Faccounts")).toBe("ep:GET:/api/accounts");
    expect(parseHashNodeId("")).toBeNull();

    useViewerStore.setState({ overview, chunks });
    await useViewerStore.getState().navigateToNodeId("ep:GET:/api/accounts");

    const state = useViewerStore.getState();
    expect(state.level).toBe("symbol");
    expect(state.partId).toBe("part:server");
    expect(state.symbolId).toBe("ep:GET:/api/accounts");
    expect(state.selectedNodeId).toBe("ep:GET:/api/accounts");
  });
});
