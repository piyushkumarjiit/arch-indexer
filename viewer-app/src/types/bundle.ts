export type Confidence = "exact" | "heuristic" | "runtime";

export type EvidenceSource =
  | "scip"
  | "tree-sitter"
  | "infra"
  | "config"
  | "rule"
  | "runtime"
  | "sarif";

export interface Evidence {
  file: string;
  line: number;
  col?: number;
  end_line?: number;
  source: EvidenceSource | string;
  rule_id?: string;
}

export type PartKind =
  | "service"
  | "ui"
  | "worker"
  | "scripts"
  | "library"
  | "infra"
  | string;

export type BoundaryCategory = "datastore" | "sink" | "sidecar" | "resource" | string;

export type EndReason =
  | "boundary"
  | "resource"
  | "no-boundary-detected"
  | "depth-cap"
  | "no-outgoing-edges";

export type EdgeKind =
  | "calls"
  | "implements"
  | "registers"
  | "boots"
  | "http_request"
  | "reads"
  | "writes"
  | "publishes"
  | "dataflow"
  | "runtime"
  | string;

export interface TierSummary {
  scip_files: number;
  treesitter_files: number;
  total_files: number;
  excluded_test_files: number;
  tier2_modules: string[];
}

export interface PartCounts {
  files: number;
  loc: number;
  entrypoints: number;
  symbols: number;
}

export interface OverviewPart {
  id: string;
  name: string;
  kind: PartKind;
  root: string;
  group_ids: string[];
  counts: PartCounts;
  evidence: Evidence[];
}

export interface OverviewGroup {
  id: string;
  name: string;
  part_id: string;
  path: string;
  language: string;
  tier: string;
  tier_mix?: Record<string, number>;
  counts: Partial<PartCounts> & { files: number };
}

export interface OverviewBoundary {
  id: string;
  boundary: BoundaryCategory;
  kind: string;
  label: string;
  rule_id: string;
  confidence: Confidence;
  evidence: Evidence[];
}

export interface OverviewEdge {
  source: string;
  target: string;
  kinds: Record<string, number>;
  evidence_sample: Evidence[];
}

export interface FlowIndexEntry {
  id: string;
  entry_id: string;
  part_id: string;
  title_ref?: string;
  step_count: number;
  ends_at: string;
  end_reason: EndReason;
}

export interface DeltaInfo {
  changed_files: string[];
  new_files: string[];
}

export interface ChunkMeta {
  file: string;
  sha256: string;
  bytes: number;
}

export interface OverviewData {
  schema_version: string;
  repo_name: string;
  generated_at_commit: string | null;
  include_tests: boolean;
  tier_summary: TierSummary;
  parts: OverviewPart[];
  groups: OverviewGroup[];
  boundaries: OverviewBoundary[];
  edges: OverviewEdge[];
  flow_index: FlowIndexEntry[];
  delta: DeltaInfo;
  chunks: Record<string, ChunkMeta>;
  editor_url?: string;
}

export interface ChunkFileSymbol {
  name: string;
  kind: string;
  line: number;
}

export interface ChunkFileRow {
  path: string;
  module_id: string;
  language: string;
  tier: string;
  loc: number;
  is_infra: boolean;
  symbols: ChunkFileSymbol[];
}

export interface ChunkNode {
  id: string;
  kind: string;
  label: string;
  module_id: string;
  file: string;
  line: number;
  symbol?: string;
  tier: string;
  confidence: Confidence;
  evidence: Evidence[];
  docstring?: string;
  metadata?: Record<string, unknown>;
}

export interface ChunkEdge {
  source: string;
  target: string;
  kind: EdgeKind;
  file?: string;
  line?: number;
  tier?: string;
  confidence: Confidence;
  evidence: Evidence[];
  metadata?: Record<string, unknown>;
}

export interface FlowStep {
  node_id: string;
  edge_kind: string | null;
  file: string;
  line: number;
}

export interface ChunkFlow {
  id: string;
  entry_id: string;
  steps: FlowStep[];
  ends_at: string;
  end_reason: EndReason;
}

export interface PartChunk {
  schema_version: string;
  part_id: string;
  files: ChunkFileRow[];
  nodes: ChunkNode[];
  edges: ChunkEdge[];
  flows: ChunkFlow[];
}

export type SearchTuple = [
  id: string,
  label: string,
  kind: string,
  part_id: string,
  route?: string,
];

export interface DescriptionEntry {
  summary: string;
  details: string;
  citations: string[];
  span_hash: string;
  verified_by: {
    judge_model: string;
    verdict: "supported" | "partial";
    at_commit: string;
  } | null;
  status: "fresh" | "stale";
  title?: string;
  steps?: Array<{
    node_id: string;
    narration: string;
    citation: string;
  }>;
}

export interface DescriptionsData {
  schema_version: string;
  writer_model: string;
  entries: Record<string, DescriptionEntry>;
}

export interface RuntimeNodeMetric {
  span_count: number;
  error_count: number;
  p50_ms: number;
  p95_ms: number;
  match_tier: string;
  top_exceptions?: Array<[string, number]>;
  error_frames?: Array<{ file: string; line: number; fqname?: string }>;
}

export interface RuntimeEdgeMetric {
  span_count: number;
  error_count: number;
  p95_ms: number;
  match_tier: string;
  static: boolean;
}

export interface RuntimeOverlayData {
  schema_version: string;
  source: {
    kind: string;
    uri_redacted: string;
    service_names: string[];
  };
  captured_at: string;
  static_commit: string;
  node_metrics: Record<string, RuntimeNodeMetric>;
  edge_metrics: Record<string, RuntimeEdgeMetric>;
  traces: Array<{
    trace_id: string;
    root_node_id: string;
    spans: Array<{
      span_id: string;
      parent_id: string | null;
      node_id: string | null;
      name: string;
      start_ns: number;
      dur_ns: number;
      status: string;
      match_tier: string;
      exception_type?: string;
    }>;
  }>;
  unmatched: {
    span_count: number;
    top_names: Array<[string, number]>;
  };
}

export interface DataflowEdge {
  entry_id: string;
  source_fqname: string;
  sink_boundary_id: string;
  kind: "dataflow";
  confidence: Confidence;
  payload_hint: string;
  evidence: Evidence[];
  path: Array<{
    file: string;
    line: number;
    col?: number;
    fqname: string | null;
    note?: string;
  }>;
}

export interface DataflowData {
  schema_version: string;
  tool: string;
  tool_version: string;
  edges: DataflowEdge[];
}

export type ViewLevel = "overview" | "part" | "group" | "file" | "symbol";

export interface BreadcrumbItem {
  level: ViewLevel;
  id: string;
  label: string;
  partId?: string;
  groupId?: string;
  filePath?: string;
  symbolId?: string;
}

export type LayerName = "control" | "data" | "integration" | "runtime";

export type LayerFilter = Record<LayerName, boolean>;

export interface ViewNodeData {
  id: string;
  label: string;
  subtitle?: string;
  kind: string;
  badge?: string;
  tier?: string | null;
  confidence?: Confidence | null;
  changeStatus?: "new" | "changed" | null;
  drillable?: boolean;
  dimmed?: boolean;
  highlighted?: boolean;
  isCurrentStep?: boolean;
  partition?: 0 | 1 | 2;
  foldCount?: number;
  foldedIds?: string[];
  partId?: string;
  groupId?: string;
  filePath?: string;
  symbolId?: string;
  evidence?: Evidence[];
  metrics?: RuntimeNodeMetric | null;
  meta?: Record<string, unknown>;
}

export interface ViewEdgeData {
  id: string;
  source: string;
  target: string;
  kinds: string[];
  label?: string;
  count: number;
  confidence?: Confidence;
  evidence?: Evidence[];
  dimmed?: boolean;
  highlighted?: boolean;
  runtimeOnly?: boolean;
  isDataflow?: boolean;
}

declare global {
  interface Window {
    __ARCH_OVERVIEW__?: OverviewData | null;
    __ARCH_BASE_PATH__?: string;
    __ARCH_CHUNK__?: (partId: string, payload: PartChunk) => void;
    __ARCH_SEARCH__?: (entries: SearchTuple[]) => void;
    __ARCH_DESCRIPTIONS__?: DescriptionsData | null;
    __ARCH_RUNTIME__?: RuntimeOverlayData | null;
    __ARCH_DATAFLOW__?: DataflowData | null;
  }
}
