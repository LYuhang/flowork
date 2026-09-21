import {
  useDeferredValue,
  useMemo,
  useState,
} from 'react';
import * as Popover from '@radix-ui/react-popover';
import {
  ArrowLeft,
  Building2,
  Check,
  ChevronRight,
  Cloud,
  Cpu,
  KeyRound,
  Router,
  Search,
  UserRound,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type {
  AgentRuntimeCapabilities,
  RuntimeModelOption,
} from '@/lib/api/agent-runtime';
import { formatNumber } from '@/lib/format/number';
import type { AgentSettings } from '@/stores/agent-settings';
import { cn } from '@/lib/utils';

const NO_MODEL_AVAILABLE = '__no_model_available__';
const MODEL_RENDER_LIMIT = 80;

interface RuntimeModelPickerProps {
  capabilities?: AgentRuntimeCapabilities;
  loading: boolean;
  settings?: AgentSettings;
  onChange: (patch: Partial<AgentSettings>) => void;
  disabled?: boolean;
}

interface ModelSourceGroup {
  id: string;
  label: string;
  models: RuntimeModelOption[];
}

function sourceId(model: RuntimeModelOption): string {
  if (model.api_source) return model.api_source;
  if (model.id.startsWith('codex:account:')) return 'chatgpt_account';
  if (model.id.startsWith('codex:openrouter:')) return 'openrouter_oauth';
  if (model.id.startsWith('codex:managed:')) {
    return 'managed_api';
  }
  return 'manual';
}

function isFreeModel(model: RuntimeModelOption): boolean {
  const id = model.provider_model_id ?? '';
  // Both the `:free` suffix and catalog prices are authoritative only for an
  // OpenRouter connection. A manually entered provider may expose missing,
  // placeholder, or zero prices that Flowork cannot interpret as free usage.
  if (sourceId(model) !== 'openrouter_oauth') return false;
  if (id === 'openrouter/free' || id.endsWith(':free')) return true;
  if (model.input_price == null || model.output_price == null) return false;
  const input = Number(model.input_price);
  const output = Number(model.output_price);
  return Number.isFinite(input) && Number.isFinite(output)
    && input === 0 && output === 0;
}

function pricePerMillion(value: string | null | undefined): string | null {
  if (value == null || value === '') return null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return null;
  return `$${(parsed * 1_000_000).toLocaleString(undefined, {
    maximumFractionDigits: 4,
  })}/M`;
}

function SourceIcon({ source }: { source: string }) {
  const className = 'h-4 w-4';
  if (source === 'openrouter_oauth') return <Router className={className} />;
  if (source === 'chatgpt_account') return <UserRound className={className} />;
  if (source === 'managed_api') return <Building2 className={className} />;
  if (source === 'manual') return <KeyRound className={className} />;
  return <Cloud className={className} />;
}

export function RuntimeModelPicker({
  capabilities,
  loading,
  settings,
  onChange,
  disabled,
}: RuntimeModelPickerProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [activeSource, setActiveSource] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const deferredQuery = useDeferredValue(query.trim().toLocaleLowerCase());
  const modelId = settings?.modelId ?? null;
  const catalogDefault = capabilities?.models.some(
    (model) => model.id === capabilities.default_model_id,
  ) ? capabilities?.default_model_id : null;
  const selectedValue = modelId ?? catalogDefault ?? NO_MODEL_AVAILABLE;
  const selectedModel = capabilities?.models.find(
    (model) => model.id === selectedValue,
  );
  const selectedMissing = !!modelId && !selectedModel;
  const connectionLocked = capabilities?.bound_agent_settings != null;

  const sourceLabel = (source: string) => {
    if (source === 'managed_api') {
      return t('agent_settings.source_platform', 'Platform models');
    }
    if (source === 'chatgpt_account') {
      return t('agent_settings.source_openai_account', 'OpenAI account');
    }
    if (source === 'openrouter_oauth') return 'OpenRouter';
    if (source === 'manual') {
      return t('agent_settings.source_manual', 'My API connections');
    }
    return t('agent_settings.source_other', 'Other sources');
  };

  const groups = useMemo<ModelSourceGroup[]>(() => {
    const grouped = new Map<string, RuntimeModelOption[]>();
    for (const model of capabilities?.models ?? []) {
      const source = sourceId(model);
      grouped.set(source, [...(grouped.get(source) ?? []), model]);
    }
    const order = ['managed_api', 'chatgpt_account', 'openrouter_oauth', 'manual'];
    return [...grouped.entries()]
      .map(([id, models]) => ({ id, label: sourceLabel(id), models }))
      .sort((left, right) => {
        const leftIndex = order.indexOf(left.id);
        const rightIndex = order.indexOf(right.id);
        return (leftIndex < 0 ? order.length : leftIndex)
          - (rightIndex < 0 ? order.length : rightIndex);
      });
  // `sourceLabel` only reads `t`; language changes replace `t` and rebuild labels.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [capabilities?.models, t]);

  const activeGroup = groups.find((group) => group.id === activeSource) ?? null;
  const visibleModels = useMemo(() => {
    if (!activeGroup) return [];
    if (!deferredQuery) return activeGroup.models;
    return activeGroup.models.filter((model) => {
      const freeKeywords = isFreeModel(model) ? ' free 免费 zero 0 ' : '';
      return [
        model.label,
        model.provider_model_id,
        model.provider,
        model.description,
        freeKeywords,
      ].filter(Boolean).join(' ').toLocaleLowerCase().includes(deferredQuery);
    });
  }, [activeGroup, deferredQuery]);
  const displayedModels = useMemo(() => {
    if (visibleModels.length <= MODEL_RENDER_LIMIT) return visibleModels;
    const selected = visibleModels.find((model) => model.id === selectedValue);
    if (!selected) return visibleModels.slice(0, MODEL_RENDER_LIMIT);
    return [
      selected,
      ...visibleModels.filter((model) => model.id !== selected.id),
    ].slice(0, MODEL_RENDER_LIMIT);
  }, [selectedValue, visibleModels]);
  const hiddenModelCount = visibleModels.length - displayedModels.length;

  const selectedLabel = selectedMissing
    ? `${modelId} · ${t('agent_settings.unavailable', 'Unavailable')}`
    : selectedModel?.label ?? t('agent_settings.no_model', 'No model configured');
  const pickerDisabled = disabled || loading
    || !capabilities?.runtime_available || groups.length === 0;

  const close = () => {
    setOpen(false);
    setActiveSource(null);
    setQuery('');
  };

  return (
    <Popover.Root
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) {
          setActiveSource(null);
          setQuery('');
        }
      }}
    >
      <Popover.Trigger asChild>
        <button
          type="button"
          disabled={pickerDisabled}
          aria-label={t('agent_settings.inline_model', 'Model')}
          aria-haspopup="dialog"
          aria-expanded={open}
          title={selectedLabel}
          className="flex h-8 w-[142px] min-w-0 items-center gap-1.5 rounded-md bg-transparent px-2 text-xs hover:bg-muted/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/25 disabled:cursor-not-allowed disabled:bg-transparent disabled:opacity-50 sm:w-[178px]"
          data-role="chat-model-select"
          data-testid="chat-model-select"
        >
          <Cpu className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          <span className="truncate">{loading
            ? t('agent_settings.loading_models', 'Loading models…')
            : selectedLabel}</span>
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          side="top"
          sideOffset={6}
          collisionPadding={12}
          className="z-modal-popover w-[min(420px,calc(100vw-1.5rem))] overflow-hidden rounded-xl border border-edge-structural bg-popover text-popover-foreground shadow-popover"
        >
          {activeGroup ? (
            <>
              <div className="flex items-center gap-2 border-b border-edge-subtle px-2 py-2">
                <button
                  type="button"
                  className="grid h-8 w-8 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
                  onClick={() => {
                    setActiveSource(null);
                    setQuery('');
                  }}
                  aria-label={t('composer.model.backToSources', 'Back to model sources')}
                >
                  <ArrowLeft className="h-4 w-4" />
                </button>
                <SourceIcon source={activeGroup.id} />
                <strong className="min-w-0 flex-1 truncate text-sm">{activeGroup.label}</strong>
                <span className="text-xs tabular-nums text-muted-foreground">
                  {activeGroup.models.length}
                </span>
              </div>
              <label className="flex items-center border-b border-edge-subtle px-3">
                <Search className="mr-2 h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="sr-only">
                  {t('agent_settings.search_models', 'Search models, providers, or free')}
                </span>
                <input
                  autoFocus
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={t('agent_settings.search_models', 'Search models, providers, or free…')}
                  className="h-10 min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
                />
              </label>
              <div className="max-h-[min(24rem,var(--radix-popover-content-available-height))] overflow-y-auto overscroll-contain p-1">
                {visibleModels.length === 0 ? (
                  <p className="px-3 py-8 text-center text-sm text-muted-foreground">
                    {t('agent_settings.no_matching_models', 'No matching models')}
                  </p>
                ) : displayedModels.map((model) => {
                  const free = isFreeModel(model);
                  const inputPrice = pricePerMillion(model.input_price);
                  const outputPrice = pricePerMillion(model.output_price);
                  return (
                    <button
                      key={model.id}
                      type="button"
                      data-role="chat-model-option"
                      data-model-id={model.id}
                      disabled={model.available === false}
                      className={cn(
                        'flex w-full items-start gap-2 rounded-lg px-2.5 py-2.5 text-left hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50',
                        model.id === selectedValue && 'bg-accent/70',
                      )}
                      onClick={() => {
                        onChange({ modelId: model.id, reasoningEffort: null });
                        close();
                      }}
                    >
                      <Check className={cn(
                        'mt-0.5 h-4 w-4 shrink-0',
                        model.id === selectedValue ? 'opacity-100' : 'opacity-0',
                      )} />
                      <span className="min-w-0 flex-1">
                        <span className="flex min-w-0 items-center gap-2">
                          <span className="truncate text-sm font-medium">{model.label}</span>
                          {free ? (
                            <span className="shrink-0 rounded-full bg-emerald-500/10 px-1.5 py-0.5 text-xs font-semibold text-emerald-700 dark:text-emerald-300">
                              {t('agent_settings.free_model', 'Free')}
                            </span>
                          ) : null}
                        </span>
                        <span className="mt-0.5 block truncate font-mono text-xs text-muted-foreground">
                          {model.provider_model_id ?? model.description}
                        </span>
                        <span className="mt-1 flex flex-wrap gap-x-2 text-xs text-muted-foreground">
                          {model.context_length ? (
                            <span>{t('agent_settings.model_context', '{{value}} context', {
                              value: formatNumber(model.context_length),
                            })}</span>
                          ) : null}
                          {!free && (inputPrice || outputPrice) ? (
                            <span>{t('agent_settings.model_price_compact', '{{input}} / {{output}}', {
                              input: inputPrice ?? '—',
                              output: outputPrice ?? '—',
                            })}</span>
                          ) : null}
                          {model.supports_tools ? (
                            <span>{t('agent_settings.tool_capable', 'Tools')}</span>
                          ) : null}
                        </span>
                      </span>
                    </button>
                  );
                })}
                {hiddenModelCount > 0 ? (
                  <p className="px-3 py-2 text-center text-xs text-muted-foreground">
                    {t(
                      'agent_settings.refine_model_search',
                      'Search to find {{count}} more models',
                      { count: hiddenModelCount },
                    )}
                  </p>
                ) : null}
              </div>
            </>
          ) : (
            <>
              <div className="border-b border-edge-subtle px-4 py-3">
                <h3 className="text-sm font-semibold">
                  {connectionLocked
                    ? t('agent_settings.connection_locked_title', 'Model connection')
                    : t('agent_settings.choose_source', 'Choose a model source')}
                </h3>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  {connectionLocked
                    ? t(
                      'agent_settings.connection_locked_hint',
                      'This Chat stays on its current connection. Start a new Chat to use another source.',
                    )
                    : t(
                      'agent_settings.choose_source_hint',
                      'Sources are filtered by the Runtime selected in Settings.',
                    )}
                </p>
              </div>
              <div className="p-1.5">
                {groups.map((group) => {
                  const selectedInGroup = group.models.some(
                    (model) => model.id === selectedValue,
                  );
                  const freeCount = group.models.filter(isFreeModel).length;
                  return (
                    <button
                      key={group.id}
                      type="button"
                      data-role="chat-model-source-option"
                      data-model-source={group.id}
                      className={cn(
                        'flex w-full items-center gap-3 rounded-lg px-3 py-3 text-left hover:bg-accent',
                        selectedInGroup && 'bg-accent/60',
                      )}
                      onClick={() => setActiveSource(group.id)}
                    >
                      <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground">
                        <SourceIcon source={group.id} />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm font-medium">{group.label}</span>
                        <span className="mt-0.5 block text-xs text-muted-foreground">
                          {t('agent_settings.source_model_count', '{{count}} models', {
                            count: group.models.length,
                          })}
                          {freeCount > 0
                            ? ` · ${t('agent_settings.source_free_count', '{{count}} free', { count: freeCount })}`
                            : ''}
                        </span>
                      </span>
                      <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
                    </button>
                  );
                })}
              </div>
            </>
          )}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
