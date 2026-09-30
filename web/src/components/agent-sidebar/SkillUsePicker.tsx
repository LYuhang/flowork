import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ExternalLink } from 'lucide-react';
import { useSkills } from '@/lib/api/queries/skills';
import type { Skill } from '@/lib/api/skills';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';

export function SkillUsePicker({ onSelect, onClose }: {
  onSelect: (skill: Skill) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [search, setSearch] = useState('');
  const query = useSkills({ refreshOnReturn: true });
  const skills = (query.data ?? []).filter((skill) => skill.access?.capabilities.includes('use')
    && `${skill.name} ${skill.description} ${skill.source} ${skill.id}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()));
  return <div role="dialog" aria-label={t('composer.skill.select', 'Select a Skill')}
    className="absolute bottom-full left-0 right-0 z-50 mb-2 rounded-xl border bg-popover p-2 shadow-lg"
    onKeyDown={(event) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onClose(); return; }
      const choices = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[data-skill-choice]'));
      const index = choices.indexOf(document.activeElement as HTMLButtonElement);
      if (choices.length && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
        event.preventDefault();
        choices[(index + (event.key === 'ArrowDown' ? 1 : choices.length - 1) + choices.length) % choices.length]?.focus();
      } else if (event.key === 'Enter' && event.target instanceof HTMLInputElement && !event.nativeEvent.isComposing) {
        event.preventDefault();
        choices[0]?.click();
      }
    }}>
    <div className="mb-2 flex items-center gap-2">
      <Button variant="ghost" size="sm" onClick={onClose}>{t('back', 'Back')}</Button>
      <Input autoFocus value={search} onChange={(event) => setSearch(event.target.value)}
        aria-label={t('composer.skill.search', 'Search Skills')} placeholder={t('composer.skill.search', 'Search Skills')} />
    </div>
    <div className="max-h-64 overflow-y-auto" role="list" aria-label={t('composer.skill.select', 'Select a Skill')}>
      {query.isPending ? <p className="p-3 text-sm text-muted-foreground">{t('skills.loading', 'Loading…')}</p>
        : query.isError ? <Button variant="ghost" onClick={() => void query.refetch()}>{t('retry', 'Retry')}</Button>
        : skills.length === 0 ? <p className="p-3 text-sm text-muted-foreground">{t('composer.skill.empty', 'No available Skills found.')}</p>
        : skills.map((skill) => <div role="listitem" key={skill.id} className="flex items-center rounded-lg hover:bg-accent/50">
          <button data-skill-choice type="button" className="min-w-0 flex-1 rounded-lg p-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus" onClick={() => onSelect(skill)}>
            <span className="block truncate text-sm font-medium">{skill.name}</span>
            <span className="block truncate text-xs text-muted-foreground">{skill.source} · {skill.provenance?.created_by?.display_name ?? skill.provenance?.owner?.display_name} · {skill.id}</span>
          </button>
          <a href={`/skills/${encodeURIComponent(skill.id)}`} target="_blank" rel="noreferrer" className="shrink-0 rounded-md p-2 hover:bg-accent"
            aria-label={t('composer.skill.details', {name:skill.name, defaultValue:'Open {{name}} details'})}>
            <ExternalLink className="size-4" />
          </a>
        </div>)}
    </div>
  </div>;
}
