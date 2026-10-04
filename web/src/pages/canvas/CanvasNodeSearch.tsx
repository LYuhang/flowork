import { useEffect, useRef, useState } from 'react';
import { getNodesBounds, getViewportForBounds, useNodes, useReactFlow } from '@xyflow/react';
import { Search } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Command, CommandInput, CommandItem, CommandList, CommandEmpty } from '@/components/ui/command';
import { useUIStore } from '@/stores/ui';
import { useExecStreamStore } from '@/stores/exec-stream';
import { useExecutionHistory } from './ExecutionHistoryContext';
import { NODE_LABELS } from './nodes/NODE_TYPES';

export function CanvasNodeSearch() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('all');
  const nodes = useNodes();
  const { setNodes, setEdges, getNode, setViewport } = useReactFlow();
  const button = useRef<HTMLButtonElement>(null);
  const focusTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (focusTimer.current) clearTimeout(focusTimer.current); }, []);
  const perNode = useExecStreamStore(s => s.perNode);
  const history = useExecutionHistory();
  const filters = ['all', 'error', 'running', 'waiting_approval'];
  return <Popover open={open} onOpenChange={setOpen}>
    <PopoverTrigger asChild><Button ref={button} variant="ghost" size="icon" className="h-8 w-8 rounded-full" aria-label={t('canvasNavigation.find')}><Search className="h-4 w-4" /></Button></PopoverTrigger>
    <PopoverContent side="top" collisionPadding={12} className="w-80 max-w-[calc(100vw-24px)] p-1">
      <Command>
        <CommandInput placeholder={t('canvasNavigation.placeholder')} />
        <div className="flex flex-wrap gap-1 p-2" role="group" aria-label={t('canvasNavigation.status')}>
          {filters.map(value => <Button key={value} size="sm" variant={filter === value ? 'secondary' : 'ghost'} aria-pressed={filter === value} onClick={() => setFilter(value)}>{t(`canvasNavigation.${value}`)}</Button>)}
        </div>
        <CommandList className="max-h-64">
          <CommandEmpty>{t('canvasNavigation.empty')}</CommandEmpty>
          {nodes.filter(node => {
            const waiting = history?.detail.approvals.some(a => a.node_id === node.id && a.status === 'pending');
            const status = waiting ? 'waiting_approval' : perNode[node.id]?.status ?? history?.latestNodeEvents[node.id]?.status;
            return filter === 'all' || status === filter;
          }).map(node => {
            const type = String(node.data.node_type ?? '');
            const label = t(`nodes_palette.label.${type}`, NODE_LABELS[type] ?? type);
            return <CommandItem key={node.id} value={node.id} keywords={[String(node.data.node_name ?? ''), type, label]} onSelect={() => {
              setNodes(items => items.map(n => ({ ...n, selected: n.id === node.id })));
              setEdges(items => items.map(e => ({ ...e, selected: false })));
              useUIStore.getState().requestInspectorTab('auto', 'node');
              useUIStore.getState().setInspectorOpen(true);
              setOpen(false);
              if (focusTimer.current) clearTimeout(focusTimer.current);
              focusTimer.current = setTimeout(() => {
                const current = getNode(node.id);
                const canvas = button.current?.closest('[data-node-picker-surface]')?.getBoundingClientRect();
                if (!current || !canvas) return;
                const inspector = document.querySelector('[data-workflow-inspector]')?.getBoundingClientRect();
                // On phones the inspector overlays the bottom of the canvas.
                const height = inspector && inspector.left < canvas.right && inspector.right > canvas.left && inspector.top > canvas.top
                  ? Math.min(canvas.height, inspector.top - canvas.top) : canvas.height;
                const bounds = getNodesBounds([current]);
                // Focus the card itself; a tall output preview can remain below it.
                bounds.height = Math.min(bounds.height, 160);
                void setViewport(getViewportForBounds(bounds, canvas.width, height, 0.7, 1, 0.3), { duration: 200 });
              }, 250);
            }}><div className="min-w-0"><div className="truncate">{String(node.data.node_name ?? node.id)}</div><div className="truncate text-xs text-muted-foreground">{node.id} · {label}</div></div></CommandItem>;
          })}
        </CommandList>
      </Command>
    </PopoverContent>
  </Popover>;
}
