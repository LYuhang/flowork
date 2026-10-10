import type { ITheme } from '@xterm/xterm';

// xterm parses concrete colors; keep both palettes here rather than inheriting
// a page foreground that may not match the terminal's background.
export const terminalThemes = {
  light: {
    background: '#f8fafc', foreground: '#243044', cursor: '#5145b5',
    cursorAccent: '#f8fafc', selectionBackground: '#dbe2f3',
    black: '#243044', red: '#b42335', green: '#176b45', yellow: '#805b0d',
    blue: '#2458ae', magenta: '#833e9e', cyan: '#126b78', white: '#667085',
    brightBlack: '#596579', brightRed: '#c3273a', brightGreen: '#197c4f',
    brightYellow: '#8a620d', brightBlue: '#2966c6', brightMagenta: '#954ab3',
    brightCyan: '#167b89', brightWhite: '#243044',
  },
  dark: {
    background: '#171b23', foreground: '#e4e9f2', cursor: '#aaa2ff',
    cursorAccent: '#171b23', selectionBackground: '#3b4360',
    black: '#596579', red: '#f08089', green: '#75cca2', yellow: '#e5c078',
    blue: '#88b3f5', magenta: '#cca1eb', cyan: '#74cbd5', white: '#d8dfea',
    brightBlack: '#8e9aaf', brightRed: '#ffa3aa', brightGreen: '#99e5bd',
    brightYellow: '#f4d899', brightBlue: '#adcaff', brightMagenta: '#e0bcfa',
    brightCyan: '#9ae2e8', brightWhite: '#f5f7fc',
  },
} satisfies Record<'light' | 'dark', ITheme>;
