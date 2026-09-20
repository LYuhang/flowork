import { useTranslation } from 'react-i18next';
import { ArrowUpRight } from 'lucide-react';

import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { cn } from '@/lib/utils';

type ExampleCategory = 'office' | 'diagram' | 'workflow' | 'operations' | 'knowledge';

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

const EXAMPLES: Record<ExampleCategory, readonly ExampleDefinition[]> = {
  office: [
    {
      id: 'presentation',
      icon: '📊',
      command: '/document',
      titleKey: 'chat.examples.office.presentation.title',
      title: 'Create a presentation',
      descriptionKey: 'chat.examples.office.presentation.description',
      description: 'Research a topic and turn it into a polished slide deck.',
      promptKey: 'chat.examples.office.presentation.prompt',
      prompt: '/document Create a polished, editable 3-slide presentation introducing Flowork to a technical team. Do not browse the web; use only these facts: Flowork is an open-source server-side Agent for browser automation, Workflows, Tasks and Deployments, professional office documents and diagrams, and reusable Knowledge storage. Use slide 1 for the problem and product position, slide 2 for the capability groups, and slide 3 for a concise getting-started flow from connecting a Runtime and model source to Chat and delivery.',
    },
    {
      id: 'report',
      icon: '📝',
      command: '/document',
      titleKey: 'chat.examples.office.report.title',
      title: 'Write a business report',
      descriptionKey: 'chat.examples.office.report.description',
      description: 'Structure evidence and recommendations into a professional report.',
      promptKey: 'chat.examples.office.report.prompt',
      prompt: '/document Create a polished, editable 3-page decision brief for a mid-sized customer-support team evaluating Flowork. Do not browse the web; use only these assumptions: the team handles 20,000 tickets per month, spends 35% of staff time on repetitive browser and reporting work, and requires human review before production actions. Cover the current problem, a proposed pilot using browser automation, Workflows, Tasks and Deployments, expected benefits and risks, and a 30-day rollout with measurable success criteria.',
    },
    {
      id: 'spreadsheet',
      icon: '📈',
      command: '/document',
      titleKey: 'chat.examples.office.spreadsheet.title',
      title: 'Build an analysis workbook',
      descriptionKey: 'chat.examples.office.spreadsheet.description',
      description: 'Create a clear spreadsheet with formulas, summaries, and charts.',
      promptKey: 'chat.examples.office.spreadsheet.prompt',
      prompt: '/document Create a polished, editable quarterly sales workbook with exactly two sheets: Data and Summary. Use sample data for four regions across four quarters. Include validated formulas, restrained conditional formatting, and three compact charts on the Summary sheet for revenue, growth, and target attainment. Set each sheet to fit on one landscape print page for visual review.',
    },
  ],
  diagram: [
    {
      id: 'architecture',
      icon: '🏗️',
      command: '/diagram',
      titleKey: 'chat.examples.diagram.architecture.title',
      title: 'Visualize a system architecture',
      descriptionKey: 'chat.examples.diagram.architecture.description',
      description: 'Show system boundaries, components, and data flows clearly.',
      promptKey: 'chat.examples.diagram.architecture.prompt',
      prompt: '/diagram Create a professional architecture diagram for an AI customer-support platform. Show the web app, API gateway, Agent service, model providers, knowledge retrieval, job queue, database, object storage, and observability services, including their main data flows and trust boundaries.',
    },
    {
      id: 'process',
      icon: '🧭',
      command: '/diagram',
      titleKey: 'chat.examples.diagram.process.title',
      title: 'Map a business process',
      descriptionKey: 'chat.examples.diagram.process.description',
      description: 'Turn roles, decisions, and exceptions into an easy-to-follow flow.',
      promptKey: 'chat.examples.diagram.process.prompt',
      prompt: '/diagram Create a professional business process diagram for an employee expense claim. Cover submission, manager approval, finance review, policy exceptions, rejection and resubmission, payment, and employee notification, with responsibilities clearly separated by role.',
    },
    {
      id: 'sequence',
      icon: '🔁',
      command: '/diagram',
      titleKey: 'chat.examples.diagram.sequence.title',
      title: 'Explain an interaction sequence',
      descriptionKey: 'chat.examples.diagram.sequence.description',
      description: 'Clarify how participants exchange requests, responses, and failures.',
      promptKey: 'chat.examples.diagram.sequence.prompt',
      prompt: '/diagram Create a professional sequence diagram for an e-commerce checkout. Include the customer, storefront, order service, inventory service, payment provider, and notification service, showing the successful path plus payment failure and inventory shortage branches.',
    },
  ],
  workflow: [
    {
      id: 'feedback',
      icon: '💬',
      command: '/workflow',
      titleKey: 'chat.examples.workflow.feedback.title',
      title: 'Triage customer feedback',
      descriptionKey: 'chat.examples.workflow.feedback.description',
      description: 'Classify feedback, identify urgency, and route follow-up work.',
      promptKey: 'chat.examples.workflow.feedback.prompt',
      prompt: '/workflow Build a compact customer-feedback workflow that accepts feedback, detects language and sentiment, classifies topic and urgency, drafts a response, and routes critical issues for human review. Keep the workflow to 5–7 clear nodes, validate it, and publish it to the canvas; do not run it yet.',
    },
    {
      id: 'research',
      icon: '🔎',
      command: '/workflow',
      titleKey: 'chat.examples.workflow.research.title',
      title: 'Produce a research digest',
      descriptionKey: 'chat.examples.workflow.research.description',
      description: 'Collect sources, extract findings, and publish a concise digest.',
      promptKey: 'chat.examples.workflow.research.prompt',
      prompt: '/workflow Build a reusable research workflow that takes a topic, searches trustworthy sources, removes duplicates, extracts key findings with citations, and produces a concise Markdown digest.',
    },
    {
      id: 'invoices',
      icon: '🧾',
      command: '/workflow',
      titleKey: 'chat.examples.workflow.invoices.title',
      title: 'Process incoming invoices',
      descriptionKey: 'chat.examples.workflow.invoices.description',
      description: 'Extract fields, validate totals, and flag exceptions for review.',
      promptKey: 'chat.examples.workflow.invoices.prompt',
      prompt: '/workflow Build an invoice-processing workflow that reads uploaded invoices, extracts supplier and line-item data, validates totals and required fields, and sends exceptions to a human reviewer.',
    },
  ],
  operations: [
    {
      id: 'batch',
      icon: '🗂️',
      command: '/workflow',
      titleKey: 'chat.examples.operations.batch.title',
      title: 'Run a workflow batch',
      descriptionKey: 'chat.examples.operations.batch.description',
      description: 'Apply a workflow to many records and collect structured results.',
      promptKey: 'chat.examples.operations.batch.prompt',
      prompt: '/workflow Build or select a customer-feedback workflow, run it against a small batch of representative feedback records, preserve row-level errors, and save successful and failed results separately.',
    },
    {
      id: 'schedule',
      icon: '🗓️',
      command: '/task',
      titleKey: 'chat.examples.operations.schedule.title',
      title: 'Schedule recurring work',
      descriptionKey: 'chat.examples.operations.schedule.description',
      description: 'Select an existing workflow, schedule it, and retain its run history.',
      promptKey: 'chat.examples.operations.schedule.prompt',
      prompt: '/task From my existing workflows, select the most recently updated valid workflow whose name or description best matches a research digest. Schedule it to run every weekday at 09:00 in my current timezone, and keep each run result available for review.',
    },
    {
      id: 'deploy',
      icon: '🚀',
      command: '/deployment',
      titleKey: 'chat.examples.operations.deploy.title',
      title: 'Publish a workflow API',
      descriptionKey: 'chat.examples.operations.deploy.description',
      description: 'Select an existing workflow and expose it through a controlled endpoint.',
      promptKey: 'chat.examples.operations.deploy.prompt',
      prompt: '/deployment From my existing workflows, select the most recently updated valid workflow whose name or description best matches customer feedback. Deploy it as an authenticated API endpoint, use the latest workflow version, and configure a conservative request rate limit.',
    },
  ],
  knowledge: [
    {
      id: 'create',
      icon: '📚',
      command: '/knowledge',
      titleKey: 'chat.examples.knowledge.create.title',
      title: 'Create a knowledge package',
      descriptionKey: 'chat.examples.knowledge.create.description',
      description: 'Organize reusable research into a documented file collection.',
      promptKey: 'chat.examples.knowledge.create.prompt',
      prompt: '/knowledge Research practical evaluation methods for Agent systems, organize the findings into a new knowledge package with a clear README and source files, and publish it to my Knowledge library.',
    },
    {
      id: 'explore',
      icon: '🧭',
      command: '/knowledge',
      titleKey: 'chat.examples.knowledge.explore.title',
      title: 'Explore existing knowledge',
      descriptionKey: 'chat.examples.knowledge.explore.description',
      description: 'Find the right package and progressively inspect relevant files.',
      promptKey: 'chat.examples.knowledge.explore.prompt',
      prompt: '/knowledge Find my knowledge about Agent evaluation, read its README first, then summarize the recommended evaluation process and cite the relevant local files.',
    },
    {
      id: 'update',
      icon: '✨',
      command: '/knowledge',
      titleKey: 'chat.examples.knowledge.update.title',
      title: 'Update a knowledge package',
      descriptionKey: 'chat.examples.knowledge.update.description',
      description: 'Add new evidence while preserving the package structure.',
      promptKey: 'chat.examples.knowledge.update.prompt',
      prompt: '/knowledge Find my Agent evaluation knowledge package and add recent findings and sources. If it does not exist, create it with a clear README and source-file structure first. Validate the complete package and publish the resulting version.',
    },
  ],
};

const CATEGORIES: readonly {
  id: ExampleCategory;
  labelKey: string;
  label: string;
}[] = [
  { id: 'office', labelKey: 'chat.examples.tab.office', label: 'Office' },
  { id: 'diagram', labelKey: 'chat.examples.tab.diagram', label: 'Diagram' },
  { id: 'workflow', labelKey: 'chat.examples.tab.workflow', label: 'Workflow' },
  { id: 'operations', labelKey: 'chat.examples.tab.operations', label: 'Tasks and deployments' },
  { id: 'knowledge', labelKey: 'chat.examples.tab.knowledge', label: 'Knowledge' },
];

const CATEGORY_STYLE: Record<ExampleCategory, {
  card: string;
  icon: string;
  command: string;
  action: string;
}> = {
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
      <Tabs defaultValue="office">
        <TabsList
          variant="underline"
          className="chat-scrollbar flex h-auto w-full justify-start overflow-x-auto"
        >
          {CATEGORIES.map((category) => (
            <TabsTrigger key={category.id} value={category.id} className="shrink-0">
              {t(category.labelKey, category.label)}
            </TabsTrigger>
          ))}
        </TabsList>
        {CATEGORIES.map((category) => (
          <TabsContent key={category.id} value={category.id} className="mt-3">
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {EXAMPLES[category.id].map((example) => (
                <button
                  key={example.id}
                  type="button"
                  className={cn(
                    'group relative grid min-h-36 grid-rows-[auto_auto_1fr_auto] content-start items-stretch overflow-hidden rounded-2xl border border-edge-subtle bg-gradient-to-b from-surface-raised to-surface-sunken/35 p-4 text-left',
                    'shadow-[0_1px_0_rgba(15,23,42,0.08),0_10px_24px_-18px_rgba(15,23,42,0.5)]',
                    'transition-[transform,border-color,box-shadow,background-color] duration-200 ease-out',
                    'hover:-translate-y-0.5 active:translate-y-px active:shadow-sm',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2',
                    'motion-reduce:transform-none motion-reduce:transition-none',
                    CATEGORY_STYLE[category.id].card,
                  )}
                  onClick={() => onSelect(t(example.promptKey, example.prompt))}
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
                        CATEGORY_STYLE[category.id].icon,
                      )}
                    >
                      {example.icon}
                    </span>
                    <span
                      className={cn(
                        'rounded-full border px-2 py-1 font-mono text-xs font-medium tracking-tight',
                        CATEGORY_STYLE[category.id].command,
                      )}
                    >
                      {example.command}
                    </span>
                  </span>
                  <span className="text-ui mt-3 block font-semibold text-content-primary">
                    {t(example.titleKey, example.title)}
                  </span>
                  <span className="text-meta mt-1.5 block text-content-secondary">
                    {t(example.descriptionKey, example.description)}
                  </span>
                  <span
                    className={cn(
                      'mt-3 inline-flex items-center gap-1 text-xs font-medium',
                      CATEGORY_STYLE[category.id].action,
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
