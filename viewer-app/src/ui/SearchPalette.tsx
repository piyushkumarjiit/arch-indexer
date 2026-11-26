import { useEffect, useMemo, useRef, useState } from "react";
import type { OverviewData, SearchTuple } from "../types/bundle";

export interface SearchCandidate {
  id: string;
  label: string;
  kind: string;
  partId: string;
  route?: string;
}

export function buildSearchCandidates(
  overview: OverviewData | null,
  searchIndex: SearchTuple[] | null,
): SearchCandidate[] {
  const byId = new Map<string, SearchCandidate>();

  if (overview) {
    for (const part of overview.parts) {
      byId.set(part.id, {
        id: part.id,
        label: part.name,
        kind: `part:${part.kind}`,
        partId: part.id,
      });
    }
    for (const grp of overview.groups) {
      byId.set(grp.id, {
        id: grp.id,
        label: grp.name,
        kind: "group",
        partId: grp.part_id,
      });
    }
    for (const b of overview.boundaries) {
      byId.set(b.id, {
        id: b.id,
        label: b.label,
        kind: b.boundary,
        partId: "",
      });
    }
  }

  if (searchIndex) {
    for (const tuple of searchIndex) {
      const [id, label, kind, partId, route] = tuple;
      byId.set(id, { id, label, kind, partId, route });
    }
  }

  return Array.from(byId.values()).sort((a, b) => a.id.localeCompare(b.id));
}

export function filterSearchCandidates(
  candidates: SearchCandidate[],
  query: string,
  limit: number = 25,
): SearchCandidate[] {
  const q = query.trim().toLowerCase();
  if (!q) return candidates.slice(0, limit);

  const scored: Array<{ item: SearchCandidate; score: number }> = [];
  for (const c of candidates) {
    const label = c.label.toLowerCase();
    const id = c.id.toLowerCase();
    const route = (c.route ?? "").toLowerCase();
    let score = 0;
    if (label === q || route === q) score = 100;
    else if (label.startsWith(q) || route.startsWith(q)) score = 80;
    else if (label.includes(q) || route.includes(q)) score = 60;
    else if (id.includes(q)) score = 40;
    if (score > 0) {
      scored.push({ item: c, score });
    }
  }

  return scored
    .sort((a, b) => b.score - a.score || a.item.label.localeCompare(b.item.label))
    .slice(0, limit)
    .map((s) => s.item);
}

export function SearchPalette({
  open,
  overview,
  searchIndex,
  onClose,
  onSelectNode,
}: {
  open: boolean;
  overview: OverviewData | null;
  searchIndex: SearchTuple[] | null;
  onClose: () => void;
  onSelectNode: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setActiveIndex(0);
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  const candidates = useMemo(
    () => buildSearchCandidates(overview, searchIndex),
    [overview, searchIndex],
  );
  const matches = useMemo(
    () => filterSearchCandidates(candidates, query),
    [candidates, query],
  );

  if (!open) return null;

  return (
    <div
      className="arch-search-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="Search architecture"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="arch-search-modal">
        <div className="arch-search-input-row">
          <input
            ref={inputRef}
            type="text"
            className="arch-search-input"
            placeholder="Search parts, groups, files, symbols, routes..."
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActiveIndex(0);
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                onClose();
              } else if (e.key === "ArrowDown") {
                e.preventDefault();
                setActiveIndex((i) => Math.min(Math.max(0, matches.length - 1), i + 1));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setActiveIndex((i) => Math.max(0, i - 1));
              } else if (e.key === "Enter" && matches[activeIndex]) {
                e.preventDefault();
                onSelectNode(matches[activeIndex].id);
                onClose();
              }
            }}
          />
          <button type="button" className="arch-icon-btn" onClick={onClose} aria-label="Close search">
            Esc
          </button>
        </div>
        <div className="arch-search-results">
          {matches.length === 0 ? (
            <div className="arch-search-empty">No matching nodes</div>
          ) : (
            matches.map((m, idx) => (
              <button
                key={m.id}
                type="button"
                className={`arch-search-item ${idx === activeIndex ? "is-active" : ""}`}
                onClick={() => {
                  onSelectNode(m.id);
                  onClose();
                }}
              >
                <div className="arch-search-item-main">
                  <span className="arch-search-item-label">{m.label}</span>
                  <span className="arch-search-item-id">{m.route || m.id}</span>
                </div>
                <span className="arch-badge">{m.kind}</span>
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
