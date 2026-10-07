import { useTranslation } from 'react-i18next';
import { ArrowDownUp } from 'lucide-react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';

export function ResourceSort({ fields, value, onValueChange }: {
  fields: readonly string[]; value: string; onValueChange: (value: string) => void;
}) {
  const { t } = useTranslation();
  return <Select value={value} onValueChange={onValueChange}>
    <SelectTrigger className="w-64 max-w-full" aria-label={t('resources.sort.label')}>
      <ArrowDownUp className="mr-2 size-4 shrink-0" aria-hidden="true" />
      <SelectValue />
    </SelectTrigger>
    <SelectContent>{fields.flatMap(field => (['desc', 'asc'] as const).map(direction =>
      <SelectItem key={`${field}:${direction}`} value={`${field}:${direction}`}>
        {t('resources.sort.option', { field: t(`resources.sort.${field}`), direction: t(`resources.sort.${field === 'name' ? 'name_' : ''}${direction}`) })}
      </SelectItem>
    ))}</SelectContent>
  </Select>;
}
