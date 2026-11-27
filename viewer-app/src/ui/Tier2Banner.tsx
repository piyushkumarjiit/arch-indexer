import { useState } from "react";

const MAX_VISIBLE_TIER2_CHIPS = 12;

export function Tier2Banner({
  tier2Modules,
  onSelectModule,
}: {
  tier2Modules: string[];
  onSelectModule?: (modId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [collapsed, setCollapsed] = useState(false);

  if (!tier2Modules || tier2Modules.length === 0) {
    return null;
  }

  const count = tier2Modules.length;
  const label = `${count} ${count === 1 ? "module" : "modules"} indexed at Tier 2: edges may be incomplete`;

  if (collapsed) {
    return (
      <div className="arch-tier2-collapsed" role="status" data-testid="tier2-banner">
        <button
          type="button"
          className="arch-tier2-pill"
          data-testid="tier2-pill"
          onClick={() => setCollapsed(false)}
          title={label}
        >
          <span className="arch-tier2-icon" aria-hidden="true">!</span>
          <span>{count} Tier-2</span>
        </button>
      </div>
    );
  }

  const visibleModules = tier2Modules.slice(0, MAX_VISIBLE_TIER2_CHIPS);
  const remaining = Math.max(0, tier2Modules.length - visibleModules.length);

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
        <button
          type="button"
          className="arch-mini-btn arch-tier2-collapse-btn"
          data-testid="tier2-collapse-btn"
          onClick={() => setCollapsed(true)}
          title="Collapse to persistent Tier-2 indicator pill"
        >
          Collapse
        </button>
      </div>
      {expanded && (
        <div className="arch-tier2-list" data-testid="tier2-list">
          {visibleModules.map((modId) => (
            <button
              key={modId}
              type="button"
              className="arch-chip"
              onClick={() => onSelectModule?.(modId)}
            >
              {modId}
            </button>
          ))}
          {remaining > 0 && (
            <span className="arch-badge arch-tier2-more" data-testid="tier2-more">
              +{remaining} more
            </span>
          )}
        </div>
      )}
    </div>
  );
}
