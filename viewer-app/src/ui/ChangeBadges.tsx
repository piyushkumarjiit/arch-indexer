/*
 * Portions adapted from floorplan (https://github.com/haydenschmitty/floorplan)
 * commit 94f81b4, assets/ui/25-recent.js. Copyright (c) 2026 Hayden Smith. MIT License.
 * See THIRD_PARTY_NOTICES.md.
 */

import type { DeltaInfo, OverviewData, PartChunk } from "../types/bundle";

export type ChangeKind = "new" | "changed";

export interface ChangeItem {
  id: string;
  label: string;
  file: string;
  partId?: string;
  change: ChangeKind;
}

export function buildDeltaFileSets(delta?: DeltaInfo | null): {
  newFiles: Set<string>;
  changedFiles: Set<string>;
} {
  return {
    newFiles: new Set(delta?.new_files ?? []),
    changedFiles: new Set(delta?.changed_files ?? []),
  };
}

export function getFileChangeStatus(
  filePath: string | undefined,
  delta?: DeltaInfo | null,
): ChangeKind | null {
  if (!filePath || !delta) return null;
  if (delta.new_files.includes(filePath)) return "new";
  if (delta.changed_files.includes(filePath)) return "changed";
  return null;
}

export function getPartChangeStatus(
  partId: string,
  overview: OverviewData | null,
): ChangeKind | null {
  if (!overview?.delta) return null;
  const part = overview.parts.find((p) => p.id === partId);
  if (!part) return null;
  const { newFiles, changedFiles } = buildDeltaFileSets(overview.delta);
  const rootPrefix = part.root ? `${part.root.replace(/\/+$/, "")}/` : "";
  const groupPaths = overview.groups
    .filter((g) => g.part_id === partId)
    .map((g) => g.path.replace(/\/+$/, ""));

  const matchesPart = (filePath: string) => {
    if (part.evidence.some((e) => e.file === filePath)) return true;
    if (rootPrefix && (filePath === part.root || filePath.startsWith(rootPrefix))) {
      return true;
    }
    return groupPaths.some((gp) => gp && (filePath === gp || filePath.startsWith(`${gp}/`)));
  };

  for (const f of newFiles) {
    if (matchesPart(f)) return "new";
  }
  for (const f of changedFiles) {
    if (matchesPart(f)) return "changed";
  }
  return null;
}

export function getGroupChangeStatus(
  groupId: string,
  overview: OverviewData | null,
): ChangeKind | null {
  if (!overview?.delta) return null;
  const group = overview.groups.find((g) => g.id === groupId);
  if (!group) return null;
  const prefix = group.path.replace(/\/+$/, "");
  if (!prefix) return null;
  const matches = (f: string) => f === prefix || f.startsWith(`${prefix}/`);
  if (overview.delta.new_files.some(matches)) return "new";
  if (overview.delta.changed_files.some(matches)) return "changed";
  return null;
}

export function collectChangeItems(
  overview: OverviewData | null,
  chunks: Record<string, PartChunk>,
): ChangeItem[] {
  if (!overview?.delta) return [];
  const items: ChangeItem[] = [];
  const seen = new Set<string>();

  const pushFile = (filePath: string, change: ChangeKind) => {
    const id = `file:${filePath}`;
    if (seen.has(id)) return;
    seen.add(id);
    let partId: string | undefined;
    for (const [pid, chunk] of Object.entries(chunks)) {
      if (chunk.files.some((f) => f.path === filePath)) {
        partId = pid;
        break;
      }
    }
    if (!partId) {
      const grp = overview.groups.find(
        (g) => g.path && (filePath === g.path || filePath.startsWith(`${g.path.replace(/\/+$/, "")}/`)),
      );
      partId = grp?.part_id;
    }
    items.push({
      id,
      label: filePath.split("/").pop() || filePath,
      file: filePath,
      partId,
      change,
    });
  };

  for (const f of overview.delta.new_files) pushFile(f, "new");
  for (const f of overview.delta.changed_files) pushFile(f, "changed");
  return items.sort((a, b) => a.file.localeCompare(b.file));
}

export function ChangeBadge({ status }: { status?: ChangeKind | null }) {
  if (!status) return null;
  return (
    <span
      className={`arch-badge arch-badge--${status}`}
      data-testid={`change-badge-${status}`}
      title={status === "new" ? "New since last index" : "Changed since last index"}
    >
      {status}
    </span>
  );
}

export function ChangeOverlayButton({
  count,
  active,
  onClick,
}: {
  count: number;
  active: boolean;
  onClick: () => void;
}) {
  if (count === 0) return null;
  return (
    <button
      type="button"
      id="btn-recent"
      className={`arch-btn ${active ? "arch-btn--active" : ""}`}
      onClick={onClick}
      title="Files changed or added since last index"
    >
      <span>Changes</span>
      <span className="arch-pill arch-pill--changed">{count}</span>
    </button>
  );
}

export function ChangeOverlayPanel({
  items,
  onSelect,
  onClose,
}: {
  items: ChangeItem[];
  onSelect: (id: string) => void;
  onClose: () => void;
}) {
  if (items.length === 0) return null;
  return (
    <div className="arch-recent-panel" role="region" aria-label="Changed files">
      <div className="arch-recent-header">
        <strong>Recent Changes ({items.length})</strong>
        <button type="button" className="arch-icon-btn" onClick={onClose} aria-label="Close changes">
          &times;
        </button>
      </div>
      <div className="arch-recent-list">
        {items.map((it) => (
          <button
            key={it.id}
            type="button"
            className="arch-recent-item"
            onClick={() => onSelect(it.id)}
          >
            <ChangeBadge status={it.change} />
            <span className="arch-recent-path">{it.file}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
