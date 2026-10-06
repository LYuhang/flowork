import { useContext, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

import { PreviewToolbarHost } from './preview-toolbar-context';

export function PreviewToolbar({ children }: { children: ReactNode }) {
  const host = useContext(PreviewToolbarHost);
  const content = <div className="flex shrink-0 items-center gap-1" data-role="preview-renderer-actions">{children}</div>;
  if (host === undefined) return <div className="flex min-h-10 shrink-0 items-center overflow-x-auto border-b border-edge-subtle px-2">{content}</div>;
  return host ? createPortal(content, host) : null;
}
