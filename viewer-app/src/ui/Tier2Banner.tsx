import { useState } from "react";

export function Tier2Banner({
  tier2Modules,
  onSelectModule,
}: {
  tier2Modules: string[];
  onSelectModule?: (modId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  if (!tier2Modules || tier2Modules.length === 0) {
    return null;
  }

  const count = tier2Modules.length;
  const label = `${count} ${count === 1 ? "module" : "modules"} indexed at Tier 2: edges may be incomplete`;

  return (
    <div className="arch-tier2-banner" role="status" data-testid="tier2-banner">
      <div className="arch-tier2-main">
        <span className="arch-tier2-icon" aria-hidden="true">!</span>
        <span>{label}</span>
        <button
          type="button"
          className="arch-link-btn"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "Hide modules" : "Show modules"}
        </button>
      </div>
      {expanded && (
        <div className="arch-tier2-list">
          {tier2Modules.map((modId) => (
            <button
              key={modId}
              type="button"
              className="arch-chip"
              onClick={() => onSelectModule?.(modId)}
            >
              {modId}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
