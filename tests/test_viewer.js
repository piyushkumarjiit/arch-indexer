/**
 * Standalone Node.js test suite for arch-indexer viewer logic and controller.
 * Checks hierarchy derivation, drill-down, breadcrumb, #node= focus,
 * trace reachability, deterministic layout with independent oracles,
 * scale performance, and browser controller interactions.
 */

const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { execFileSync } = require("child_process");

const repoRoot = path.resolve(__dirname, "..");

const ArchViewerLogic = require("../src/arch_indexer/viewer/assets/viewer_logic.js");
const {
  buildHierarchy,
  getHierarchyScope,
  traceFlow,
  computeLayeredLayout,
  getNodeDetails,
  ViewerState,
} = ArchViewerLogic;

function runTests() {
  console.log("Loading fixture sample-graph.json...");
  const fixturePath = path.join(__dirname, "fixtures/graph/sample-graph.json");
  const rawData = fs.readFileSync(fixturePath, "utf-8");
  const graph = JSON.parse(rawData);

  // 1. Test hierarchy derivation
  console.log("Checking hierarchy derivation...");
  const hierarchy = buildHierarchy(graph);
  assert(hierarchy, "Hierarchy should be generated");
  assert.strictEqual(hierarchy.systemNode.level, "system");
  assert(
    hierarchy.systemNode.children.includes("mod:orders"),
    "System should contain mod:orders"
  );
  assert(
    hierarchy.systemNode.children.includes("mod:auth"),
    "System should contain mod:auth"
  );
  assert(
    hierarchy.systemNode.children.includes("mod:billing"),
    "System should contain mod:billing"
  );
  assert(
    hierarchy.systemNode.children.includes("ds:postgres:orders"),
    "System should contain ds:postgres:orders"
  );
  assert(
    hierarchy.systemNode.children.includes("ext:stripe"),
    "System should contain ext:stripe"
  );

  const modOrders = hierarchy.nodeLookup.get("mod:orders");
  assert(modOrders, "mod:orders should exist in nodeLookup");
  assert.strictEqual(modOrders.kind, "module");
  assert(modOrders.fileCount >= 3, `mod:orders fileCount should be >= 3, got ${modOrders.fileCount}`);
  assert(modOrders.symbolCount >= 4, `mod:orders symbolCount should be >= 4, got ${modOrders.symbolCount}`);
  assert(modOrders.children.length > 0, "mod:orders should have directory children");

  const dirServices = hierarchy.nodeLookup.get("dir:src/orders/services");
  assert(dirServices, "dir:src/orders/services should exist");
  assert.strictEqual(dirServices.kind, "file_group");
  assert(dirServices.children.includes("file:src/orders/services/order_service.py"));

  const fileOrderService = hierarchy.nodeLookup.get("file:src/orders/services/order_service.py");
  assert(fileOrderService, "file:src/orders/services/order_service.py should exist");
  assert.strictEqual(fileOrderService.kind, "file");
  assert(fileOrderService.children.includes("sym:OrderService.create"));

  const symCreate = hierarchy.nodeLookup.get("sym:OrderService.create");
  assert(symCreate, "sym:OrderService.create should exist");
  assert.strictEqual(symCreate.kind, "service");
  assert.strictEqual(symCreate.file, "src/orders/services/order_service.py");
  assert.strictEqual(symCreate.line, 25);

  // Check tree-sitter tier
  const feCheckout = hierarchy.nodeLookup.get("fe:checkout_page");
  assert(feCheckout, "fe:checkout_page should exist");
  assert.strictEqual(feCheckout.tier, "tree-sitter", "fe:checkout_page must have canonical tree-sitter tier");

  // Check data store does not have hard-coded scip tier
  const dsOrders = hierarchy.nodeLookup.get("ds:postgres:orders");
  assert(dsOrders, "ds:postgres:orders should exist");
  assert.strictEqual(dsOrders.tier, null, "data stores without code tier should have null tier");

  // Module fallback assignment must respect path-separator boundaries
  const boundaryGraph = {
    schema_version: "1.0",
    repo_name: "boundary",
    modules: [{ id: "mod:auth", name: "auth", path: "src/auth", file_count: 2, tier: "scip" }],
    nodes: [
      { id: "sym:auth", kind: "service", label: "auth_fn", file: "src/auth/x.py", line: 2, tier: "scip" },
      { id: "sym:authz", kind: "service", label: "authz_fn", file: "src/authz/x.py", line: 1, tier: "scip" },
      { id: "sym:exact", kind: "service", label: "exact_fn", file: "src/auth", line: 3, tier: "scip" },
    ],
    edges: [],
    data_stores: [],
    external_sinks: [],
  };
  const boundaryHierarchy = buildHierarchy(boundaryGraph);
  assert.strictEqual(boundaryHierarchy.nodeLookup.get("sym:auth").moduleId, "mod:auth");
  assert.strictEqual(boundaryHierarchy.nodeLookup.get("sym:exact").moduleId, "mod:auth");
  assert.strictEqual(
    boundaryHierarchy.nodeLookup.get("sym:authz").moduleId,
    "mod:root",
    "src/authz must not be assigned to the module rooted at src/auth"
  );

  // Search must rank label matches even when many earlier nodes match on file path
  const searchGraph = { nodes: [], modules: [], data_stores: [], external_sinks: [] };
  for (let i = 0; i < 40; i++) {
    searchGraph.nodes.push({
      id: `sym:filler_${i}`,
      kind: "service",
      label: `filler_${i}`,
      file: `src/orders/filler_${i}.py`,
      line: 1,
      tier: "scip",
    });
  }
  searchGraph.nodes.push({
    id: "sym:Order",
    kind: "service",
    label: "Order",
    file: "src/core/order.py",
    line: 9,
    tier: "scip",
  });
  const searchResults = ArchViewerLogic.searchNodes(searchGraph, "order");
  assert(
    searchResults.some((r) => r.id === "sym:Order"),
    "Exact label match must be returned even when many earlier nodes match only by file path"
  );
  assert.strictEqual(searchResults[0].id, "sym:Order", "Label match must rank above file-path matches");

  // 2. Test Drill-down & Breadcrumbs
  console.log("Checking drill-down and breadcrumb navigation...");
  const state = new ViewerState(graph);
  assert.strictEqual(state.currentLevel, "system");
  assert.strictEqual(state.breadcrumbs.length, 1);
  assert.strictEqual(state.breadcrumbs[0].id, "system");

  // Drill down into mod:orders
  const drill1 = state.drillDown("mod:orders");
  assert(drill1, "drillDown into mod:orders should succeed");
  assert.strictEqual(state.currentLevel, "module");
  assert.strictEqual(state.currentScope, "mod:orders");
  assert.strictEqual(state.breadcrumbs.length, 2);
  assert.strictEqual(state.breadcrumbs[1].id, "mod:orders");

  // An absolute deep link to a directory must rebuild the full ancestor trail
  state.goToBreadcrumb(0);
  assert(state.drillDown("dir:src/orders/services"));
  assert.deepStrictEqual(
    state.breadcrumbs.map((c) => c.id),
    ["system", "mod:orders", "dir:src/orders/services"],
    "Deep link to a directory must rebuild the ancestor trail from the root"
  );
  state.goToBreadcrumb(0);
  assert(state.drillDown("mod:orders"));

  // Drill down into dir:src/orders/services
  const drill2 = state.drillDown("dir:src/orders/services");
  assert(drill2, "drillDown into dir:src/orders/services should succeed");
  assert.strictEqual(state.currentLevel, "file_group");
  assert.strictEqual(state.currentScope, "dir:src/orders/services");
  assert.strictEqual(state.breadcrumbs.length, 3);

  // Drill down into file
  const drill3 = state.drillDown("file:src/orders/services/order_service.py");
  assert(drill3, "drillDown into file should succeed");
  assert.strictEqual(state.currentLevel, "file");
  assert.strictEqual(state.breadcrumbs.length, 4);

  // Test Breadcrumb goUp
  state.goUp();
  assert.strictEqual(state.currentLevel, "file_group");
  assert.strictEqual(state.breadcrumbs.length, 3);
  assert.strictEqual(state.currentScope, "dir:src/orders/services");

  // Test jump to breadcrumb root
  state.goToBreadcrumb(0);
  assert.strictEqual(state.currentLevel, "system");
  assert.strictEqual(state.breadcrumbs.length, 1);
  assert.strictEqual(state.currentScope, "system");

  // 3. Test #node= focus
  console.log("Checking #node= focus navigation...");
  const focusSuccess = state.focusNode("ep:POST:/api/orders");
  assert(focusSuccess, "focusNode(ep:POST:/api/orders) should succeed");
  assert.strictEqual(state.selectedNodeId, "ep:POST:/api/orders");
  // Parent file level should be active and the trail must end at the displayed scope
  assert.strictEqual(state.currentLevel, "file");
  assert.strictEqual(state.currentScope, "file:src/orders/routes.py");
  const lastCrumb = state.breadcrumbs[state.breadcrumbs.length - 1];
  assert.strictEqual(
    lastCrumb.id,
    state.currentScope,
    "Breadcrumb trail must end at the displayed scope, not at the focused symbol"
  );
  assert.strictEqual(lastCrumb.level, state.currentLevel);
  assert(
    state.breadcrumbs.some((c) => c.id === "mod:orders"),
    "Breadcrumb trail must include the ancestor module"
  );

  // Focusing a module or system-level store shows the system scope, so the trail is just System
  state.focusNode("mod:orders");
  assert.strictEqual(state.currentLevel, "system");
  assert.strictEqual(state.currentScope, "system");
  assert.strictEqual(state.breadcrumbs.length, 1);
  state.focusNode("ds:postgres:orders");
  assert.strictEqual(state.currentLevel, "system");
  assert.strictEqual(state.currentScope, "system");
  assert.strictEqual(state.breadcrumbs.length, 1);

  // Restore file-level focus for the trace view checks below
  assert(state.focusNode("ep:POST:/api/orders"));

  const details = getNodeDetails(state.hierarchy, "ep:POST:/api/orders");
  assert(details, "Node details should be returned");
  assert.strictEqual(details.label, "POST /api/orders");
  assert.strictEqual(details.citation, "src/orders/routes.py:42");
  assert.strictEqual(details.tier, "scip");
  assert(details.auth.includes("jwt_required"), "Auth should include jwt_required");
  assert(details.auth.includes("require_role(admin)"), "Auth should include require_role(admin)");
  const targetIds = details.uses.map((u) => u.id);
  assert(targetIds.includes("sym:AuthService.verify_token"), "Uses should include verify_token");
  assert(targetIds.includes("sym:OrderService.create"), "Uses should include OrderService.create");

  // 4. Test Trace Reachability & Visibility Across Scopes
  console.log("Checking trace reachability and scope isolation...");
  const trace = traceFlow(graph, "ep:POST:/api/orders");
  assert(trace, "Trace result should exist");
  assert.strictEqual(trace.entrypointId, "ep:POST:/api/orders");

  const reachableSinkIds = trace.reachableSinks.map((s) => s.id);
  assert(
    reachableSinkIds.includes("ds:postgres:orders"),
    "Trace should reach ds:postgres:orders"
  );
  assert(
    reachableSinkIds.includes("ext:stripe"),
    "Trace should reach ext:stripe"
  );

  assert(trace.steps.length >= 3, `Trace should have >= 3 steps, got ${trace.steps.length}`);
  const step1 = trace.steps[0];
  assert.strictEqual(step1.nodeId, "ep:POST:/api/orders");
  assert(step1.auth.includes("jwt_required"), "Step 1 should have auth jwt_required");
  assert.strictEqual(step1.fileLine, "src/orders/routes.py:42");

  // Check intermediate citations
  const allStepCitations = trace.steps.map((s) => s.fileLine);
  assert(allStepCitations.includes("src/orders/routes.py:42"));

  // Check trace mode in ViewerState: downstream services across files must be visible in trace view
  state.startTrace("ep:POST:/api/orders");
  const traceView = state.getCurrentView();
  assert.strictEqual(traceView.level, "trace");
  const traceNodeIds = traceView.nodes.map((n) => n.id);
  assert(
    traceNodeIds.includes("ep:POST:/api/orders"),
    "Trace view must contain entrypoint"
  );
  assert(
    traceNodeIds.includes("sym:OrderService.create"),
    "Trace view must contain downstream service OrderService.create located in a different file"
  );
  assert(
    traceNodeIds.includes("ds:postgres:orders"),
    "Trace view must contain datastore"
  );
  assert(
    traceNodeIds.includes("ext:stripe"),
    "Trace view must contain external sink"
  );

  // Stop trace returns to normal level
  state.stopTrace();
  const normalView = state.getCurrentView();
  assert.strictEqual(normalView.level, "file");

  // 5. Test Deterministic Layered Layout (Independent Oracle: SPEC 6.3 column order)
  console.log("Checking deterministic layered layout column ordering (SPEC 6.3)...");
  // Oracle is SPEC 6.3's column order Frontend -> Entrypoints -> Services/Modules ->
  // Data Stores / External Sinks, asserted as relative ordering rather than copied arithmetic.
  const layout = computeLayeredLayout([
    { id: "fe:b", kind: "frontend_caller" },
    { id: "fe:a", kind: "frontend_caller" },
    { id: "ep:one", kind: "entrypoint" },
    { id: "sym:svc_b", kind: "service" },
    { id: "sym:svc_a", kind: "service" },
    { id: "mod:m", kind: "module" },
    { id: "ds:db", kind: "datastore" },
    { id: "ext:api", kind: "external_sink" },
  ], []);
  const xById = new Map(layout.nodes.map((n) => [n.id, n.x]));
  const xsOf = (ids) => ids.map((id) => xById.get(id));
  const maxX = (xs) => Math.max(...xs);
  const minX = (xs) => Math.min(...xs);
  const frontendXs = xsOf(["fe:a", "fe:b"]);
  const entrypointXs = xsOf(["ep:one"]);
  const serviceXs = xsOf(["sym:svc_a", "sym:svc_b", "mod:m"]);
  const sinkXs = xsOf(["ds:db", "ext:api"]);

  assert(maxX(frontendXs) < minX(entrypointXs), "Every frontend node must be left of every entrypoint");
  assert(maxX(entrypointXs) < minX(serviceXs), "Every entrypoint must be left of every service/module");
  assert(maxX(serviceXs) < minX(sinkXs), "Every service/module must be left of every data store/sink");
  assert.strictEqual(xById.get("sym:svc_a"), xById.get("sym:svc_b"), "Service nodes must share one column");
  assert.strictEqual(xById.get("mod:m"), xById.get("sym:svc_a"), "Modules must share the services column");
  assert.strictEqual(xById.get("ds:db"), xById.get("ext:api"), "Stores and sinks must share one column");

  // System view: modules must precede stores and sinks
  state.goToBreadcrumb(0);
  const sysView = state.getCurrentView();
  const moduleXs = sysView.nodes.filter((n) => n.kind === "module").map((n) => n.x);
  const sysSinkXs = sysView.nodes
    .filter((n) => n.kind === "datastore" || n.kind === "external_sink")
    .map((n) => n.x);
  assert(moduleXs.length > 0 && sysSinkXs.length > 0, "System view must contain modules and sinks");
  assert(maxX(moduleXs) < minX(sysSinkXs), "Module column must precede the store/sink column");

  // Symbol view: frontend -> entrypoint -> service ordering for one entrypoint
  state.drillDown("ep:POST:/api/orders");
  const epView = state.getCurrentView();
  const epFrontendXs = epView.nodes.filter((n) => n.kind === "frontend_caller").map((n) => n.x);
  const epEntryXs = epView.nodes.filter((n) => n.kind === "entrypoint").map((n) => n.x);
  const epServiceXs = epView.nodes.filter((n) => n.kind === "service").map((n) => n.x);
  assert(epFrontendXs.length > 0, "Symbol view must contain the calling frontend");
  assert(epEntryXs.length > 0, "Symbol view must contain the entrypoint");
  assert(epServiceXs.length > 0, "Symbol view must contain downstream services");
  assert(maxX(epFrontendXs) < minX(epEntryXs), "Frontend column must precede the entrypoint column");
  assert(maxX(epEntryXs) < minX(epServiceXs), "Entrypoint column must precede the service column");

  // 6. Test Kind Filter Preserves Structural Hierarchy Levels
  console.log("Checking kind filter preserves structural aggregates...");
  state.goToBreadcrumb(0);
  // Uncheck frontend_caller
  state.filters.kinds = new Set(["entrypoint", "service", "module", "datastore", "external_sink"]);
  state.drillDown("mod:orders");
  const modViewFiltered = state.getCurrentView();
  // Directory (file_group) nodes must NOT be dropped by unchecking leaf kinds
  const dirNodeCount = modViewFiltered.nodes.filter((n) => n.kind === "file_group").length;
  assert(dirNodeCount > 0, "Module drill-down must contain file_group directories when kind filter is active");
  state.filters.kinds.clear();

  // 7. Test Module Filter Re-check
  console.log("Checking module filter re-check behavior...");
  state.goToBreadcrumb(0);
  // Exclude mod:orders
  state.filters.modules = new Set(["mod:auth", "mod:billing"]);
  let viewExcluded = state.getCurrentView();
  assert(!viewExcluded.nodes.some((n) => n.id === "mod:orders"), "mod:orders must be excluded");
  // Re-check mod:orders (all modules included)
  state.filters.modules.clear();
  let viewRestored = state.getCurrentView();
  assert(viewRestored.nodes.some((n) => n.id === "mod:orders"), "mod:orders must be restored on re-check");

  // 8. Test Tier Filter with Canonical 'tree-sitter' and SCIP
  console.log("Checking tier filter behavior...");
  state.focusNode("fe:checkout_page");
  // Uncheck scip: leaves tree-sitter
  state.filters.tiers = new Set(["tree-sitter"]);
  let tierView = state.getCurrentView();
  const feNode = tierView.nodes.find((n) => n.id === "fe:checkout_page");
  assert(feNode, "fe:checkout_page with tree-sitter tier must remain visible when filtering by tree-sitter");
  // Datastore without code tier should not be hidden
  state.goToBreadcrumb(0);
  let sysTierView = state.getCurrentView();
  assert(
    sysTierView.nodes.some((n) => n.id === "ds:postgres:orders"),
    "Data store must remain visible when filtering code tiers"
  );
  state.filters.tiers.clear();

  // 8b. Filters must evaluate the leaves behind each container and aggregated edge
  console.log("Checking filters apply to aggregated containers and edges...");
  state.filters.tiers = new Set(["tree-sitter"]);
  state.goToBreadcrumb(0);
  const tsSystemView = state.getCurrentView();
  const tsNodeIds = tsSystemView.nodes.map((n) => n.id);
  assert(
    tsNodeIds.includes("mod:root"),
    "mod:root must stay visible while its only leaf (fe:checkout_page) is tree-sitter"
  );
  for (const scipOnlyModule of ["mod:auth", "mod:orders", "mod:billing"]) {
    assert(
      !tsNodeIds.includes(scipOnlyModule),
      `${scipOnlyModule} must be hidden when all of its leaves are scip-tier`
    );
  }
  assert(
    !tsSystemView.edges.some((e) => e.source === "mod:root"),
    "An edge whose target leaf is filtered out must not be aggregated"
  );
  state.filters.tiers.clear();

  state.filters.kinds = new Set([
    "entrypoint",
    "service",
    "module",
    "datastore",
    "external_sink",
  ]);
  state.goToBreadcrumb(0);
  const noFrontendView = state.getCurrentView();
  const noFrontendIds = noFrontendView.nodes.map((n) => n.id);
  assert(
    !noFrontendIds.includes("mod:root"),
    "mod:root must disappear when its only leaf kind (frontend_caller) is unchecked"
  );
  assert(
    noFrontendIds.includes("mod:orders"),
    "A module with passing leaves must stay visible under the kind filter"
  );
  assert(
    !noFrontendView.edges.some((e) => e.source === "mod:root"),
    "Edges from filtered-out leaves must not be aggregated"
  );

  state.filters.kinds = new Set(["entrypoint", "module", "datastore", "external_sink"]);
  state.drillDown("mod:orders");
  const noServiceView = state.getCurrentView();
  const noServiceIds = noServiceView.nodes.map((n) => n.id);
  assert(
    noServiceIds.includes("dir:src/orders"),
    "A directory with a passing entrypoint leaf must stay visible"
  );
  assert(
    !noServiceIds.includes("dir:src/orders/services"),
    "A directory whose only leaves are filtered-out services must be hidden"
  );
  assert(
    !noServiceIds.includes("ds:postgres:orders"),
    "A boundary store reached only through filtered-out leaves must not be added"
  );
  assert.strictEqual(
    noServiceView.edges.length,
    0,
    "Edges between filtered-out leaves must not be aggregated"
  );
  state.filters.kinds.clear();

  // 8c. Unfiltered views must show every declared module, including leafless ones
  console.log("Checking unfiltered views keep declared modules without flow nodes...");
  const leaflessGraph = JSON.parse(JSON.stringify(graph));
  leaflessGraph.modules.push({
    id: "mod:utils",
    name: "utils",
    language: "python",
    path: "src/utils",
    file_count: 8,
    tier: "scip",
  });
  const leaflessHierarchy = buildHierarchy(leaflessGraph);
  const unfilteredScope = getHierarchyScope(leaflessHierarchy, "system", "system", {
    modules: new Set(),
    kinds: new Set(),
    tiers: new Set(),
  });
  const unfilteredIds = new Set(unfilteredScope.nodes.map((n) => n.id));
  for (const childId of leaflessHierarchy.systemNode.children) {
    assert(
      unfilteredIds.has(childId),
      `An unfiltered system view must show the declared entry ${childId}`
    );
  }
  assert(
    unfilteredIds.has("mod:utils"),
    "A declared module with no flow nodes must stay visible when no filters are active"
  );

  // 9. Test Scale Graph traceFlow (Independent Oracle: graph-reachable sink set)
  console.log("Checking scale graph traceFlow reachability on ~5,000 nodes/15,000 edges...");
  const pythonExe = process.env.ARCH_VIEWER_PYTHON || "python3";
  const scaleJson = execFileSync(
    pythonExe,
    [
      "-c",
      "import json; from tests.synthetic_generator import generate_synthetic_graph; print(json.dumps(generate_synthetic_graph(5000, 15000)))",
    ],
    { cwd: repoRoot, maxBuffer: 64 * 1024 * 1024, encoding: "utf-8" }
  );
  const scaleGraph = JSON.parse(scaleJson);

  assert.strictEqual(scaleGraph.nodes.length, 5000);
  assert.strictEqual(scaleGraph.edges.length, 15000);

  const scaleTrace = traceFlow(scaleGraph, "ep:ep_0000");

  // Independent oracle: plain BFS over graph.edges for the sinks reachable from the entrypoint.
  const adjacency = new Map();
  for (const e of scaleGraph.edges) {
    if (!adjacency.has(e.source)) adjacency.set(e.source, []);
    adjacency.get(e.source).push(e.target);
  }
  const isSink = (id) => id.startsWith("ds:") || id.startsWith("ext:");
  const seen = new Set(["ep:ep_0000"]);
  const bfsQueue = ["ep:ep_0000"];
  const oracleSinks = new Set();
  while (bfsQueue.length > 0) {
    const current = bfsQueue.shift();
    if (current !== "ep:ep_0000" && isSink(current)) {
      oracleSinks.add(current);
      continue;
    }
    for (const target of adjacency.get(current) || []) {
      if (!seen.has(target)) {
        seen.add(target);
        bfsQueue.push(target);
      }
    }
  }
  assert(oracleSinks.size > 0, "Scale fixture must reach at least one sink from ep:ep_0000");
  assert.deepStrictEqual(
    scaleTrace.reachableSinks.map((s) => s.id).sort(),
    Array.from(oracleSinks).sort(),
    "traceFlow must reach exactly the graph-reachable data stores and external sinks"
  );

  // Step walkthrough must follow real edges, stay inside the reachable subgraph, and end at a sink
  const reachableSet = new Set(scaleTrace.reachableNodes);
  const edgePairs = new Set(scaleGraph.edges.map((e) => `${e.source}->${e.target}`));
  assert(scaleTrace.steps.length > 0, "Scale trace must produce steps");
  assert.strictEqual(scaleTrace.steps[0].nodeId, "ep:ep_0000", "Step walkthrough must start at the entrypoint");
  for (let i = 1; i < scaleTrace.steps.length; i++) {
    const from = scaleTrace.steps[i - 1].nodeId;
    const to = scaleTrace.steps[i].nodeId;
    assert(reachableSet.has(to), `Step node ${to} must be in the reachable subgraph`);
    assert(edgePairs.has(`${from}->${to}`), `Step ${from} -> ${to} must follow a real graph edge`);
  }
  assert(
    isSink(scaleTrace.steps[scaleTrace.steps.length - 1].nodeId),
    "Step walkthrough must end at a data store or external sink"
  );

  // 10. Test Browser Controller (viewer.js) Drill-Down & Hashchange Handling
  console.log("Checking browser controller drill-down and hashchange handling...");
  testBrowserController(rawData);

  console.log("All viewer logic tests passed successfully!");
}

/**
 * Minimal DOM harness to test viewer.js interactions.
 */
function testBrowserController(graphJsonText) {
  // Setup minimal DOM elements
  const listeners = new Map();
  const elements = new Map();

  function createMockElement(tag, id) {
    const el = {
      tagName: (tag || "div").toUpperCase(),
      id: id || "",
      className: "",
      textContent: "",
      value: "",
      checked: true,
      children: [],
      style: {},
      attributes: {},
      classList: {
        _classes: new Set(),
        add(c) { this._classes.add(c); },
        remove(c) { this._classes.delete(c); },
        contains(c) { return this._classes.has(c); },
      },
      setAttribute(k, v) { this.attributes[k] = String(v); },
      getAttribute(k) { return this.attributes[k] || null; },
      appendChild(child) { this.children.push(child); child.parentElement = this; return child; },
      querySelector(sel) {
        if (sel === "input") return this.children.find((c) => c.tagName === "INPUT") || null;
        return null;
      },
      querySelectorAll(sel) {
        const res = [];
        function walk(node) {
          for (const ch of node.children) {
            if (sel === "input" && ch.tagName === "INPUT") res.push(ch);
            else if (sel === ".link-item" && ch.className && ch.className.includes("link-item")) res.push(ch);
            walk(ch);
          }
        }
        walk(this);
        return res;
      },
      addEventListener(evt, handler) {
        if (!this._evts) this._evts = new Map();
        if (!this._evts.has(evt)) this._evts.set(evt, []);
        this._evts.get(evt).push(handler);
      },
      dispatchEvent(event) {
        const handlers = (this._evts && this._evts.get(event.type)) || [];
        for (const h of handlers) h(event);
      },
      closest(sel) { return null; },
      getBoundingClientRect() { return { left: 0, top: 0, width: 800, height: 600 }; },
    };
    let htmlValue = "";
    Object.defineProperty(el, "innerHTML", {
      get() { return htmlValue; },
      set(v) { htmlValue = v; el.children.length = 0; },
    });
    if (id) elements.set(id, el);
    return el;
  }

  // SVG elements expose className as a read-only SVGAnimatedString, so assigning
  // to el.className throws in a real browser. Mirror that contract in the DOM stub.
  function createMockSvgElement(tag) {
    const el = createMockElement(tag);
    Object.defineProperty(el, "className", {
      configurable: true,
      get() {
        return { baseVal: el.attributes["class"] || "" };
      },
      set() {
        throw new TypeError("Cannot set property className of an SVG element");
      },
    });
    return el;
  }

  // Pre-create required DOM elements
  createMockElement("div", "repo-title");
  createMockElement("div", "search-input");
  createMockElement("div", "search-dropdown");
  createMockElement("button", "btn-load-file");
  createMockElement("input", "file-input");
  createMockElement("div", "breadcrumbs");
  createMockElement("div", "zoom-level-badge");
  createMockElement("div", "filter-modules");
  createMockElement("div", "filter-kinds");
  createMockElement("div", "filter-tiers");
  createMockElement("svg", "canvas-svg");
  createMockElement("g", "viewport");
  createMockElement("div", "inspector-panel");
  createMockElement("div", "inspector-content");
  createMockElement("button", "btn-inspector-drill");
  createMockElement("button", "btn-inspector-trace");
  createMockElement("button", "btn-zoom-in");
  createMockElement("button", "btn-zoom-out");
  createMockElement("button", "btn-zoom-reset");
  createMockElement("button", "btn-close-inspector");
  createMockElement("button", "btn-prev-step");
  createMockElement("button", "btn-next-step");
  createMockElement("button", "btn-exit-trace");
  createMockElement("div", "trace-bar");
  createMockElement("div", "trace-step-text");

  // Script tag holding graph json
  const dataScript = createMockElement("script", "arch-graph-data");
  dataScript.textContent = graphJsonText;

  // Window & Document mock
  const windowListeners = new Map();
  let currentHash = "";

  global.window = {
    location: {
      get hash() { return currentHash; },
      set hash(v) {
        currentHash = v;
        // Direct assignment to window.location.hash in browser fires hashchange event asynchronously
        const handlers = windowListeners.get("hashchange") || [];
        for (const h of handlers) h({ type: "hashchange" });
      },
      pathname: "/index.html",
      search: "",
    },
    history: {
      replaceState(state, title, url) {
        if (url && url.includes("#")) {
          currentHash = url.substring(url.indexOf("#"));
        } else {
          currentHash = "";
        }
        // replaceState does NOT fire hashchange
      },
    },
    addEventListener(evt, h) {
      if (!windowListeners.has(evt)) windowListeners.set(evt, []);
      windowListeners.get(evt).push(h);
    },
  };

  global.document = {
    getElementById(id) { return elements.get(id) || null; },
    createElement(tag) { return createMockElement(tag); },
    createElementNS(ns, tag) { return createMockSvgElement(tag); },
    addEventListener(evt, h) {
      if (!listeners.has(evt)) listeners.set(evt, []);
      listeners.get(evt).push(h);
    },
  };

  global.ArchViewerLogic = ArchViewerLogic;

  // Load viewer.js
  const viewerController = require("../src/arch_indexer/viewer/assets/viewer.js");
  viewerController.init();

  const state = viewerController.getState();
  assert(state, "ViewerState should be initialized");
  assert.strictEqual(state.currentLevel, "system", "Initial level should be system");

  // Find mod:orders node in viewport
  const viewport = elements.get("viewport");
  let modOrdersNode = null;
  function findNode(node) {
    if (node.attributes && node.attributes["data-id"] === "mod:orders") {
      modOrdersNode = node;
      return;
    }
    for (const ch of node.children) findNode(ch);
  }
  findNode(viewport);
  assert(modOrdersNode, "mod:orders SVG node should be rendered in viewport");

  // Trigger double-click on mod:orders
  modOrdersNode.dispatchEvent({ type: "dblclick", stopPropagation() {} });

  // Fire hashchange if any
  viewerController.handleHashChange();

  // Crucial check: verify currentLevel is 'module' and did NOT revert to 'system'!
  assert.strictEqual(
    state.currentLevel,
    "module",
    `Double-clicking mod:orders must stay at 'module' level, but was ${state.currentLevel}`
  );
  assert.strictEqual(
    state.currentScope,
    "mod:orders",
    `Current scope must be 'mod:orders', but was ${state.currentScope}`
  );

  // Test URL #node= focus with encoded characters
  global.window.location.hash = "#node=" + encodeURIComponent("ep:POST:/api/orders");
  viewerController.handleHashChange();
  assert.strictEqual(state.selectedNodeId, "ep:POST:/api/orders");
  assert.strictEqual(state.currentLevel, "file");

  // Test URL with malformed percent encoding: must not throw URIError
  global.window.location.hash = "#node=bad%percent";
  assert.doesNotThrow(() => {
    viewerController.handleHashChange();
  }, "handleHashChange must not throw URIError on malformed percent encoding");

  const viewer = global.window.__archViewer;
  assert(viewer, "Controller must expose window.__archViewer");

  // Same-hash navigation must still navigate (search result / inspector link re-selection)
  global.window.location.hash = "";
  assert(viewer.navigateToNode("sym:OrderService.create"), "navigateToNode should succeed for a known node");
  assert.strictEqual(state.currentScope, "file:src/orders/services/order_service.py");
  const nodeHash = global.window.location.hash;
  assert.strictEqual(nodeHash, "#node=" + encodeURIComponent("sym:OrderService.create"));

  state.goToBreadcrumb(0);
  viewerController.render();
  assert.strictEqual(state.currentLevel, "system");
  assert.strictEqual(global.window.location.hash, nodeHash, "Breadcrumb navigation leaves the #node= hash untouched");
  assert(viewer.navigateToNode("sym:OrderService.create"), "Re-selecting the same target must navigate");
  assert.strictEqual(
    state.currentScope,
    "file:src/orders/services/order_service.py",
    "Navigating to the current #node= target must still focus it"
  );

  // Deep links must rebuild an absolute breadcrumb trail instead of appending
  global.window.location.hash = "#drill=" + encodeURIComponent("file:src/orders/services/order_service.py");
  viewerController.handleHashChange();
  assert.strictEqual(state.currentLevel, "file");
  assert.strictEqual(state.currentScope, "file:src/orders/services/order_service.py");
  assert.deepStrictEqual(
    state.breadcrumbs.map((c) => c.id),
    ["system", "mod:orders", "dir:src/orders/services", "file:src/orders/services/order_service.py"],
    "Reloading a file deep link must rebuild the full ancestor trail"
  );

  // Trace bar must clear whenever trace mode ends
  const traceBar = elements.get("trace-bar");
  viewer.startTrace("ep:POST:/api/orders");
  assert(traceBar.classList.contains("active"), "Trace bar must be active during trace mode");
  assert.strictEqual(state.trace.entrypointId, "ep:POST:/api/orders");
  const breadcrumbsEl = elements.get("breadcrumbs");
  const systemCrumb = breadcrumbsEl.children.find(
    (c) => c.className && c.className.split(" ").includes("crumb")
  );
  assert(systemCrumb, "Breadcrumb container must render crumb elements");
  systemCrumb.dispatchEvent({ type: "click" });
  assert.strictEqual(state.trace, null, "Breadcrumb navigation must end trace mode");
  assert(!traceBar.classList.contains("active"), "Trace bar must hide once the trace ends");
  assert.strictEqual(state.currentLevel, "system");

  // Test XSS escaping
  const escaped = viewerController.escapeHtml('"><script>alert(1)</script>');
  assert(!escaped.includes("<script>"), "escapeHtml must escape HTML tags");
  assert(escaped.includes("&lt;script&gt;"), "escapeHtml must encode angle brackets");

  // Synthetic root module must be filterable and never silently hidden
  global.window.location.hash = "";
  const orphanGraph = {
    schema_version: "1.0",
    repo_name: "orphan-repo",
    modules: [{ id: "mod:auth", name: "auth", path: "src/auth", file_count: 1, tier: "scip" }],
    nodes: [
      { id: "sym:orphan", kind: "service", label: "orphan", file: "scripts/orphan.py", line: 3, tier: "scip" },
      { id: "sym:auth", kind: "service", label: "auth_fn", module_id: "mod:auth", file: "src/auth/a.py", line: 4, tier: "scip" },
    ],
    edges: [],
    data_stores: [],
    external_sinks: [],
  };
  dataScript.textContent = JSON.stringify(orphanGraph);
  viewerController.init();
  const orphanState = viewerController.getState();
  assert.strictEqual(
    orphanState.hierarchy.nodeLookup.get("sym:orphan").moduleId,
    "mod:root",
    "A node outside every declared module must live in the synthetic root module"
  );

  const moduleInputs = elements.get("filter-modules").querySelectorAll("input");
  const rootInput = moduleInputs.find((i) => i.value === "mod:root");
  const authInput = moduleInputs.find((i) => i.value === "mod:auth");
  assert(rootInput, "Module filter must list the synthetic root module");
  assert(authInput, "Module filter must list declared modules");

  authInput.checked = false;
  authInput.dispatchEvent({ type: "change" });
  assert(!orphanState.filters.modules.has("mod:auth"), "Unchecked module must leave the allow-set");
  assert(
    orphanState.filters.modules.has("mod:root"),
    "Unchecking another module must keep mod:root allowed"
  );
  const orphanView = orphanState.getCurrentView();
  assert(
    orphanView.nodes.some((n) => n.id === "mod:root"),
    "Root module must stay visible when another module is unchecked"
  );
  assert(!orphanView.nodes.some((n) => n.id === "mod:auth"), "Unchecked module must be hidden");
}

try {
  runTests();
} catch (err) {
  console.error("Test failed:", err);
  process.exit(1);
}
