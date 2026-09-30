import { createContext, useContext, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

/** Renderer actions share the file toolbar instead of adding another header. */
export const PreviewToolbarHost = createContext<HTMLElement | null | undefined>(undefined);
export function PreviewToolbar({ children }: { children: ReactNode }) {
  const host = useContext(PreviewToolbarHost);
  const content = <div className="flex shrink-0 items-center gap-1" data-role="preview-renderer-actions">{children}</div>;
  if (host === undefined) return <div className="flex min-h-10 shrink-0 items-center overflow-x-auto border-b border-edge-subtle px-2">{content}</div>;
  return host ? createPortal(content, host) : null;
}
