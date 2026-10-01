import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import i18n from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import en from '@/lib/i18n/locales/en.json';
import zh from '@/lib/i18n/locales/zh.json';
import { EmptyChatExamples } from '../EmptyChatExamples';

beforeAll(async () => {
  await i18n.use(initReactI18next).init({
    lng: 'en',
    fallbackLng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
});

afterEach(async () => {
  await i18n.changeLanguage('en');
});

function renderExamples(visible: boolean, onSelect = vi.fn()) {
  render(
    <I18nextProvider i18n={i18n}>
      <EmptyChatExamples visible={visible} onSelect={onSelect} />
    </I18nextProvider>,
  );
  return onSelect;
}

const scenarios = [
  ['tables', ['tables.clean', 'tables.audit', 'office.spreadsheet']],
  ['reports', ['office.report', 'office.presentation', 'reports.weekly']],
  ['automation', ['automation.orderAudit', 'operations.schedule', 'operations.deploy']],
  ['diagrams', ['diagram.architecture', 'diagram.process', 'diagram.sequence']],
  ['knowledge', ['knowledge.create', 'knowledge.explore', 'knowledge.update']],
  ['more', ['skill.template', 'skill.download', 'skill.update']],
] as const;

describe('EmptyChatExamples', () => {
  it.each(['en', 'zh'])('groups examples by user goals and fills localized drafts in %s', async (language) => {
    await i18n.changeLanguage(language);
    const locale = language === 'zh' ? zh : en;
    const onSelect = renderExamples(true);
    const user = userEvent.setup();
    expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual(
      scenarios.map(([id]) => locale[`chat.examples.scenario.${id}`]),
    );
    expect(screen.getAllByRole('tab')[0]).toHaveAttribute('aria-selected', 'true');
    expect(onSelect).not.toHaveBeenCalled();
    for (const [id, keys] of scenarios) {
      await user.click(screen.getByRole('tab', { name: locale[`chat.examples.scenario.${id}`], exact: true }));
      expect(screen.getAllByRole('button')).toHaveLength(3);
      for (const key of keys) {
        const card = screen.getByRole('button', { name: new RegExp(locale[`chat.examples.${key}.title`]) });
        expect(card).toHaveTextContent(locale[`chat.examples.${key}.description`]);
        await user.click(card);
        expect(onSelect).toHaveBeenLastCalledWith(
          locale[`chat.examples.${key}.prompt`].replaceAll('{{publicUrl}}', window.location.origin),
        );
      }
    }
    expect(onSelect).toHaveBeenCalledTimes(18);
  });

  it('renders only for an empty Chat', () => {
    const { rerender } = render(<I18nextProvider i18n={i18n}>
      <EmptyChatExamples visible onSelect={vi.fn()} />
    </I18nextProvider>);
    expect(screen.getByRole('region', { name: 'Start with an example' })).toBeInTheDocument();
    rerender(<I18nextProvider i18n={i18n}>
      <EmptyChatExamples visible={false} onSelect={vi.fn()} />
    </I18nextProvider>);
    expect(screen.queryByRole('region')).not.toBeInTheDocument();
  });

  it('keeps knowledge examples under the materials scenario', async () => {
    const onSelect = renderExamples(true);
    expect(screen.queryByRole('tab', { name: 'Web research' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: 'Organize knowledge' }));
    for (const card of screen.getAllByRole('button')) await userEvent.click(card);
    expect(onSelect).toHaveBeenCalledTimes(3);
    for (const [prompt] of onSelect.mock.calls) expect(prompt).toMatch(/^\/knowledge /);
  });
});
