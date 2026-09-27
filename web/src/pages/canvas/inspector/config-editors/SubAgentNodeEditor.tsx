/** SubAgentNode editor. Uses the same authorized manual API picker as PromptNode. */
import { Label } from '@/components/ui/label';
import {
  CommitOnBlurNumber,
} from '@/pages/canvas/inspector/CommitOnBlur';
import { WorkflowModelSelect } from './WorkflowModelSelect';
import { useTranslation } from 'react-i18next';
import { useState } from 'react';
import { History, Maximize2 } from 'lucide-react';
import type { Extension } from '@codemirror/state';
import { Button } from '@/components/ui/button';
import { PromptDiffDialog } from '@/components/modals/PromptDiffDialog';
import { useWorkflowVersions } from '@/lib/api/queries/workflow';
import { CodeMirrorField } from './CodeMirrorField';
import { ExpandedCodeMirrorDialog } from './ExpandedCodeMirrorDialog';
import { placeholderHighlight, placeholderTheme } from './prompt-template';
import type { NodeConfigEditorProps } from './types';

/** UI default for the optional engine cap. */
const DEFAULT_MAX_ITERATIONS = 25;
const TASK_TEMPLATE_EXTENSIONS: Extension[] = [placeholderHighlight, placeholderTheme];

export function SubAgentNodeEditor({
  config,
  readOnly,
  onChange,
  nodeId,
  wfId,
}: NodeConfigEditorProps) {
  const { t } = useTranslation();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [expandedOpen, setExpandedOpen] = useState(false);
  const canShowHistory = !!wfId && !!nodeId;
  const versionsQuery = useWorkflowVersions(canShowHistory ? wfId : undefined);
  const versionCount = Array.isArray(
    (versionsQuery.data as { versions?: unknown[] } | undefined)?.versions,
  )
    ? (versionsQuery.data as { versions: unknown[] }).versions.length
    : 0;
  const showHistoryButton = canShowHistory && versionCount >= 2;

  const taskTemplate =
    typeof config.task_template === 'string' ? (config.task_template as string) : '';
  const modelName =
    typeof config.model_name === 'string' ? (config.model_name as string) : '';
  const maxIterations =
    typeof config.max_iterations === 'number'
      ? (config.max_iterations as number)
      : DEFAULT_MAX_ITERATIONS;
  const emitConfig = (patch: Record<string, unknown>) => {
    const next: Record<string, unknown> = {
      task_template: taskTemplate,
      model_name: modelName,
      max_iterations: maxIterations,
      ...patch,
    };
    onChange(next);
  };


  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <Label className="text-xs font-medium">
            {t('subagent_node.task_template', 'Task template')}
          </Label>
          <div className="flex items-center gap-1">
            {showHistoryButton && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-6 gap-1 px-2 text-xs text-muted-foreground"
                onClick={() => setHistoryOpen(true)}
                data-testid="cfg-subagent-task-history-btn"
              >
                <History className="h-3.5 w-3.5" />
                {t('prompt_history.button', 'History')}
              </Button>
            )}
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="h-6 w-6 text-muted-foreground"
              onClick={() => setExpandedOpen(true)}
              aria-label={t('inspector.config.expandEditor', 'Expand editor')}
              title={t('inspector.config.expandEditor', 'Expand editor')}
              data-testid="cfg-subagent-task-expand-btn"
            >
              <Maximize2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
        <CodeMirrorField
          value={taskTemplate}
          onCommit={(next) => emitConfig({ task_template: next })}
          readOnly={readOnly}
          extensions={TASK_TEMPLATE_EXTENSIONS}
          minHeight="160px"
          placeholder={t(
            'subagent_node.task_template_placeholder',
            '# Task\nRead {{file_path}} and summarize its key points.\n\n# Instructions\nUse file tools to inspect the file and extract important information.\n\n# Output\nReturn the declared output fields.',
          )}
          data-testid="cfg-subagent-task-template"
        />
        <ExpandedCodeMirrorDialog
          open={expandedOpen}
          onOpenChange={setExpandedOpen}
          title={t('subagent_node.task_template', 'Task template')}
          meta="task_template"
          value={taskTemplate}
          onCommit={(next) => emitConfig({ task_template: next })}
          readOnly={readOnly}
          extensions={TASK_TEMPLATE_EXTENSIONS}
          placeholder={t(
            'subagent_node.task_template_placeholder',
            '# Task\nRead {{file_path}} and summarize its key points.\n\n# Instructions\nUse file tools to inspect the file and extract important information.\n\n# Output\nReturn the declared output fields.',
          )}
          testId="cfg-subagent-task-expanded-editor"
        />
        {wfId && nodeId && (
          <PromptDiffDialog
            open={historyOpen}
            onOpenChange={setHistoryOpen}
            wfId={wfId}
            nodeId={nodeId}
            currentPrompt={taskTemplate}
            field="task_template"
            title={t('subagent_node.task_history_title', 'Task template history')}
            subtitle={t(
              'subagent_node.task_history_subtitle',
              'Compare this sub-agent task template across workflow versions.',
            )}
          />
        )}
      </div>

      <div className="space-y-1.5">
        <Label className="text-xs font-medium">
          {t('subagent_node.model', 'Model')}
        </Label>
        <WorkflowModelSelect value={modelName} readOnly={readOnly} testId="cfg-subagent-model-select"
          onChange={(next) => emitConfig({ model_name: next })} />
      </div>

      <div className="space-y-1.5">
        <Label className="text-xs font-medium">
          {t('subagent_node.max_iterations', 'Max iterations')}
        </Label>
        <CommitOnBlurNumber
          kind="int"
          min={1}
          step={1}
          value={maxIterations}
          onCommit={(next) => emitConfig({ max_iterations: next })}
          disabled={readOnly}
          className="h-8 text-xs"
          data-testid="cfg-subagent-max-iterations"
        />
        <p className="text-xs leading-tight text-muted-foreground">
          {t(
            'subagent_node.max_iterations_hint',
            'Upper bound on the sub-agent reasoning/tool steps before it must finish.',
          )}
        </p>
      </div>
    </div>
  );
}
