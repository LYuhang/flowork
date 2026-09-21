import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { http, HttpResponse } from 'msw';
import '@/lib/i18n';
import { server } from '@/__tests__/msw-handlers';
import { LoginPage } from '@/pages/auth/LoginPage';

function renderLogin() {
  return render(
    <MemoryRouter>
      <LoginPage />
    </MemoryRouter>,
  );
}

describe('<LoginPage>', () => {
  it('switches the complete authentication surface between Chinese and English', async () => {
    server.use(http.get('*/api/v1/public-config', () => HttpResponse.json({
      enable_test_user: false,
      enterprise_sso_enabled: false,
    })));
    const user = userEvent.setup();

    renderLogin();

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(
      screen.getByText(
        'Turn knowledge and ideas into working workflows.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('Deployment')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '中文' }));

    expect(screen.getByRole('heading', { name: '登录' })).toBeInTheDocument();
    expect(
      screen.getByText(
        '让知识与想法，真正落到工作流中。',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('部署')).toBeInTheDocument();
    expect(document.documentElement).toHaveAttribute('lang', 'zh-CN');
  });

  it('shows only the email flow when enterprise SSO is disabled', async () => {
    server.use(http.get('*/api/v1/public-config', () => HttpResponse.json({
      enable_test_user: false,
      enterprise_sso_enabled: false,
    })));

    renderLogin();

    expect(await screen.findByLabelText(/^email$/i)).toBeInTheDocument();
    expect(screen.queryByRole('tab')).not.toBeInTheDocument();
    expect(screen.queryByText(/company sso/i)).not.toBeInTheDocument();
  });

  it('separates email and SSO into switchable tabs when enabled', async () => {
    server.use(http.get('*/api/v1/public-config', () => HttpResponse.json({
      enable_test_user: false,
      enterprise_sso_enabled: true,
    })));
    const user = userEvent.setup();

    renderLogin();

    const ssoTab = await screen.findByRole('tab', { name: /company sso/i });
    expect(screen.getByRole('tab', { name: /^email$/i })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    await user.click(ssoTab);
    expect(await screen.findByLabelText(/organization slug/i)).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: /^email$/i })).not.toBeInTheDocument();
  });
});
