import type { LayerFilter, LayerName } from "../types/bundle";

export interface LayerTogglesProps {
  layers: LayerFilter;
  hasRuntimeOverlay: boolean;
  exactOnly: boolean;
  onToggleLayer: (layer: LayerName) => void;
  onToggleExactOnly: (exactOnly: boolean) => void;
}

const LAYER_META: Array<{ key: LayerName; label: string; hint: string }> = [
  { key: "control", label: "Control", hint: "calls, registers, boots, implements" },
  { key: "data", label: "Data", hint: "reads, writes, publishes, resources, dataflow" },
  { key: "integration", label: "Integration", hint: "http_request, sidecars, external sinks" },
  { key: "runtime", label: "Runtime", hint: "OTLP runtime overlay & drift edges" },
];

export function LayerToggles({
  layers,
  hasRuntimeOverlay,
  exactOnly,
  onToggleLayer,
  onToggleExactOnly,
}: LayerTogglesProps) {
  return (
    <div className="arch-layer-toggles" role="group" aria-label="Graph layers">
      <span className="arch-toolbar-label">Layers</span>
      {LAYER_META.map((item) => {
        const isRuntime = item.key === "runtime";
        const hidden = isRuntime && !hasRuntimeOverlay;
        return (
          <label
            key={item.key}
            className={`arch-layer-toggle ${layers[item.key] ? "is-on" : ""}`}
            data-layer={item.key}
            hidden={hidden}
            style={hidden ? { display: "none" } : undefined}
            title={item.hint}
          >
            <input
              type="checkbox"
              checked={layers[item.key]}
              onChange={() => onToggleLayer(item.key)}
            />
            <span>{item.label}</span>
          </label>
        );
      })}
      <label
        className={`arch-layer-toggle arch-layer-toggle--exact ${exactOnly ? "is-on" : ""}`}
        title="Hide heuristic/approx edges and nodes"
      >
        <input
          type="checkbox"
          checked={exactOnly}
          onChange={(e) => onToggleExactOnly(e.target.checked)}
        />
        <span>Exact only</span>
      </label>
    </div>
  );
}
