import { isRuntimeOverlayPayload } from "../ui/RuntimeLayer";
import type {
  ChunkMeta,
  DataflowData,
  DescriptionsData,
  OverviewData,
  PartChunk,
  RuntimeOverlayData,
  SearchTuple,
} from "../types/bundle";

export interface ScriptLoadOptions {
  basePath?: string;
  scriptLoader?: (src: string) => Promise<void>;
}

const chunkCache = new Map<string, PartChunk>();
const inFlightChunks = new Map<string, Promise<PartChunk>>();
const pendingChunkResolvers = new Map<
  string,
  {
    resolve: (chunk: PartChunk) => void;
    reject: (err: Error) => void;
    meta: ChunkMeta;
  }
>();

let searchCache: SearchTuple[] | null = null;
let inFlightSearch: Promise<SearchTuple[]> | null = null;
let pendingSearchResolver: {
  resolve: (entries: SearchTuple[]) => void;
  reject: (err: Error) => void;
} | null = null;

function utf8ByteLength(text: string): number {
  if (typeof TextEncoder !== "undefined") {
    return new TextEncoder().encode(text).length;
  }
  return Buffer.byteLength(text, "utf-8");
}

/**
 * Verify that the received JSONP chunk payload is plausibly consistent with the
 * expected file byte count recorded in chunks/index.json.
 */
export function verifyChunkBytes(
  partId: string,
  payload: PartChunk,
  expectedBytes: number,
): void {
  if (!Number.isFinite(expectedBytes) || expectedBytes <= 0) {
    throw new Error(`Invalid expected chunk byte size for ${partId}: ${expectedBytes}`);
  }
  const serializedPayload = JSON.stringify(payload);
  // Wrapper is: window.__ARCH_CHUNK__("<partId>",<payload>);\n
  const wrapperBytes = 25 + utf8ByteLength(JSON.stringify(partId));
  const actualEstimatedBytes = wrapperBytes + utf8ByteLength(serializedPayload);
  const tolerance = Math.max(256, Math.ceil(expectedBytes * 0.25));
  if (Math.abs(expectedBytes - actualEstimatedBytes) > tolerance) {
    throw new Error(
      `Chunk byte-size check failed for ${partId}: expected ~${expectedBytes} B, got ~${actualEstimatedBytes} B`,
    );
  }
}

function resolveChunkBasePath(explicitBase?: string): string {
  if (explicitBase !== undefined) {
    return explicitBase.replace(/\/+$/, "");
  }
  if (typeof window !== "undefined" && window.__ARCH_BASE_PATH__) {
    return window.__ARCH_BASE_PATH__.replace(/\/+$/, "");
  }
  return "arch/chunks";
}

function buildChunkUrl(basePath: string, file: string): string {
  const normalizedFile = file.replace(/^\/+/, "").replace(/^chunks\//, "");
  if (!basePath) {
    return normalizedFile;
  }
  return `${basePath}/${normalizedFile}`;
}

function installChunkCallback(): void {
  if (typeof window === "undefined") return;
  window.__ARCH_CHUNK__ = (partId: string, payload: PartChunk) => {
    const pending = pendingChunkResolvers.get(partId);
    if (!pending) {
      // Allow direct pre-populated or unsolicited chunk caching if valid
      if (payload && payload.part_id === partId) {
        chunkCache.set(partId, payload);
      }
      return;
    }
    pendingChunkResolvers.delete(partId);
    try {
      if (!payload || typeof payload !== "object" || payload.part_id !== partId) {
        throw new Error(
          `Chunk payload part_id mismatch: expected ${partId}, got ${payload?.part_id}`,
        );
      }
      verifyChunkBytes(partId, payload, pending.meta.bytes);
      chunkCache.set(partId, payload);
      pending.resolve(payload);
    } catch (err) {
      pending.reject(err instanceof Error ? err : new Error(String(err)));
    }
  };
}

function installSearchCallback(): void {
  if (typeof window === "undefined") return;
  window.__ARCH_SEARCH__ = (entries: SearchTuple[]) => {
    const pending = pendingSearchResolver;
    pendingSearchResolver = null;
    const normalized = Array.isArray(entries) ? entries : [];
    searchCache = normalized;
    if (pending) {
      pending.resolve(normalized);
    }
  };
}

function defaultScriptLoader(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    if (typeof document === "undefined") {
      reject(new Error(`Cannot inject <script src="${src}"> outside a browser DOM`));
      return;
    }
    const script = document.createElement("script");
    script.src = src;
    script.async = true;
    const cleanup = () => {
      script.onload = null;
      script.onerror = null;
      if (script.parentNode) {
        script.parentNode.removeChild(script);
      }
    };
    script.onload = () => {
      cleanup();
      resolve();
    };
    script.onerror = () => {
      cleanup();
      reject(new Error(`Failed to load JSONP script: ${src}`));
    };
    (document.head || document.body || document.documentElement).appendChild(script);
  });
}

export async function loadPartChunk(
  partId: string,
  overview: OverviewData,
  options: ScriptLoadOptions = {},
): Promise<PartChunk> {
  const cached = chunkCache.get(partId);
  if (cached) {
    return cached;
  }
  const existingFlight = inFlightChunks.get(partId);
  if (existingFlight) {
    return existingFlight;
  }

  const meta = overview.chunks?.[partId];
  if (!meta) {
    throw new Error(`No chunk metadata registered for part "${partId}"`);
  }
  if (!Number.isFinite(meta.bytes) || meta.bytes <= 0) {
    throw new Error(`Invalid byte length (${meta.bytes}) for chunk "${partId}"`);
  }

  installChunkCallback();
  const basePath = resolveChunkBasePath(options.basePath);
  const src = buildChunkUrl(basePath, meta.file);
  const loader = options.scriptLoader ?? defaultScriptLoader;

  const promise = new Promise<PartChunk>((resolve, reject) => {
    pendingChunkResolvers.set(partId, { resolve, reject, meta });
    loader(src)
      .then(() => {
        if (pendingChunkResolvers.has(partId)) {
          pendingChunkResolvers.delete(partId);
          reject(new Error(`JSONP script "${src}" loaded without calling __ARCH_CHUNK__`));
        }
      })
      .catch((err) => {
        pendingChunkResolvers.delete(partId);
        reject(err instanceof Error ? err : new Error(String(err)));
      });
  }).finally(() => {
    inFlightChunks.delete(partId);
  });

  inFlightChunks.set(partId, promise);
  return promise;
}

export async function loadSearchIndex(
  options: ScriptLoadOptions = {},
): Promise<SearchTuple[]> {
  if (searchCache !== null) {
    return searchCache;
  }
  if (inFlightSearch) {
    return inFlightSearch;
  }

  installSearchCallback();
  const basePath = resolveChunkBasePath(options.basePath);
  const src = buildChunkUrl(basePath, "search.js");
  const loader = options.scriptLoader ?? defaultScriptLoader;

  inFlightSearch = new Promise<SearchTuple[]>((resolve, reject) => {
    pendingSearchResolver = { resolve, reject };
    loader(src)
      .then(() => {
        if (pendingSearchResolver) {
          pendingSearchResolver = null;
          reject(new Error(`Search JSONP "${src}" loaded without calling __ARCH_SEARCH__`));
        }
      })
      .catch((err) => {
        pendingSearchResolver = null;
        reject(err instanceof Error ? err : new Error(String(err)));
      });
  }).finally(() => {
    inFlightSearch = null;
  });

  return inFlightSearch;
}

export function getCachedChunk(partId: string): PartChunk | undefined {
  return chunkCache.get(partId);
}

export function seedCachedChunk(chunk: PartChunk): void {
  chunkCache.set(chunk.part_id, chunk);
}

export function clearChunkCache(): void {
  chunkCache.clear();
  inFlightChunks.clear();
  pendingChunkResolvers.clear();
  searchCache = null;
  inFlightSearch = null;
  pendingSearchResolver = null;
}

export interface OptionalOverlays {
  descriptions: DescriptionsData | null;
  runtime: RuntimeOverlayData | null;
  dataflow: DataflowData | null;
}

export function readOptionalOverlays(): OptionalOverlays {
  if (typeof window === "undefined") {
    return { descriptions: null, runtime: null, dataflow: null };
  }
  const runtime = window.__ARCH_RUNTIME__;
  return {
    descriptions: window.__ARCH_DESCRIPTIONS__ ?? null,
    runtime: isRuntimeOverlayPayload(runtime) ? runtime : null,
    dataflow: window.__ARCH_DATAFLOW__ ?? null,
  };
}
