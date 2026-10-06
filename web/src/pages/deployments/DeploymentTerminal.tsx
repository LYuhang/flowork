import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { TerminalSquare, Unplug } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { CopyButton } from '@/components/ui/copy-button';
import { resolveApiUrl } from '@/lib/base-path';
import type { Deployment } from '@/lib/api/deployments';

type ConnectionState = 'idle' | 'connecting' | 'connected' | 'closed' | 'changed' | 'error';

/** One explicitly opened PTY per mounted tab; never auto-replay shell input. */
export function DeploymentTerminal({ dep }: { dep: Deployment }) {
  const { t } = useTranslation();
  const container = useRef<HTMLDivElement>(null);
  const cleanup = useRef<() => void>(() => {});
  const generation = useRef(0);
  const [state, setState] = useState<ConnectionState>('idle');
  const [revision, setRevision] = useState<string | null>(null);
  const available = dep.enabled && !!dep.active_revision_id;

  useEffect(() => () => { generation.current++; cleanup.current(); }, [dep.id]);

  const connect = async () => {
    cleanup.current();
    const attempt = ++generation.current;
    setState('connecting');
    setRevision(null);
    try {
      const [{ Terminal }, { FitAddon }] = await Promise.all([
        import('@xterm/xterm'), import('@xterm/addon-fit'), import('@xterm/xterm/css/xterm.css'),
      ]);
      if (generation.current !== attempt || !container.current) return;
      const term = new Terminal({
        cursorBlink: true, fontSize: 13, scrollback: 2000,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        theme: {
          background: getComputedStyle(container.current).backgroundColor,
          foreground: getComputedStyle(container.current).color,
          cursor: getComputedStyle(container.current).color,
        },
        allowProposedApi: false,
      });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(container.current);
      const url = new URL(resolveApiUrl(`/api/v1/deployments/${encodeURIComponent(dep.id)}/terminal`), window.location.href);
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      const socket = new WebSocket(url);
      socket.binaryType = 'arraybuffer';
      let ended = false;
      let frame = 0;
      let ready = false;
      const finish = (next: ConnectionState) => {
        if (ended) return;
        ended = true;
        ready = false;
        socket.close();
        if (generation.current === attempt) setState(next);
      };
      const send = (message: unknown) => {
        if (socket.readyState !== WebSocket.OPEN || ended) return;
        if (socket.bufferedAmount > 262_144) { finish('error'); return; }
        socket.send(JSON.stringify(message));
      };
      const resize = () => {
        cancelAnimationFrame(frame);
        frame = requestAnimationFrame(() => {
          if (ended || !container.current?.clientWidth) return;
          fit.fit();
          if (ready) send({ type: 'resize', columns: term.cols, rows: term.rows });
        });
      };
      const observer = new ResizeObserver(resize);
      observer.observe(container.current);
      const input = term.onData((data) => {
        if (!ready) return;
        const bytes = new TextEncoder().encode(data);
        // Never silently truncate paste or leave a partial command queued.
        if (bytes.length > 131_072) { finish('error'); return; }
        for (let offset = 0; offset < bytes.length; offset += 12_000) {
          send({ type: 'input', data: btoa(String.fromCharCode(...bytes.subarray(offset, offset + 12_000))) });
        }
      });
      const timeout = window.setTimeout(() => { if (!ready) finish('error'); }, 15_000);
      socket.onmessage = (event) => {
        if (ended) return;
        if (event.data instanceof ArrayBuffer) {
          const data = new Uint8Array(event.data);
          term.write(data, () => send({ type: 'ack', bytes: data.byteLength }));
          return;
        }
        try {
          const message = JSON.parse(event.data as string);
          if (message.type === 'ready') {
            ready = true;
            clearTimeout(timeout);
            setRevision(message.revision_id);
            setState('connected');
            resize();
            term.focus();
          } else if (message.type === 'instance_changed') finish('changed');
          else if (message.type === 'exit') finish('closed');
          else if (message.type === 'error') finish('error');
        } catch { finish('error'); }
      };
      socket.onerror = () => finish('error');
      socket.onclose = () => finish(ready ? 'closed' : 'error');
      cleanup.current = () => {
        ended = true;
        clearTimeout(timeout);
        cancelAnimationFrame(frame);
        observer.disconnect();
        input.dispose();
        socket.onmessage = socket.onerror = socket.onclose = null;
        socket.close();
        term.dispose();
      };
    } catch {
      if (generation.current === attempt) { cleanup.current(); setState('error'); }
    }
  };
  const disconnect = () => {
    generation.current++;
    cleanup.current();
    setState('closed');
  };
  const busy = state === 'connected' || state === 'connecting';
  return (
    <section className="min-w-0 space-y-4 py-3" aria-label={t('deployments.terminal.title')}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <h2 className="flex items-center gap-2 text-sm font-semibold"><TerminalSquare className="size-4" />{t('deployments.terminal.title')}</h2>
          <p className="max-w-2xl text-sm leading-6 text-content-secondary">{t('deployments.terminal.description')}</p>
        </div>
        {busy ? <Button variant="outline" onClick={disconnect}><Unplug className="size-4" />{t('deployments.terminal.disconnect')}</Button>
          : <Button disabled={!available} onClick={() => void connect()}>{t(state === 'idle' ? 'deployments.terminal.connect' : 'deployments.terminal.reconnect')}</Button>}
      </div>
      <div role="status" className="flex flex-wrap items-center gap-2 text-xs text-content-secondary">
        <span>{t(`deployments.terminal.state.${state}`)}</span>
        {revision && <><span className="font-mono">{revision.slice(0, 8)}</span><CopyButton value={revision} /></>}
      </div>
      {!available && <p className="text-sm text-content-secondary">{t('deployments.terminal.unavailable')}</p>}
      <div ref={container} className={`${state === 'idle' ? 'hidden' : ''} h-[min(60vh,36rem)] min-h-72 min-w-0 overflow-hidden rounded-xl border border-edge-subtle bg-[#111827] p-3`} />
      <p className="text-xs leading-5 text-content-tertiary">{t('deployments.terminal.hint')}</p>
    </section>
  );
}
