import { useEffect, useState } from 'react';

/**
 * Highlight one immutable source block without delaying the first render.
 * Shiki and the requested grammar stay outside the initial application chunk;
 * callers keep a plain-code fallback until highlighting is ready.
 */
export function useShiki(code: string, lang: string): string | null {
  const key = `${lang}\u0000${code}`;
  const [result, setResult] = useState<{ key: string; html: string | null } | null>(null);

  useEffect(() => {
    // Large execution payloads remain copyable/readable without an expensive
    // syntax-tokenisation pass blocking interaction on the main thread.
    if (code.length > 65_536) return;
    let cancelled = false;
    import('shiki')
      .then(({ codeToHtml }) => codeToHtml(code, {
        lang,
        themes: { light: 'github-light', dark: 'github-dark' },
        defaultColor: false,
      }))
      .then((html) => {
        if (!cancelled) setResult({ key, html });
      })
      .catch(() => {
        if (!cancelled) setResult({ key, html: null });
      });
    return () => {
      cancelled = true;
    };
  }, [code, key, lang]);

  return result?.key === key ? result.html : null;
}
