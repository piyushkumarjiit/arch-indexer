import { afterEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { GraphCanvas } from "../src/views/GraphCanvas";
import type { ViewNodeData } from "../src/types/bundle";

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal("ResizeObserver", ResizeObserverMock);
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const partNode: ViewNodeData = {
  id: "part:web",
  label: "web",
  kind: "part",
};

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(async () => {
  if (root) {
    await act(async () => root?.unmount());
  }
  container?.remove();
  root = null;
  container = null;
});

describe("GraphCanvas interactions", () => {
  it("drills into the node on double-click instead of swallowing it for canvas zoom", async () => {
    const onDrillNode = vi.fn();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root!.render(
        <GraphCanvas
          viewKey="overview"
          nodes={[partNode]}
          edges={[]}
          selectedNodeId={null}
          onSelectNode={() => {}}
          onSelectEdge={() => {}}
          onDrillNode={onDrillNode}
        />,
      );
    });

    const nodeEl = await vi.waitFor(() => {
      const el = container!.querySelector<HTMLElement>(".react-flow__node");
      if (!el) throw new Error("graph node not rendered");
      return el;
    });

    await act(async () => {
      nodeEl.dispatchEvent(new MouseEvent("dblclick", { bubbles: true, cancelable: true }));
    });

    expect(onDrillNode).toHaveBeenCalledTimes(1);
    expect(onDrillNode).toHaveBeenCalledWith(partNode);
  });
});
