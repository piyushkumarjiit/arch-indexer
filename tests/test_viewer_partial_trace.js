/** Partial-trace behaviour of traceFlow when no boundary is reached (slice 1). */
const assert = require("assert");
const { traceFlow } = require("../src/arch_indexer/viewer/assets/viewer_logic.js");

function node(id, kind = "service") {
  return { id, kind, label: id, file: "f.py", line: 1 };
}

function chain(ids, kinds = {}) {
  return {
    nodes: ids.map((id) => node(id, kinds[id])),
    edges: ids.slice(1).map((id, i) => ({ source: ids[i], target: id, kind: "calls" })),
    data_stores: [],
    external_sinks: [],
  };
}

function runTests() {
  // 4-node chain, no sink
  let graph = chain(["ep:a", "s1", "s2", "s3"], { "ep:a": "entrypoint" });
  let trace = traceFlow(graph, "ep:a");
  assert.strictEqual(trace.steps.length, 4);
  assert.strictEqual(trace.endReason, "no-boundary-detected");
  assert.strictEqual(trace.endsAt, "s3");
  assert.deepStrictEqual(trace.paths, []);

  // 20-hop chain is capped at 12 hops / 13 steps
  const ids = ["ep:a"];
  for (let i = 1; i <= 20; i++) ids.push(`n${String(i).padStart(2, "0")}`);
  trace = traceFlow(chain(ids, { "ep:a": "entrypoint" }), "ep:a");
  assert.strictEqual(trace.steps.length, 13);
  assert.strictEqual(trace.endReason, "depth-cap");
  assert.strictEqual(trace.endsAt, "n12");

  // lone entry point
  graph = chain(["ep:a"], { "ep:a": "entrypoint" });
  trace = traceFlow(graph, "ep:a");
  assert.strictEqual(trace.steps.length, 1);
  assert.strictEqual(trace.endReason, "no-outgoing-edges");
  assert.strictEqual(trace.endsAt, "ep:a");

  // ties at greatest depth go to the smaller id
  graph = {
    nodes: [node("ep:a", "entrypoint"), node("zeta"), node("alpha")],
    edges: [
      { source: "ep:a", target: "zeta", kind: "calls" },
      { source: "ep:a", target: "alpha", kind: "calls" },
    ],
    data_stores: [],
    external_sinks: [],
  };
  trace = traceFlow(graph, "ep:a");
  assert.strictEqual(trace.endsAt, "alpha");
  assert.deepStrictEqual(trace.steps.map((s) => s.nodeId), ["ep:a", "alpha"]);

  // a sink beyond 12 hops is still reached
  const deep = ["ep:a"];
  for (let i = 1; i <= 15; i++) deep.push(`n${String(i).padStart(2, "0")}`);
  graph = chain(deep, { "ep:a": "entrypoint" });
  graph.data_stores = [{ id: "ds:x", kind: "sql", label: "X", evidence: ["a.py:1"] }];
  graph.edges.push({ source: "n15", target: "ds:x", kind: "writes" });
  trace = traceFlow(graph, "ep:a");
  assert.strictEqual(trace.endReason, "boundary");
  assert.strictEqual(trace.endsAt, "ds:x");
  assert.strictEqual(trace.steps.length, 17);

  // missing entry node
  trace = traceFlow(chain(["a"]), "ep:missing");
  assert.strictEqual(trace.endReason, null);
  assert.strictEqual(trace.endsAt, null);
  assert.deepStrictEqual(trace.steps, []);

  console.log("partial trace tests passed");
}

runTests();
