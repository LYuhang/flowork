import { render, screen } from '@testing-library/react';
import i18n from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { describe, expect, it } from 'vitest';

import { ResourceProvenanceLine } from '@/components/resources/ResourceProvenanceLine';
import en from '@/lib/i18n/locales/en.json';
import zh from '@/lib/i18n/locales/zh.json';

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: {
    en: { translation: en },
    zh: { translation: zh },
  },
  interpolation: { escapeValue: false },
});

function renderLine(
  provenance: Parameters<typeof ResourceProvenanceLine>[0]['provenance'],
) {
  return render(
    <I18nextProvider i18n={testI18n}>
      <ResourceProvenanceLine provenance={provenance} />
    </I18nextProvider>,
  );
}

describe('ResourceProvenanceLine', () => {
  it('does not take down a resource page when a stale response has no provenance', () => {
    const view = renderLine(undefined);

    expect(view.container).toBeEmptyDOMElement();
  });

  it('shows a personal owner and origin without repeating the creator', () => {
    const view = renderLine({
      ownership_scope: 'personal',
      origin_type: 'created',
      owner: { type: 'user', display_name: 'Alice' },
      created_by: { type: 'user', display_name: 'Alice' },
    });

    expect(screen.getByText('Alice')).toBeInTheDocument();
    expect(screen.getByText('Created')).toBeInTheDocument();
    expect(view.container).not.toHaveTextContent('by Alice');
  });

  it('shows organization ownership, catalog origin, and a distinct creator', () => {
    renderLine({
      ownership_scope: 'organization',
      origin_type: 'catalog_install',
      owner: { type: 'organization', display_name: 'Acme' },
      created_by: { type: 'user', display_name: 'Sam' },
    });

    expect(screen.getByText('Acme')).toBeInTheDocument();
    expect(screen.getByText('Catalog install')).toBeInTheDocument();
    expect(screen.getByText('by Sam')).toBeInTheDocument();
  });

  it('never renders internal identity fields', () => {
    const view = renderLine({
      ownership_scope: 'platform',
      origin_type: 'system',
      owner: { type: 'platform', display_name: 'Flowork' },
      created_by: null,
    });

    expect(screen.getByText('Flowork')).toBeInTheDocument();
    expect(view.container).not.toHaveTextContent(/tenant|user[_-]?id/i);
  });

  it('uses the native Simplified Chinese vocabulary', async () => {
    await testI18n.changeLanguage('zh');
    renderLine({
      ownership_scope: 'organization',
      origin_type: 'uploaded',
      owner: { type: 'organization', display_name: '示例公司' },
      created_by: { type: 'user', display_name: '小林' },
    });

    expect(screen.getByText('示例公司')).toBeInTheDocument();
    expect(screen.getByText('已上传')).toBeInTheDocument();
    expect(screen.getByText('创建者：小林')).toBeInTheDocument();
    await testI18n.changeLanguage('en');
  });
});
