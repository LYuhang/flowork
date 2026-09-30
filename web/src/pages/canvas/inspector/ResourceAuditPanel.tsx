import { useTranslation } from 'react-i18next';
import type { ResourceAudit } from '@/lib/resource-audit';

export function ResourceAuditPanel({ audit }: { audit?: ResourceAudit }) {
  const { t } = useTranslation();
  if (!audit) return null;
  return <details className="rounded-md border p-3 text-xs" data-testid="resource-audit">
    <summary className="cursor-pointer font-medium">{t('subagent_audit.title', 'Resource and tool activity')} · {audit.tools.length}</summary>
    <div className="mt-3 max-h-80 space-y-3 overflow-auto break-words">
      <p className="text-muted-foreground">{t('subagent_audit.note', 'Declared resources and observed tool calls. A referenced path alone does not prove that a file was read.')}</p>
      {audit.skills.map(s => <div key={s.id}><strong>{s.name}</strong><div className="font-mono text-muted-foreground">{s.id}<br />{s.revision_hash}</div></div>)}
      {audit.mcp_servers.map(s => <div key={s.id}><strong>{s.name}</strong><div className="font-mono text-muted-foreground">{s.id}<br />{s.tools_fingerprint}</div></div>)}
      <ol className="space-y-2">{audit.tools.map((tool, i) => <li key={i} className="rounded bg-muted/50 p-2">
        <div className="flex flex-wrap justify-between gap-2"><code>{tool.name}</code><span>{tool.status}{tool.exit_code !== undefined ? ` · exit ${tool.exit_code}` : ''}</span></div>
        {tool.skill_paths?.map(path => <div key={path} className="mt-1 break-all font-mono text-muted-foreground">{path}</div>)}
      </li>)}</ol>
      {audit.truncated && <p>{t('subagent_audit.truncated', 'Activity was truncated at the recording limit.')}</p>}
    </div>
  </details>;
}
