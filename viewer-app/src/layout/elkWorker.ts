import ELK from "elkjs/lib/elk-api.js";
import type { ElkExtendedEdge, ElkNode, ELK as ElkInstance } from "elkjs/lib/elk-api.js";

export interface LayoutNodeInput {
  id: string;
  width?: number;
  height?: number;
  partition?: 0 | 1 | 2;
}

export interface LayoutEdgeInput {
  id: string;
  source: string;
  target: string;
}

export interface PositionedNode {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Point2D {
  x: number;
  y: number;
}

export interface RoutedEdge {
  id: string;
  source: string;
  target: string;
  points: Point2D[];
}

export interface LayoutResult {
  nodes: PositionedNode[];
  edges: RoutedEdge[];
  width: number;
  height: number;
  usedWorker: boolean;
}

export const DEFAULT_NODE_WIDTH = 228;
export const DEFAULT_NODE_HEIGHT = 76;

export const SPEC_ELK_LAYOUT_OPTIONS: Record<string, string> = {
  "elk.algorithm": "layered",
  "elk.edgeRouting": "ORTHOGONAL",
  "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
  "elk.randomSeed": "1",
  "elk.direction": "RIGHT",
  "elk.spacing.nodeNode": "32",
  "elk.layered.spacing.nodeNodeBetweenLayers": "64",
  "elk.spacing.edgeNode": "20",
  "elk.layered.spacing.edgeNodeBetweenLayers": "24",
};

interface WorkerLike {
  postMessage: (msg: unknown) => void;
  onmessage: ((ev: { data: unknown }) => void) | null;
  terminate?: () => void;
}

let cachedInstancePromise: Promise<{ elk: ElkInstance; usedWorker: boolean }> | null = null;

function resolveElkWorkerScriptUrl(): string {
  if (typeof document !== "undefined") {
    const existing = document.querySelector<HTMLScriptElement>(
      'script[src*="elk-worker.min.js"]',
    );
    if (existing?.getAttribute("src")) {
      return new URL(existing.getAttribute("src")!, window.location.href).href;
    }
    return new URL("arch/vendor/elk-worker.min.js", window.location.href).href;
  }
  return "arch/vendor/elk-worker.min.js";
}

function loadScriptWithModuleShim(src: string): Promise<new () => WorkerLike> {
  return new Promise((resolve, reject) => {
    const w = window as unknown as Record<string, unknown>;
    const cached = w.__ARCH_ELK_INTHREAD_WORKER__ as
      | (new () => WorkerLike)
      | undefined;
    if (cached) {
      resolve(cached);
      return;
    }

    const hadModule = Object.prototype.hasOwnProperty.call(w, "module");
    const hadExports = Object.prototype.hasOwnProperty.call(w, "exports");
    const prevModule = w.module;
    const prevExports = w.exports;

    const fakeExports: Record<string, unknown> = {};
    w.exports = fakeExports;
    w.module = { exports: fakeExports };

    const restoreGlobals = () => {
      if (hadModule) w.module = prevModule;
      else delete w.module;
      if (hadExports) w.exports = prevExports;
      else delete w.exports;
    };

    const script = document.createElement("script");
    script.src = src;
    script.async = true;
    script.onload = () => {
      const mod = w.module as { exports?: { Worker?: new () => WorkerLike; default?: new () => WorkerLike } };
      const WorkerCtor = mod?.exports?.Worker ?? mod?.exports?.default;
      restoreGlobals();
      if (script.parentNode) script.parentNode.removeChild(script);
      if (WorkerCtor) {
        w.__ARCH_ELK_INTHREAD_WORKER__ = WorkerCtor;
        resolve(WorkerCtor);
      } else {
        reject(new Error("elk-worker.min.js loaded in-thread without exposing Worker"));
      }
    };
    script.onerror = () => {
      restoreGlobals();
      if (script.parentNode) script.parentNode.removeChild(script);
      reject(new Error(`Failed to load ELK worker script in-thread: ${src}`));
    };
    (document.head || document.body || document.documentElement).appendChild(script);
  });
}

async function createNodeInThreadElk(): Promise<{ elk: ElkInstance; usedWorker: boolean }> {
  // Computed path keeps Vite from bundling the 1.63 MB worker into app.js.
  const workerModulePath = "elkjs/lib/" + "elk-worker.min.js";
  const mod = (await import(/* @vite-ignore */ workerModulePath)) as {
    Worker?: new () => WorkerLike;
    default?: new () => WorkerLike;
  };
  const InThreadWorker = mod.Worker ?? mod.default;
  if (!InThreadWorker) {
    throw new Error("Could not resolve ELK Worker export in Node environment");
  }
  const elk = new ELK({
    workerFactory: () => new InThreadWorker() as unknown as Worker,
  });
  return { elk, usedWorker: false };
}

async function tryCreateBlobWorkerElk(): Promise<{ elk: ElkInstance; usedWorker: boolean }> {
  if (
    typeof window === "undefined" ||
    typeof Worker === "undefined" ||
    typeof Blob === "undefined" ||
    typeof URL === "undefined" ||
    typeof URL.createObjectURL !== "function"
  ) {
    throw new Error("Web Worker / Blob URL unavailable");
  }

  const workerScriptUrl = resolveElkWorkerScriptUrl();
  const bootstrap = `importScripts(${JSON.stringify(workerScriptUrl)});`;
  const blob = new Blob([bootstrap], { type: "application/javascript" });
  const blobUrl = URL.createObjectURL(blob);

  let rawWorker: Worker | null = null;
  try {
    let workerFailed = false;
    let onFailure: ((err: Error) => void) | null = null;

    rawWorker = new Worker(blobUrl);
    rawWorker.addEventListener("error", (ev) => {
      ev.preventDefault();
      workerFailed = true;
      onFailure?.(new Error(ev.message || "ELK Blob Worker failed"));
    });

    const elk = new ELK({
      workerFactory: () => rawWorker!,
    });

    // Probe the worker once to confirm importScripts succeeded under file:// or http://
    await new Promise<void>((resolve, reject) => {
      if (workerFailed) {
        reject(new Error("ELK Blob Worker errored on startup"));
        return;
      }
      const timer = setTimeout(() => {
        reject(new Error("ELK Blob Worker startup probe timed out"));
      }, 2500);

      onFailure = (err) => {
        clearTimeout(timer);
        reject(err);
      };

      elk
        .knownLayoutAlgorithms()
        .then(() => {
          clearTimeout(timer);
          resolve();
        })
        .catch((err) => {
          clearTimeout(timer);
          reject(err instanceof Error ? err : new Error(String(err)));
        });
    });

    URL.revokeObjectURL(blobUrl);
    return { elk, usedWorker: true };
  } catch (err) {
    if (rawWorker) {
      try {
        rawWorker.terminate();
      } catch {
        // ignore termination error
      }
    }
    URL.revokeObjectURL(blobUrl);
    throw err;
  }
}

async function createBrowserInThreadFallbackElk(): Promise<{
  elk: ElkInstance;
  usedWorker: boolean;
}> {
  const candidates = [
    resolveElkWorkerScriptUrl(),
    new URL("vendor/elk-worker.min.js", window.location.href).href,
  ];

  let lastErr: Error | null = null;
  for (const src of candidates) {
    try {
      const InThreadWorker = await loadScriptWithModuleShim(src);
      const elk = new ELK({
        workerFactory: () => new InThreadWorker() as unknown as Worker,
      });
      return { elk, usedWorker: false };
    } catch (err) {
      lastErr = err instanceof Error ? err : new Error(String(err));
    }
  }
  throw lastErr ?? new Error("Unable to initialize ELK in-thread fallback");
}

export function resetElkInstanceForTests(): void {
  cachedInstancePromise = null;
}

export async function getElkInstance(): Promise<{ elk: ElkInstance; usedWorker: boolean }> {
  if (cachedInstancePromise) {
    return cachedInstancePromise;
  }
  cachedInstancePromise = (async () => {
    const isNode =
      typeof process !== "undefined" &&
      Boolean(process.versions?.node) &&
      typeof Worker === "undefined";
    if (isNode) {
      return createNodeInThreadElk();
    }
    try {
      return await tryCreateBlobWorkerElk();
    } catch {
      if (typeof document !== "undefined") {
        try {
          return await createBrowserInThreadFallbackElk();
        } catch {
          // Fall back to Node module loader if running in jsdom with Node runtime
          if (typeof process !== "undefined" && Boolean(process.versions?.node)) {
            return createNodeInThreadElk();
          }
          throw new Error("ELK initialization failed in both Blob Worker and in-thread fallback");
        }
      }
      return createNodeInThreadElk();
    }
  })();
  return cachedInstancePromise;
}

export async function layoutWithElk(
  nodes: LayoutNodeInput[],
  edges: LayoutEdgeInput[],
  extraOptions: Record<string, string> = {},
): Promise<LayoutResult> {
  if (nodes.length === 0) {
    return { nodes: [], edges: [], width: 0, height: 0, usedWorker: false };
  }

  const sortedNodes = [...nodes].sort((a, b) => a.id.localeCompare(b.id));
  const nodeIdSet = new Set(sortedNodes.map((n) => n.id));
  const sortedEdges = edges
    .filter((e) => nodeIdSet.has(e.source) && nodeIdSet.has(e.target) && e.source !== e.target)
    .sort((a, b) => {
      const idCmp = a.id.localeCompare(b.id);
      if (idCmp !== 0) return idCmp;
      const srcCmp = a.source.localeCompare(b.source);
      if (srcCmp !== 0) return srcCmp;
      return a.target.localeCompare(b.target);
    });

  const hasPartitions = sortedNodes.some((n) => n.partition !== undefined);
  const layoutOptions: Record<string, string> = {
    ...SPEC_ELK_LAYOUT_OPTIONS,
    ...(hasPartitions ? { "elk.partitioning.activate": "true" } : {}),
    ...extraOptions,
  };

  const elkGraph: ElkNode = {
    id: "root",
    layoutOptions,
    children: sortedNodes.map((n) => ({
      id: n.id,
      width: n.width ?? DEFAULT_NODE_WIDTH,
      height: n.height ?? DEFAULT_NODE_HEIGHT,
      ...(n.partition !== undefined
        ? { layoutOptions: { "elk.partitioning.partition": String(n.partition) } }
        : {}),
    })),
    edges: sortedEdges.map(
      (e): ElkExtendedEdge => ({
        id: e.id,
        sources: [e.source],
        targets: [e.target],
      }),
    ),
  };

  const { elk, usedWorker } = await getElkInstance();
  const laidOut = await elk.layout(elkGraph);

  const positionedNodes: PositionedNode[] = (laidOut.children ?? [])
    .map((c) => ({
      id: c.id,
      x: Math.round((c.x ?? 0) * 100) / 100,
      y: Math.round((c.y ?? 0) * 100) / 100,
      width: c.width ?? DEFAULT_NODE_WIDTH,
      height: c.height ?? DEFAULT_NODE_HEIGHT,
    }))
    .sort((a, b) => a.id.localeCompare(b.id));

  const edgeById = new Map(sortedEdges.map((e) => [e.id, e]));
  const routedEdges: RoutedEdge[] = (laidOut.edges ?? [])
    .map((e) => {
      const orig = edgeById.get(e.id);
      const pts: Point2D[] = [];
      for (const sec of e.sections ?? []) {
        pts.push(sec.startPoint);
        if (sec.bendPoints) pts.push(...sec.bendPoints);
        pts.push(sec.endPoint);
      }
      return {
        id: e.id,
        source: orig?.source ?? e.sources[0] ?? "",
        target: orig?.target ?? e.targets[0] ?? "",
        points: pts,
      };
    })
    .sort((a, b) => a.id.localeCompare(b.id));

  return {
    nodes: positionedNodes,
    edges: routedEdges,
    width: laidOut.width ?? 0,
    height: laidOut.height ?? 0,
    usedWorker,
  };
}

export function findOverlappingNodePairs(
  nodes: PositionedNode[],
): Array<[string, string]> {
  const overlaps: Array<[string, string]> = [];
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j];
      const horizOverlap = a.x < b.x + b.width && a.x + a.width > b.x;
      const vertOverlap = a.y < b.y + b.height && a.y + a.height > b.y;
      if (horizOverlap && vertOverlap) {
        overlaps.push([a.id, b.id]);
      }
    }
  }
  return overlaps;
}

function segmentsProperlyIntersect(
  a1: Point2D,
  a2: Point2D,
  b1: Point2D,
  b2: Point2D,
): boolean {
  const orient = (p: Point2D, q: Point2D, r: Point2D) =>
    (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x);

  const o1 = orient(a1, a2, b1);
  const o2 = orient(a1, a2, b2);
  const o3 = orient(b1, b2, a1);
  const o4 = orient(b1, b2, a2);

  // Strict proper intersection (ignores shared endpoints)
  if ((o1 > 0 && o2 < 0 || o1 < 0 && o2 > 0) && (o3 > 0 && o4 < 0 || o3 < 0 && o4 > 0)) {
    return true;
  }
  return false;
}

/**
 * Count geometric segment-segment edge crossings between distinct non-adjacent edges.
 */
export function countEdgeCrossings(
  edges: RoutedEdge[],
  nodes?: PositionedNode[],
): number {
  const nodeCenters = new Map<string, Point2D>();
  if (nodes) {
    for (const n of nodes) {
      nodeCenters.set(n.id, { x: n.x + n.width / 2, y: n.y + n.height / 2 });
    }
  }

  const polylines = edges.map((e) => {
    if (e.points.length >= 2) {
      return { edge: e, pts: e.points };
    }
    const src = nodeCenters.get(e.source);
    const tgt = nodeCenters.get(e.target);
    return { edge: e, pts: src && tgt ? [src, tgt] : [] };
  });

  let crossings = 0;
  for (let i = 0; i < polylines.length; i++) {
    const a = polylines[i];
    for (let j = i + 1; j < polylines.length; j++) {
      const b = polylines[j];
      // Edges sharing an endpoint naturally meet at that node
      if (
        a.edge.source === b.edge.source ||
        a.edge.source === b.edge.target ||
        a.edge.target === b.edge.source ||
        a.edge.target === b.edge.target
      ) {
        continue;
      }
      let crossed = false;
      for (let sa = 0; sa < a.pts.length - 1 && !crossed; sa++) {
        for (let sb = 0; sb < b.pts.length - 1 && !crossed; sb++) {
          if (segmentsProperlyIntersect(a.pts[sa], a.pts[sa + 1], b.pts[sb], b.pts[sb + 1])) {
            crossed = true;
            crossings++;
          }
        }
      }
    }
  }
  return crossings;
}
