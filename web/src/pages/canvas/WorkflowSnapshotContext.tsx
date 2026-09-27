import { createContext, useContext } from 'react';
import type { WorkflowDraft } from '@/stores/workflow-edit';

/** A snapshot is not the live editor draft, even when node IDs coincide. */
export const WorkflowSnapshotContext = createContext<WorkflowDraft | null>(null);
export const useWorkflowSnapshot = () => useContext(WorkflowSnapshotContext);
