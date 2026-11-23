/**
 * Standalone Node.js test suite for arch-indexer viewer logic and controller.
 * Checks hierarchy derivation, drill-down, breadcrumb, #node= focus,
 * trace reachability, deterministic layout with independent oracles,
 * scale performance, and browser controller interactions.
 */

const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { execSync } = require("child_process");

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

  // 5. Test Deterministic Layered Layout with INDEPENDENT ORACLE
  console.log("Checking deterministic layered layout with independent coordinate oracle...");
  // Oracle: SPEC 6.3 layer order:
  // Layer 0: Frontend (x = paddingX + 0 * (nodeWidth + colGap))
  // Layer 1: Entrypoints (x = paddingX + 1 * (nodeWidth + colGap))
  // Layer 2: Services / Modules / Directories / Files (x = paddingX + 2 * (nodeWidth + colGap))
  // Layer 3: Data Stores & External Sinks (x = paddingX + 3 * (nodeWidth + colGap))
  const paddingX = 60;
  const nodeWidth = 220;
  const colGap = 140;
  const expectedX = {
    layer0_frontend: paddingX + 0 * (nodeWidth + colGap), // 60
    layer1_entrypoint: paddingX + 1 * (nodeWidth + colGap), // 420
    layer2_service_or_module: paddingX + 2 * (nodeWidth + colGap), // 780
    layer3_datastore_or_sink: paddingX + 3 * (nodeWidth + colGap), // 1140
  };

  assert.strictEqual(expectedX.layer0_frontend, 60);
  assert.strictEqual(expectedX.layer1_entrypoint, 420);
  assert.strictEqual(expectedX.layer2_service_or_module, 780);
  assert.strictEqual(expectedX.layer3_datastore_or_sink, 1140);

  // Check system level layout against expected x positions
  state.goToBreadcrumb(0); // System
  const sysView = state.getCurrentView({ paddingX, nodeWidth, colGap });
  for (const n of sysView.nodes) {
    if (n.kind === "module") {
      assert.strictEqual(
        n.x,
        expectedX.layer2_service_or_module,
        `Module node ${n.id} must be at x=${expectedX.layer2_service_or_module}, got ${n.x}`
      );
    } else if (n.kind === "datastore" || n.kind === "external_sink") {
      assert.strictEqual(
        n.x,
        expectedX.layer3_datastore_or_sink,
        `Store/sink node ${n.id} must be at x=${expectedX.layer3_datastore_or_sink}, got ${n.x}`
      );
    }
  }

  // Check file level layout containing frontend, entrypoint, service, store
  state.focusNode("ep:POST:/api/orders");
  const fileView = state.getCurrentView({ paddingX, nodeWidth, colGap });
  for (const n of fileView.nodes) {
    if (n.kind === "frontend_caller") {
      assert.strictEqual(n.x, expectedX.layer0_frontend, `Frontend node ${n.id} must be at x=${expectedX.layer0_frontend}`);
    } else if (n.kind === "entrypoint") {
      assert.strictEqual(n.x, expectedX.layer1_entrypoint, `Entrypoint node ${n.id} must be at x=${expectedX.layer1_entrypoint}`);
    } else if (n.kind === "service") {
      assert.strictEqual(n.x, expectedX.layer2_service_or_module, `Service node ${n.id} must be at x=${expectedX.layer2_service_or_module}`);
    } else if (n.kind === "datastore" || n.kind === "external_sink") {
      assert.strictEqual(n.x, expectedX.layer3_datastore_or_sink, `Store node ${n.id} must be at x=${expectedX.layer3_datastore_or_sink}`);
    }
  }

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

  // 9. Test Scale Graph traceFlow Performance (Independent Oracle: Linear Bounded Execution Time)
  console.log("Checking scale graph traceFlow performance (< 500ms for ~5,000 nodes/15,000 edges)...");
  const pyCmd = 'python3 -c "import json; from tests.synthetic_generator import generate_synthetic_graph; print(json.dumps(generate_synthetic_graph(5000, 15000)))"';
  const scaleJson = execSync(pyCmd, { maxBuffer: 64 * 1024 * 1024, encoding: "utf-8" });
  const scaleGraph = JSON.parse(scaleJson);

  assert.strictEqual(scaleGraph.nodes.length, 5000);
  assert.strictEqual(scaleGraph.edges.length, 15000);

  const tStart = Date.now();
  const scaleTrace = traceFlow(scaleGraph, "ep:ep_0000");
  const elapsed = Date.now() - tStart;

  console.log(`scaleTrace completed in ${elapsed}ms`);
  assert(elapsed < 500, `traceFlow on 5000 nodes/15000 edges took ${elapsed}ms, must be < 500ms`);
  assert(scaleTrace.reachableNodes.length > 0, "Scale trace must find reachable nodes");
  assert(scaleTrace.steps.length > 0, "Scale trace must find steps");

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
    if (id) elements.set(id, el);
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
    createElementNS(ns, tag) { return createMockElement(tag); },
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

  // Test XSS escaping
  const escaped = viewerController.escapeHtml('"><script>alert(1)</script>');
  assert(!escaped.includes("<script>"), "escapeHtml must escape HTML tags");
  assert(escaped.includes("&lt;script&gt;"), "escapeHtml must encode angle brackets");
}

try {
  runTests();
} catch (err) {
  console.error("Test failed:", err);
  process.exit(1);
}
