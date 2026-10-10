import { themeQuartz } from 'ag-grid-community';

/** CSS variables follow the app theme without rebuilding grid data or selection. */
export const applicationGridTheme = themeQuartz.withParams({
  backgroundColor: 'oklch(var(--surface-work))',
  foregroundColor: 'oklch(var(--text-primary))',
  headerBackgroundColor: 'oklch(var(--surface-sunken))',
  headerTextColor: 'oklch(var(--text-secondary))',
  borderColor: 'oklch(var(--edge-structural))',
  accentColor: 'oklch(var(--focus))',
  rowHoverColor: 'oklch(var(--surface-hover))',
  selectedRowBackgroundColor: 'oklch(var(--focus) / 0.12)',
  fontFamily: 'var(--font-ui)',
  fontSize: 14,
  browserColorScheme: 'inherit',
});
