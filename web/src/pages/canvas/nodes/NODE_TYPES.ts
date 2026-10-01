/**
 * Static per-node-type metadata: header colors and human-readable labels.
 *
 * `NODE_COLORS` is the canonical palette ported verbatim from
 * the legacy `frontend/constants.ts`. We keep it as inline hex (rather
 * than Tailwind tokens) because each node owns its own background hue
 * outside the global semantic palette, and we render it via inline
 * `backgroundColor` style — see `CustomNode.tsx`.
 *
 * `NODE_LABELS` is a placeholder English-only mapping. T18 swaps it for
 * an `i18next`-backed lookup; until then, `CustomNode` falls back to
 * the raw `node_type` key when a label is missing.
 */

export const NODE_COLORS: Record<string, string> = {
  HumanApprovalNode: '#d97706',
  StartNode: '#10b981',
  EndNode: '#ef4444',
  CodeNode: '#3b82f6',
  PromptNode: '#8b5cf6',
  ParallelStartNode: '#06b6d4',
  ParallelEndNode: '#0ea5e9',
  ConditionNode: '#f97316',
  LoopBeginNode: '#ec4899',
  LoopEndNode: '#f43f5e',
  HTTPRequestNode: '#14b8a6',
  TransformNode: '#a855f7',
  TemplateNode: '#f472b6',
  TableReadNode: '#22d3ee',
  TableWriteNode: '#34d399',
  SubAgentNode: '#6366f1',
};

export const NODE_LABELS: Record<string, string> = {
  HumanApprovalNode: 'Human approval',
  StartNode: 'Start',
  EndNode: 'End',
  CodeNode: 'Code',
  PromptNode: 'Prompt',
  ParallelStartNode: 'Parallel start',
  ParallelEndNode: 'Parallel end',
  ConditionNode: 'Condition',
  LoopBeginNode: 'Loop begin',
  LoopEndNode: 'Loop end',
  HTTPRequestNode: 'HTTP request',
  TransformNode: 'Transform',
  TemplateNode: 'Template',
  TableReadNode: 'Table read',
  TableWriteNode: 'Table write',
  SubAgentNode: 'SubAgent',
};

/**
 * Node types kept for back-compat (an existing workflow that still contains one
 * renders + runs fine) but NO LONGER offered when ADDING a node — they are
 * deprecated for general use. Currently empty; the set is retained so
 * re-deprecating a type later is a one-line edit, and `ADDABLE_NODE_TYPES`
 * keeps filtering against it.
 */
export const HIDDEN_NODE_TYPES: ReadonlySet<string> = new Set<string>();

/** Node types a user may ADD from a palette (canonical add list, minus hidden). */
export const ADDABLE_NODE_TYPES: readonly string[] = Object.keys(NODE_LABELS).filter(
  (nodeType) => !HIDDEN_NODE_TYPES.has(nodeType),
);

/** Default header color when a node_type is unknown. */
export const DEFAULT_NODE_COLOR = '#64748b';

/**
 * Per-node-type icon, used by the compact canvas card header (and reusable by
 * any palette that wants a glyph instead of a color dot). There was no prior
 * icon map in the repo — the Explorer palette (`NodeCard`) used a
 * color dot only — so this is the single source of truth for node-type glyphs.
 * Each entry is a `lucide-react` icon component chosen to read at a glance for
 * non-technical users; `DEFAULT_NODE_ICON` covers unknown types.
 */
import {
  Bot,
  Box,
  Braces,
  Code2,
  Flag,
  FlagOff,
  GitBranch,
  GitMerge,
  Globe,
  type LucideIcon,
  Repeat,
  RotateCcw,
  Shuffle,
  Sparkles,
  StickyNote,
  Table,
  TableProperties,
  UserCheck,
} from 'lucide-react';

export const DEFAULT_NODE_ICON: LucideIcon = Box;

export const NODE_ICONS: Record<string, LucideIcon> = {
  HumanApprovalNode: UserCheck,
  StartNode: Flag,
  EndNode: FlagOff,
  CodeNode: Code2,
  PromptNode: Sparkles,
  ParallelStartNode: GitBranch,
  ParallelEndNode: GitMerge,
  ConditionNode: Shuffle,
  LoopBeginNode: Repeat,
  LoopEndNode: RotateCcw,
  HTTPRequestNode: Globe,
  TransformNode: Braces,
  TemplateNode: StickyNote,
  TableReadNode: Table,
  TableWriteNode: TableProperties,
  SubAgentNode: Bot,
};
