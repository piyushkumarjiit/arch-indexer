/**
 * arch-indexer flow-viewer interactive UI controller
 * Handles DOM events, SVG rendering, pan/zoom, search, inspector, trace mode, and file picker.
 */

(function () {
  "use strict";

  let state = null;
  let svgEl = null;
  let viewportEl = null;
  let transform = { x: 40, y: 40, scale: 1.0 };
  let isPanning = false;
  let startX = 0;
  let startY = 0;
  let activeTraceStep = 0;

  function init() {
    svgEl = document.getElementById("canvas-svg");
    viewportEl = document.getElementById("viewport");

    // Load initial graph data from script tag
    const dataEl = document.getElementById("arch-graph-data");
    let initialGraph = null;
    if (dataEl && dataEl.textContent) {
      try {
        initialGraph = JSON.parse(dataEl.textContent);
      } catch (e) {
        console.error("Failed to parse initial graph JSON:", e);
      }
    }

    if (!initialGraph) {
      initialGraph = {
        schema_version: "1.0",
        repo_name: "No Graph Loaded",
        modules: [],
        nodes: [],
        edges: [],
        data_stores: [],
        external_sinks: [],
      };
    }

    setupState(initialGraph);
    setupPanZoom();
    setupSearch();
    setupFilePicker();
    setupControls();

    // Check for #node= in hash on load
    handleHashChange();
    window.addEventListener("hashchange", handleHashChange);
  }

  function setupState(graph) {
    state = new ArchViewerLogic.ViewerState(graph);

    // Update UI headers
    const repoTitle = document.getElementById("repo-title");
    if (repoTitle) repoTitle.textContent = graph.repo_name || "Repository Architecture";

    setupFilterCheckboxes();
    render();
  }

  function setupFilterCheckboxes() {
    const modulesContainer = document.getElementById("filter-modules");
    const kindsContainer = document.getElementById("filter-kinds");
    const tiersContainer = document.getElementById("filter-tiers");

    function selectedValues(container) {
      return new Set(
        Array.from(container.querySelectorAll("input"))
          .filter((input) => input.checked)
          .map((input) => input.value)
      );
    }

    if (modulesContainer) {
      modulesContainer.innerHTML = "";
      state.hierarchy.modules.forEach((mod) => {
        const label = document.createElement("label");
        label.className = "filter-item";

        const input = document.createElement("input");
        input.type = "checkbox";
        input.value = mod.id;
        input.checked = state.filters.modules.has(mod.id);

        const span = document.createElement("span");
        span.textContent = mod.name || mod.label || mod.id;

        const count = document.createElement("span");
        count.className = "count-pill";
        count.textContent = String(mod.fileCount || 0);

        label.appendChild(input);
        label.appendChild(span);
        label.appendChild(count);

        input.addEventListener("change", () => {
          state.filters.modules = selectedValues(modulesContainer);
          render();
        });
        modulesContainer.appendChild(label);
      });
    }

    if (kindsContainer) {
      kindsContainer.innerHTML = "";
      ArchViewerLogic.FILTER_KINDS.forEach((k) => {
        const label = document.createElement("label");
        label.className = "filter-item";

        const input = document.createElement("input");
        input.type = "checkbox";
        input.value = k;
        input.checked = state.filters.kinds.has(k);

        const span = document.createElement("span");
        span.textContent = k.replace("_", " ");

        label.appendChild(input);
        label.appendChild(span);

        input.addEventListener("change", () => {
          state.filters.kinds = selectedValues(kindsContainer);
          render();
        });
        kindsContainer.appendChild(label);
      });
    }

    if (tiersContainer) {
      tiersContainer.innerHTML = "";
      ArchViewerLogic.FILTER_TIERS.forEach((t) => {
        const label = document.createElement("label");
        label.className = "filter-item";

        const input = document.createElement("input");
        input.type = "checkbox";
        input.value = t;
        input.checked = state.filters.tiers.has(t);

        const span = document.createElement("span");
        span.textContent = t === "scip" ? "SCIP" : t === "tree-sitter" ? "Tree-sitter" : t;

        label.appendChild(input);
        label.appendChild(span);

        input.addEventListener("change", () => {
          state.filters.tiers = selectedValues(tiersContainer);
          render();
        });
        tiersContainer.appendChild(label);
      });
    }
  }

  function render() {
    if (!state.trace) activeTraceStep = 0;
    updateTraceBar();
    renderBreadcrumbs();
    renderGraph();
  }

  function renderBreadcrumbs() {
    const container = document.getElementById("breadcrumbs");
    const zoomBadge = document.getElementById("zoom-level-badge");
    if (!container) return;

    container.innerHTML = "";
    if (zoomBadge) {
      zoomBadge.textContent = state.trace ? "flow trace" : state.currentLevel.replace("_", " ");
    }

    state.breadcrumbs.forEach((crumb, idx) => {
      const isLast = idx === state.breadcrumbs.length - 1;
      const crumbEl = document.createElement("div");
      crumbEl.className = `crumb ${isLast ? "active" : ""}`;
      crumbEl.textContent = crumb.label || crumb.id;
      crumbEl.addEventListener("click", () => {
        state.goToBreadcrumb(idx);
        render();
      });
      container.appendChild(crumbEl);

      if (!isLast) {
        const sep = document.createElement("span");
        sep.className = "crumb-sep";
        sep.textContent = "›";
        container.appendChild(sep);
      }
    });
  }

  function renderGraph() {
    const view = state.getCurrentView();
    if (!viewportEl) return;

    viewportEl.innerHTML = "";

    if (view.nodes.length === 0) {
      const filtersHideNodes = !ArchViewerLogic.areAllFilterGroupsSelected(
        state.hierarchy,
        state.filters
      );
      const message = document.createElementNS("http://www.w3.org/2000/svg", "text");
      message.setAttribute("x", "40");
      message.setAttribute("y", "80");
      message.setAttribute("fill", "#64748b");
      message.setAttribute("font-size", "16");
      message.textContent = filtersHideNodes
        ? "No nodes match the selected filters."
        : "No nodes in this graph.";
      viewportEl.appendChild(message);
      return;
    }

    // 1. Defs (markers for arrowheads)
    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    defs.innerHTML = `
      <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M 0 1 L 10 5 L 0 9 z" fill="#64748b" />
      </marker>
      <marker id="arrow-trace" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M 0 1 L 10 5 L 0 9 z" fill="#06b6d4" />
      </marker>
    `;
    viewportEl.appendChild(defs);

    const edgesGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");
    edgesGroup.setAttribute("class", "edges-layer");
    viewportEl.appendChild(edgesGroup);

    const nodesGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");
    nodesGroup.setAttribute("class", "nodes-layer");
    viewportEl.appendChild(nodesGroup);

    // Determine trace highlights
    const isTraceActive = Boolean(state.trace);
    const traceNodeSet = new Set(state.trace ? state.trace.reachableNodes : []);
    const traceEdgeSet = new Set(state.trace ? state.trace.reachableEdges : []);
    const currentStepNodeId =
      isTraceActive && state.trace.steps[activeTraceStep]
        ? state.trace.steps[activeTraceStep].nodeId
        : null;

    // 2. Render Edges
    view.edges.forEach((edge) => {
      const edgeKey = `${edge.source}->${edge.target}`;
      const inTrace = isTraceActive && traceEdgeSet.has(edgeKey);
      const isDimmed = isTraceActive && !inTrace;

      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", edge.d);
      path.setAttribute("class", `svg-edge ${inTrace ? "in-trace" : ""} ${isDimmed ? "dimmed" : ""}`);
      path.setAttribute("marker-end", inTrace ? "url(#arrow-trace)" : "url(#arrow)");

      path.addEventListener("click", () => {
        showEdgeDetails(edge);
      });
      edgesGroup.appendChild(path);

      if (edge.count && edge.count > 1) {
        const midX = (edge.x1 + edge.x2) / 2;
        const midY = (edge.y1 + edge.y2) / 2 - 6;
        const txt = document.createElementNS("http://www.w3.org/2000/svg", "text");
        txt.setAttribute("x", midX);
        txt.setAttribute("y", midY);
        txt.setAttribute("class", "svg-edge-label");
        txt.textContent = `${edge.count} edges`;
        edgesGroup.appendChild(txt);
      }
    });

    // 3. Render Nodes
    view.nodes.forEach((node) => {
      const isSelected = state.selectedNodeId === node.id;
      const inTrace = isTraceActive && traceNodeSet.has(node.id);
      const isCurrentStep = node.id === currentStepNodeId;
      const isDimmed = isTraceActive && !inTrace;

      const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
      g.setAttribute("class", `svg-node kind-${node.kind} ${isSelected ? "selected" : ""} ${inTrace ? "in-trace" : ""} ${isDimmed ? "dimmed" : ""}`);
      g.setAttribute("data-id", node.id);
      g.setAttribute("transform", `translate(${node.x}, ${node.y})`);

      // Rect box
      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("class", "node-box");
      rect.setAttribute("width", node.width);
      rect.setAttribute("height", node.height);
      if (isCurrentStep) {
        rect.style.stroke = "#f59e0b";
        rect.style.strokeWidth = "3px";
      }
      g.appendChild(rect);

      // Title
      const title = document.createElementNS("http://www.w3.org/2000/svg", "text");
      title.setAttribute("class", "node-title");
      title.setAttribute("x", 12);
      title.setAttribute("y", 28);
      title.textContent = truncate(node.label || node.id, 24);
      g.appendChild(title);

      // Subtitle / Citation / Count
      const sub = document.createElementNS("http://www.w3.org/2000/svg", "text");
      sub.setAttribute("class", "node-sub");
      sub.setAttribute("x", 12);
      sub.setAttribute("y", 48);

      if (node.level === "module") {
        sub.textContent = `${node.fileCount || 0} files, ${node.symbolCount || 0} symbols`;
      } else if (node.level === "file_group") {
        sub.textContent = `${node.fileCount || 0} files`;
      } else if (node.level === "file") {
        sub.textContent = `${node.symbolCount || 0} symbols`;
      } else if (node.file && node.line) {
        sub.textContent = `${truncate(node.file, 20)}:${node.line}`;
      } else if (node.evidence && node.evidence[0]) {
        sub.textContent = truncate(node.evidence[0], 25);
      } else {
        sub.textContent = node.kind.replace("_", " ");
      }
      g.appendChild(sub);

      // Kind badge on right top
      const badge = document.createElementNS("http://www.w3.org/2000/svg", "text");
      badge.setAttribute("class", "node-badge");
      badge.setAttribute("x", node.width - 12);
      badge.setAttribute("y", 22);
      badge.textContent = (node.tier || node.kind || "").substring(0, 8);
      g.appendChild(badge);

      // Click to select
      g.addEventListener("click", (e) => {
        e.stopPropagation();
        selectNode(node.id);
      });

      // Double-click to drill down
      g.addEventListener("dblclick", (e) => {
        e.stopPropagation();
        if (state.drillDown(node.id)) {
          render();
        }
      });

      nodesGroup.appendChild(g);
    });

    updateTransform();
  }

  function selectNode(nodeId) {
    state.selectedNodeId = nodeId;
    const details = ArchViewerLogic.getNodeDetails(state.hierarchy, nodeId);
    showInspector(details);
    renderGraph();
  }

  function navigateToNode(nodeId) {
    if (!state || !nodeId || !state.focusNode(nodeId)) return false;
    render();
    selectNode(nodeId);
    const target = `#node=${encodeURIComponent(nodeId)}`;
    if (window.location.hash !== target) {
      window.location.hash = target;
    }
    return true;
  }

  function showInspector(details) {
    const panel = document.getElementById("inspector-panel");
    const content = document.getElementById("inspector-content");
    if (!panel || !content) return;

    if (!details) {
      panel.classList.remove("open");
      return;
    }

    content.innerHTML = `
      <div class="detail-row">
        <span class="detail-label">Kind</span>
        <div><span class="kind-badge badge-${escapeHtml(details.kind)}">${escapeHtml(details.kind.replace("_", " "))}</span></div>
      </div>
      <div class="detail-row">
        <span class="detail-label">ID</span>
        <span class="detail-value detail-mono">${escapeHtml(details.id)}</span>
      </div>
      <div class="detail-row">
        <span class="detail-label">Label</span>
        <span class="detail-value" style="font-weight:600">${escapeHtml(details.label)}</span>
      </div>
      ${
        details.symbol
          ? `<div class="detail-row">
              <span class="detail-label">Symbol</span>
              <span class="detail-value detail-mono">${escapeHtml(details.symbol)}</span>
            </div>`
          : ""
      }
      ${
        details.citation
          ? `<div class="detail-row">
              <span class="detail-label">Citation</span>
              <span class="detail-value detail-mono">${escapeHtml(details.citation)}</span>
            </div>`
          : ""
      }
      <div class="detail-row">
        <span class="detail-label">Indexing Tier</span>
        <span class="detail-value">${escapeHtml(details.tier)}</span>
      </div>
      ${
        details.auth && details.auth.length > 0
          ? `<div class="detail-row">
              <span class="detail-label">Auth Guards</span>
              <div class="tag-list">${details.auth.map((a) => `<span class="tag">${escapeHtml(a)}</span>`).join("")}</div>
            </div>`
          : ""
      }
      ${
        details.canDrillDown
          ? `<div class="detail-row">
              <button class="btn btn-primary" id="btn-inspector-drill" style="width:100%">Drill Down</button>
            </div>`
          : ""
      }
      ${
        details.kind === "entrypoint" || details.uses.length > 0
          ? `<div class="detail-row">
              <button class="btn" id="btn-inspector-trace" style="width:100%">🔍 Trace Flow From Here</button>
            </div>`
          : ""
      }
      <div class="detail-row">
        <span class="detail-label">Uses (${details.uses.length})</span>
        <div class="links-list" id="inspector-uses">
          ${details.uses.map((u) => `<div class="link-item" data-id="${escapeHtml(u.id)}"><span>${escapeHtml(u.label)}</span><span class="count-pill">${escapeHtml(u.edgeKind)}</span></div>`).join("")}
        </div>
      </div>
      <div class="detail-row">
        <span class="detail-label">Used By (${details.usedBy.length})</span>
        <div class="links-list" id="inspector-used-by">
          ${details.usedBy.map((u) => `<div class="link-item" data-id="${escapeHtml(u.id)}"><span>${escapeHtml(u.label)}</span><span class="count-pill">${escapeHtml(u.edgeKind)}</span></div>`).join("")}
        </div>
      </div>
    `;

    panel.classList.add("open");

    const drillBtn = document.getElementById("btn-inspector-drill");
    if (drillBtn) {
      drillBtn.addEventListener("click", () => {
        if (state.drillDown(details.id)) {
          render();
        }
      });
    }

    const traceBtn = document.getElementById("btn-inspector-trace");
    if (traceBtn) {
      traceBtn.addEventListener("click", () => {
        startTrace(details.id);
      });
    }

    content.querySelectorAll(".link-item").forEach((el) => {
      el.addEventListener("click", () => {
        const targetId = el.getAttribute("data-id");
        if (targetId) {
          navigateToNode(targetId);
        }
      });
    });
  }

  function showEdgeDetails(edge) {
    const panel = document.getElementById("inspector-panel");
    const content = document.getElementById("inspector-content");
    if (!panel || !content) return;

    const underlying = edge.underlying || [];
    const kinds = (edge.kinds || []).join(", ");
    const citations = Array.from(
      new Set(
        underlying
          .map((u) => (u.file && u.line ? `${u.file}:${u.line}` : ""))
          .filter((citation) => citation)
      )
    );
    const tiers = Array.from(new Set(underlying.map((u) => u.tier).filter((tier) => tier)));

    content.innerHTML = `
      <div class="detail-row">
        <span class="detail-label">Edge Connection</span>
        <span class="detail-value detail-mono">${escapeHtml(edge.source)} &rarr; ${escapeHtml(edge.target)}</span>
      </div>
      <div class="detail-row">
        <span class="detail-label">Kind</span>
        <span class="detail-value">${escapeHtml(kinds)}</span>
      </div>
      <div class="detail-row">
        <span class="detail-label">Underlying Edges</span>
        <span class="detail-value">${edge.count || 1}</span>
      </div>
      ${
        citations.length > 0
          ? `<div class="detail-row">
              <span class="detail-label">Citation</span>
              <span class="detail-value detail-mono">${escapeHtml(citations.join(", "))}</span>
            </div>`
          : ""
      }
      ${
        tiers.length > 0
          ? `<div class="detail-row">
              <span class="detail-label">Indexing Tier</span>
              <span class="detail-value">${escapeHtml(tiers.join(", "))}</span>
            </div>`
          : ""
      }
      <div class="detail-row">
        <div class="links-list">
          <div class="link-item" data-id="${escapeHtml(edge.source)}"><span>Source: ${escapeHtml(edge.source)}</span></div>
          <div class="link-item" data-id="${escapeHtml(edge.target)}"><span>Target: ${escapeHtml(edge.target)}</span></div>
        </div>
      </div>
    `;

    content.querySelectorAll(".link-item").forEach((el) => {
      el.addEventListener("click", () => {
        const targetId = el.getAttribute("data-id");
        if (targetId) {
          navigateToNode(targetId);
        }
      });
    });

    panel.classList.add("open");
  }

  function startTrace(entrypointId) {
    state.startTrace(entrypointId);
    activeTraceStep = 0;
    render();
  }

  function updateTraceBar() {
    const bar = document.getElementById("trace-bar");
    if (!bar) return;

    if (!state.trace) {
      bar.classList.remove("active");
      return;
    }

    const steps = state.trace.steps || [];
    const currentStep = steps[activeTraceStep];

    const stepText = document.getElementById("trace-step-text");
    if (stepText && currentStep) {
      const authText =
        currentStep.auth && currentStep.auth.length > 0
          ? ` Auth: ${currentStep.auth.join(", ")}`
          : "";
      stepText.textContent = `Step ${activeTraceStep + 1} of ${steps.length}: ${currentStep.label} (${currentStep.kind}) ${currentStep.fileLine ? `[${currentStep.fileLine}]` : ""}${authText}`;
    }

    bar.classList.add("active");
  }

  function setupPanZoom() {
    if (!svgEl) return;

    svgEl.addEventListener("mousedown", (e) => {
      if (e.target.closest(".svg-node") || e.target.closest(".svg-edge")) return;
      isPanning = true;
      startX = e.clientX - transform.x;
      startY = e.clientY - transform.y;
      svgEl.classList.add("dragging");
    });

    window.addEventListener("mousemove", (e) => {
      if (!isPanning) return;
      transform.x = e.clientX - startX;
      transform.y = e.clientY - startY;
      updateTransform();
    });

    window.addEventListener("mouseup", () => {
      isPanning = false;
      if (svgEl) svgEl.classList.remove("dragging");
    });

    svgEl.addEventListener("wheel", (e) => {
      e.preventDefault();
      const zoomFactor = e.deltaY < 0 ? 1.15 : 0.85;
      const mouseX = e.clientX - svgEl.getBoundingClientRect().left;
      const mouseY = e.clientY - svgEl.getBoundingClientRect().top;

      const newScale = Math.min(Math.max(0.1, transform.scale * zoomFactor), 4.0);

      transform.x = mouseX - (mouseX - transform.x) * (newScale / transform.scale);
      transform.y = mouseY - (mouseY - transform.y) * (newScale / transform.scale);
      transform.scale = newScale;

      updateTransform();
    });
  }

  function updateTransform() {
    if (!viewportEl) return;
    viewportEl.setAttribute(
      "transform",
      `translate(${transform.x}, ${transform.y}) scale(${transform.scale})`
    );
  }

  function resetView() {
    transform = { x: 40, y: 40, scale: 1.0 };
    updateTransform();
  }

  function setupSearch() {
    const input = document.getElementById("search-input");
    const dropdown = document.getElementById("search-dropdown");
    if (!input || !dropdown) return;

    input.addEventListener("input", () => {
      const q = input.value;
      if (!q.trim()) {
        dropdown.classList.remove("active");
        dropdown.innerHTML = "";
        return;
      }

      const results = ArchViewerLogic.searchNodes(state.graph, q);
      dropdown.innerHTML = "";

      if (results.length === 0) {
        dropdown.innerHTML = `<div class="search-item" style="color:var(--text-muted)">No matches found</div>`;
      } else {
        results.forEach((res) => {
          const item = document.createElement("div");
          item.className = "search-item";

          const info = document.createElement("div");
          info.className = "search-item-info";

          const labelSpan = document.createElement("span");
          labelSpan.className = "search-item-label";
          labelSpan.textContent = res.label || res.id;

          const subSpan = document.createElement("span");
          subSpan.className = "search-item-sub";
          subSpan.textContent = res.file ? `${res.file}:${res.line || 0}` : res.id;

          info.appendChild(labelSpan);
          info.appendChild(subSpan);

          const badge = document.createElement("span");
          badge.className = `kind-badge badge-${res.kind}`;
          badge.textContent = res.kind || "";

          item.appendChild(info);
          item.appendChild(badge);

          item.addEventListener("click", () => {
            dropdown.classList.remove("active");
            input.value = "";
            navigateToNode(res.id);
          });
          dropdown.appendChild(item);
        });
      }
      dropdown.classList.add("active");
    });

    document.addEventListener("click", (e) => {
      if (!e.target.closest(".topbar-center")) {
        dropdown.classList.remove("active");
      }
    });
  }

  function setupFilePicker() {
    const btn = document.getElementById("btn-load-file");
    const fileInput = document.getElementById("file-input");
    if (!btn || !fileInput) return;

    btn.addEventListener("click", () => {
      fileInput.click();
    });

    fileInput.addEventListener("change", (e) => {
      const file = e.target.files[0];
      if (!file) return;

      const reader = new FileReader();
      reader.onload = (event) => {
        try {
          const newGraph = JSON.parse(event.target.result);
          setupState(newGraph);
          resetView();
          window.location.hash = "";
        } catch (err) {
          alert("Error parsing graph JSON: " + err.message);
        }
      };
      reader.readAsText(file);
    });
  }

  function setupControls() {
    const btnZoomIn = document.getElementById("btn-zoom-in");
    const btnZoomOut = document.getElementById("btn-zoom-out");
    const btnZoomReset = document.getElementById("btn-zoom-reset");
    const btnCloseInspector = document.getElementById("btn-close-inspector");
    const btnPrevStep = document.getElementById("btn-prev-step");
    const btnNextStep = document.getElementById("btn-next-step");
    const btnExitTrace = document.getElementById("btn-exit-trace");

    if (btnZoomIn) {
      btnZoomIn.addEventListener("click", () => {
        transform.scale = Math.min(4.0, transform.scale * 1.25);
        updateTransform();
      });
    }

    if (btnZoomOut) {
      btnZoomOut.addEventListener("click", () => {
        transform.scale = Math.max(0.1, transform.scale * 0.8);
        updateTransform();
      });
    }

    if (btnZoomReset) {
      btnZoomReset.addEventListener("click", resetView);
    }

    if (btnCloseInspector) {
      btnCloseInspector.addEventListener("click", () => {
        const panel = document.getElementById("inspector-panel");
        if (panel) panel.classList.remove("open");
        state.selectedNodeId = null;
        renderGraph();
      });
    }

    if (btnPrevStep) {
      btnPrevStep.addEventListener("click", () => {
        if (!state.trace) return;
        if (activeTraceStep > 0) {
          activeTraceStep--;
          render();
        }
      });
    }

    if (btnNextStep) {
      btnNextStep.addEventListener("click", () => {
        if (!state.trace) return;
        if (activeTraceStep < state.trace.steps.length - 1) {
          activeTraceStep++;
          render();
        }
      });
    }

    if (btnExitTrace) {
      btnExitTrace.addEventListener("click", () => {
        state.stopTrace();
        render();
      });
    }
  }

  function handleHashChange() {
    const hash = window.location.hash;
    if (hash && hash.startsWith("#node=")) {
      const raw = hash.substring(6);
      let nodeId = raw;
      try {
        nodeId = decodeURIComponent(raw);
      } catch (e) {
        nodeId = raw;
      }
      if (state && state.focusNode(nodeId)) {
        render();
        selectNode(nodeId);
      }
    }
  }

  function truncate(str, max) {
    if (!str) return "";
    return str.length > max ? str.substring(0, max - 3) + "..." : str;
  }

  function escapeHtml(str) {
    if (str == null) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("DOMContentLoaded", init);
  }

  if (typeof window !== "undefined") {
    window.__archViewer = {
      getState: () => state,
      init,
      handleHashChange,
      render,
      selectNode,
      navigateToNode,
      startTrace,
    };
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      init,
      getState: () => state,
      handleHashChange,
      render,
      navigateToNode,
      escapeHtml,
    };
  }
})();
