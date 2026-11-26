/**
 * arch-indexer flow-viewer logic module
 * Pure JavaScript logic for hierarchy derivation, state management,
 * deterministic layered layout, guided flow traces, and node focus.
 * Works in Node.js (CommonJS) and browser.
 */

(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.ArchViewerLogic = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  /**
   * Helper to safely extract directory path from a file path.
   */
  function getDirectory(filePath) {
    if (!filePath) return "";
    const idx = filePath.lastIndexOf("/");
    return idx >= 0 ? filePath.substring(0, idx) : "";
  }

  /**
   * Helper to extract filename basename.
   */
  function getBasename(filePath) {
    if (!filePath) return "";
    const idx = filePath.lastIndexOf("/");
    return idx >= 0 ? filePath.substring(idx + 1) : filePath;
  }

  const FILTER_KINDS = [
    "entrypoint",
    "service",
    "module",
    "datastore",
    "external_sink",
    "frontend_caller",
  ];

  const FILTER_TIERS = ["scip", "tree-sitter"];

  function isBoundaryKind(node) {
    const kind = (node && node.kind) || "";
    return kind === "datastore" || kind === "external_sink";
  }

  function isLeafNode(node) {
    return Boolean(node) && (node.level === "symbol" || isBoundaryKind(node));
  }

  function aggregatedEdgeKinds(edge) {
    return Array.from(
      new Set((edge.underlying || []).map((underlying) => underlying.kind).filter(Boolean))
    ).sort();
  }

  function collectDescendantLeafIds(hierarchy, node, result) {
    const ids = result || [];
    if (!node) return ids;
    if (isLeafNode(node)) {
      ids.push(node.id);
      return ids;
    }
    (node.children || []).forEach((childId) =>
      collectDescendantLeafIds(hierarchy, hierarchy.nodeLookup.get(childId), ids)
    );
    return ids;
  }

  /**
   * Build complete 5-level hierarchy tree and lookup tables from graph data.
   * Levels: system -> module -> file_group (directory) -> file -> symbol/endpoint
   * Plus data_stores and external_sinks.
   */
  function buildHierarchy(graph) {
    const modules = graph.modules || [];
    const nodes = graph.nodes || [];
    const edges = graph.edges || [];
    const dataStores = graph.data_stores || [];
    const externalSinks = graph.external_sinks || [];

    // Master node lookup: every node at any level has an entry
    const nodeLookup = new Map();
    // Maps each node ID to its ancestor chain: [ { level, id, label }, ... ]
    const ancestorMap = new Map();

    // Map module paths to module IDs for auto-assignment if node.module_id is missing
    const moduleByPath = new Map();
    modules.forEach((mod) => {
      if (mod.path) {
        moduleByPath.set(mod.path.replace(/\/+$/, ""), mod.id);
      }
    });

    // 1. System level container
    const systemNode = {
      id: "system",
      level: "system",
      label: graph.repo_name || "System",
      kind: "system",
      children: [],
    };
    nodeLookup.set("system", systemNode);
    ancestorMap.set("system", [{ level: "system", id: "system", label: systemNode.label }]);

    // 2. Add data stores to system level and lookup
    dataStores.forEach((ds) => {
      const node = {
        id: ds.id,
        level: "system",
        kind: "datastore",
        label: ds.label || ds.id,
        storeKind: ds.kind,
        evidence: ds.evidence || [],
        tier: ds.tier || null,
        file: (ds.evidence && ds.evidence[0]) ? ds.evidence[0].split(":")[0] : "",
        line: (ds.evidence && ds.evidence[0]) ? parseInt(ds.evidence[0].split(":")[1], 10) : 0,
      };
      nodeLookup.set(ds.id, node);
      ancestorMap.set(ds.id, [
        { level: "system", id: "system", label: systemNode.label },
        { level: "system", id: ds.id, label: node.label },
      ]);
      systemNode.children.push(ds.id);
    });

    // 3. Add external sinks to system level and lookup
    externalSinks.forEach((ext) => {
      const node = {
        id: ext.id,
        level: "system",
        kind: "external_sink",
        label: ext.label || ext.id,
        sinkKind: ext.kind,
        evidence: ext.evidence || [],
        tier: ext.tier || null,
        file: (ext.evidence && ext.evidence[0]) ? ext.evidence[0].split(":")[0] : "",
        line: (ext.evidence && ext.evidence[0]) ? parseInt(ext.evidence[0].split(":")[1], 10) : 0,
      };
      nodeLookup.set(ext.id, node);
      ancestorMap.set(ext.id, [
        { level: "system", id: "system", label: systemNode.label },
        { level: "system", id: ext.id, label: node.label },
      ]);
      systemNode.children.push(ext.id);
    });

    // 4. Map modules
    const moduleMap = new Map();
    modules.forEach((mod) => {
      const modNode = {
        id: mod.id,
        level: "module",
        kind: "module",
        label: mod.name || mod.id,
        language: mod.language,
        path: mod.path,
        tier: mod.tier || "scip",
        fileCount: mod.file_count || 0,
        symbolCount: 0,
        children: [], // will contain directory IDs
        files: new Set(),
      };
      moduleMap.set(mod.id, modNode);
      nodeLookup.set(mod.id, modNode);
      ancestorMap.set(mod.id, [
        { level: "system", id: "system", label: systemNode.label },
        { level: "module", id: mod.id, label: modNode.label },
      ]);
      systemNode.children.push(mod.id);
    });

    // Helper to find module ID for a file
    function resolveModuleId(node) {
      if (node.module_id && moduleMap.has(node.module_id)) {
        return node.module_id;
      }
      if (node.file) {
        // Match longest prefix against module paths
        let bestModId = null;
        let longestPath = -1;
        for (const [modPath, modId] of moduleByPath.entries()) {
          if (
            (node.file === modPath || node.file.startsWith(`${modPath}/`)) &&
            modPath.length > longestPath
          ) {
            longestPath = modPath.length;
            bestModId = modId;
          }
        }
        if (bestModId) return bestModId;
      }
      // If no module found, check if synthetic root module exists or create it
      if (!moduleMap.has("mod:root")) {
        const rootMod = {
          id: "mod:root",
          level: "module",
          kind: "module",
          label: "root",
          path: "",
          fileCount: 0,
          symbolCount: 0,
          children: [],
          files: new Set(),
        };
        moduleMap.set("mod:root", rootMod);
        nodeLookup.set("mod:root", rootMod);
        ancestorMap.set("mod:root", [
          { level: "system", id: "system", label: systemNode.label },
          { level: "module", id: "mod:root", label: "root" },
        ]);
        systemNode.children.push("mod:root");
      }
      return "mod:root";
    }

    // 5. Build directories (file groups) and files
    const dirMap = new Map();
    const fileMap = new Map();

    nodes.forEach((node) => {
      // Data stores or external sinks embedded in nodes array
      if (isBoundaryKind(node)) {
        if (!nodeLookup.has(node.id)) {
          nodeLookup.set(node.id, {
            ...node,
            level: "system",
          });
          ancestorMap.set(node.id, [
            { level: "system", id: "system", label: systemNode.label },
            { level: "system", id: node.id, label: node.label || node.id },
          ]);
          systemNode.children.push(node.id);
        }
        return;
      }

      const modId = resolveModuleId(node);
      const modNode = moduleMap.get(modId);
      const filePath = node.file || `unknown/${node.id}`;
      modNode.files.add(filePath);

      const dirPath = getDirectory(filePath) || modNode.path || ".";
      // Scope the directory id by module: several modules can share one directory (for
      // example every src/services/*.ts file), and a graph-wide `dir:<path>` id would let
      // the first module claim the directory, leaving the others with no drill-down.
      const dirId = `dir:${modId}:${dirPath}`;
      const fileId = `file:${filePath}`;

      // Create Directory (file group) if not exists
      if (!dirMap.has(dirId)) {
        const dirLabel = getBasename(dirPath) || dirPath;
        const dirNode = {
          id: dirId,
          level: "file_group",
          kind: "file_group",
          label: dirLabel + "/",
          fullPath: dirPath,
          moduleId: modId,
          fileCount: 0,
          symbolCount: 0,
          children: [], // will contain file IDs
          files: new Set(),
        };
        dirMap.set(dirId, dirNode);
        nodeLookup.set(dirId, dirNode);
        modNode.children.push(dirId);
        ancestorMap.set(dirId, [
          { level: "system", id: "system", label: systemNode.label },
          { level: "module", id: modId, label: modNode.label },
          { level: "file_group", id: dirId, label: dirNode.label },
        ]);
      }
      const dirNode = dirMap.get(dirId);
      dirNode.files.add(filePath);

      // Create File node if not exists
      if (!fileMap.has(fileId)) {
        const fileLabel = getBasename(filePath);
        const fileNodeObj = {
          id: fileId,
          level: "file",
          kind: "file",
          label: fileLabel,
          filePath: filePath,
          dirId: dirId,
          moduleId: modId,
          symbolCount: 0,
          children: [], // will contain leaf symbol IDs
        };
        fileMap.set(fileId, fileNodeObj);
        nodeLookup.set(fileId, fileNodeObj);
        dirNode.children.push(fileId);
        ancestorMap.set(fileId, [
          { level: "system", id: "system", label: systemNode.label },
          { level: "module", id: modId, label: modNode.label },
          { level: "file_group", id: dirId, label: dirNode.label },
          { level: "file", id: fileId, label: fileNodeObj.label },
        ]);
      }
      const fileNodeObj = fileMap.get(fileId);

      // 6. Leaf symbol/endpoint node
      const leafNode = {
        ...node,
        level: "symbol",
        fileId: fileId,
        dirId: dirId,
        moduleId: modId,
      };
      nodeLookup.set(node.id, leafNode);
      fileNodeObj.children.push(node.id);
      ancestorMap.set(node.id, [
        { level: "system", id: "system", label: systemNode.label },
        { level: "module", id: modId, label: modNode.label },
        { level: "file_group", id: dirId, label: dirNode.label },
        { level: "file", id: fileId, label: fileNodeObj.label },
        { level: "symbol", id: node.id, label: node.label || node.id },
      ]);

      // Increment counts
      modNode.symbolCount++;
      dirNode.symbolCount++;
      fileNodeObj.symbolCount++;
    });

    // Update file counts on modules and directories
    moduleMap.forEach((modNode) => {
      modNode.fileCount = modNode.files.size;
    });
    dirMap.forEach((dirNode) => {
      dirNode.fileCount = dirNode.files.size;
    });

    // Precompute incoming and outgoing edge lists for each leaf node
    const outgoing = new Map();
    const incoming = new Map();

    edges.forEach((edge) => {
      if (!outgoing.has(edge.source)) outgoing.set(edge.source, []);
      if (!incoming.has(edge.target)) incoming.set(edge.target, []);
      outgoing.get(edge.source).push(edge);
      incoming.get(edge.target).push(edge);
    });

    return {
      systemNode,
      modules: Array.from(moduleMap.values()),
      dataStores,
      externalSinks,
      nodeLookup,
      ancestorMap,
      outgoingEdges: outgoing,
      incomingEdges: incoming,
      rawGraph: graph,
    };
  }

  /**
   * Resolve which node represents a given leaf node ID at a specific hierarchy level.
   */
  function mapNodeToLevel(hierarchy, nodeId, targetLevel, targetScope) {
    const ancestors = hierarchy.ancestorMap.get(nodeId);
    if (!ancestors) return nodeId;

    for (let i = ancestors.length - 1; i >= 0; i--) {
      const crumb = ancestors[i];
      if (crumb.level === targetLevel) {
        return crumb.id;
      }
    }
    // If targetLevel is not in ancestors (e.g. datastore/sink at module level)
    return nodeId;
  }

  /**
   * Filter semantics, defined once here for every group (modules, kinds, tiers):
   * each group holds the explicit set of selected values. All values selected means no
   * restriction; an empty set excludes every leaf in that group (the UI shows an empty
   * state). A container is visible when at least one leaf beneath it passes all groups;
   * an aggregate edge is visible when at least one underlying raw edge connects leaves
   * that pass all groups. A container with no leaves is visible only when every group is
   * fully selected.
   */

  /**
   * Check whether a leaf node (symbol, data store, external sink) passes the filters.
   */
  function leafPassesFilters(node, activeFilters) {
    if (!node) return false;
    if (activeFilters.modules) {
      if (activeFilters.modules.size === 0) return false;
      if (node.moduleId && !activeFilters.modules.has(node.moduleId)) return false;
    }
    if (activeFilters.kinds) {
      if (activeFilters.kinds.size === 0 || !activeFilters.kinds.has(node.kind)) return false;
    }
    if (activeFilters.tiers) {
      if (activeFilters.tiers.size === 0) return false;
      if (node.tier && !activeFilters.tiers.has(node.tier)) return false;
    }
    return true;
  }

  /**
   * Check whether every filter group holds all of its selectable values.
   */
  function areAllFilterGroupsSelected(hierarchy, activeFilters) {
    if (activeFilters.modules) {
      const selected = activeFilters.modules;
      if (!hierarchy.modules.every((mod) => selected.has(mod.id))) return false;
    }
    if (activeFilters.kinds && !FILTER_KINDS.every((kind) => activeFilters.kinds.has(kind))) {
      return false;
    }
    if (activeFilters.tiers && !FILTER_TIERS.every((tier) => activeFilters.tiers.has(tier))) {
      return false;
    }
    return true;
  }

  /**
   * Get visible nodes and aggregated edges for a given zoom level and scope.
   */
  function getHierarchyScope(hierarchy, level, scopeId, filters) {
    const activeFilters = filters || { modules: null, kinds: null, tiers: null };
    const nodeLookup = hierarchy.nodeLookup;
    let nodeIds = [];
    let boundaryNodeIds = new Set();

    const passesLeaf = (node) => leafPassesFilters(node, activeFilters);
    const allGroupsSelected = areAllFilterGroupsSelected(hierarchy, activeFilters);

    const leafPresenceCache = new Map();
    function hasLeaves(node) {
      if (!node) return false;
      if (leafPresenceCache.has(node.id)) return leafPresenceCache.get(node.id);
      const result = isLeafNode(node)
        ? true
        : (node.children || []).some((childId) => hasLeaves(nodeLookup.get(childId)));
      leafPresenceCache.set(node.id, result);
      return result;
    }

    const passingLeafCache = new Map();
    function hasPassingLeaf(node) {
      if (!node) return false;
      if (passingLeafCache.has(node.id)) return passingLeafCache.get(node.id);
      let result;
      if (isLeafNode(node)) {
        result = passesLeaf(node);
      } else if (!hasLeaves(node)) {
        result = allGroupsSelected;
      } else {
        result = (node.children || []).some((childId) =>
          hasPassingLeaf(nodeLookup.get(childId))
        );
      }
      passingLeafCache.set(node.id, result);
      return result;
    }

    if (level === "system") {
      // Root level: modules + data stores + external sinks + frontend callers
      hierarchy.systemNode.children.forEach((id) => nodeIds.push(id));
    } else if (level === "module") {
      // Drilled into a module: show directory (file_group) children
      const modNode = nodeLookup.get(scopeId);
      if (modNode && modNode.children) {
        nodeIds.push(...modNode.children);
      }
      // Include connected data stores and external sinks as boundary context
      collectConnectedSinksAndStores(hierarchy, scopeId, boundaryNodeIds, passesLeaf);
    } else if (level === "file_group") {
      // Drilled into a directory: show file children
      const dirNode = nodeLookup.get(scopeId);
      if (dirNode && dirNode.children) {
        nodeIds.push(...dirNode.children);
      }
      collectConnectedSinksAndStores(hierarchy, scopeId, boundaryNodeIds, passesLeaf);
    } else if (level === "file") {
      // Drilled into a file: show symbol/endpoint children
      const fileNode = nodeLookup.get(scopeId);
      if (fileNode && fileNode.children) {
        nodeIds.push(...fileNode.children);
      }
      collectConnectedSinksAndStores(hierarchy, scopeId, boundaryNodeIds, passesLeaf);
    } else if (level === "symbol") {
      // Focus on symbol and its 1-hop upstream and downstream neighbors
      nodeIds.push(scopeId);
      const outEdges = hierarchy.outgoingEdges.get(scopeId) || [];
      const inEdges = hierarchy.incomingEdges.get(scopeId) || [];
      outEdges.forEach((e) => nodeIds.push(e.target));
      inEdges.forEach((e) => nodeIds.push(e.source));
      nodeIds = Array.from(new Set(nodeIds));
    }

    // Merge boundary nodes (data stores / external sinks) into nodeIds
    boundaryNodeIds.forEach((id) => {
      if (!nodeIds.includes(id)) {
        nodeIds.push(id);
      }
    });

    let visibleNodes = nodeIds
      .map((id) => nodeLookup.get(id))
      .filter((n) => hasPassingLeaf(n));

    // Create a Set of visible IDs for fast lookup
    const visibleIdSet = new Set(visibleNodes.map((n) => n.id));

    // Aggregate edges between visible nodes
    const rawEdges = hierarchy.rawGraph.edges || [];
    const edgeMap = new Map();

    rawEdges.forEach((edge) => {
      if (
        !hasPassingLeaf(nodeLookup.get(edge.source)) ||
        !hasPassingLeaf(nodeLookup.get(edge.target))
      ) {
        return;
      }
      const srcMapped = mapToVisible(hierarchy, edge.source, visibleIdSet);
      const tgtMapped = mapToVisible(hierarchy, edge.target, visibleIdSet);
      if (srcMapped && tgtMapped && srcMapped !== tgtMapped) {
        const edgeKey = `${srcMapped}->${tgtMapped}`;
        if (!edgeMap.has(edgeKey)) {
          edgeMap.set(edgeKey, {
            source: srcMapped,
            target: tgtMapped,
            count: 0,
            underlying: [],
          });
        }
        const aggEdge = edgeMap.get(edgeKey);
        aggEdge.count++;
        aggEdge.underlying.push(edge);
      }
    });

    // Deterministic sort of nodes and edges
    visibleNodes.sort((a, b) => a.id.localeCompare(b.id));
    const visibleEdges = Array.from(edgeMap.values());
    visibleEdges.forEach((edge) => {
      edge.kinds = aggregatedEdgeKinds(edge);
    });
    visibleEdges.sort((a, b) => {
      const keyA = `${a.source}->${a.target}`;
      const keyB = `${b.source}->${b.target}`;
      return keyA.localeCompare(keyB);
    });

    return {
      level,
      scopeId,
      nodes: visibleNodes,
      edges: visibleEdges,
    };
  }

  /**
   * Helper to find visible ancestor of a leaf node.
   */
  function mapToVisible(hierarchy, nodeId, visibleIdSet) {
    if (visibleIdSet.has(nodeId)) return nodeId;
    const ancestors = hierarchy.ancestorMap.get(nodeId);
    if (!ancestors) return null;
    for (let i = ancestors.length - 1; i >= 0; i--) {
      if (visibleIdSet.has(ancestors[i].id)) {
        return ancestors[i].id;
      }
    }
    return null;
  }

  /**
   * Helper to collect data stores and sinks connected to descendants of a scope node.
   */
  function collectConnectedSinksAndStores(hierarchy, scopeId, boundarySet, leafPasses) {
    const scopeNode = hierarchy.nodeLookup.get(scopeId);
    if (!scopeNode) return;

    // Collect all descendant leaf IDs
    const descendantIds = [];
    function collectLeafs(n) {
      if (!n) return;
      if (n.level === "symbol") {
        descendantIds.push(n.id);
      } else if (n.children) {
        n.children.forEach((cid) => collectLeafs(hierarchy.nodeLookup.get(cid)));
      }
    }
    collectLeafs(scopeNode);

    const addBoundary = (id) => {
      const boundary = hierarchy.nodeLookup.get(id);
      if (boundary && isBoundaryKind(boundary) && leafPasses(boundary)) {
        boundarySet.add(id);
      }
    };

    // For each passing leaf, check outgoing/incoming edges to datastores or sinks
    descendantIds.forEach((lid) => {
      const leaf = hierarchy.nodeLookup.get(lid);
      if (!leafPasses(leaf)) return;
      const outs = hierarchy.outgoingEdges.get(lid) || [];
      outs.forEach((e) => addBoundary(e.target));
      const ins = hierarchy.incomingEdges.get(lid) || [];
      ins.forEach((e) => addBoundary(e.source));
    });
  }

  /**
   * Trace guided flow from an Entrypoint to all reachable Data Stores and External Sinks.
   * Returns step-by-step path, auth metadata, and file:line citations.
   */
  function traceFlow(graph, entrypointId) {
    const nodes = graph.nodes || [];
    const edges = graph.edges || [];
    const dataStores = graph.data_stores || [];
    const externalSinks = graph.external_sinks || [];

    const nodeMap = new Map();
    nodes.forEach((n) => nodeMap.set(n.id, n));
    dataStores.forEach((d) => nodeMap.set(d.id, { ...d, kind: "datastore" }));
    externalSinks.forEach((e) => nodeMap.set(e.id, { ...e, kind: "external_sink" }));

    const entryNode = nodeMap.get(entrypointId);
    if (!entryNode) {
      return {
        entrypointId,
        entrypoint: null,
        reachableSinks: [],
        reachableNodes: [],
        reachableEdges: [],
        paths: [],
        steps: [],
        endReason: null,
        endsAt: null,
      };
    }

    // Build forward and backward adjacency maps
    const forwardAdj = new Map();
    const backwardAdj = new Map();

    edges.forEach((edge) => {
      if (!forwardAdj.has(edge.source)) forwardAdj.set(edge.source, []);
      forwardAdj.get(edge.source).push(edge);

      if (!backwardAdj.has(edge.target)) backwardAdj.set(edge.target, []);
      backwardAdj.get(edge.target).push(edge);
    });

    const isTarget = (id) => {
      const n = nodeMap.get(id);
      return Boolean(n) && isBoundaryKind(n);
    };

    // 1. Forward BFS from entrypointId tracking shortest path tree via parent pointers
    const visitedForward = new Set([entrypointId]);
    const parentMap = new Map(); // childId -> { from: parentId, edge }
    const queue = [entrypointId];
    const depthMap = new Map([[entrypointId, 0]]);
    const reachedSinks = [];
    const reachedSinkIds = new Set();

    while (queue.length > 0) {
      const current = queue.shift();

      if (isTarget(current) && current !== entrypointId) {
        if (!reachedSinkIds.has(current)) {
          reachedSinkIds.add(current);
          reachedSinks.push(nodeMap.get(current));
        }
        // Targets are sinks, don't expand further downstream from them
        continue;
      }

      const nextEdges = forwardAdj.get(current) || [];
      for (const e of nextEdges) {
        if (!visitedForward.has(e.target)) {
          visitedForward.add(e.target);
          parentMap.set(e.target, { from: current, edge: e });
          depthMap.set(e.target, depthMap.get(current) + 1);
          queue.push(e.target);
        }
      }
    }

    // 2. Backward BFS from reached sinks within the forward-reachable subgraph
    let reachableNodesSet;
    let reachableEdgesSet;

    if (reachedSinks.length > 0) {
      const visitedBackward = new Set(reachedSinkIds);
      const queueBack = Array.from(reachedSinkIds);

      while (queueBack.length > 0) {
        const current = queueBack.shift();
        const incomingEdges = backwardAdj.get(current) || [];
        for (const e of incomingEdges) {
          const u = e.source;
          if (visitedForward.has(u) && !visitedBackward.has(u)) {
            visitedBackward.add(u);
            queueBack.push(u);
          }
        }
      }

      // Nodes on any path from entrypoint to a sink: intersection of visitedForward & visitedBackward
      reachableNodesSet = new Set();
      visitedForward.forEach((id) => {
        if (visitedBackward.has(id)) {
          reachableNodesSet.add(id);
        }
      });
      // Always ensure entrypoint is in reachableNodes
      reachableNodesSet.add(entrypointId);

      // Edges connecting nodes in reachableNodesSet
      reachableEdgesSet = new Set();
      edges.forEach((e) => {
        if (reachableNodesSet.has(e.source) && reachableNodesSet.has(e.target)) {
          reachableEdgesSet.add(`${e.source}->${e.target}`);
        }
      });
    } else {
      // If no sinks reached, include the entire forward reachable closure
      reachableNodesSet = visitedForward;
      reachableEdgesSet = new Set();
      edges.forEach((e) => {
        if (visitedForward.has(e.source) && visitedForward.has(e.target)) {
          reachableEdgesSet.add(`${e.source}->${e.target}`);
        }
      });
    }

    // 3. Reconstruct shortest path(s) using parent pointers for the step walkthrough
    const paths = [];
    if (reachedSinks.length > 0) {
      // Build a path for each reached sink (deterministic sort by sink ID)
      reachedSinks.sort((a, b) => a.id.localeCompare(b.id));
      reachedSinks.forEach((sink) => {
        const path = [];
        let curr = sink.id;
        while (curr) {
          path.push(curr);
          if (curr === entrypointId) break;
          const p = parentMap.get(curr);
          curr = p ? p.from : null;
        }
        if (path[path.length - 1] === entrypointId) {
          path.reverse();
          paths.push(path);
        }
      });
    }

    const MAX_PARTIAL_HOPS = 12;
    let primaryPath;
    let endReason;
    if (paths.length > 0) {
      primaryPath = paths[0];
      endReason = "boundary";
    } else if (visitedForward.size === 1) {
      primaryPath = [entrypointId];
      endReason = "no-outgoing-edges";
    } else {
      // No boundary reached: show the longest path (deepest BFS node, ties to smaller id).
      let deepest = entrypointId;
      depthMap.forEach((depth, id) => {
        const best = depthMap.get(deepest);
        if (depth > best || (depth === best && id < deepest)) deepest = id;
      });
      primaryPath = [];
      let curr = deepest;
      while (curr) {
        primaryPath.push(curr);
        if (curr === entrypointId) break;
        const p = parentMap.get(curr);
        curr = p ? p.from : null;
      }
      primaryPath.reverse();
      if (depthMap.get(deepest) > MAX_PARTIAL_HOPS) {
        primaryPath = primaryPath.slice(0, MAX_PARTIAL_HOPS + 1);
        endReason = "depth-cap";
      } else {
        endReason = "no-boundary-detected";
      }
    }
    const endsAt = primaryPath[primaryPath.length - 1];

    // 4. Build step walkthrough
    const steps = [];
    primaryPath.forEach((nodeId, idx) => {
      const n = nodeMap.get(nodeId) || { id: nodeId, label: nodeId, kind: "unknown" };
      let fileLine = "";
      if (n.file && n.line) {
        fileLine = `${n.file}:${n.line}`;
      } else if (n.evidence && n.evidence.length > 0) {
        fileLine = n.evidence[0];
      }

      const authGuards = (n.metadata && n.metadata.auth) || [];

      steps.push({
        stepIndex: idx + 1,
        nodeId: n.id,
        label: n.label || n.id,
        kind: n.kind,
        fileLine,
        auth: authGuards,
        tier: n.tier || "scip",
        description:
          n.kind === "entrypoint"
            ? `Entrypoint: ${n.label}${authGuards.length ? ` (Auth: ${authGuards.join(", ")})` : ""}`
            : n.kind === "datastore"
            ? `Data Store: ${n.label}`
            : n.kind === "external_sink"
            ? `External Sink: ${n.label}`
            : `Service: ${n.label}`,
      });
    });

    return {
      entrypointId,
      entrypoint: entryNode,
      reachableSinks: reachedSinks,
      reachableNodes: Array.from(reachableNodesSet).sort(),
      reachableEdges: Array.from(reachableEdgesSet).sort(),
      paths,
      steps,
      endReason,
      endsAt,
    };
  }

  /**
   * Deterministic layered layout algorithm by kind:
   * Layer 0: Frontend (frontend_caller)
   * Layer 1: Entrypoints (entrypoint)
   * Layer 2: Services / Modules / Files / File Groups
   * Layer 3: Data Stores & External Sinks (datastore, external_sink)
   */
  function computeLayeredLayout(nodes, edges, options) {
    const opts = options || {};
    const colGap = opts.colGap || 140;
    const rowGap = opts.rowGap || 40;
    const nodeWidth = opts.nodeWidth || 220;
    const nodeHeight = opts.nodeHeight || 70;
    const paddingX = opts.paddingX || 60;
    const paddingY = opts.paddingY || 60;

    // 1. Group nodes into layers
    const layers = [[], [], [], []];

    function getLayerIndex(node) {
      const kind = (node.kind || "").toLowerCase();
      if (kind === "frontend_caller") return 0;
      if (kind === "entrypoint") return 1;
      if (isBoundaryKind(node)) return 3;
      return 2; // default: services, modules, directories, files
    }

    nodes.forEach((node) => {
      const lIdx = getLayerIndex(node);
      layers[lIdx].push(node);
    });

    // 2. Deterministic sorting inside each layer by ID
    for (let l = 0; l < 4; l++) {
      layers[l].sort((a, b) => a.id.localeCompare(b.id));
    }

    // 3. Compute (x, y) coordinates
    const layoutNodes = [];
    const nodeCoordMap = new Map();

    // Find max layer height to center smaller columns vertically
    let maxHeight = 0;
    layers.forEach((layer) => {
      const h = layer.length * nodeHeight + Math.max(0, layer.length - 1) * rowGap;
      if (h > maxHeight) maxHeight = h;
    });

    layers.forEach((layer, layerIdx) => {
      const colX = paddingX + layerIdx * (nodeWidth + colGap);
      const colHeight = layer.length * nodeHeight + Math.max(0, layer.length - 1) * rowGap;
      const startY = paddingY + Math.max(0, (maxHeight - colHeight) / 2);

      layer.forEach((node, rowIdx) => {
        const x = colX;
        const y = startY + rowIdx * (nodeHeight + rowGap);
        const layoutNode = {
          ...node,
          x,
          y,
          width: nodeWidth,
          height: nodeHeight,
          layer: layerIdx,
        };
        layoutNodes.push(layoutNode);
        nodeCoordMap.set(node.id, layoutNode);
      });
    });

    // 4. Compute edge routes (smooth cubic bezier curves)
    const layoutEdges = [];

    edges.forEach((edge) => {
      const srcNode = nodeCoordMap.get(edge.source);
      const tgtNode = nodeCoordMap.get(edge.target);

      if (srcNode && tgtNode) {
        let x1, y1, x2, y2, pathD;

        if (tgtNode.x > srcNode.x) {
          // Standard left-to-right edge
          x1 = srcNode.x + srcNode.width;
          y1 = srcNode.y + srcNode.height / 2;
          x2 = tgtNode.x;
          y2 = tgtNode.y + tgtNode.height / 2;
          const dx = (x2 - x1) * 0.5;
          pathD = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
        } else if (tgtNode.x === srcNode.x) {
          // Same column edge: route outward to the right
          x1 = srcNode.x + srcNode.width;
          y1 = srcNode.y + srcNode.height / 2;
          x2 = tgtNode.x + tgtNode.width;
          y2 = tgtNode.y + tgtNode.height / 2;
          const cx = Math.max(x1, x2) + 40;
          pathD = `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`;
        } else {
          // Backward edge: route over or under
          x1 = srcNode.x;
          y1 = srcNode.y + srcNode.height / 2;
          x2 = tgtNode.x + tgtNode.width;
          y2 = tgtNode.y + tgtNode.height / 2;
          const cy = Math.min(y1, y2) - 40;
          pathD = `M ${x1} ${y1} C ${x1 - 30} ${cy}, ${x2 + 30} ${cy}, ${x2} ${y2}`;
        }

        layoutEdges.push({
          ...edge,
          x1,
          y1,
          x2,
          y2,
          d: pathD,
        });
      }
    });

    // Compute bounding box
    const totalWidth = paddingX * 2 + 4 * nodeWidth + 3 * colGap;
    const totalHeight = paddingY * 2 + maxHeight;

    return {
      nodes: layoutNodes,
      edges: layoutEdges,
      width: totalWidth,
      height: Math.max(400, totalHeight),
    };
  }

  /**
   * Search across all nodes in the hierarchy.
   */
  function searchNodes(graph, query, limit) {
    if (!query || !query.trim()) return [];
    const maxResults = limit || 20;
    const q = query.trim().toLowerCase();
    const results = [];

    const nodes = graph.nodes || [];
    const modules = graph.modules || [];
    const dataStores = graph.data_stores || [];
    const externalSinks = graph.external_sinks || [];

    // Search nodes
    for (const n of nodes) {
      const idMatch = (n.id || "").toLowerCase().includes(q);
      const labelMatch = (n.label || "").toLowerCase().includes(q);
      const symbolMatch = (n.symbol || "").toLowerCase().includes(q);
      const fileMatch = (n.file || "").toLowerCase().includes(q);

      if (idMatch || labelMatch || symbolMatch || fileMatch) {
        results.push({
          id: n.id,
          label: n.label || n.id,
          kind: n.kind,
          file: n.file,
          line: n.line,
          symbol: n.symbol,
          score: labelMatch ? 1 : symbolMatch ? 2 : idMatch ? 3 : 4,
        });
      }
    }

    // Search modules
    for (const m of modules) {
      if ((m.name || "").toLowerCase().includes(q) || (m.id || "").toLowerCase().includes(q)) {
        results.push({
          id: m.id,
          label: m.name || m.id,
          kind: "module",
          file: m.path,
          score: 1,
        });
      }
    }

    // Search stores and sinks
    for (const ds of dataStores) {
      if ((ds.label || "").toLowerCase().includes(q) || (ds.id || "").toLowerCase().includes(q)) {
        results.push({
          id: ds.id,
          label: ds.label || ds.id,
          kind: "datastore",
          score: 1,
        });
      }
    }

    for (const ext of externalSinks) {
      if ((ext.label || "").toLowerCase().includes(q) || (ext.id || "").toLowerCase().includes(q)) {
        results.push({
          id: ext.id,
          label: ext.label || ext.id,
          kind: "external_sink",
          score: 1,
        });
      }
    }

    results.sort((a, b) => a.score - b.score || a.id.localeCompare(b.id));
    return results.slice(0, maxResults);
  }

  /**
   * Get detailed metadata for the side panel inspection.
   */
  function getNodeDetails(hierarchy, nodeId) {
    const node = hierarchy.nodeLookup.get(nodeId);
    if (!node) return null;

    const leafScoped = isLeafNode(node);
    const leafIds = leafScoped ? [node.id] : collectDescendantLeafIds(hierarchy, node);

    const mapOtherEndpoint = (otherId) => {
      if (leafScoped) return otherId;
      const ancestors = hierarchy.ancestorMap.get(otherId);
      if (!ancestors) return otherId;
      for (let i = ancestors.length - 1; i >= 0; i--) {
        if (ancestors[i].level === node.level) return ancestors[i].id;
      }
      return otherId;
    };

    const aggregateConnected = (direction) => {
      const entries = new Map();
      leafIds.forEach((leafId) => {
        const edges =
          (direction === "out"
            ? hierarchy.outgoingEdges.get(leafId)
            : hierarchy.incomingEdges.get(leafId)) || [];
        edges.forEach((edge) => {
          const mappedId = mapOtherEndpoint(direction === "out" ? edge.target : edge.source);
          if (!leafScoped && mappedId === node.id) return;
          if (!entries.has(mappedId)) {
            const other = hierarchy.nodeLookup.get(mappedId);
            entries.set(mappedId, {
              id: mappedId,
              label: other ? other.label || other.id : mappedId,
              kind: other ? other.kind : "unknown",
              kinds: new Set(),
              count: 0,
              file: edge.file,
              line: edge.line,
            });
          }
          const entry = entries.get(mappedId);
          if (edge.kind) entry.kinds.add(edge.kind);
          entry.count++;
        });
      });
      return Array.from(entries.values())
        .sort((a, b) => a.id.localeCompare(b.id))
        .map((entry) => ({
          id: entry.id,
          label: entry.label,
          kind: entry.kind,
          edgeKind: Array.from(entry.kinds).sort().join(", "),
          count: entry.count,
          file: entry.file,
          line: entry.line,
        }));
    };

    const uses = aggregateConnected("out");
    const usedBy = aggregateConnected("in");

    let tier = node.tier || null;
    if (!leafScoped) {
      const leafTiers = Array.from(
        new Set(
          leafIds
            .map((leafId) => hierarchy.nodeLookup.get(leafId))
            .map((leaf) => leaf && leaf.tier)
            .filter(Boolean)
        )
      ).sort();
      if (leafTiers.length > 0) tier = leafTiers.join(", ");
    }

    let citation = "";
    if (node.file && node.line) {
      citation = `${node.file}:${node.line}`;
    } else if (node.evidence && node.evidence.length > 0) {
      citation = node.evidence.join(", ");
    }

    const authGuards = (node.metadata && node.metadata.auth) || [];

    return {
      id: node.id,
      label: node.label || node.id,
      kind: node.kind,
      level: node.level,
      isLeaf: leafScoped,
      tier,
      citation,
      file: node.file,
      line: node.line,
      symbol: node.symbol,
      auth: authGuards,
      fileCount: node.fileCount || 0,
      symbolCount: node.symbolCount || 0,
      metadata: node.metadata || {},
      evidence: node.evidence || [],
      uses,
      usedBy,
      canDrillDown: Boolean(node.children && node.children.length > 0),
    };
  }

  /**
   * Complete ViewerState class managing zoom levels, breadcrumbs, selection, and filters.
   */
  class ViewerState {
    constructor(graph) {
      this.graph = graph;
      this.hierarchy = buildHierarchy(graph);
      this.currentLevel = "system";
      this.currentScope = "system";
      this.selectedNodeId = null;
      this.breadcrumbs = [{ level: "system", id: "system", label: this.hierarchy.systemNode.label }];
      this.filters = {
        modules: new Set(this.hierarchy.modules.map((mod) => mod.id)),
        kinds: new Set(FILTER_KINDS),
        tiers: new Set(FILTER_TIERS),
      };
      this.trace = null;
    }

    setBreadcrumbs(ancestors, scopeId) {
      const systemCrumb = {
        level: "system",
        id: "system",
        label: this.hierarchy.systemNode.label,
      };
      if (scopeId === "system") {
        this.breadcrumbs = [systemCrumb];
        return;
      }
      const scopeIndex = ancestors.findIndex((crumb) => crumb.id === scopeId);
      this.breadcrumbs = scopeIndex >= 0 ? ancestors.slice(0, scopeIndex + 1) : [systemCrumb];
    }

    /**
     * Drill down one level into a node.
     */
    drillDown(nodeId) {
      if (this.trace) this.stopTrace();
      const node = this.hierarchy.nodeLookup.get(nodeId);
      if (!node) return false;

      let nextLevel = null;
      if (node.kind === "module" || node.level === "module") {
        nextLevel = "module";
      } else if (node.kind === "file_group" || node.level === "file_group") {
        nextLevel = "file_group";
      } else if (node.kind === "file" || node.level === "file") {
        nextLevel = "file";
      } else if (
        node.level === "symbol" ||
        node.kind === "service" ||
        node.kind === "entrypoint" ||
        node.kind === "frontend_caller"
      ) {
        nextLevel = "symbol";
      }

      if (!nextLevel) return false;

      const ancestors = this.hierarchy.ancestorMap.get(nodeId);
      if (!ancestors) return false;

      this.selectedNodeId = nodeId;
      this.setBreadcrumbs(ancestors, nodeId);
      const scope = this.breadcrumbs[this.breadcrumbs.length - 1];
      this.currentLevel = scope.level;
      this.currentScope = scope.id;
      return true;
    }

    /**
     * Navigate up one level in hierarchy.
     */
    goUp() {
      if (this.trace) this.stopTrace();
      if (this.breadcrumbs.length <= 1) return false;
      this.breadcrumbs.pop();
      const parent = this.breadcrumbs[this.breadcrumbs.length - 1];
      this.currentLevel = parent.level;
      this.currentScope = parent.id;
      this.selectedNodeId = parent.id === "system" ? null : parent.id;
      return true;
    }

    /**
     * Jump directly to a breadcrumb index.
     */
    goToBreadcrumb(index) {
      if (this.trace) this.stopTrace();
      if (index < 0 || index >= this.breadcrumbs.length) return false;
      this.breadcrumbs = this.breadcrumbs.slice(0, index + 1);
      const target = this.breadcrumbs[this.breadcrumbs.length - 1];
      this.currentLevel = target.level;
      this.currentScope = target.id;
      this.selectedNodeId = target.id === "system" ? null : target.id;
      return true;
    }

    /**
     * Focus on a node by ID (for #node=<id> routing or search selection).
     * Navigates the hierarchy so the node is visible, selects it, and sets breadcrumbs.
     */
    focusNode(nodeId) {
      if (this.trace) this.stopTrace();
      const node = this.hierarchy.nodeLookup.get(nodeId);
      if (!node) return false;

      const ancestors = this.hierarchy.ancestorMap.get(nodeId);
      if (!ancestors || ancestors.length === 0) return false;

      this.selectedNodeId = nodeId;

      const parentLevels = {
        symbol: "file",
        file: "file_group",
        file_group: "module",
        module: "system",
      };
      const parentLevel = parentLevels[node.level];
      let scopeId = "system";
      if (parentLevel && parentLevel !== "system") {
        const parent = ancestors.find((crumb) => crumb.level === parentLevel);
        if (parent) {
          scopeId = parent.id;
        }
      }

      this.setBreadcrumbs(ancestors, scopeId);
      const scope = this.breadcrumbs[this.breadcrumbs.length - 1];
      this.currentLevel = scope.level;
      this.currentScope = scope.id;
      return true;
    }

    /**
     * Start guided flow trace mode for an entrypoint.
     */
    startTrace(entrypointId) {
      this.trace = traceFlow(this.graph, entrypointId);
      return this.trace;
    }

    /**
     * Stop guided flow trace mode.
     */
    stopTrace() {
      this.trace = null;
    }

    /**
     * Get current view nodes and layout.
     */
    getCurrentView(options) {
      if (this.trace) {
        // Trace mode: isolate and display the full transitive trace subgraph
        const traceNodeIds = new Set(this.trace.reachableNodes);
        const traceNodes = [];
        traceNodeIds.forEach((id) => {
          const n = this.hierarchy.nodeLookup.get(id);
          if (n) traceNodes.push(n);
        });

        const rawEdges = this.graph.edges || [];
        const edgeMap = new Map();

        rawEdges.forEach((e) => {
          if (traceNodeIds.has(e.source) && traceNodeIds.has(e.target)) {
            const key = `${e.source}->${e.target}`;
            if (!edgeMap.has(key)) {
              edgeMap.set(key, {
                source: e.source,
                target: e.target,
                count: 0,
                underlying: [],
              });
            }
            const agg = edgeMap.get(key);
            agg.count++;
            agg.underlying.push(e);
          }
        });

        traceNodes.sort((a, b) => a.id.localeCompare(b.id));
        const edgesList = Array.from(edgeMap.values());
        edgesList.forEach((edge) => {
          edge.kinds = aggregatedEdgeKinds(edge);
        });
        edgesList.sort((a, b) => {
          return `${a.source}->${a.target}`.localeCompare(`${b.source}->${b.target}`);
        });

        const layout = computeLayeredLayout(traceNodes, edgesList, options);
        return {
          level: "trace",
          scopeId: this.trace.entrypointId,
          breadcrumbs: this.breadcrumbs,
          selectedNodeId: this.selectedNodeId,
          nodes: layout.nodes,
          edges: layout.edges,
          width: layout.width,
          height: layout.height,
          trace: this.trace,
        };
      }

      const scopeData = getHierarchyScope(
        this.hierarchy,
        this.currentLevel,
        this.currentScope,
        this.filters
      );
      const layout = computeLayeredLayout(scopeData.nodes, scopeData.edges, options);
      return {
        level: this.currentLevel,
        scopeId: this.currentScope,
        breadcrumbs: this.breadcrumbs,
        selectedNodeId: this.selectedNodeId,
        nodes: layout.nodes,
        edges: layout.edges,
        width: layout.width,
        height: layout.height,
        trace: this.trace,
      };
    }
  }

  return {
    buildHierarchy,
    getHierarchyScope,
    traceFlow,
    computeLayeredLayout,
    searchNodes,
    getNodeDetails,
    ViewerState,
    areAllFilterGroupsSelected,
    FILTER_KINDS,
    FILTER_TIERS,
  };
});
