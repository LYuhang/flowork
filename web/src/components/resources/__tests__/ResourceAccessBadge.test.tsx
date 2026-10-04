import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ResourceAction } from '@/lib/api/organizations';
import { ResourceAccessBadge } from '../ResourceAccessBadge';

vi.mock('react-i18next', () => ({useTranslation: () => ({t: (key: string) => key})}));

describe('ResourceAccessBadge', () => {
  it.each<[ResourceAction[], string]>([
    [['view'], 'read'],
    [['view', 'use'], 'read'],
    [['view', 'update'], 'edit'],
    [['view', 'execute'], 'execute'],
    [['view', 'update', 'execute'], 'editExecute'],
    [['view', 'manage_access'], 'manage'],
  ])('describes effective capabilities %j independently of a role label', (capabilities, label) => {
    render(<ResourceAccessBadge access={{capabilities, effective_role: 'viewer', source: 'computed'}} />);
    expect(screen.getByText(`resourceAccess.${label}`)).toBeInTheDocument();
  });
});
