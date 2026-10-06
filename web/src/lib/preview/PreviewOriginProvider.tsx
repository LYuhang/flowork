import type { ReactNode } from 'react';
import { PreviewOriginContext, type PreviewOrigin } from './context-origin';

export function PreviewOriginProvider({ origin, children }: { origin: PreviewOrigin | null; children: ReactNode }) {
  return <PreviewOriginContext.Provider value={origin}>{children}</PreviewOriginContext.Provider>;
}
