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
  FILTER_KINDS,
  FILTER_TIERS,
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
  // The fixture intentionally names an undeclared module so the synthetic root is exercised.
  assert.strictEqual(
    feCheckout.moduleId,
    "mod:root",
    "A node whose module_id is undeclared must fall back to the synthetic root module"
  );

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
  const allModuleIds = (hierarchy) => new Set(hierarchy.modules.map((mod) => mod.id));
  const fullFilters = (hierarchy) => ({
    modules: allModuleIds(hierarchy),
    kinds: new Set(FILTER_KINDS),
    tiers: new Set(FILTER_TIERS),
  });
  const resetFilters = () => {
    state.filters = fullFilters(state.hierarchy);
  };
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

  // SPEC 6.1 gives every node an explicit kind; ID prefixes and raw store kinds carry no meaning.
  const kindLayout = computeLayeredLayout([
    { id: "custom:entry", kind: "entrypoint" },
    { id: "ep:not_entry", kind: "service" },
    { id: "ds:raw_store_kind", kind: "sql" },
    { id: "custom:sink", kind: "external_sink" },
  ], []);
  const kindX = new Map(kindLayout.nodes.map((n) => [n.id, n.x]));
  assert.strictEqual(
    kindX.get("ep:not_entry"),
    kindX.get("ds:raw_store_kind"),
    "A service and an unknown raw store kind must share the services column"
  );
  assert(
    kindX.get("custom:entry") < kindX.get("ep:not_entry"),
    "The entrypoint kind must classify a node regardless of its ID prefix"
  );
  assert(
    kindX.get("custom:sink") > kindX.get("ep:not_entry"),
    "The external_sink kind must classify a node regardless of its ID prefix"
  );

  // Trace sink detection must also rely on node.kind, not the ds:/ext: ID prefix.
  const kindOnlyTraceGraph = {
    nodes: [
      { id: "ep:e", kind: "entrypoint", label: "E", file: "a.py", line: 1, tier: "scip" },
      { id: "ds:lookalike", kind: "service", label: "Lookalike", file: "a.py", line: 2, tier: "scip" },
      { id: "sym:real", kind: "service", label: "Real", file: "a.py", line: 3, tier: "scip" },
    ],
    edges: [
      { source: "ep:e", target: "ds:lookalike", kind: "calls", file: "a.py", line: 4, tier: "scip" },
      { source: "ds:lookalike", target: "sym:real", kind: "calls", file: "a.py", line: 5, tier: "scip" },
      { source: "sym:real", target: "store:no-prefix", kind: "writes", file: "a.py", line: 6, tier: "scip" },
    ],
    data_stores: [{ id: "store:no-prefix", kind: "sql", label: "Store", evidence: ["a.py:7"] }],
    external_sinks: [],
  };
  const kindOnlyTrace = traceFlow(kindOnlyTraceGraph, "ep:e");
  assert.deepStrictEqual(
    kindOnlyTrace.reachableSinks.map((sink) => sink.id),
    ["store:no-prefix"],
    "Only nodes whose kind is datastore or external_sink may be treated as trace sinks"
  );
  assert(
    kindOnlyTrace.reachableNodes.includes("ds:lookalike"),
    "A service node with a ds: ID prefix must stay an intermediate step, not a sink"
  );

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
  resetFilters();

  // 7. Test Module Filter Re-check
  console.log("Checking module filter re-check behavior...");
  state.goToBreadcrumb(0);
  // Exclude mod:orders
  state.filters.modules = new Set(["mod:auth", "mod:billing"]);
  let viewExcluded = state.getCurrentView();
  assert(!viewExcluded.nodes.some((n) => n.id === "mod:orders"), "mod:orders must be excluded");
  // Re-check mod:orders (all modules included)
  state.filters.modules = allModuleIds(state.hierarchy);
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
  resetFilters();

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
  resetFilters();

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
  resetFilters();

  // 8c. Explicit filter selections: all values selected = no restriction,
  // none selected = the group excludes everything
  console.log("Checking explicit filter selection semantics...");
  state.goToBreadcrumb(0);
  state.filters = fullFilters(state.hierarchy);
  const allSelectedView = state.getCurrentView();
  const allSelectedIds = new Set(allSelectedView.nodes.map((n) => n.id));
  for (const childId of state.hierarchy.systemNode.children) {
    assert(
      allSelectedIds.has(childId),
      `Selecting every filter value must show the declared entry ${childId}`
    );
  }
  assert(state.filters.modules.size > 0, "The module filter must list selectable modules");

  for (const group of ["modules", "kinds", "tiers"]) {
    state.filters = fullFilters(state.hierarchy);
    state.filters[group] = new Set();
    const emptyGroupView = state.getCurrentView();
    assert.strictEqual(
      emptyGroupView.nodes.length,
      0,
      `An empty ${group} selection must exclude every node`
    );
    assert.strictEqual(
      emptyGroupView.edges.length,
      0,
      `An empty ${group} selection must exclude every edge`
    );
  }

  state.filters = fullFilters(state.hierarchy);
  state.filters.tiers = new Set(["treesitter"]);
  const aliasTierView = state.getCurrentView();
  assert(
    !aliasTierView.nodes.some((n) => n.id === "mod:root"),
    "The non-canonical 'treesitter' value must not match tree-sitter nodes"
  );
  assert(
    aliasTierView.nodes.some((n) => n.id === "ds:postgres:orders"),
    "Nodes without a code tier are not excluded by a code-tier selection"
  );
  resetFilters();

  // 8d. Unfiltered views must show every declared module, including leafless ones
  console.log("Checking declared modules without flow nodes...");
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
  const unfilteredScope = getHierarchyScope(
    leaflessHierarchy,
    "system",
    "system",
    fullFilters(leaflessHierarchy)
  );
  const unfilteredIds = new Set(unfilteredScope.nodes.map((n) => n.id));
  for (const childId of leaflessHierarchy.systemNode.children) {
    assert(
      unfilteredIds.has(childId),
      `A fully selected system view must show the declared entry ${childId}`
    );
  }
  assert(
    unfilteredIds.has("mod:utils"),
    "A declared module with no flow nodes must stay visible when every group is fully selected"
  );

  const restrictedFilters = fullFilters(leaflessHierarchy);
  restrictedFilters.tiers = new Set(["scip"]);
  const restrictedScope = getHierarchyScope(
    leaflessHierarchy,
    "system",
    "system",
    restrictedFilters
  );
  assert(
    !restrictedScope.nodes.some((n) => n.id === "mod:utils"),
    "A module with no leaves must be hidden unless every filter group is fully selected"
  );
  resetFilters();

  // 8e. Aggregated edges must keep every distinct underlying kind
  console.log("Checking aggregated edge kinds and container inspector details...");
  state.goToBreadcrumb(0);
  const systemEdges = state.getCurrentView().edges;
  const mixedEdge = systemEdges.find(
    (edge) => edge.source === "mod:orders" && edge.target === "ds:postgres:orders"
  );
  assert(mixedEdge, "System view must aggregate mod:orders -> ds:postgres:orders");
  const fixtureDsKinds = Array.from(
    new Set(
      graph.edges
        .filter((edge) => edge.target === "ds:postgres:orders")
        .map((edge) => edge.kind)
    )
  ).sort();
  assert.deepStrictEqual(
    mixedEdge.kinds,
    fixtureDsKinds,
    "An aggregated edge must keep every distinct underlying kind"
  );
  assert.strictEqual(mixedEdge.count, 2, "Both data store accesses must be aggregated");

  const parallelTraceGraph = {
    nodes: [{ id: "ep:e", kind: "entrypoint", label: "E", file: "a.py", line: 1, tier: "scip" }],
    edges: [
      { source: "ep:e", target: "ds:d", kind: "writes", file: "a.py", line: 2, tier: "scip" },
      { source: "ep:e", target: "ds:d", kind: "reads", file: "a.py", line: 3, tier: "scip" },
    ],
    data_stores: [{ id: "ds:d", kind: "sql", label: "D", evidence: ["a.py:4"] }],
    external_sinks: [],
  };
  const parallelTraceState = new ViewerState(parallelTraceGraph);
  parallelTraceState.startTrace("ep:e");
  const parallelTraceEdges = parallelTraceState.getCurrentView().edges;
  assert.strictEqual(parallelTraceEdges.length, 1, "Parallel trace edges must aggregate into one");
  assert.deepStrictEqual(
    parallelTraceEdges[0].kinds,
    ["reads", "writes"],
    "Trace-mode aggregation must keep every distinct underlying kind"
  );

  const ordersDetails = getNodeDetails(state.hierarchy, "mod:orders");
  assert.deepStrictEqual(
    ordersDetails.uses.map((use) => use.id),
    ["ds:postgres:orders", "mod:auth", "mod:billing"],
    "A module inspector must aggregate outgoing edges at module level"
  );
  assert.deepStrictEqual(
    ordersDetails.usedBy.map((use) => use.id),
    ["mod:root"],
    "A module inspector must aggregate incoming edges from outside the module"
  );
  assert.strictEqual(
    ordersDetails.usedBy[0].edgeKind,
    "http_request",
    "A module inspector must report its underlying edge kinds"
  );
  assert.strictEqual(
    ordersDetails.tier,
    "scip",
    "A module inspector must derive its tier from descendant leaves"
  );
  assert.strictEqual(
    getNodeDetails(state.hierarchy, "mod:root").tier,
    "tree-sitter",
    "A synthetic-root inspector must derive its tier from descendant leaves"
  );
  const dirDetails = getNodeDetails(state.hierarchy, "dir:src/orders/services");
  assert.strictEqual(
    dirDetails.tier,
    "scip",
    "A directory inspector must derive its tier from descendant files"
  );
  assert(dirDetails.uses.length > 0, "A directory inspector must show outgoing edges");
  assert.strictEqual(ordersDetails.isLeaf, false, "A module detail must not be reported as a leaf");
  assert.strictEqual(
    getNodeDetails(state.hierarchy, "ep:POST:/api/orders").isLeaf,
    true,
    "An entrypoint detail must be reported as a leaf"
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
  const fixtureGraph = JSON.parse(graphJsonText);
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
  function findSvgEdges(node, found) {
    const results = found || [];
    if (node.attributes && node.attributes.class && node.attributes.class.includes("svg-edge")) {
      results.push(node);
    }
    for (const ch of node.children || []) findSvgEdges(ch, results);
    return results;
  }
  function findText(node, text) {
    if (node.textContent === text) return node;
    for (const ch of node.children) {
      const found = findText(ch, text);
      if (found) return found;
    }
    return null;
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

  // The removed #drill= route must be ignored; only #node= drives navigation
  state.goToBreadcrumb(0);
  global.window.location.hash = "";
  global.window.location.hash = "#drill=" + encodeURIComponent("file:src/orders/services/order_service.py");
  viewerController.handleHashChange();
  assert.strictEqual(state.currentLevel, "system", "The removed #drill= route must not navigate");
  assert.deepStrictEqual(
    state.breadcrumbs.map((c) => c.id),
    ["system"],
    "The removed #drill= route must not rebuild a breadcrumb trail"
  );

  // Aggregated edges keep every underlying kind and use kind-neutral labels
  viewerController.render();
  const inspectorContent = elements.get("inspector-content");
  const systemViewEdges = state.getCurrentView().edges;
  const mixedEdgeIndex = systemViewEdges.findIndex(
    (edge) => edge.source === "mod:orders" && edge.target === "ds:postgres:orders"
  );
  assert(mixedEdgeIndex >= 0, "System view must aggregate mod:orders -> ds:postgres:orders");
  const fixtureDsKinds = Array.from(
    new Set(
      fixtureGraph.edges
        .filter((edge) => edge.target === "ds:postgres:orders")
        .map((edge) => edge.kind)
    )
  ).sort();
  assert.deepStrictEqual(
    systemViewEdges[mixedEdgeIndex].kinds,
    fixtureDsKinds,
    "An aggregated edge must keep every distinct underlying kind"
  );
  assert(findText(viewport, "2 edges"), "Aggregated edge labels must be kind-neutral");
  assert(
    !findText(viewport, "2 calls"),
    "Aggregated edge labels must not call reads or writes 'calls'"
  );
  findSvgEdges(viewport)[mixedEdgeIndex].dispatchEvent({ type: "click" });
  assert(
    inspectorContent.innerHTML.includes(`>${fixtureDsKinds.join(", ")}<`),
    "Edge inspector must list every underlying kind of the clicked aggregate"
  );

  // A container inspector must show aggregated edges and a derived tier
  viewer.selectNode("mod:orders");
  assert(
    inspectorContent.innerHTML.includes("Uses (3)"),
    "Module inspector must aggregate outgoing edges"
  );
  assert(
    inspectorContent.innerHTML.includes('data-id="mod:auth"'),
    "Module inspector uses must include connected modules"
  );
  assert(
    inspectorContent.innerHTML.includes('data-id="mod:billing"'),
    "Module inspector uses must include connected modules"
  );
  assert(
    inspectorContent.innerHTML.includes('data-id="ds:postgres:orders"'),
    "Module inspector uses must include the connected data store"
  );
  assert(
    inspectorContent.innerHTML.includes('data-id="mod:root"'),
    "Module inspector usedBy must include the calling module"
  );
  assert(inspectorContent.innerHTML.includes(">scip<"), "Module inspector must show its derived tier");
  assert(
    !inspectorContent.innerHTML.includes('id="btn-inspector-trace"'),
    "A container inspector must not offer trace flow"
  );

  // Boundary and terminal leaves must not offer trace flow
  viewer.selectNode("ds:postgres:orders");
  assert(
    !inspectorContent.innerHTML.includes('id="btn-inspector-trace"'),
    "A data store inspector must not offer trace flow"
  );
  viewer.selectNode("sym:AuthService.login");
  assert(
    !inspectorContent.innerHTML.includes('id="btn-inspector-trace"'),
    "A leaf with no outgoing edges must not offer trace flow"
  );

  // Node inspector must show the fixture node's own symbol, file:line and tier
  const inspectorNodeId = "ep:POST:/api/orders";
  const inspectorNodeFixture = fixtureGraph.nodes.find((n) => n.id === inspectorNodeId);
  assert(inspectorNodeFixture, "Fixture must contain the inspected node");
  viewer.selectNode(inspectorNodeId);
  assert(
    inspectorContent.innerHTML.includes(`>${inspectorNodeFixture.symbol}<`),
    "Node inspector must show the fixture symbol"
  );
  assert(
    inspectorContent.innerHTML.includes(
      `>${inspectorNodeFixture.file}:${inspectorNodeFixture.line}<`
    ),
    "Node inspector must show the fixture file:line"
  );
  assert(
    inspectorContent.innerHTML.includes(`>${inspectorNodeFixture.tier}<`),
    "Node inspector must show the fixture indexing tier"
  );
  assert(
    inspectorContent.innerHTML.includes('id="btn-inspector-trace"'),
    "A leaf inspector must offer trace flow"
  );
  assert(inspectorContent.innerHTML.includes("Uses ("), "Node inspector must show outgoing edges");
  assert(inspectorContent.innerHTML.includes("Used By ("), "Node inspector must show incoming edges");

  // Edge inspector must cite the fixture call sites and tier of the clicked edge
  state.drillDown(inspectorNodeId);
  viewerController.render();
  const symbolViewEdges = state.getCurrentView().edges;
  assert(symbolViewEdges.length > 0, "Symbol view must render edges");
  const svgEdges = findSvgEdges(viewport);
  assert.strictEqual(
    svgEdges.length,
    symbolViewEdges.length,
    "Every symbol-view edge must be rendered as a path"
  );
  const firstViewEdge = symbolViewEdges[0];
  const fixtureEdges = fixtureGraph.edges.filter(
    (edge) => edge.source === firstViewEdge.source && edge.target === firstViewEdge.target
  );
  assert(fixtureEdges.length > 0, "Clicked edge must resolve to fixture edges");
  svgEdges[0].dispatchEvent({ type: "click" });
  const expectedCitations = Array.from(
    new Set(fixtureEdges.map((edge) => `${edge.file}:${edge.line}`))
  ).join(", ");
  const expectedTiers = Array.from(
    new Set(fixtureEdges.map((edge) => edge.tier).filter(Boolean))
  ).join(", ");
  assert(
    inspectorContent.innerHTML.includes(`>${expectedCitations}<`),
    "Edge inspector must cite the fixture call sites of the clicked edge"
  );
  assert(
    inspectorContent.innerHTML.includes(`>${expectedTiers}<`),
    "Edge inspector must show the fixture tier of the clicked edge"
  );

  // The tree-sitter frontend edge must report its own fixture tier, not a hard-coded one
  const frontendEdgeIndex = symbolViewEdges.findIndex(
    (edge) => edge.source === "fe:checkout_page" && edge.target === inspectorNodeId
  );
  assert(frontendEdgeIndex >= 0, "Symbol view must render the frontend-to-entrypoint edge");
  const frontendFixtureEdges = fixtureGraph.edges.filter(
    (edge) =>
      edge.source === symbolViewEdges[frontendEdgeIndex].source &&
      edge.target === symbolViewEdges[frontendEdgeIndex].target
  );
  const frontendExpectedTiers = Array.from(
    new Set(frontendFixtureEdges.map((edge) => edge.tier).filter(Boolean))
  ).join(", ");
  assert.strictEqual(frontendExpectedTiers, "tree-sitter", "Fixture frontend edge must be tree-sitter tier");
  svgEdges[frontendEdgeIndex].dispatchEvent({ type: "click" });
  assert(
    inspectorContent.innerHTML.includes(`>${frontendExpectedTiers}<`),
    "Edge inspector must show the clicked edge's own tier"
  );

  // Trace bar must clear whenever trace mode ends
  const traceBar = elements.get("trace-bar");
  viewer.startTrace("ep:POST:/api/orders");
  assert(traceBar.classList.contains("active"), "Trace bar must be active during trace mode");
  assert.strictEqual(state.trace.entrypointId, "ep:POST:/api/orders");
  const traceStepText = elements.get("trace-step-text");
  assert(
    traceStepText.textContent.includes("jwt_required"),
    "Trace walkthrough must show the entrypoint auth guards"
  );
  assert(
    traceStepText.textContent.includes("src/orders/routes.py:42"),
    "Trace walkthrough must show step file:line"
  );
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

  // Unchecking every value in a filter group must show an empty-state message
  const moduleFilterInputs = elements.get("filter-modules").querySelectorAll("input");
  moduleFilterInputs.forEach((input) => {
    input.checked = false;
  });
  moduleFilterInputs[0].dispatchEvent({ type: "change" });
  assert.strictEqual(
    state.getCurrentView().nodes.length,
    0,
    "Unchecking every module must hide every node"
  );
  assert(
    findText(viewport, "No nodes match the selected filters."),
    "The viewer must show an empty-state message when no nodes match the filters"
  );

  // An empty graph must not tell the user that filters are hiding nodes
  dataScript.textContent = JSON.stringify({
    schema_version: "1.0",
    repo_name: "empty-repo",
    modules: [],
    nodes: [],
    edges: [],
    data_stores: [],
    external_sinks: [],
  });
  viewerController.init();
  assert.strictEqual(
    viewerController.getState().getCurrentView().nodes.length,
    0,
    "An empty graph must produce an empty view"
  );
  assert(
    findText(viewport, "No nodes in this graph."),
    "An empty graph must show a neutral empty-state message"
  );
  assert(
    !findText(viewport, "No nodes match the selected filters."),
    "An empty graph must not blame the filters"
  );

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
