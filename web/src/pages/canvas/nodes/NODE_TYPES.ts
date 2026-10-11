/** Node identity colors follow paired theme tokens. Selection and execution
 * states remain separate from type identity. All canvas insertion surfaces
 * share this registry, including their inline icon/background styles. */
export const NODE_COLORS: Record<string, string> = {
  HumanApprovalNode: 'var(--node-human-approval-node)',
  StartNode: 'var(--node-start-node)',
  EndNode: 'var(--node-end-node)',
  CodeNode: 'var(--node-code-node)',
  PromptNode: 'var(--node-prompt-node)',
  ParallelStartNode: 'var(--node-parallel-start-node)',
  ParallelEndNode: 'var(--node-parallel-end-node)',
  ConditionNode: 'var(--node-condition-node)',
  LoopBeginNode: 'var(--node-loop-begin-node)',
  LoopEndNode: 'var(--node-loop-end-node)',
  HTTPRequestNode: 'var(--node-h-t-t-p-request-node)',
  TransformNode: 'var(--node-transform-node)',
  TemplateNode: 'var(--node-template-node)',
  TableReadNode: 'var(--node-table-read-node)',
  TableWriteNode: 'var(--node-table-write-node)',
  SubAgentNode: 'var(--node-sub-agent-node)',
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
export const DEFAULT_NODE_COLOR = 'var(--node-unknown)';

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
