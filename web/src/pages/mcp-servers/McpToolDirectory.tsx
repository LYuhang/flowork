import { useDeferredValue, useEffect, useMemo, useState } from 'react';
import { Braces, Search, Wrench } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

export interface McpToolDocument {
  name: string;
  description?: string | null;
  input_schema?: unknown;
  annotations?: {
    readOnlyHint?: boolean;
    destructiveHint?: boolean;
    idempotentHint?: boolean;
    openWorldHint?: boolean;
  } | null;
}

interface SchemaProperty {
  type?: unknown;
  description?: unknown;
  title?: unknown;
  default?: unknown;
}

function objectSchema(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function typeLabel(value: unknown): string {
  if (Array.isArray(value)) return value.map(String).join(' | ');
  return typeof value === 'string' ? value : 'any';
}

export function McpToolDirectory({
  tools,
  className,
}: {
  tools: readonly McpToolDocument[];
  className?: string;
}) {
  const { t } = useTranslation();
  const [search, setSearch] = useState('');
  const deferredSearch = useDeferredValue(search.trim().toLocaleLowerCase());
  const filteredTools = useMemo(() => {
    if (!deferredSearch) return [...tools];
    return tools.filter((tool) => (
      tool.name.toLocaleLowerCase().includes(deferredSearch)
      || (tool.description ?? '').toLocaleLowerCase().includes(deferredSearch)
    ));
  }, [deferredSearch, tools]);
  const [selectedName, setSelectedName] = useState(tools[0]?.name ?? '');

  useEffect(() => {
    if (filteredTools.some((tool) => tool.name === selectedName)) return;
    queueMicrotask(() => setSelectedName(filteredTools[0]?.name ?? ''));
  }, [filteredTools, selectedName]);

  const selectedTool = filteredTools.find((tool) => tool.name === selectedName)
    ?? filteredTools[0]
    ?? null;
  const schema = objectSchema(selectedTool?.input_schema);
  const properties = objectSchema(schema?.properties) ?? {};
  const required = new Set(
    Array.isArray(schema?.required) ? schema.required.map(String) : [],
  );

  return (
    <div
      className={cn(
        'grid min-h-0 min-w-0 flex-1 grid-cols-[minmax(0,1fr)] overflow-hidden bg-surface-work md:grid-cols-[minmax(220px,300px)_minmax(0,1fr)]',
        className,
      )}
      data-testid="mcp-tool-directory"
    >
      <aside className="flex min-h-0 min-w-0 flex-col border-b border-edge-subtle bg-surface-sunken/55 md:border-b-0 md:border-r">
        <div className="shrink-0 border-b border-edge-subtle p-3">
          <div className="mb-2 flex items-center justify-between gap-3 px-1">
            <span className="text-xs font-semibold uppercase tracking-[0.08em] text-content-tertiary">
              {t('mcp.detail.tools.directory', 'Tool directory')}
            </span>
            <span className="rounded-full bg-surface-raised px-2 py-0.5 text-xs tabular-nums text-content-tertiary ring-1 ring-inset ring-edge-subtle">
              {filteredTools.length}
            </span>
          </div>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-content-tertiary" />
            <Input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              className="h-8 bg-surface-raised pl-8 text-sm"
              placeholder={t('mcp.detail.tools.search', 'Search tools')}
              aria-label={t('mcp.detail.tools.search', 'Search tools')}
            />
          </div>
        </div>
        <div className="max-h-56 min-h-0 overflow-y-auto overscroll-contain p-1.5 md:max-h-none md:flex-1">
          {filteredTools.map((tool) => (
            <button
              key={tool.name}
              type="button"
              onClick={() => setSelectedName(tool.name)}
              className={cn(
                'group flex w-full items-start gap-2.5 rounded-md px-2.5 py-2 text-left transition-colors',
                selectedTool?.name === tool.name
                  ? 'bg-surface-raised text-content-primary shadow-sm ring-1 ring-inset ring-edge-subtle'
                  : 'text-content-secondary hover:bg-surface-hover hover:text-content-primary',
              )}
              aria-pressed={selectedTool?.name === tool.name}
            >
              <Wrench className="mt-0.5 h-3.5 w-3.5 shrink-0 text-content-tertiary group-aria-pressed:text-primary" />
              <span className="min-w-0">
                <span className="block truncate font-mono text-xs font-semibold">{tool.name}</span>
                <span className="mt-0.5 block truncate text-xs text-content-tertiary">
                  {tool.description || t('mcp.no_description', 'No description provided.')}
                </span>
              </span>
            </button>
          ))}
          {filteredTools.length === 0 ? (
            <div className="px-3 py-8 text-center text-sm text-muted-foreground">
              {t('mcp.detail.tools.no_match', 'No tools match your search.')}
            </div>
          ) : null}
        </div>
      </aside>

      <section className="min-h-0 min-w-0 overflow-y-auto overscroll-contain">
        {selectedTool ? (
          <div className="mx-auto w-full max-w-4xl p-5 sm:p-7">
            <div className="flex items-start gap-3 border-b border-edge-subtle pb-5">
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary ring-1 ring-inset ring-primary/15">
                <Wrench className="h-4 w-4" />
              </div>
              <div className="min-w-0 flex-1">
                <code className="break-all text-base font-semibold text-content-primary">{selectedTool.name}</code>
                <p className="mt-1.5 whitespace-pre-line text-sm leading-6 text-muted-foreground">
                  {selectedTool.description || t('mcp.no_description', 'No description provided.')}
                </p>
              </div>
            </div>

            {selectedTool.annotations ? (
              <div className="mt-4 flex flex-wrap gap-1.5 text-xs">
                {selectedTool.annotations.readOnlyHint ? <span className="rounded-full bg-state-success/10 px-2.5 py-1 text-state-success">{t('mcp.detail.tools.read_only', 'Read only')}</span> : null}
                {selectedTool.annotations.destructiveHint ? <span className="rounded-full bg-state-danger/10 px-2.5 py-1 text-state-danger">{t('mcp.detail.tools.mutates', 'Changes data')}</span> : null}
                {selectedTool.annotations.idempotentHint ? <span className="rounded-full bg-primary/10 px-2.5 py-1 text-primary">{t('mcp.detail.tools.idempotent', 'Idempotent')}</span> : null}
                {selectedTool.annotations.openWorldHint ? <span className="rounded-full bg-state-warning/10 px-2.5 py-1 text-state-warning">{t('mcp.detail.tools.external', 'External access')}</span> : null}
              </div>
            ) : null}

            <div className="mt-6 flex items-center gap-2">
              <Braces className="h-4 w-4 text-primary" />
              <h3 className="text-sm font-semibold text-content-primary">
                {t('mcp.detail.tools.inputs', 'Input parameters')}
              </h3>
            </div>
            {Object.keys(properties).length > 0 ? (
              <div className="mt-3 overflow-hidden rounded-lg border border-edge-subtle">
                {Object.entries(properties).map(([name, raw], index) => {
                  const property = objectSchema(raw) as SchemaProperty | null;
                  return (
                    <div
                      key={name}
                      className={cn(
                        'grid gap-2 px-3.5 py-3 sm:grid-cols-[minmax(9rem,0.35fr)_minmax(0,1fr)]',
                        index > 0 && 'border-t border-edge-subtle',
                      )}
                    >
                      <div className="min-w-0">
                        <code className="break-all text-xs font-semibold text-content-primary">{name}</code>
                        {required.has(name) ? <span className="ml-1.5 text-xs text-destructive">{t('common.required', 'Required')}</span> : null}
                        <div className="mt-0.5 text-xs text-content-tertiary">{typeLabel(property?.type)}</div>
                      </div>
                      <p className="text-sm leading-5 text-muted-foreground">
                        {typeof property?.description === 'string'
                          ? property.description
                          : typeof property?.title === 'string'
                            ? property.title
                            : t('mcp.detail.tools.no_parameter_description', 'No parameter description provided.')}
                      </p>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="mt-3 rounded-lg border border-dashed border-edge-subtle px-4 py-5 text-sm text-muted-foreground">
                {t('mcp.detail.tools.no_parameters', 'No user parameters')}
              </div>
            )}
          </div>
        ) : null}
      </section>
    </div>
  );
}
