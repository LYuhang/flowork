import { ModelRetryField } from './ModelRetryField';
/** PromptNode editor. Models are restricted to authorized manually added APIs. */
import { Label } from '@/components/ui/label';
import {
  CommitOnBlurNumber,
  CommitOnBlurTextarea,
} from '@/pages/canvas/inspector/CommitOnBlur';
import { useWorkflowModels } from '@/lib/api/queries/llm-credentials';
import { WorkflowModelSelect } from './WorkflowModelSelect';
import { useTranslation } from 'react-i18next';
import { useMemo, useState } from 'react';
import { History, Maximize2 } from 'lucide-react';
import type { Extension } from '@codemirror/state';
import { Button } from '@/components/ui/button';
import { PromptDiffDialog } from '@/components/modals/PromptDiffDialog';
import { useWorkflowVersions } from '@/lib/api/queries/workflow';
import { CodeMirrorField } from './CodeMirrorField';
import { ExpandedCodeMirrorDialog } from './ExpandedCodeMirrorDialog';
import {
  missingOutputFields,
  placeholderHighlight,
  placeholderTheme,
} from './prompt-template';
import type { NodeConfigEditorProps } from './types';

/** Live {{placeholder}} highlight extensions (stable identity). */
const PROMPT_EXTENSIONS: Extension[] = [placeholderHighlight, placeholderTheme];


/** Providers whose engine class actually forwards `extra_body` to the request
 * (OpenAIModel / AzureOpenAIModel, both on the OpenAI SDK). AnthropicModel and
 * GeminiModel build their own request and ignore it, so the field is hidden for
 * them. Kept in sync with engine `custom_llms.py`. */
const EXTRA_BODY_PROVIDERS = ['openai', 'azure_openai'];

export function PromptNodeEditor({
  config,
  readOnly,
  onChange,
  outputFieldNames,
  nodeId,
  wfId,
}: NodeConfigEditorProps) {
  const { t } = useTranslation();
  // Prompt-template version-diff modal. The "History" button only renders when
  // this node lives in a workflow with >= 2 versions (otherwise there's
  // nothing to compare). The version list is cheap + shared with the modal.
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
  const { data: modelCatalog } = useWorkflowModels();

  const promptTemplate =
    typeof config.prompt_template === 'string'
      ? (config.prompt_template as string)
      : '';
  // Output fields the template does NOT reference as a quoted "name"/'name'.
  // Updates live as the template OR the declared output fields change.
  const missingFields = useMemo(
    () => missingOutputFields(promptTemplate, outputFieldNames ?? []),
    [promptTemplate, outputFieldNames],
  );
  const modelName =
    typeof config.model_name === 'string'
      ? (config.model_name as string)
      : '';
  const inference =
    config.inference_config &&
    typeof config.inference_config === 'object' &&
    !Array.isArray(config.inference_config)
      ? (config.inference_config as Record<string, unknown>)
      : {};
  const selectedModel = modelCatalog?.models[modelName];
  const extraBodySupported = !!selectedModel && EXTRA_BODY_PROVIDERS.includes(selectedModel.provider);

  const updateInference = (next: Record<string, unknown>) => {
    onChange({ ...config, inference_config: next });
  };
  // Engine-aligned defaults (see CONFIG_SCHEMA in
  // engine/src/vibecanvas_engine/nodes/prompt.py). Rendered when the key is
  // absent so a fresh PromptNode satisfies the required-keys check.
  const DEFAULT_TEMPERATURE = 1.0;
  const DEFAULT_MAX_TOKENS = 512;
  const DEFAULT_TOP_K = -1;
  const DEFAULT_TOP_P = 0.9;

  const temperature =
    typeof inference.temperature === 'number'
      ? (inference.temperature as number)
      : DEFAULT_TEMPERATURE;
  const maxTokens =
    typeof inference.max_tokens === 'number'
      ? (inference.max_tokens as number)
      : DEFAULT_MAX_TOKENS;
  const topK =
    typeof inference.top_k === 'number'
      ? (inference.top_k as number)
      : DEFAULT_TOP_K;
  const topP =
    typeof inference.top_p === 'number'
      ? (inference.top_p as number)
      : DEFAULT_TOP_P;
  // extra_body: an optional JSON object string merged into the model request
  // body (OpenAI-compatible). Stored raw; soft-validated (non-blocking).
  const extraBody =
    typeof inference.extra_body === 'string' ? (inference.extra_body as string) : '';
  const extraBodyInvalid =
    extraBody.trim() !== '' &&
    (() => {
      try {
        const v = JSON.parse(extraBody);
        return typeof v !== 'object' || v === null || Array.isArray(v);
      } catch {
        return true;
      }
    })();

  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <Label className="text-xs font-medium">prompt_template</Label>
          <div className="flex items-center gap-1">
            {showHistoryButton && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-6 gap-1 px-2 text-xs text-muted-foreground"
                onClick={() => setHistoryOpen(true)}
                data-testid="cfg-prompt-history-btn"
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
              data-testid="cfg-prompt-expand-btn"
            >
              <Maximize2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
        <CodeMirrorField
          value={promptTemplate}
          onCommit={(next) =>
            onChange({ ...config, prompt_template: next })
          }
          readOnly={readOnly}
          data-testid="cfg-prompt-template"
          extensions={PROMPT_EXTENSIONS}
          minHeight="160px"
          placeholder="Prompt with {{variable}} interpolation slots…"
        />
        <ExpandedCodeMirrorDialog
          open={expandedOpen}
          onOpenChange={setExpandedOpen}
          title="prompt_template"
          meta="template"
          value={promptTemplate}
          onCommit={(next) =>
            onChange({ ...config, prompt_template: next })
          }
          readOnly={readOnly}
          extensions={PROMPT_EXTENSIONS}
          placeholder="Prompt with {{variable}} interpolation slots…"
          testId="cfg-prompt-expanded-editor"
        />
        {missingFields.length > 0 && (
          <div className="space-y-0.5" data-testid="cfg-prompt-missing-fields">
            {missingFields.map((name) => (
              <p
                key={name}
                className="text-xs leading-tight text-state-danger"
              >
                {t('prompt_node.missing_output_field', {
                  name,
                  defaultValue: 'Output field {{name}} not referenced',
                })}
              </p>
            ))}
          </div>
        )}
      </div>

      <div className="space-y-1.5">
        <Label className="text-xs font-medium">model</Label>
        <WorkflowModelSelect value={modelName} readOnly={readOnly} testId="cfg-prompt-model-select"
          onChange={(next) => {
            const updated: Record<string, unknown> = { ...config, model_name: next };
            delete updated.custom_model_config;
            onChange(updated);
          }} />
      </div>

      <ModelRetryField value={config.retry} readOnly={!!readOnly} onChange={next => onChange({ ...config, retry: next })} />

      <div className="space-y-1.5">
        <Label className="text-xs font-medium">inference_config</Label>
        <div className="grid grid-cols-[auto_1fr] items-center gap-x-2 gap-y-1.5">
          <Label className="text-xs text-muted-foreground">temperature</Label>
          <CommitOnBlurNumber
            kind="float"
            step="0.1"
            min={0}
            max={2}
            value={temperature}
            onCommit={(next) =>
              updateInference({ ...inference, temperature: next })
            }
            disabled={readOnly}
            className="h-8 text-xs"
          />
          <Label className="text-xs text-muted-foreground">max_tokens</Label>
          <CommitOnBlurNumber
            kind="int"
            min={1}
            step={1}
            value={maxTokens}
            onCommit={(next) =>
              updateInference({ ...inference, max_tokens: next })
            }
            disabled={readOnly}
            className="h-8 text-xs"
          />
          <Label className="text-xs text-muted-foreground">top_k</Label>
          <CommitOnBlurNumber
            kind="int"
            min={-1}
            step={1}
            value={topK}
            onCommit={(next) =>
              updateInference({ ...inference, top_k: next })
            }
            disabled={readOnly}
            className="h-8 text-xs"
          />
          <Label className="text-xs text-muted-foreground">top_p</Label>
          <CommitOnBlurNumber
            kind="float"
            step="0.05"
            min={0}
            max={1}
            value={topP}
            onCommit={(next) =>
              updateInference({ ...inference, top_p: next })
            }
            disabled={readOnly}
            className="h-8 text-xs"
          />
        </div>

        {extraBodySupported && (
          <div className="space-y-1">
            <Label className="text-xs text-muted-foreground">extra_body</Label>
            <CommitOnBlurTextarea
              value={extraBody}
              onCommit={(next) =>
                updateInference({ ...inference, extra_body: next })
              }
              disabled={readOnly}
              spellCheck={false}
              className="min-h-[56px] font-mono text-xs"
              placeholder={'{"reasoning_effort": "high"}'}
              data-testid="cfg-prompt-extra-body"
            />
            <p className="text-xs leading-tight text-muted-foreground">
              {t(
                'inspector.config.prompt.extraBodyHint',
                'Optional JSON object merged into the model request body.',
              )}
            </p>
            {extraBodyInvalid && (
              <p className="text-xs leading-tight text-state-warning">
                {t(
                  'inspector.config.prompt.extraBodyInvalid',
                  'Not valid JSON — saved as-is; the model will ignore it until fixed.',
                )}
              </p>
            )}
          </div>
        )}
      </div>

      {showHistoryButton && wfId && nodeId && (
        <PromptDiffDialog
          open={historyOpen}
          onOpenChange={setHistoryOpen}
          wfId={wfId}
          nodeId={nodeId}
          currentPrompt={promptTemplate}
        />
      )}
    </div>
  );
}
