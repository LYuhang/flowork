import { createContext } from 'react';

/** Embedded conversations open branches in their owning surface. */
export const ChatBranchNavigationContext = createContext<((chatId: string) => void) | null>(null);
