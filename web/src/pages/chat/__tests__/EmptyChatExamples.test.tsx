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

describe('EmptyChatExamples', () => {
  it.each(['en', 'zh'])('specifies an editable DOCX report in %s instead of an ambiguous slide brief', async (language) => {
    await i18n.changeLanguage(language);
    const onSelect = renderExamples(true);
    await userEvent.click(document.querySelector('[data-example-id="office:report"]')!);
    expect(onSelect).toHaveBeenCalledWith(expect.stringContaining('DOCX'));
    expect(onSelect.mock.calls[0][0]).toMatch(/^\/document /);
  });

  it('renders only while the caller reports a genuinely empty Chat', () => {
    const { rerender } = render(
      <I18nextProvider i18n={i18n}>
        <EmptyChatExamples visible onSelect={vi.fn()} />
      </I18nextProvider>,
    );
    expect(screen.getByRole('region', { name: 'Start with an example' })).toBeInTheDocument();
    expect(screen.getByText('📊')).toBeInTheDocument();
    expect(screen.getAllByText('/document')).toHaveLength(3);
    rerender(
      <I18nextProvider i18n={i18n}>
        <EmptyChatExamples visible={false} onSelect={vi.fn()} />
      </I18nextProvider>,
    );
    expect(screen.queryByRole('region', { name: 'Start with an example' })).toBeNull();
  });

  it('uses explicit grid rows so cards with shorter copy keep icons top-aligned', () => {
    renderExamples(true);

    const cards = screen.getAllByRole('button', { name: /创建|create|撰写|write|build/i });
    expect(cards).toHaveLength(3);
    cards.forEach((card) => {
      expect(card).toHaveClass(
        'grid',
        'grid-rows-[auto_auto_1fr_auto]',
        'content-start',
        'items-stretch',
      );
      expect(card.querySelector('[data-role="example-card-icon"]')).not.toBeNull();
    });
  });

  it('switches categories and fills an editable prompt without sending it', async () => {
    const user = userEvent.setup();
    const onSelect = renderExamples(true);

    await user.click(screen.getByRole('tab', { name: 'Tasks and deployments' }));
    await user.click(screen.getByRole('button', { name: /run a workflow batch/i }));
    expect(onSelect).toHaveBeenLastCalledWith(expect.stringMatching(/^\/workflow /));
    await user.click(screen.getByRole('button', { name: /publish a workflow api/i }));

    expect(onSelect).toHaveBeenCalledTimes(2);
    expect(onSelect.mock.calls[1]?.[0]).toMatch(/^\/deployment /);
  });

  it('offers diagram examples for architecture, process, and sequence diagrams', async () => {
    const user = userEvent.setup();
    const onSelect = renderExamples(true);

    await user.click(screen.getByRole('tab', { name: 'Diagram' }));
    expect(
      screen.getByRole('button', { name: /visualize a system architecture/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /map a business process/i })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /explain an interaction sequence/i }));

    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect.mock.calls[0]?.[0]).toMatch(/^\/diagram /);
  });

  it('uses native Chinese copy while preserving slash-command tokens', async () => {
    await i18n.changeLanguage('zh');
    const user = userEvent.setup();
    const onSelect = renderExamples(true);

    expect(screen.getByRole('tab', { name: '办公' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '绘图' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /创建演示文稿/i }));
    expect(onSelect.mock.calls[0]?.[0]).toMatch(/^\/document 请/);
  });
});
