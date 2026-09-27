/**
 * Agent settings store — defaults used to seed an unstarted Chat plus the
 * per-turn authorization policy.
 *
 * These are per-user defaults, so we persist them to
 * localStorage and mirror them in this tiny zustand store for reactivity —
 * exactly the seam `@/lib/timezone` uses (localStorage = persisted source of
 * truth at bootstrap; the store = the runtime mirror that re-renders
 * subscribers on change).
 *
 * `approvalMode` is the default for the next turn. Active turns keep their
 * server-side policy; changing this preference cannot approve a pending request.
 *
 * SECRET BOUNDARY: the runtime capability API returns opaque model selection
 * IDs. Provider credentials and api_key values never reach this store.
 *
 * Empty/unset fields mean "use the provider default" only for generation
 * knobs. The composer materializes a concrete catalog model id before a new
 * Chat starts; an unavailable saved id is preserved and rejected rather than
 * silently replaced with another credential:
 *   - `modelId === null`       → draft has not hydrated its model catalog yet
 *   - a `null` hyperparameter  → omitted from the request → provider default
 */
import { create } from 'zustand';

export interface AgentSettings {
  /** Opaque id from the bound runtime's capability catalog. */
  modelId: string | null;
  /** Sampling temperature (e.g. 0–2), or null for the provider default. */
  temperature: number | null;
  /** Max output tokens, or null for the provider default. */
  maxTokens: number | null;
  /** Per-request timeout (seconds), or null for the provider default. */
  timeout: number | null;
  /** Default reasoning effort for a new Chat. */
  reasoningEffort: ReasoningEffort | null;
}

export type ApprovalMode = 'agent' | 'always_ask' | 'always_allow';
/** Runtime-advertised value; intentionally not a frontend-owned enum. */
export type ReasoningEffort = string;

export interface AgentSettingsState extends AgentSettings {
  /** Authorization policy captured when the next turn is submitted. */
  approvalMode: ApprovalMode;
  /** Replace all four fields at once (the modal Save). */
  setAll: (next: AgentSettings) => void;
  /** Patch a subset (handy for tests / future inline controls). */
  set: (patch: Partial<AgentSettings>) => void;
  setApprovalMode: (mode: ApprovalMode) => void;
  /** Clear account-scoped agent model selection on auth boundary changes. */
  reset: () => void;
}

export const STORAGE_KEY = 'vibecanvas.agentSettings.v2';

const EMPTY: AgentSettings = {
  modelId: null,
  temperature: null,
  maxTokens: null,
  timeout: null,
  reasoningEffort: null,
};

const DEFAULT_APPROVAL_MODE: ApprovalMode = 'always_allow';

function bootstrap(): AgentSettings & { approvalMode: ApprovalMode } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...EMPTY, approvalMode: DEFAULT_APPROVAL_MODE };
    const parsed = JSON.parse(raw) as Partial<AgentSettingsState>;
    return {
      modelId: typeof parsed.modelId === 'string' ? parsed.modelId : null,
      temperature:
        typeof parsed.temperature === 'number' ? parsed.temperature : null,
      maxTokens: typeof parsed.maxTokens === 'number' ? parsed.maxTokens : null,
      timeout: typeof parsed.timeout === 'number' ? parsed.timeout : null,
      reasoningEffort:
        typeof parsed.reasoningEffort === 'string' && parsed.reasoningEffort.trim()
          ? parsed.reasoningEffort
          : null,
      approvalMode: parsed.approvalMode === 'agent' || parsed.approvalMode === 'always_ask'
        || parsed.approvalMode === 'always_allow' ? parsed.approvalMode : DEFAULT_APPROVAL_MODE,
    };
  } catch {
    return { ...EMPTY, approvalMode: DEFAULT_APPROVAL_MODE };
  }
}

/** Persist the current settings; private-mode/quota errors are swallowed. */
function persist(s: AgentSettings & { approvalMode?: ApprovalMode }): void {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        modelId: s.modelId,
        temperature: s.temperature,
        maxTokens: s.maxTokens,
        timeout: s.timeout,
        reasoningEffort: s.reasoningEffort,
        approvalMode: s.approvalMode ?? useAgentSettingsStore.getState().approvalMode,
      }),
    );
  } catch {
    // keep the in-memory switch working regardless
  }
}

export const useAgentSettingsStore = create<AgentSettingsState>((set, get) => ({
  ...bootstrap(),
  setAll: (next) => {
    persist(next);
    set({ ...next });
  },
  set: (patch) => {
    const next = { ...currentSettings(get()), ...patch };
    persist(next);
    set({ ...patch });
  },
  setApprovalMode: (approvalMode) => {
    persist({ ...currentSettings(get()), approvalMode });
    set({ approvalMode });
  },
  reset: () => {
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      // keep reset best-effort
    }
    set({ ...EMPTY, approvalMode: DEFAULT_APPROVAL_MODE });
  },
}));

/** Pluck just the data fields out of the full state (drop the setters). */
function currentSettings(s: AgentSettingsState): AgentSettings {
  return {
    modelId: s.modelId,
    temperature: s.temperature,
    maxTokens: s.maxTokens,
    timeout: s.timeout,
    reasoningEffort: s.reasoningEffort,
  };
}

/**
 * Non-reactive snapshot of the current model settings — used by the SSE request
 * builder (which runs outside React and just needs the value at send time).
 */
export function getAgentSettings(): AgentSettings {
  return currentSettings(useAgentSettingsStore.getState());
}

export function getApprovalMode(): ApprovalMode {
  return useAgentSettingsStore.getState().approvalMode;
}
