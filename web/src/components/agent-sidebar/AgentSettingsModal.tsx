/**
 * AgentSettingsModal — default generation parameters for new Chats. MCP
 * servers and skills are installed globally for the tenant
 * and exposed to the agent through lightweight catalogs; this modal no longer
 * owns integration allowlists. Custom MCP selection belongs to the Project;
 * skill instructions and tool invocation authority remain scoped to each Turn.
 */
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  useAgentSettingsStore,
  type AgentSettings,
} from '@/stores/agent-settings';
import { AgentModelTab } from '@/components/agent-sidebar/tabs/AgentModelTab';

export interface AgentSettingsModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Retained for call-site compatibility; settings are model-only now. */
  modelOnly?: boolean;
}

/** Parse a numeric input string → number | null (empty/invalid = null). */
function parseNum(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === '') return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

export function AgentSettingsModal({
  open,
  onOpenChange,
}: AgentSettingsModalProps) {
  const { t } = useTranslation();
  const setAll = useAgentSettingsStore((s) => s.setAll);

  // Local draft — strings so empty fields round-trip to "use default".
  const [temperature, setTemperature] = useState('');
  const [maxTokens, setMaxTokens] = useState('');
  const [timeout, setTimeoutValue] = useState('');

  // Seed from the store every time the modal opens.
  useEffect(() => {
    if (!open) return;
    const s = useAgentSettingsStore.getState();
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      setTemperature(s.temperature != null ? String(s.temperature) : '');
      setMaxTokens(s.maxTokens != null ? String(s.maxTokens) : '');
      setTimeoutValue(s.timeout != null ? String(s.timeout) : '');
    });
    return () => {
      active = false;
    };
  }, [open]);

  const onSave = () => {
    const next: AgentSettings = {
      // Model/effort defaults are selected in an unstarted Chat's composer.
      // This advanced dialog must not overwrite that draft selection.
      modelId: useAgentSettingsStore.getState().modelId,
      temperature: parseNum(temperature),
      maxTokens: parseNum(maxTokens),
      timeout: parseNum(timeout),
      // Preserve the new-Chat reasoning default while editing advanced knobs.
      reasoningEffort: useAgentSettingsStore.getState().reasoningEffort,
    };
    setAll(next);
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="agent-settings-modal" className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t('agent_settings.title', 'Agent settings')}</DialogTitle>
          <DialogDescription>
            {t(
              'agent_settings.subtitle',
              'Choose which saved API credential and parameters the chat agent uses.',
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="max-h-[60vh] overflow-y-auto">
          <AgentModelTab
            temperature={temperature}
            setTemperature={setTemperature}
            maxTokens={maxTokens}
            setMaxTokens={setMaxTokens}
            timeout={timeout}
            setTimeout={setTimeoutValue}
          />
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            data-action="agent-settings-cancel"
            onClick={() => onOpenChange(false)}
          >
            {t('cancel', 'Cancel')}
          </Button>
          <Button data-action="agent-settings-save" onClick={onSave}>
            {t('save', 'Save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
