import type { ComponentType } from 'react';
import type { NodeConfigEditorProps } from './config-editors/types';
import { StartNodeEditor } from './config-editors/StartNodeEditor';
import { EndNodeEditor } from './config-editors/EndNodeEditor';
import { CodeNodeEditor } from './config-editors/CodeNodeEditor';
import { PromptNodeEditor } from './config-editors/PromptNodeEditor';
import { ParallelStartNodeEditor } from './config-editors/ParallelStartNodeEditor';
import { ParallelEndNodeEditor } from './config-editors/ParallelEndNodeEditor';
import { ConditionNodeEditor } from './config-editors/ConditionNodeEditor';
import { LoopBeginNodeEditor } from './config-editors/LoopBeginNodeEditor';
import { LoopEndNodeEditor } from './config-editors/LoopEndNodeEditor';
import { HTTPRequestNodeEditor } from './config-editors/HTTPRequestNodeEditor';
import { TransformNodeEditor } from './config-editors/TransformNodeEditor';
import { TemplateNodeEditor } from './config-editors/TemplateNodeEditor';
import { TableReadNodeEditor } from './config-editors/TableReadNodeEditor';
import { TableWriteNodeEditor } from './config-editors/TableWriteNodeEditor';
import { SubAgentNodeEditor } from './config-editors/SubAgentNodeEditor';
import { HumanApprovalNodeEditor } from './config-editors/HumanApprovalNodeEditor';

export const NODE_CONFIG_EDITORS: Record<string, ComponentType<NodeConfigEditorProps>> = {
  StartNode: StartNodeEditor,
  EndNode: EndNodeEditor,
  CodeNode: CodeNodeEditor,
  PromptNode: PromptNodeEditor,
  ParallelStartNode: ParallelStartNodeEditor,
  ParallelEndNode: ParallelEndNodeEditor,
  ConditionNode: ConditionNodeEditor,
  LoopBeginNode: LoopBeginNodeEditor,
  LoopEndNode: LoopEndNodeEditor,
  HTTPRequestNode: HTTPRequestNodeEditor,
  TransformNode: TransformNodeEditor,
  TemplateNode: TemplateNodeEditor,
  TableReadNode: TableReadNodeEditor,
  TableWriteNode: TableWriteNodeEditor,
  SubAgentNode: SubAgentNodeEditor,
  HumanApprovalNode: HumanApprovalNodeEditor,
};

const CONFIGLESS_NODE_TYPES = new Set(['StartNode', 'EndNode', 'LoopEndNode']);

export function nodeTypeHasConfig(nodeType: string): boolean {
  return nodeType in NODE_CONFIG_EDITORS && !CONFIGLESS_NODE_TYPES.has(nodeType);
}
