import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function writeJson(filePath, data) {
  const json = JSON.stringify(data, null, 2) + "\n";
  writeFileSync(filePath, json, "utf-8");
  return json;
}

function writeChunkJsonp(chunksDir, slug, partId, payload) {
  const fileName = `${slug}.js`;
  const filePath = path.join(chunksDir, fileName);
  const line = `window.__ARCH_CHUNK__(${JSON.stringify(partId)},${JSON.stringify(payload)});\n`;
  writeFileSync(filePath, line, "utf-8");
  const buf = Buffer.from(line, "utf-8");
  const sha256 = createHash("sha256").update(buf).digest("hex");
  return { file: fileName, sha256, bytes: buf.length };
}

function writeSearchJsonp(chunksDir, entries) {
  const line = `window.__ARCH_SEARCH__(${JSON.stringify(entries)});\n`;
  writeFileSync(path.join(chunksDir, "search.js"), line, "utf-8");
}

function writeNullStubs(rootDir) {
  writeFileSync(path.join(rootDir, "descriptions.js"), "window.__ARCH_DESCRIPTIONS__ = null;\n", "utf-8");
  writeFileSync(path.join(rootDir, "runtime-overlay.v1.js"), "window.__ARCH_RUNTIME__ = null;\n", "utf-8");
  writeFileSync(path.join(rootDir, "dataflow.v1.js"), "window.__ARCH_DATAFLOW__ = null;\n", "utf-8");
}

function buildFqdFixture() {
  const dir = path.join(__dirname, "fqd");
  const chunksDir = path.join(dir, "chunks");
  mkdirSync(chunksDir, { recursive: true });

  const serverChunk = {
    schema_version: "2.0",
    part_id: "part:server",
    files: [
      {
        path: "server/src/app.ts",
        module_id: "mod:server",
        language: "typescript",
        tier: "scip",
        loc: 145,
        is_infra: false,
        symbols: [
          { name: "createAppServer", kind: "function", line: 12 },
          { name: "handleGetAccounts", kind: "function", line: 34 },
          { name: "handleGetAlerts", kind: "function", line: 68 },
          { name: "handleSyncPing", kind: "function", line: 102 },
        ],
      },
      {
        path: "server/src/store.ts",
        module_id: "mod:server",
        language: "typescript",
        tier: "scip",
        loc: 88,
        is_infra: false,
        symbols: [
          { name: "Store.listAccounts", kind: "method", line: 18 },
          { name: "Store.listAlerts", kind: "method", line: 42 },
          { name: "Store.saveUsage", kind: "method", line: 64 },
        ],
      },
      {
        path: "server/src/upstream.ts",
        module_id: "mod:server",
        language: "typescript",
        tier: "scip",
        loc: 52,
        is_infra: false,
        symbols: [{ name: "fetchProviderQuota", kind: "function", line: 15 }],
      },
    ],
    nodes: [
      {
        id: "ep:GET:/api/accounts",
        kind: "entrypoint",
        label: "GET /api/accounts",
        module_id: "mod:server",
        file: "server/src/app.ts",
        line: 34,
        symbol: "handleGetAccounts",
        tier: "scip",
        confidence: "heuristic",
        evidence: [{ file: "server/src/app.ts", line: 34, source: "rule", rule_id: "builtin:route-literal-compare" }],
        docstring: "Lists tracked accounts from SQLite.",
      },
      {
        id: "ep:GET:/api/alerts",
        kind: "entrypoint",
        label: "GET /api/alerts",
        module_id: "mod:server",
        file: "server/src/app.ts",
        line: 68,
        symbol: "handleGetAlerts",
        tier: "scip",
        confidence: "heuristic",
        evidence: [{ file: "server/src/app.ts", line: 68, source: "rule", rule_id: "builtin:route-literal-compare" }],
        docstring: "Returns active usage threshold alerts.",
      },
      {
        id: "ep:POST:/api/ping",
        kind: "entrypoint",
        label: "POST /api/ping",
        module_id: "mod:server",
        file: "server/src/app.ts",
        line: 102,
        symbol: "handleSyncPing",
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "server/src/app.ts", line: 102, source: "scip" }],
        docstring: "Internal health/ping helper without downstream boundary.",
      },
      {
        id: "sym:server.Store.listAccounts",
        kind: "service",
        label: "Store.listAccounts",
        module_id: "mod:server",
        file: "server/src/store.ts",
        line: 18,
        symbol: "Store.listAccounts",
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "server/src/store.ts", line: 18, source: "scip" }],
      },
      {
        id: "sym:server.Store.listAlerts",
        kind: "service",
        label: "Store.listAlerts",
        module_id: "mod:server",
        file: "server/src/store.ts",
        line: 42,
        symbol: "Store.listAlerts",
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "server/src/store.ts", line: 42, source: "scip" }],
      },
      {
        id: "sym:server.Store.saveUsage",
        kind: "service",
        label: "Store.saveUsage",
        module_id: "mod:server",
        file: "server/src/store.ts",
        line: 64,
        symbol: "Store.saveUsage",
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "server/src/store.ts", line: 64, source: "scip" }],
      },
      {
        id: "sym:server.fetchProviderQuota",
        kind: "service",
        label: "fetchProviderQuota",
        module_id: "mod:server",
        file: "server/src/upstream.ts",
        line: 15,
        symbol: "fetchProviderQuota",
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "server/src/upstream.ts", line: 15, source: "scip" }],
      },
      {
        id: "sym:server.normalizeQuota",
        kind: "service",
        label: "normalizeQuota",
        module_id: "mod:server",
        file: "server/src/app.ts",
        line: 118,
        symbol: "normalizeQuota",
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "server/src/app.ts", line: 118, source: "scip" }],
      },
    ],
    edges: [
      {
        source: "ep:GET:/api/accounts",
        target: "sym:server.Store.listAccounts",
        kind: "calls",
        file: "server/src/app.ts",
        line: 38,
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "server/src/app.ts", line: 38, source: "scip" }],
      },
      {
        source: "ep:GET:/api/accounts",
        target: "sym:server.fetchProviderQuota",
        kind: "calls",
        file: "server/src/app.ts",
        line: 41,
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "server/src/app.ts", line: 41, source: "scip" }],
      },
      {
        source: "sym:server.Store.listAccounts",
        target: "ds:sqlite",
        kind: "reads",
        file: "server/src/store.ts",
        line: 22,
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "server/src/store.ts", line: 22, source: "scip", rule_id: "builtin:node:sqlite" }],
      },
      {
        source: "ep:GET:/api/alerts",
        target: "sym:server.Store.listAlerts",
        kind: "calls",
        file: "server/src/app.ts",
        line: 72,
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "server/src/app.ts", line: 72, source: "scip" }],
      },
      {
        source: "sym:server.Store.listAlerts",
        target: "ds:sqlite",
        kind: "reads",
        file: "server/src/store.ts",
        line: 48,
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "server/src/store.ts", line: 48, source: "scip", rule_id: "builtin:node:sqlite" }],
      },
      {
        source: "sym:server.fetchProviderQuota",
        target: "ext:http",
        kind: "http_request",
        file: "server/src/upstream.ts",
        line: 24,
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "server/src/upstream.ts", line: 24, source: "scip", rule_id: "builtin:http.client" }],
      },
      {
        source: "sym:server.Store.saveUsage",
        target: "res:path:usage_db",
        kind: "writes",
        file: "server/src/store.ts",
        line: 70,
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "server/src/store.ts", line: 70, source: "rule" }],
      },
      {
        source: "ep:POST:/api/ping",
        target: "sym:server.normalizeQuota",
        kind: "calls",
        file: "server/src/app.ts",
        line: 108,
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "server/src/app.ts", line: 108, source: "scip" }],
      },
    ],
    flows: [
      {
        id: "flow:ep:GET:/api/accounts#1",
        entry_id: "ep:GET:/api/accounts",
        steps: [
          { node_id: "ep:GET:/api/accounts", edge_kind: null, file: "server/src/app.ts", line: 34 },
          { node_id: "sym:server.Store.listAccounts", edge_kind: "calls", file: "server/src/app.ts", line: 38 },
          { node_id: "ds:sqlite", edge_kind: "reads", file: "server/src/store.ts", line: 22 },
        ],
        ends_at: "ds:sqlite",
        end_reason: "boundary",
      },
      {
        id: "flow:ep:GET:/api/alerts#1",
        entry_id: "ep:GET:/api/alerts",
        steps: [
          { node_id: "ep:GET:/api/alerts", edge_kind: null, file: "server/src/app.ts", line: 68 },
          { node_id: "sym:server.Store.listAlerts", edge_kind: "calls", file: "server/src/app.ts", line: 72 },
          { node_id: "ds:sqlite", edge_kind: "reads", file: "server/src/store.ts", line: 48 },
        ],
        ends_at: "ds:sqlite",
        end_reason: "boundary",
      },
      {
        id: "flow:ep:POST:/api/ping#1",
        entry_id: "ep:POST:/api/ping",
        steps: [
          { node_id: "ep:POST:/api/ping", edge_kind: null, file: "server/src/app.ts", line: 102 },
          { node_id: "sym:server.normalizeQuota", edge_kind: "calls", file: "server/src/app.ts", line: 108 },
        ],
        ends_at: "sym:server.normalizeQuota",
        end_reason: "no-boundary-detected",
      },
    ],
  };

  const webChunk = {
    schema_version: "2.0",
    part_id: "part:web",
    files: [
      {
        path: "web/src/api.ts",
        module_id: "mod:web",
        language: "typescript",
        tier: "scip",
        loc: 46,
        is_infra: false,
        symbols: [
          { name: "fetchAccounts", kind: "function", line: 10 },
          { name: "fetchAlerts", kind: "function", line: 24 },
        ],
      },
      {
        path: "web/src/components/UsagePanel.tsx",
        module_id: "mod:web",
        language: "typescript",
        tier: "scip",
        loc: 72,
        is_infra: false,
        symbols: [{ name: "UsagePanel", kind: "function", line: 8 }],
      },
    ],
    nodes: [
      {
        id: "fe:web.fetchAccounts",
        kind: "frontend_caller",
        label: "fetchAccounts",
        module_id: "mod:web",
        file: "web/src/api.ts",
        line: 10,
        symbol: "fetchAccounts",
        tier: "scip",
        confidence: "heuristic",
        evidence: [{ file: "web/src/api.ts", line: 12, source: "rule" }],
      },
      {
        id: "fe:web.fetchAlerts",
        kind: "frontend_caller",
        label: "fetchAlerts",
        module_id: "mod:web",
        file: "web/src/api.ts",
        line: 24,
        symbol: "fetchAlerts",
        tier: "scip",
        confidence: "heuristic",
        evidence: [{ file: "web/src/api.ts", line: 26, source: "rule" }],
      },
      {
        id: "sym:web.UsagePanel",
        kind: "service",
        label: "UsagePanel",
        module_id: "mod:web",
        file: "web/src/components/UsagePanel.tsx",
        line: 8,
        symbol: "UsagePanel",
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "web/src/components/UsagePanel.tsx", line: 8, source: "scip" }],
      },
    ],
    edges: [
      {
        source: "sym:web.UsagePanel",
        target: "fe:web.fetchAccounts",
        kind: "calls",
        file: "web/src/components/UsagePanel.tsx",
        line: 21,
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "web/src/components/UsagePanel.tsx", line: 21, source: "scip" }],
      },
      {
        source: "fe:web.fetchAccounts",
        target: "ep:GET:/api/accounts",
        kind: "http_request",
        file: "web/src/api.ts",
        line: 12,
        tier: "scip",
        confidence: "heuristic",
        evidence: [{ file: "web/src/api.ts", line: 12, source: "rule" }],
      },
      {
        source: "fe:web.fetchAlerts",
        target: "ep:GET:/api/alerts",
        kind: "http_request",
        file: "web/src/api.ts",
        line: 26,
        tier: "scip",
        confidence: "heuristic",
        evidence: [{ file: "web/src/api.ts", line: 26, source: "rule" }],
      },
    ],
    flows: [],
  };

  const sharedChunk = {
    schema_version: "2.0",
    part_id: "part:shared",
    files: [
      {
        path: "src/shared/types.ts",
        module_id: "mod:shared",
        language: "typescript",
        tier: "scip",
        loc: 42,
        is_infra: false,
        symbols: [{ name: "QuotaSnapshot", kind: "interface", line: 5 }],
      },
    ],
    nodes: [
      {
        id: "sym:shared.formatLimit",
        kind: "service",
        label: "formatLimit",
        module_id: "mod:shared",
        file: "src/shared/types.ts",
        line: 22,
        symbol: "formatLimit",
        tier: "scip",
        confidence: "exact",
        evidence: [{ file: "src/shared/types.ts", line: 22, source: "scip" }],
      },
    ],
    edges: [],
    flows: [],
  };

  const scriptsChunk = {
    schema_version: "2.0",
    part_id: "part:scripts",
    files: [
      {
        path: "scripts/backfill_usage.ts",
        module_id: "mod:scripts",
        language: "typescript",
        tier: "tree-sitter",
        loc: 60,
        is_infra: false,
        symbols: [{ name: "backfillUsage", kind: "function", line: 12 }],
      },
    ],
    nodes: [
      {
        id: "ep:cli:backfill_usage",
        kind: "entrypoint",
        label: "CLI backfill_usage",
        module_id: "mod:scripts",
        file: "scripts/backfill_usage.ts",
        line: 12,
        symbol: "backfillUsage",
        tier: "tree-sitter",
        confidence: "exact",
        evidence: [{ file: "scripts/backfill_usage.ts", line: 12, source: "tree-sitter" }],
      },
    ],
    edges: [
      {
        source: "ep:cli:backfill_usage",
        target: "res:path:usage_db",
        kind: "reads",
        file: "scripts/backfill_usage.ts",
        line: 25,
        tier: "tree-sitter",
        confidence: "exact",
        evidence: [{ file: "scripts/backfill_usage.ts", line: 25, source: "tree-sitter" }],
      },
    ],
    flows: [
      {
        id: "flow:ep:cli:backfill_usage#1",
        entry_id: "ep:cli:backfill_usage",
        steps: [
          { node_id: "ep:cli:backfill_usage", edge_kind: null, file: "scripts/backfill_usage.ts", line: 12 },
          { node_id: "res:path:usage_db", edge_kind: "reads", file: "scripts/backfill_usage.ts", line: 25 },
        ],
        ends_at: "res:path:usage_db",
        end_reason: "resource",
      },
    ],
  };

  const chunksIndex = {
    "part:server": writeChunkJsonp(chunksDir, "server", "part:server", serverChunk),
    "part:web": writeChunkJsonp(chunksDir, "web", "part:web", webChunk),
    "part:shared": writeChunkJsonp(chunksDir, "shared", "part:shared", sharedChunk),
    "part:scripts": writeChunkJsonp(chunksDir, "scripts", "part:scripts", scriptsChunk),
  };
  writeJson(path.join(chunksDir, "index.json"), chunksIndex);

  const searchEntries = [
    ["ep:GET:/api/accounts", "GET /api/accounts", "entrypoint", "part:server", "/api/accounts"],
    ["ep:GET:/api/alerts", "GET /api/alerts", "entrypoint", "part:server", "/api/alerts"],
    ["ep:POST:/api/ping", "POST /api/ping", "entrypoint", "part:server", "/api/ping"],
    ["sym:server.Store.listAccounts", "Store.listAccounts", "service", "part:server"],
    ["sym:server.Store.listAlerts", "Store.listAlerts", "service", "part:server"],
    ["fe:web.fetchAccounts", "fetchAccounts", "frontend_caller", "part:web"],
    ["sym:shared.formatLimit", "formatLimit", "service", "part:shared"],
    ["ep:cli:backfill_usage", "CLI backfill_usage", "entrypoint", "part:scripts"],
  ];
  writeSearchJsonp(chunksDir, searchEntries);
  writeNullStubs(dir);

  const overview = {
    schema_version: "2.0",
    repo_name: "fqd-sample",
    generated_at_commit: "c373aec",
    include_tests: false,
    tier_summary: {
      scip_files: 6,
      treesitter_files: 1,
      total_files: 7,
      excluded_test_files: 2,
      tier2_modules: ["mod:scripts"],
    },
    parts: [
      {
        id: "part:server",
        name: "server",
        kind: "service",
        root: "server",
        group_ids: ["mod:server"],
        counts: { files: 3, loc: 285, entrypoints: 3, symbols: 8 },
        evidence: [{ file: "server/src/app.ts", line: 12, source: "scip" }],
      },
      {
        id: "part:web",
        name: "web",
        kind: "ui",
        root: "web",
        group_ids: ["mod:web"],
        counts: { files: 2, loc: 118, entrypoints: 0, symbols: 3 },
        evidence: [{ file: "web/src/api.ts", line: 10, source: "scip" }],
      },
      {
        id: "part:shared",
        name: "shared",
        kind: "library",
        root: "src/shared",
        group_ids: ["mod:shared"],
        counts: { files: 1, loc: 42, entrypoints: 0, symbols: 1 },
        evidence: [{ file: "src/shared/types.ts", line: 5, source: "scip" }],
      },
      {
        id: "part:scripts",
        name: "scripts",
        kind: "scripts",
        root: "scripts",
        group_ids: ["mod:scripts"],
        counts: { files: 1, loc: 60, entrypoints: 1, symbols: 1 },
        evidence: [{ file: "scripts/backfill_usage.ts", line: 12, source: "tree-sitter" }],
      },
    ],
    groups: [
      {
        id: "mod:server",
        name: "server",
        part_id: "part:server",
        path: "server/src",
        language: "typescript",
        tier: "scip",
        tier_mix: { scip: 3 },
        counts: { files: 3, loc: 285, entrypoints: 3, symbols: 8 },
      },
      {
        id: "mod:web",
        name: "web",
        part_id: "part:web",
        path: "web/src",
        language: "typescript",
        tier: "scip",
        tier_mix: { scip: 2 },
        counts: { files: 2, loc: 118, entrypoints: 0, symbols: 3 },
      },
      {
        id: "mod:shared",
        name: "shared",
        part_id: "part:shared",
        path: "src/shared",
        language: "typescript",
        tier: "scip",
        tier_mix: { scip: 1 },
        counts: { files: 1, loc: 42, entrypoints: 0, symbols: 1 },
      },
      {
        id: "mod:scripts",
        name: "scripts",
        part_id: "part:scripts",
        path: "scripts",
        language: "typescript",
        tier: "tree-sitter",
        tier_mix: { "tree-sitter": 1 },
        counts: { files: 1, loc: 60, entrypoints: 1, symbols: 1 },
      },
    ],
    boundaries: [
      {
        id: "ds:sqlite",
        boundary: "datastore",
        kind: "sql",
        label: "SQLite",
        rule_id: "builtin:node:sqlite",
        confidence: "exact",
        evidence: [{ file: "server/src/store.ts", line: 1, source: "scip", rule_id: "builtin:node:sqlite" }],
      },
      {
        id: "ext:http",
        boundary: "sink",
        kind: "http_api",
        label: "HTTP API",
        rule_id: "builtin:http.client",
        confidence: "exact",
        evidence: [{ file: "server/src/upstream.ts", line: 24, source: "scip", rule_id: "builtin:http.client" }],
      },
      {
        id: "res:path:usage_db",
        boundary: "resource",
        kind: "path",
        label: "usage.db",
        rule_id: "builtin:resource-path",
        confidence: "exact",
        evidence: [
          { file: "server/src/store.ts", line: 70, source: "rule" },
          { file: "scripts/backfill_usage.ts", line: 25, source: "tree-sitter" },
        ],
      },
    ],
    edges: [
      {
        source: "part:web",
        target: "part:server",
        kinds: { http_request: 2 },
        evidence_sample: [{ file: "web/src/api.ts", line: 12, source: "rule" }],
      },
      {
        source: "part:web",
        target: "part:shared",
        kinds: { calls: 1 },
        evidence_sample: [{ file: "web/src/components/UsagePanel.tsx", line: 4, source: "scip" }],
      },
      {
        source: "part:server",
        target: "part:shared",
        kinds: { calls: 1 },
        evidence_sample: [{ file: "server/src/app.ts", line: 5, source: "scip" }],
      },
      {
        source: "part:server",
        target: "ds:sqlite",
        kinds: { reads: 2 },
        evidence_sample: [{ file: "server/src/store.ts", line: 22, source: "scip" }],
      },
      {
        source: "part:server",
        target: "ext:http",
        kinds: { http_request: 1 },
        evidence_sample: [{ file: "server/src/upstream.ts", line: 24, source: "scip" }],
      },
      {
        source: "part:server",
        target: "res:path:usage_db",
        kinds: { writes: 1 },
        evidence_sample: [{ file: "server/src/store.ts", line: 70, source: "rule" }],
      },
      {
        source: "part:scripts",
        target: "res:path:usage_db",
        kinds: { reads: 1 },
        evidence_sample: [{ file: "scripts/backfill_usage.ts", line: 25, source: "tree-sitter" }],
      },
    ],
    flow_index: [
      {
        id: "flow:ep:GET:/api/accounts#1",
        entry_id: "ep:GET:/api/accounts",
        part_id: "part:server",
        step_count: 3,
        ends_at: "ds:sqlite",
        end_reason: "boundary",
      },
      {
        id: "flow:ep:GET:/api/alerts#1",
        entry_id: "ep:GET:/api/alerts",
        part_id: "part:server",
        step_count: 3,
        ends_at: "ds:sqlite",
        end_reason: "boundary",
      },
      {
        id: "flow:ep:POST:/api/ping#1",
        entry_id: "ep:POST:/api/ping",
        part_id: "part:server",
        step_count: 2,
        ends_at: "sym:server.normalizeQuota",
        end_reason: "no-boundary-detected",
      },
      {
        id: "flow:ep:cli:backfill_usage#1",
        entry_id: "ep:cli:backfill_usage",
        part_id: "part:scripts",
        step_count: 2,
        ends_at: "res:path:usage_db",
        end_reason: "resource",
      },
    ],
    delta: {
      changed_files: ["server/src/app.ts"],
      new_files: ["web/src/components/UsagePanel.tsx"],
    },
    chunks: chunksIndex,
  };

  writeJson(path.join(dir, "overview.json"), overview);
}

function buildLargeFixture() {
  const dir = path.join(__dirname, "large");
  const chunksDir = path.join(dir, "chunks");
  mkdirSync(chunksDir, { recursive: true });

  const totalParts = 320;
  const parts = [];
  const groups = [];
  const edges = [];

  for (let i = 0; i < totalParts; i++) {
    const num = String(i).padStart(3, "0");
    const pid = `part:svc-${num}`;
    const gid = `mod:svc-${num}`;
    parts.push({
      id: pid,
      name: `svc_${num}`,
      kind: i === 0 ? "ui" : "service",
      root: `services/svc_${num}`,
      group_ids: [gid],
      counts: { files: 1, loc: 40, entrypoints: 1, symbols: 2 },
      evidence: [{ file: `services/svc_${num}/index.ts`, line: 1, source: "scip" }],
    });
    groups.push({
      id: gid,
      name: `svc_${num}`,
      part_id: pid,
      path: `services/svc_${num}`,
      language: "typescript",
      tier: "scip",
      tier_mix: { scip: 1 },
      counts: { files: 1, loc: 40, entrypoints: 1, symbols: 2 },
    });
    if (i > 0) {
      edges.push({
        source: "part:svc-000",
        target: pid,
        kinds: { calls: 1 },
        evidence_sample: [{ file: "services/svc_000/index.ts", line: 10, source: "scip" }],
      });
    }
  }

  // Also give part:svc-000 a chunk with 330 symbols and 30 files to test file/group/symbol caps
  const megaFiles = [];
  const megaNodes = [
    {
      id: "ep:mega.root",
      kind: "entrypoint",
      label: "mega.root",
      module_id: "mod:svc-000",
      file: "services/svc_000/index.ts",
      line: 1,
      symbol: "root",
      tier: "scip",
      confidence: "exact",
      evidence: [{ file: "services/svc_000/index.ts", line: 1, source: "scip" }],
    },
  ];
  const megaEdges = [];

  for (let f = 0; f < 30; f++) {
    const fnum = String(f).padStart(2, "0");
    const sub = f < 15 ? "handlers" : "workers";
    megaFiles.push({
      path: `services/svc_000/${sub}/f_${fnum}.ts`,
      module_id: "mod:svc-000",
      language: "typescript",
      tier: "scip",
      loc: 30,
      is_infra: false,
      symbols: [{ name: `fn_${fnum}`, kind: "function", line: 5 }],
    });
  }

  for (let s = 0; s < 330; s++) {
    const snum = String(s).padStart(3, "0");
    const sid = `sym:mega.fn_${snum}`;
    megaNodes.push({
      id: sid,
      kind: "service",
      label: `fn_${snum}`,
      module_id: "mod:svc-000",
      file: "services/svc_000/index.ts",
      line: 10 + s,
      symbol: `fn_${snum}`,
      tier: "scip",
      confidence: "exact",
      evidence: [{ file: "services/svc_000/index.ts", line: 10 + s, source: "scip" }],
    });
    megaEdges.push({
      source: "ep:mega.root",
      target: sid,
      kind: "calls",
      file: "services/svc_000/index.ts",
      line: 5,
      tier: "scip",
      confidence: "exact",
      evidence: [{ file: "services/svc_000/index.ts", line: 5, source: "scip" }],
    });
  }

  const megaChunk = {
    schema_version: "2.0",
    part_id: "part:svc-000",
    files: megaFiles,
    nodes: megaNodes,
    edges: megaEdges,
    flows: [],
  };

  const chunksIndex = {
    "part:svc-000": writeChunkJsonp(chunksDir, "svc-000", "part:svc-000", megaChunk),
  };
  writeJson(path.join(chunksDir, "index.json"), chunksIndex);
  writeSearchJsonp(chunksDir, [
    ["ep:mega.root", "mega.root", "entrypoint", "part:svc-000"],
  ]);
  writeNullStubs(dir);

  const overview = {
    schema_version: "2.0",
    repo_name: "large-cap-fixture",
    generated_at_commit: "0000000",
    include_tests: false,
    tier_summary: {
      scip_files: totalParts,
      treesitter_files: 0,
      total_files: totalParts,
      excluded_test_files: 0,
      tier2_modules: [],
    },
    parts,
    groups,
    boundaries: [],
    edges,
    flow_index: [],
    delta: { changed_files: [], new_files: [] },
    chunks: chunksIndex,
  };
  writeJson(path.join(dir, "overview.json"), overview);
}

buildFqdFixture();
buildLargeFixture();
console.log("Generated viewer-app/fixtures/{fqd,large}");
