import { createContext } from 'react';

/** Renderer actions share the file toolbar. */
export const PreviewToolbarHost = createContext<HTMLElement | null | undefined>(undefined);
