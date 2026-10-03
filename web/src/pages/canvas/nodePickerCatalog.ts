import { NODE_CATALOG } from './explorer/nodeCatalog';
const groups: Record<string, string[]> = {
  io: ['StartNode', 'EndNode'], ai: ['PromptNode', 'SubAgentNode'],
  control: ['ConditionNode', 'HumanApprovalNode', 'LoopBeginNode', 'LoopEndNode', 'ParallelStartNode', 'ParallelEndNode'],
  data: ['TransformNode', 'TemplateNode', 'TableReadNode', 'TableWriteNode'],
  integration: ['CodeNode', 'HTTPRequestNode'],
};
export const NODE_PICKER_GROUPS = [...Object.entries(groups).map(([id, types]) => ({ id, types: types.filter((type) => NODE_CATALOG.includes(type)) })),
  { id: 'other', types: NODE_CATALOG.filter((type) => !Object.values(groups).flat().includes(type)) }].filter((group) => group.types.length);
export const NODE_SEARCH_ALIASES: Record<string, string> = {
  StartNode: '开始 输入 start input', EndNode: '结束 输出 end output',
  PromptNode: '模型 提示词 大模型 LLM prompt model', SubAgentNode: '智能体 子代理 AI agent subagent',
  ConditionNode: '条件 分支 if else condition', HumanApprovalNode: '人工 审批 确认 human approval',
  LoopBeginNode: '循环 开始 loop begin', LoopEndNode: '循环 结束 loop end',
  ParallelStartNode: '并行 开始 parallel start', ParallelEndNode: '并行 结束 汇合 parallel end join',
  TransformNode: '数据 转换 transform', TemplateNode: '文本 模板 template',
  TableReadNode: '表格 读取 table read', TableWriteNode: '表格 写入 table write',
  CodeNode: '代码 执行 python code', HTTPRequestNode: '接口 请求 http api request',
};
export function availableNodePosition(center: { x: number; y: number }, nodes: Array<{ position: { x: number; y: number }; measured?: { width?: number; height?: number } }>) {
  const origin = { x: center.x - 120, y: center.y - 60 };
  const clear = (p: typeof origin) => nodes.every((node) =>
    p.x + 260 < node.position.x || p.x > node.position.x + (node.measured?.width ?? 240) + 20 ||
    p.y + 160 < node.position.y || p.y > node.position.y + (node.measured?.height ?? 140) + 20);
  for (let ring = 0; ring <= nodes.length + 1; ring++) {
    for (let y = -ring; y <= ring; y++) for (let x = -ring; x <= ring; x++) {
      if (Math.max(Math.abs(x), Math.abs(y)) !== ring) continue;
      const candidate = { x: origin.x + x * 300, y: origin.y + y * 200 };
      if (clear(candidate)) return candidate;
    }
  }
  return origin;
}
