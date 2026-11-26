import type { DeltaInfo, OverviewData, PartChunk } from "../types/bundle";

export type ChangeKind = "new" | "changed";

export interface ChangeItem {
  id: string;
  label: string;
  file: string;
  partId?: string;
  change: ChangeKind;
}

const trimTrailingSlashes = (p: string): string => p.replace(/\/+$/, "");

/** True when `file` is `dir` itself or lives somewhere beneath it. */
const isUnder = (file: string, dir: string): boolean =>
  file === dir || file.startsWith(`${dir}/`);

/** New files take precedence over changed ones; `null` when neither matches. */
function classify(
  delta: DeltaInfo,
  predicate: (file: string) => boolean,
): ChangeKind | null {
  if (delta.new_files.some(predicate)) return "new";
  if (delta.changed_files.some(predicate)) return "changed";
  return null;
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
  return classify(delta, (f) => f === filePath);
}

export function getPartChangeStatus(
  partId: string,
  overview: OverviewData | null,
): ChangeKind | null {
  const delta = overview?.delta;
  if (!overview || !delta) return null;
  const part = overview.parts.find((p) => p.id === partId);
  if (!part) return null;

  const root = part.root ? trimTrailingSlashes(part.root) : "";
  const evidenceFiles = new Set(part.evidence.map((e) => e.file));
  const groupDirs = overview.groups
    .filter((g) => g.part_id === partId)
    .map((g) => trimTrailingSlashes(g.path))
    .filter((dir) => dir !== "");

  return classify(
    delta,
    (file) =>
      evidenceFiles.has(file) ||
      (root !== "" && isUnder(file, root)) ||
      groupDirs.some((dir) => isUnder(file, dir)),
  );
}

export function getGroupChangeStatus(
  groupId: string,
  overview: OverviewData | null,
): ChangeKind | null {
  const delta = overview?.delta;
  if (!overview || !delta) return null;
  const group = overview.groups.find((g) => g.id === groupId);
  if (!group) return null;
  const dir = trimTrailingSlashes(group.path);
  if (!dir) return null;
  return classify(delta, (file) => isUnder(file, dir));
}

export function collectChangeItems(
  overview: OverviewData | null,
  chunks: Record<string, PartChunk>,
): ChangeItem[] {
  const delta = overview?.delta;
  if (!overview || !delta) return [];

  const owningPart = (file: string): string | undefined => {
    const hit = Object.entries(chunks).find(([, chunk]) =>
      chunk.files.some((f) => f.path === file),
    );
    if (hit) return hit[0];
    return overview.groups.find((g) => g.path && isUnder(file, trimTrailingSlashes(g.path)))
      ?.part_id;
  };

  const byId = new Map<string, ChangeItem>();
  const tagged: Array<[string, ChangeKind]> = [
    ...delta.new_files.map((f): [string, ChangeKind] => [f, "new"]),
    ...delta.changed_files.map((f): [string, ChangeKind] => [f, "changed"]),
  ];
  for (const [file, change] of tagged) {
    const id = `file:${file}`;
    if (byId.has(id)) continue;
    byId.set(id, {
      id,
      label: file.split("/").pop() || file,
      file,
      partId: owningPart(file),
      change,
    });
  }

  return [...byId.values()].sort((a, b) => a.file.localeCompare(b.file));
}

const BADGE_TITLES: Record<ChangeKind, string> = {
  new: "New since last index",
  changed: "Changed since last index",
};

export function ChangeBadge({ status }: { status?: ChangeKind | null }) {
  if (!status) return null;
  return (
    <span
      className={`arch-badge arch-badge--${status}`}
      data-testid={`change-badge-${status}`}
      title={BADGE_TITLES[status]}
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
  const className = active ? "arch-btn arch-btn--active" : "arch-btn";
  return (
    <button
      type="button"
      id="btn-recent"
      className={className}
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
        {items.map((item) => (
          <button
            key={item.id}
            type="button"
            className="arch-recent-item"
            onClick={() => onSelect(item.id)}
          >
            <ChangeBadge status={item.change} />
            <span className="arch-recent-path">{item.file}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
