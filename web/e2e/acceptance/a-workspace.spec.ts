/**
 * Workspace creation and opening acceptance.
 */
import { test, expect } from '@playwright/test';
import { registerRealUser, seedAuth, screenshot, type RealUser } from './fixtures';

let user: RealUser;

test.beforeAll(async () => {
  user = await registerRealUser();
});

test.beforeEach(async ({ context }) => {
  await seedAuth(context, user.token);
});

test('A1 authenticated workspace route renders the workflow list', async ({ page }) => {
  await page.goto('/workspace');
  await expect(page).toHaveURL(/\/workspace$/);
  await expect(
    page.getByRole('heading', { name: /^workflows$/i, level: 1 }),
  ).toBeVisible();
  await screenshot(page, '01-workspace');
});

test('A2 create workflow lands on an empty onboarding canvas', async ({
  page,
}) => {
  await page.goto('/workspace');
  await page.getByRole('button', { name: /^New Workflow$/i }).first().click();
  await page.getByTestId('create-workflow-name').fill('Acc Create Flow');
  await page.getByRole('button', { name: /^Create$/ }).click();

  // Lands on /workflow/<id>
  await page.waitForURL(/\/workflow\/[^/]+$/);
  await expect(page.locator('[data-action="canvas-save"]')).toBeVisible();

  // New workflows are EMPTY by design (commit 32781b9 "feat(ux): empty
  // canvas"). The pure UI create flow shows the onboarding overlay rather
  // than a seeded StartNode.
  await expect(page.locator('[data-canvas-empty-state]')).toBeVisible({
    timeout: 15_000,
  });
  await expect(
    page.getByText(/Open File Explorer to add nodes/i),
  ).toBeVisible();
  await page.getByRole('button', { name: /^Open File Explorer$/i }).click();
  await expect(page.getByText(/^Nodes$/i).first()).toBeVisible();
  await screenshot(page, '02-create-workflow');
});

test('A3 workspace lists the row; edit info + delete', async ({ page }) => {
  const suffix = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  const workflowName = `Acc CRUD Flow ${suffix}`;
  const renamedWorkflow = `Acc CRUD Renamed ${suffix}`;
  await page.goto('/workspace');
  await page.getByRole('button', { name: /^New Workflow$/i }).first().click();
  await page.getByTestId('create-workflow-name').fill(workflowName);
  await page.getByRole('button', { name: /^Create$/ }).click();
  await page.waitForURL(/\/workflow\/[^/]+$/);

  // Back to workspace; the workflow is listed. The workspace is now a TABLE
  // (was a card grid): each workflow is a `wf-row` <tr> with a trailing
  // actions cell (Open / Duplicate buttons + a `wf-row-menu` kebab whose
  // dropdown holds Edit info + Delete).
  await page.goto('/workspace');
  await page.waitForURL(/\/workspace$/);
  const row = page.getByTestId('wf-row').filter({ hasText: workflowName });
  await expect(row).toBeVisible();

  // Edit info → rename via the row's kebab menu.
  await row.getByTestId('wf-row-menu').click();
  await page.getByRole('menuitem', { name: /edit info/i }).click();
  const editDialog = page.getByRole('dialog');
  await editDialog.getByLabel(/^name$/i).fill(renamedWorkflow);
  await editDialog.getByRole('button', { name: /^save$/i }).click();
  const renamedRow = page.getByTestId('wf-row').filter({ hasText: renamedWorkflow });
  await expect(renamedRow).toBeVisible({ timeout: 10_000 });
  await screenshot(page, '03-workspace-crud');

  await renamedRow.getByTestId('wf-row-menu').click();
  await page.getByTestId('wf-row-delete').click();
  const deleteDialog = page.getByRole('dialog');
  await deleteDialog.locator('#delete-workflow-confirm').fill(renamedWorkflow);
  await deleteDialog.getByRole('button', { name: /^delete$/i }).click();
  await expect(renamedRow).toHaveCount(0);
});
