import type { VfsReadOut } from '@/lib/api/vfs';
import { parseDelimitedTable } from '@/lib/files/delimited';
import { useTranslation } from 'react-i18next';

function parseRows(entry: VfsReadOut): Record<string, unknown>[] {
  const ct = entry.content_type.toLowerCase();
  if (ct === 'table/jsonl') {
    return (entry.content ?? '')
      .split('\n')
      .filter((l) => l.trim())
      .map((l) => {
        try {
          return JSON.parse(l) as Record<string, unknown>;
        } catch {
          return { _raw: l };
        }
      });
  }
  return parseDelimitedTable(entry.content ?? '', ct === 'table/tsv' ? '\t' : ',').rows;
}

export function TableRenderer({ entry }: { entry: VfsReadOut }) {
  const { t } = useTranslation();
  const rows = parseRows(entry);
  if (rows.length === 0) return <div className="text-sm text-muted-foreground">{t('table.empty', 'No rows.')}</div>;
  const cols = Array.from(new Set(rows.flatMap((r) => Object.keys(r))));
  return (
    <div className="overflow-auto">
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c} className="border-b px-2 py-1 text-left font-semibold">{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {cols.map((c) => (
                <td key={c} className="border-b px-2 py-1 align-top">
                  {typeof r[c] === 'object' ? JSON.stringify(r[c]) : String(r[c] ?? '')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
