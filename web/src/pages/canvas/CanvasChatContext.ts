import { createContext, useContext } from 'react';
import type { components } from '@/lib/api/schema';

export type CanvasChatPoint = { x: number; y: number };
export type OpenCanvasChat = (target: components['schemas']['WorkflowChatTarget'], point: CanvasChatPoint) => void;
export const CanvasChatContext = createContext<OpenCanvasChat | null>(null);
export const useOpenCanvasChat = () => useContext(CanvasChatContext);
