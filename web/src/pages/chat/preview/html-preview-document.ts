import type { PreviewResourceSessionV1 } from '@/lib/preview/protocol';

export const FILE_PREVIEW_CHANNEL = 'vibecanvas:file-preview:v1';

function inlineJson(value: unknown): string {
  return JSON.stringify(value)
    .replace(/</g, '\\u003c')
    .replace(/\u2028/g, '\\u2028')
    .replace(/\u2029/g, '\\u2029');
}

function escapeAttribute(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function createCspNonce(): string {
  const bytes = new Uint8Array(18);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
}

function inertHtmlFile(html: string): string {
  const parsed = new DOMParser().parseFromString(html, 'text/html');
  parsed.querySelectorAll('script, iframe, object, embed').forEach((element) => element.remove());
  parsed.querySelectorAll('meta[http-equiv]').forEach((element) => {
    const directive = element.getAttribute('http-equiv')?.trim().toLowerCase();
    if (directive === 'refresh' || directive === 'content-security-policy') {
      element.remove();
    }
  });
  parsed.querySelectorAll('*').forEach((element) => {
    for (const attribute of [...element.attributes]) {
      const name = attribute.name.toLowerCase();
      const value = attribute.value.trim();
      if (
        name.startsWith('on')
        || name === 'srcdoc'
        || (/^(?:href|src|action|formaction|poster|xlink:href)$/i.test(name)
          && /^javascript\s*:/i.test(value))
      ) {
        element.removeAttribute(attribute.name);
      }
    }
  });
  return `<!doctype html>${parsed.documentElement.outerHTML}`;
}

export function buildFilePreviewHtmlDocument(
  html: string,
  session: PreviewResourceSessionV1,
): string {
  const inertHtml = inertHtmlFile(html);
  const mounts = [...session.resourceMounts]
    .map((mount) => ({
      pathPrefix: mount.pathPrefix.replace(/\/*$/, '/'),
      rootUrl: mount.rootUrl.replace(/\/*$/, '/'),
    }))
    .sort((left, right) => right.pathPrefix.length - left.pathPrefix.length);
  const roots = mounts.map((mount) => mount.rootUrl).join(' ');
  const nonce = createCspNonce();
  const csp = [
    "default-src 'none'",
    // Only the platform bridge may execute. Scripts embedded in a user/Agent
    // HTML file do not receive this per-render nonce.
    `script-src 'nonce-${nonce}'`,
    "style-src 'unsafe-inline'",
    // Silent subresource loads are restricted to the short-lived VFS
    // capability roots. Ordinary external anchors remain clickable in the
    // opaque sandbox, but Agent HTML cannot exfiltrate data through a remote
    // image/audio/video query string. Remote assets must first be saved to VFS.
    `img-src data: blob: ${roots}`,
    `media-src data: blob: ${roots}`,
    `font-src data: blob: ${roots}`,
    `connect-src data: blob: ${roots}`,
    "worker-src 'none'",
    "object-src 'none'",
    "frame-src 'none'",
    "child-src 'none'",
    "form-action 'none'",
  ].join('; ');
  const bootstrap = `
<meta http-equiv="Content-Security-Policy" content="${escapeAttribute(csp)}">
<meta name="referrer" content="no-referrer">
<base href="${escapeAttribute(session.baseUrl)}">
<script nonce="${escapeAttribute(nonce)}">
(() => {
  const CHANNEL = ${inlineJson(FILE_PREVIEW_CHANNEL)};
  const MOUNTS = ${inlineJson(mounts)};
  const resolveUrl = (value) => {
    if (typeof value !== 'string' || !value) return value;
    const mount = MOUNTS.find((item) => value.startsWith(item.pathPrefix));
    return mount
      ? mount.rootUrl + value.slice(mount.pathPrefix.length).replace(/^\\/+/, '')
      : value;
  };
  const attrs = new Set(['src', 'href', 'poster']);
  const nativeSet = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function(name, value) {
    return nativeSet.call(this, name, attrs.has(String(name).toLowerCase())
      ? resolveUrl(String(value))
      : value);
  };
  const rewrite = (root) => {
    if (!(root instanceof Element)) return;
    for (const name of attrs) {
      if (root.hasAttribute(name)) {
        nativeSet.call(root, name, resolveUrl(root.getAttribute(name)));
      }
    }
    root.querySelectorAll('[src],[href],[poster]').forEach(rewrite);
  };
  new MutationObserver((records) => records.forEach((record) =>
    record.addedNodes.forEach(rewrite)
  )).observe(document.documentElement, { childList: true, subtree: true });
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    if (typeof input === 'string') return nativeFetch(resolveUrl(input), init);
    if (input instanceof URL) return nativeFetch(new URL(resolveUrl(input.toString())), init);
    if (input instanceof Request) return nativeFetch(new Request(resolveUrl(input.url), input), init);
    return nativeFetch(input, init);
  };
  document.addEventListener('click', (event) => {
    const anchor = event.target instanceof Element ? event.target.closest('a[href]') : null;
    if (!anchor) return;
    const raw = anchor.getAttribute('href') || '';
    if (!raw.startsWith('/')) return;
    event.preventDefault();
    window.parent.postMessage({ channel: CHANNEL, type: 'preview.open', path: raw }, '*');
  }, true);
  document.addEventListener('contextmenu', (event) => {
    event.preventDefault();
    window.parent.postMessage({channel: CHANNEL, type: 'preview.context', x: event.clientX, y: event.clientY,
      text: (window.getSelection()?.toString() || '').slice(0, 32769)}, '*');
  });
  window.addEventListener('DOMContentLoaded', () => rewrite(document.documentElement), { once: true });
})();
</script>`;
  const head = /<head(?:\s[^>]*)?>/i;
  return inertHtml.replace(head, (match) => `${match}${bootstrap}`);
}
