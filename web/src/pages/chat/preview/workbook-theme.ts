import { spreadsheetRenderer, type SpreadsheetInstance } from '@file-viewer/renderer-spreadsheet';

type Theme = 'light' | 'dark';

/** Keep theme changes out of FileViewer options: its update reloads the file. */
export function createWorkbookTheme(initialTheme: Theme) {
  let theme = initialTheme;
  const instances = new Set<SpreadsheetInstance>();
  return {
    initialTheme,
    renderer: {
      ...spreadsheetRenderer,
      handlers: spreadsheetRenderer.handlers?.map(({ rendererId, handler }) => ({
        rendererId,
        handler: async (...args: Parameters<typeof handler>) => {
          const instance = await handler(...args);
          instance.setTheme(theme);
          instances.add(instance);
          return {
            ...instance,
            unmount() {
              instances.delete(instance);
              instance.unmount();
            },
          };
        },
      })),
    },
    setTheme(nextTheme: Theme) {
      theme = nextTheme;
      instances.forEach(instance => instance.setTheme(theme));
    },
  };
}
