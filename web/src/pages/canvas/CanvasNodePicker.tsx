import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useNodes, useReactFlow } from '@xyflow/react';
import { Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Popover, PopoverAnchor, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { useUIStore } from '@/stores/ui';
import { useCanvasViewport } from './CanvasViewportContext';
import { NodePickerContext, type NodePickerPoint } from './NodePickerContext';
import { NODE_CATALOG, nodeDescKey, nodeInsertPayload } from './explorer/nodeCatalog';
import { NODE_COLORS, NODE_ICONS, NODE_LABELS, DEFAULT_NODE_ICON } from './nodes/NODE_TYPES';
import { availableNodePosition, NODE_PICKER_GROUPS, NODE_SEARCH_ALIASES } from './nodePickerCatalog';
const recentKey = 'flowork:recent-node-types';
function readRecent(): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(recentKey) ?? '[]');
    return Array.isArray(value) ? [...new Set(value.filter((x): x is string => typeof x === 'string' && NODE_CATALOG.includes(x)))].slice(0, 4) : [];
  } catch { return []; }
}
export function CanvasNodePicker({ children, readOnly }: { children: ReactNode; readOnly: boolean }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [point, setPoint] = useState<NodePickerPoint | null>(null);
  const flowPoint = useRef<NodePickerPoint | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [recent, setRecent] = useState(readRecent);
  const nodes = useNodes();
  const { screenToFlowPosition, getNodes, setNodes } = useReactFlow();
  const { viewportCenterFlowPos } = useCanvasViewport();
  const input = useRef<HTMLInputElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (readOnly) setOpen(false); }, [readOnly]);
  useEffect(() => {
    if (!pendingId || !nodes.some((node) => node.id === pendingId)) return;
    setNodes((items) => items.map((node) => ({ ...node, selected: node.id === pendingId })));
    useUIStore.getState().requestInspectorTab('auto', 'node');
    useUIStore.getState().setInspectorOpen(true);
    setPendingId(null);
  }, [nodes, pendingId, setNodes]);
  const show = (anchor?: NodePickerPoint) => {
    if (readOnly || useUIStore.getState().canvasReadOnly) return;
    setPoint(anchor ?? null);
    flowPoint.current = anchor ? screenToFlowPosition(anchor) : null;
    setQuery(''); setOpen(true);
  };
  const insert = (type: string) => {
    if (readOnly || useUIStore.getState().canvasReadOnly) return;
    const store = useWorkflowEditStore.getState();
    if (!store.draft || !NODE_CATALOG.includes(type)) return;
    const previous = new Set(Object.keys(store.draft));
    const position = flowPoint.current ?? availableNodePosition(viewportCenterFlowPos() ?? { x: 0, y: 0 }, getNodes());
    store.addNode(nodeInsertPayload(type), position);
    const id = Object.keys(useWorkflowEditStore.getState().draft ?? {}).find((key) => !previous.has(key));
    if (id) setPendingId(id);
    const next = [type, ...recent.filter((item) => item !== type)].slice(0, 4);
    setRecent(next);
    try { localStorage.setItem(recentKey, JSON.stringify(next)); } catch { /* Optional preference. */ }
    setOpen(false);
  };
  const item = (type: string, prefix = '') => {
    const Icon = NODE_ICONS[type] ?? DEFAULT_NODE_ICON;
    const label = t(`nodes_palette.label.${type}`, NODE_LABELS[type] ?? type);
    const description = t(nodeDescKey(type), '');
    return <CommandItem key={prefix + type} value={prefix + type} keywords={[type, label, description, NODE_LABELS[type], NODE_SEARCH_ALIASES[type] ?? '']}
      data-node-picker-type={type} onSelect={() => insert(type)} className="cursor-pointer items-start gap-3 rounded-lg px-3 py-2.5">
      <span className="mt-0.5 rounded-md p-1.5" style={{ color: NODE_COLORS[type], backgroundColor: `${NODE_COLORS[type] ?? '#64748b'}12` }}><Icon className="h-4 w-4" aria-hidden="true" /></span>
      <span className="min-w-0"><span className="block text-[13px] font-medium">{label}</span><span className="block text-xs leading-5 text-muted-foreground">{description}</span></span>
    </CommandItem>;
  };
  return <NodePickerContext.Provider value={show}>
    <div className="relative h-full min-h-0 w-full" data-node-picker-surface>
      {children}
      {!readOnly && <Popover open={open} onOpenChange={(value) => { setOpen(value); if (!value) setQuery(''); }}>
        {point && <PopoverAnchor className="pointer-events-none fixed h-px w-px" style={{ left: point.x, top: point.y }} />}
        <div className="pointer-events-none absolute inset-x-0 bottom-5 z-20 flex justify-center">
          <PopoverTrigger asChild><Button ref={button} variant="outline" className="pointer-events-auto gap-2 rounded-full bg-popover px-5 shadow-md" data-action="add-node"
            onClick={() => { setPoint(null); flowPoint.current = null; setQuery(''); }}>
            <Plus className="h-4 w-4" />{t('nodePicker.add', 'Add node')}
          </Button></PopoverTrigger>
        </div>
        <PopoverContent side={point ? 'right' : 'top'} align={point ? 'start' : 'center'} sideOffset={10} collisionPadding={12}
          className="w-[380px] max-w-[calc(100vw-24px)] overflow-hidden rounded-xl p-0 shadow-xl" aria-label={t('nodePicker.add', 'Add node')}
          onOpenAutoFocus={(event) => { event.preventDefault(); input.current?.focus(); }}
          onCloseAutoFocus={(event) => { event.preventDefault(); button.current?.focus(); }}>
          <Command label={t('nodePicker.add', 'Add node')} loop>
            <CommandInput ref={input} value={query} onValueChange={setQuery} placeholder={t('nodePicker.search', 'Search nodes…')} aria-label={t('nodePicker.search', 'Search nodes…')} />
            <CommandList className="max-h-[min(420px,calc(var(--radix-popover-content-available-height)-60px))] p-1.5">
              <CommandEmpty>{t('nodes_palette.no_match', 'No nodes match your filter.')}</CommandEmpty>
              {!query && recent.length > 0 && <CommandGroup heading={t('nodePicker.recent', 'Recently used')}>{recent.map((type) => item(type, 'recent-'))}</CommandGroup>}
              {NODE_PICKER_GROUPS.map((group) => <CommandGroup key={group.id} heading={t(`nodePicker.group.${group.id}`)}>{group.types.map((type) => item(type))}</CommandGroup>)}
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>}
    </div>
  </NodePickerContext.Provider>;
}
