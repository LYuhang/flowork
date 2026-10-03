import { createContext, useContext, type ReactNode } from 'react';
import type { components } from '@/lib/api/schema';

export type CanvasChatPoint = { x: number; y: number };
export type OpenCanvasChat = (target: components['schemas']['WorkflowChatTarget'], point: CanvasChatPoint) => void;
export const CanvasChatContext = createContext<OpenCanvasChat | null>(null);
export const useOpenCanvasChat = () => useContext(CanvasChatContext);

export const CanvasChatControlsContext = createContext<ReactNode>(null);
export const useCanvasChatControls = () => useContext(CanvasChatControlsContext);

export const CanvasChatReferenceContext = createContext<{
  enabled: boolean;
  add: (target: components['schemas']['WorkflowChatTarget']) => Promise<void>;
} | null>(null);
export const useCanvasChatReference = () => useContext(CanvasChatReferenceContext);
