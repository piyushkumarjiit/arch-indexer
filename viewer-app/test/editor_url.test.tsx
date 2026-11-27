import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Evidence, OverviewData, ViewEdgeData, ViewNodeData } from "../src/types/bundle";
import { ALLOWED_EDITOR_URL_SCHEMES, buildEditorHref, Inspector } from "../src/ui/Inspector";

const SAMPLE_EV: Evidence = {
  file: "server/src/app.ts",
  line: 42,
  source: "scip",
};

function makeOverview(editor_url?: string): OverviewData {
  return {
    schema_version: "2.0",
    repo_name: "demo",
    generated_at_commit: "abc123",
    include_tests: false,
    tier_summary: {
      total_files: 1,
      excluded_test_files: 0,
      scip_files: 1,
      treesitter_files: 0,
      tier2_modules: [],
    },
    parts: [],
    groups: [],
    boundaries: [],
    edges: [],
    flow_index: [],
    chunks: {},
    delta: { changed_files: [], new_files: [] },
    ...(editor_url !== undefined ? { editor_url } : {}),
  };
}

const SAMPLE_NODE: ViewNodeData = {
  id: "sym:server.app.handler",
  label: "handler",
  kind: "entrypoint",
  drillable: false,
  evidence: [SAMPLE_EV],
};

const SAMPLE_EDGE: ViewEdgeData = {
  id: "a->b",
  source: "part:a",
  target: "ds:sqlite",
  kinds: ["reads"],
  count: 1,
  evidence: [SAMPLE_EV],
};

describe("Inspector editor_url URL-scheme allowlist", () => {
  it("exports the expected allowlist", () => {
    expect([...ALLOWED_EDITOR_URL_SCHEMES].sort()).toEqual([
      "cursor",
      "file",
      "http",
      "https",
      "idea",
      "jetbrains",
      "subl",
      "vscode",
      "vscode-insiders",
      "zed",
    ]);
  });

  it.each([
    ["vscode://file/{abs_path}:{line}", "vscode://file/server/src/app.ts:42"],
    ["vscode-insiders://file/{file}:{line}", "vscode-insiders://file/server/src/app.ts:42"],
    ["cursor://file/{abs_path}:{line}", "cursor://file/server/src/app.ts:42"],
    ["idea://open?file={file}&line={line}", "idea://open?file=server/src/app.ts&line=42"],
    [
      "jetbrains://pycharm/navigate/reference?project=app&path={file}:{line}",
      "jetbrains://pycharm/navigate/reference?project=app&path=server/src/app.ts:42",
    ],
    ["zed://file/{abs_path}:{line}", "zed://file/server/src/app.ts:42"],
    ["subl://open?url=file://{file}&line={line}", "subl://open?url=file://server/src/app.ts&line=42"],
    ["http://localhost:8080/{file}#L{line}", "http://localhost:8080/server/src/app.ts#L42"],
    ["https://github.com/org/repo/blob/main/{file}#L{line}", "https://github.com/org/repo/blob/main/server/src/app.ts#L42"],
    ["file://{abs_path}", "file://server/src/app.ts"],
    ["VSCODE://file/{file}?line={line}&dup={file}:{line}", "VSCODE://file/server/src/app.ts?line=42&dup=server/src/app.ts:42"],
  ])("accepts allowlisted template %s", (template, expected) => {
    expect(buildEditorHref(SAMPLE_EV, template)).toBe(expected);

    const nodeHtml = renderToStaticMarkup(
      <Inspector
        open
        overview={makeOverview(template)}
        chunks={{}}
        selectedNode={SAMPLE_NODE}
        selectedEdge={null}
        visibleEdges={[]}
        descriptions={null}
        runtime={null}
        dataflow={null}
        onClose={() => {}}
        onSelectNode={() => {}}
        onStartFlow={() => {}}
      />,
    );
    expect(nodeHtml).toContain("<a ");
    expect(nodeHtml).toContain("server/src/app.ts:42");

    const edgeHtml = renderToStaticMarkup(
      <Inspector
        open
        overview={makeOverview(template)}
        chunks={{}}
        selectedNode={null}
        selectedEdge={SAMPLE_EDGE}
        visibleEdges={[]}
        descriptions={null}
        runtime={null}
        dataflow={null}
        onClose={() => {}}
        onSelectNode={() => {}}
        onStartFlow={() => {}}
      />,
    );
    expect(edgeHtml).toContain("<a ");
    expect(edgeHtml).toContain("server/src/app.ts:42");
  });

  it.each([
    "javascript:alert(1)",
    "JAVASCRIPT:alert('{file}')",
    "  javascript:alert(1)",
    "java\tscript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "ftp://example.com/{file}",
    "/relative/path/{file}:{line}",
    "//evil.example.com/{file}",
    "{file}:{line}",
    "vscode://file/\n{file}",
  ])("rejects disallowed template %s and renders no link", (template) => {
    expect(buildEditorHref(SAMPLE_EV, template)).toBeNull();

    const nodeHtml = renderToStaticMarkup(
      <Inspector
        open
        overview={makeOverview(template)}
        chunks={{}}
        selectedNode={SAMPLE_NODE}
        selectedEdge={null}
        visibleEdges={[]}
        descriptions={null}
        runtime={null}
        dataflow={null}
        onClose={() => {}}
        onSelectNode={() => {}}
        onStartFlow={() => {}}
      />,
    );
    expect(nodeHtml).not.toContain("<a ");
    expect(nodeHtml).toContain('<span class="arch-evidence-cite">server/src/app.ts:42</span>');

    const edgeHtml = renderToStaticMarkup(
      <Inspector
        open
        overview={makeOverview(template)}
        chunks={{}}
        selectedNode={null}
        selectedEdge={SAMPLE_EDGE}
        visibleEdges={[]}
        descriptions={null}
        runtime={null}
        dataflow={null}
        onClose={() => {}}
        onSelectNode={() => {}}
        onStartFlow={() => {}}
      />,
    );
    expect(edgeHtml).not.toContain("<a ");
    expect(edgeHtml).toContain('<span class="arch-evidence-cite">server/src/app.ts:42</span>');
  });

  it("rejects placeholder expansion that introduces a disallowed scheme", () => {
    const hostileEv: Evidence = { file: "javascript:alert(1)", line: 1, source: "scip" };
    expect(buildEditorHref(hostileEv, "{file}")).toBeNull();
  });
});
