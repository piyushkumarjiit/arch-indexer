/**
 * Standalone Node.js test suite for arch-indexer viewer logic.
 * Checks hierarchy derivation, drill-down, breadcrumb, #node= focus,
 * trace reachability, and deterministic layout on sample-graph.json.
 */

const fs = require("fs");
const path = require("path");
const assert = require("assert");

const {
  buildHierarchy,
  getHierarchyScope,
  traceFlow,
  computeLayeredLayout,
  getNodeDetails,
  ViewerState,
} = require("../src/arch_indexer/viewer/assets/viewer_logic.js");

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
  // Parent file level should be active
  assert.strictEqual(state.currentLevel, "file");
  assert.strictEqual(state.currentScope, "file:src/orders/routes.py");
  const lastCrumb = state.breadcrumbs[state.breadcrumbs.length - 1];
  assert.strictEqual(lastCrumb.id, "ep:POST:/api/orders");

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

  // 4. Test Trace Reachability
  console.log("Checking trace reachability...");
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

  // 5. Test Deterministic Layered Layout
  console.log("Checking deterministic layered layout...");
  const view1 = state.getCurrentView();
  const view2 = state.getCurrentView();
  assert.strictEqual(view1.nodes.length, view2.nodes.length);
  assert.strictEqual(view1.edges.length, view2.edges.length);
  for (let i = 0; i < view1.nodes.length; i++) {
    assert.strictEqual(view1.nodes[i].x, view2.nodes[i].x);
    assert.strictEqual(view1.nodes[i].y, view2.nodes[i].y);
    assert.strictEqual(view1.nodes[i].id, view2.nodes[i].id);
  }
  for (let i = 0; i < view1.edges.length; i++) {
    assert.strictEqual(view1.edges[i].d, view2.edges[i].d);
  }

  console.log("All viewer logic tests passed successfully!");
}

try {
  runTests();
} catch (err) {
  console.error("Test failed:", err);
  process.exit(1);
}
