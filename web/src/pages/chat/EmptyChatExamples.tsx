import { useTranslation } from 'react-i18next';
import { ArrowUpRight } from 'lucide-react';

import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { cn } from '@/lib/utils';
import en from '@/lib/i18n/locales/en.json';

type ExampleCategory = 'automation' | 'office' | 'diagram' | 'workflow' | 'operations' | 'knowledge' | 'skill';

interface ExampleDefinition {
  id: string;
  icon: string;
  command: string;
  titleKey: string;
  title: string;
  descriptionKey: string;
  description: string;
  promptKey: string;
  prompt: string;
}

// Keep presentation grouped by user goals; prompts still activate the required tools.
type LocaleKey = { [K in keyof typeof en]: typeof en[K] extends string ? K : never }[keyof typeof en];
type ExampleKey = { [K in LocaleKey]: K extends `chat.examples.${infer Name}.prompt` ? `chat.examples.${Name}` : never }[LocaleKey];
function example(id: string, icon: string, command: string, key: ExampleKey): ExampleDefinition {
  const titleKey = `${key}.title` as LocaleKey;
  const descriptionKey = `${key}.description` as LocaleKey;
  const promptKey = `${key}.prompt` as LocaleKey;
  return { id, icon, command, titleKey, title: en[titleKey],
    descriptionKey, description: en[descriptionKey],
    promptKey, prompt: en[promptKey] };
}

const CATEGORIES: readonly {
  id: string;
  labelKey: LocaleKey;
  style: ExampleCategory;
  examples: readonly ExampleDefinition[];
}[] = [
  { id: 'tables', labelKey: 'chat.examples.scenario.tables', style: 'office', examples: [
    example('clean', '🧹', '/document', 'chat.examples.tables.clean'),
    example('audit', '🧾', '/document', 'chat.examples.tables.audit'),
    example('spreadsheet', '📈', '/document', 'chat.examples.office.spreadsheet'),
  ] },
  { id: 'reports', labelKey: 'chat.examples.scenario.reports', style: 'office', examples: [
    example('report', '📝', '/document', 'chat.examples.office.report'),
    example('presentation', '📊', '/document', 'chat.examples.office.presentation'),
    example('weekly', '📅', '/document', 'chat.examples.reports.weekly'),
  ] },
  { id: 'automation', labelKey: 'chat.examples.scenario.automation', style: 'automation', examples: [
    example('order-audit', '⚙️', '/workflow', 'chat.examples.automation.orderAudit'),
    example('schedule', '🗓️', '/task', 'chat.examples.operations.schedule'),
    example('deploy', '🚀', '/deployment', 'chat.examples.operations.deploy'),
  ] },
  { id: 'diagrams', labelKey: 'chat.examples.scenario.diagrams', style: 'diagram', examples: [
    example('architecture', '🏗️', '/diagram', 'chat.examples.diagram.architecture'),
    example('process', '🧭', '/diagram', 'chat.examples.diagram.process'),
    example('sequence', '🔁', '/diagram', 'chat.examples.diagram.sequence'),
  ] },
  { id: 'knowledge', labelKey: 'chat.examples.scenario.knowledge', style: 'knowledge', examples: [
    example('create', '📚', '/knowledge', 'chat.examples.knowledge.create'),
    example('explore', '🧭', '/knowledge', 'chat.examples.knowledge.explore'),
    example('update', '✨', '/knowledge', 'chat.examples.knowledge.update'),
  ] },
  { id: 'more', labelKey: 'chat.examples.scenario.more', style: 'skill', examples: [
    example('template', '🧩', '/skill', 'chat.examples.skill.template'),
    example('download', '📥', '/skill', 'chat.examples.skill.download'),
    example('update', '🛠️', '/skill', 'chat.examples.skill.update'),
  ] },
];

const CATEGORY_STYLE: Record<ExampleCategory, {
  card: string;
  icon: string;
  command: string;
  action: string;
}> = {
  automation: {
    card: 'hover:border-emerald-300/70 hover:shadow-lg dark:hover:border-emerald-500/45',
    icon: 'border-emerald-200/80 bg-emerald-50 dark:border-emerald-500/30 dark:bg-emerald-500/15',
    command: 'border-emerald-200/70 bg-emerald-50 text-emerald-700 dark:border-emerald-500/25 dark:bg-emerald-500/10 dark:text-emerald-300',
    action: 'text-emerald-700 dark:text-emerald-300',
  },
  office: {
    card: 'hover:border-violet-300/70 hover:shadow-[0_2px_0_rgba(109,40,217,0.12),0_18px_34px_-20px_rgba(109,40,217,0.45)] dark:hover:border-violet-500/45',
    icon: 'border-violet-200/80 bg-gradient-to-br from-violet-100 via-fuchsia-50 to-white shadow-violet-950/15 dark:border-violet-500/30 dark:from-violet-500/25 dark:via-fuchsia-500/10 dark:to-surface-raised',
    command: 'border-violet-200/70 bg-violet-50 text-violet-700 dark:border-violet-500/25 dark:bg-violet-500/10 dark:text-violet-300',
    action: 'text-violet-700 dark:text-violet-300',
  },
  diagram: {
    card: 'hover:border-rose-300/70 hover:shadow-[0_2px_0_rgba(225,29,72,0.12),0_18px_34px_-20px_rgba(225,29,72,0.42)] dark:hover:border-rose-500/45',
    icon: 'border-rose-200/80 bg-gradient-to-br from-rose-100 via-pink-50 to-white shadow-rose-950/15 dark:border-rose-500/30 dark:from-rose-500/25 dark:via-pink-500/10 dark:to-surface-raised',
    command: 'border-rose-200/70 bg-rose-50 text-rose-700 dark:border-rose-500/25 dark:bg-rose-500/10 dark:text-rose-300',
    action: 'text-rose-700 dark:text-rose-300',
  },
  workflow: {
    card: 'hover:border-sky-300/70 hover:shadow-[0_2px_0_rgba(2,132,199,0.12),0_18px_34px_-20px_rgba(2,132,199,0.45)] dark:hover:border-sky-500/45',
    icon: 'border-sky-200/80 bg-gradient-to-br from-sky-100 via-cyan-50 to-white shadow-sky-950/15 dark:border-sky-500/30 dark:from-sky-500/25 dark:via-cyan-500/10 dark:to-surface-raised',
    command: 'border-sky-200/70 bg-sky-50 text-sky-700 dark:border-sky-500/25 dark:bg-sky-500/10 dark:text-sky-300',
    action: 'text-sky-700 dark:text-sky-300',
  },
  operations: {
    card: 'hover:border-amber-300/70 hover:shadow-[0_2px_0_rgba(217,119,6,0.12),0_18px_34px_-20px_rgba(217,119,6,0.45)] dark:hover:border-amber-500/45',
    icon: 'border-amber-200/80 bg-gradient-to-br from-amber-100 via-orange-50 to-white shadow-amber-950/15 dark:border-amber-500/30 dark:from-amber-500/25 dark:via-orange-500/10 dark:to-surface-raised',
    command: 'border-amber-200/70 bg-amber-50 text-amber-800 dark:border-amber-500/25 dark:bg-amber-500/10 dark:text-amber-300',
    action: 'text-amber-800 dark:text-amber-300',
  },
  skill: {
    card: 'hover:border-indigo-300/70 hover:shadow-[0_2px_0_rgba(5,150,105,0.12),0_18px_34px_-20px_rgba(5,150,105,0.45)] dark:hover:border-indigo-500/45',
    icon: 'border-indigo-200/80 bg-gradient-to-br from-indigo-100 via-violet-50 to-white shadow-indigo-950/15 dark:border-indigo-500/30 dark:from-indigo-500/25 dark:via-violet-500/10 dark:to-surface-raised',
    command: 'border-indigo-200/70 bg-indigo-50 text-indigo-700 dark:border-indigo-500/25 dark:bg-indigo-500/10 dark:text-indigo-300',
    action: 'text-indigo-700 dark:text-indigo-300',
  },

  knowledge: {
    card: 'hover:border-emerald-300/70 hover:shadow-[0_2px_0_rgba(5,150,105,0.12),0_18px_34px_-20px_rgba(5,150,105,0.45)] dark:hover:border-emerald-500/45',
    icon: 'border-emerald-200/80 bg-gradient-to-br from-emerald-100 via-teal-50 to-white shadow-emerald-950/15 dark:border-emerald-500/30 dark:from-emerald-500/25 dark:via-teal-500/10 dark:to-surface-raised',
    command: 'border-emerald-200/70 bg-emerald-50 text-emerald-700 dark:border-emerald-500/25 dark:bg-emerald-500/10 dark:text-emerald-300',
    action: 'text-emerald-700 dark:text-emerald-300',
  },
};

export function EmptyChatExamples({
  visible,
  onSelect,
}: {
  visible: boolean;
  onSelect: (prompt: string) => void;
}) {
  const { t } = useTranslation();
  if (!visible) return null;

  return (
    <section
      className="mt-5 w-full"
      aria-label={t('chat.examples.label', 'Start with an example')}
      data-role="empty-chat-examples"
    >
      <Tabs defaultValue="tables">
        <TabsList
          variant="underline"
          className="chat-scrollbar flex h-auto w-full justify-start overflow-x-auto"
        >
          {CATEGORIES.map((category) => (
            <TabsTrigger key={category.id} value={category.id} className="shrink-0">
              {t(category.labelKey, en[category.labelKey])}
            </TabsTrigger>
          ))}
        </TabsList>
        {CATEGORIES.map((category) => (
          <TabsContent key={category.id} value={category.id} className="mt-3">
            <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,14rem),1fr))] gap-2">
              {category.examples.map((example) => (
                <button
                  key={example.id}
                  type="button"
                  className={cn(
                    'group relative grid min-w-0 min-h-36 grid-rows-[auto_auto_1fr_auto] content-start items-stretch overflow-hidden rounded-2xl border border-edge-subtle bg-gradient-to-b from-surface-raised to-surface-sunken/35 p-4 text-left',
                    'shadow-[0_1px_0_rgba(15,23,42,0.08),0_10px_24px_-18px_rgba(15,23,42,0.5)]',
                    'transition-[transform,border-color,box-shadow,background-color] duration-200 ease-out',
                    'hover:-translate-y-0.5 active:translate-y-px active:shadow-sm',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2',
                    'motion-reduce:transform-none motion-reduce:transition-none',
                    CATEGORY_STYLE[category.style].card,
                  )}
                  onClick={() => onSelect(t(example.promptKey, {
                    defaultValue: example.prompt,
                    publicUrl: window.location.origin,
                  }))}
                  data-example-id={`${category.id}:${example.id}`}
                >
                  <span className="flex items-start justify-between gap-3">
                    <span
                      aria-hidden="true"
                      data-role="example-card-icon"
                      className={cn(
                        'grid size-11 shrink-0 place-items-center rounded-xl border text-[1.35rem] leading-none',
                        'shadow-[0_5px_12px_-7px_currentColor,inset_0_1px_0_rgba(255,255,255,0.9)]',
                        'transition-transform duration-200 group-hover:-translate-y-0.5 group-hover:scale-[1.04] motion-reduce:transform-none motion-reduce:transition-none',
                        CATEGORY_STYLE[category.style].icon,
                      )}
                    >
                      {example.icon}
                    </span>
                    {example.command && <span
                      className={cn(
                        'rounded-full border px-2 py-1 font-mono text-xs font-medium tracking-tight',
                        CATEGORY_STYLE[category.style].command,
                      )}
                    >
                      {example.command}
                    </span>}
                  </span>
                  <span className="text-ui mt-3 block break-words font-semibold text-content-primary">
                    {t(example.titleKey, example.title)}
                  </span>
                  <span className="text-meta mt-1.5 block break-words text-content-secondary">
                    {t(example.descriptionKey, example.description)}
                  </span>
                  <span
                    className={cn(
                      'mt-3 inline-flex items-center gap-1 text-xs font-medium',
                      CATEGORY_STYLE[category.style].action,
                    )}
                  >
                    {t('chat.examples.use', 'Use this example')}
                    <ArrowUpRight className="size-3.5 transition-transform duration-200 group-hover:translate-x-0.5 group-hover:-translate-y-0.5 motion-reduce:transform-none motion-reduce:transition-none" />
                  </span>
                </button>
              ))}
            </div>
          </TabsContent>
        ))}
      </Tabs>
    </section>
  );
}
