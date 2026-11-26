/** graph.files[] makes file-only modules drill down to file leaves (graph.json 1.2). */
const assert = require("assert");
const {
  buildHierarchy,
  getHierarchyScope,
  getNodeDetails,
} = require("../src/arch_indexer/viewer/assets/viewer_logic.js");

function graph() {
  return {
    repo_name: "demo",
    modules: [
      { id: "mod:services", name: "services", path: "src/services", file_count: 1 },
      { id: "mod:models", name: "models", path: "src/models", file_count: 2 },
      { id: "mod:infra", name: "infra", path: "deploy", file_count: 1 },
    ],
    nodes: [
      {
        id: "sym:svc.run",
        kind: "service",
        label: "run",
        module_id: "mod:services",
        file: "src/services/run.ts",
        line: 1,
        tier: "tree-sitter",
      },
    ],
    edges: [],
    data_stores: [],
    external_sinks: [],
    files: [
      { path: "src/services/run.ts", module_id: "mod:services", symbols: [], is_infra: false },
      { path: "src/models/types.ts", module_id: "mod:models", symbols: [{ name: "T", kind: "interface", line: 1 }], is_infra: false },
      { path: "src/models/index.ts", module_id: "mod:models", symbols: [], is_infra: false },
      { path: "deploy/Dockerfile", module_id: "mod:infra", symbols: [], is_infra: true, language: "infra" },
    ],
  };
}

function runTests() {
  const hierarchy = buildHierarchy(graph());

  // A module made only of type files drills down to its directory and then its files.
  const moduleScope = getHierarchyScope(hierarchy, "module", "mod:models");
  assert.ok(moduleScope.nodes.length >= 1, "module scope lists a directory");
  const dir = moduleScope.nodes[0];
  assert.strictEqual(dir.kind, "file_group");
  const dirScope = getHierarchyScope(hierarchy, "file_group", dir.id);
  const fileLeaves = dirScope.nodes.filter((n) => n.kind === "file");
  assert.strictEqual(fileLeaves.length, 2);
  assert.deepStrictEqual(
    fileLeaves.map((n) => n.filePath).sort(),
    ["src/models/index.ts", "src/models/types.ts"]
  );
  const types = fileLeaves.find((n) => n.filePath === "src/models/types.ts");
  assert.deepStrictEqual(types.symbols, [{ name: "T", kind: "interface", line: 1 }]);

  // Infra files register too, flagged for the inspector.
  const infraDir = getHierarchyScope(hierarchy, "module", "mod:infra").nodes[0];
  const infraFile = getHierarchyScope(hierarchy, "file_group", infraDir.id).nodes[0];
  assert.strictEqual(infraFile.isInfra, true);

  // Ancestors and breadcrumbs resolve for registered files without throwing.
  const ancestors = hierarchy.ancestorMap.get("file:src/models/types.ts");
  assert.deepStrictEqual(
    ancestors.map((a) => a.level),
    ["system", "module", "file_group", "file"]
  );
  assert.doesNotThrow(() => getNodeDetails(hierarchy, "file:src/models/types.ts"));

  // A file that already has symbol nodes is not registered twice.
  const serviceDir = getHierarchyScope(hierarchy, "module", "mod:services").nodes;
  assert.strictEqual(serviceDir.length, 1);
  const serviceFiles = getHierarchyScope(hierarchy, "file_group", serviceDir[0].id).nodes;
  assert.strictEqual(serviceFiles.length, 1);
  assert.strictEqual(serviceFiles[0].symbolCount, 1);

  // Older graphs without files[] still build.
  const legacy = graph();
  delete legacy.files;
  assert.doesNotThrow(() => buildHierarchy(legacy));
  const legacyScope = getHierarchyScope(buildHierarchy(legacy), "module", "mod:models");
  assert.strictEqual(legacyScope.nodes.length, 0);

  console.log("test_viewer_files_drill: ok");
}

runTests();
