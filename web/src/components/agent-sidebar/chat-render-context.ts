import { createContext, useContext } from 'react';
import type { InteractiveArtifact } from './tool-render/interactive-artifact-contract';

export interface ChatRenderIdentity {
  chatId: string | null;
  onOpenInteractivePreview?: (artifact: InteractiveArtifact) => void;
  surface: 'chat' | 'browser';
}

const ChatRenderContext = createContext<ChatRenderIdentity | null>(null);

export const ChatRenderProvider = ChatRenderContext.Provider;
export const useChatRenderIdentity = () => useContext(ChatRenderContext);
