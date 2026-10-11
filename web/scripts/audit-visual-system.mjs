import { readdir, readFile } from 'node:fs/promises';
import { join, relative, resolve } from 'node:path';

const webRoot = resolve(process.cwd(), 'src');

async function sourceFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = await Promise.all(entries.map(async (entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return /\.(?:ts|tsx|css)$/.test(entry.name) && !/\.test\.|\.spec\./.test(entry.name) ? [path] : [];
  }));
  return files.flat();
}

const forbidden = [
  { label: '10 px functional text', pattern: /text-\[10px\]|text-tiny/g },
  { label: '11 px functional text (12 px is the minimum meta size)', pattern: /text-\[11px\]/g },
  { label: 'broad transition-all', pattern: /\btransition-all\b/g },
  { label: 'native confirm dialog', pattern: /window\.confirm\s*\(|(?<![\w.])confirm\s*\(/g },
  { label: 'legacy blocking file-view geometry', pattern: /(?:48|72)vw|min-w-\[680px\]|shadow-2xl/g },
];

const colorExceptions = JSON.parse(await readFile(resolve(process.cwd(), 'scripts/visual-color-exceptions.json'), 'utf8'));
const concreteColor = /(?:bg|text|border|ring|from|via|to|shadow)-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}|\[#[0-9a-fA-F]{3,8}\]|['"]#[0-9a-fA-F]{3,8}['"]/g;
const violations = [];
for (const [file, exception] of Object.entries(colorExceptions)) {
  if (!exception.reason?.trim() || !Array.isArray(exception.tokens)) {
    violations.push(`${file}: color exceptions require a reason and explicit tokens`);
  }
}
for (const file of await sourceFiles(webRoot)) {
  const source = await readFile(file, 'utf8');
  const localPath = relative(process.cwd(), file);
  const allowed = colorExceptions[localPath]?.tokens ?? [];
  for (const match of source.matchAll(concreteColor)) {
    if (!allowed.includes(match[0])) {
      const line = source.slice(0, match.index).split('\n').length;
      violations.push(`${localPath}:${line}: concrete color ${match[0]} needs semantic tokens or a documented exception`);
    }
  }
  for (const rule of forbidden) {
    const localPath = relative(process.cwd(), file);
    if (rule.pageOnly && !/^src\/(?:pages|components)\//.test(localPath)) continue;
    for (const match of source.matchAll(rule.pattern)) {
      const line = source.slice(0, match.index).split('\n').length;
      violations.push(`${localPath}:${line}: ${rule.label}`);
    }
  }
}

const shell = await readFile(resolve(process.cwd(), '../extension/sidepanel.html'), 'utf8');
if (concreteColor.test(shell) || /(?:oklch|rgb|hsl)a?\(\s*[\d.]/.test(shell)) {
  violations.push('../extension/sidepanel.html: use generated theme variables for shell colors');
}
if (!shell.includes('href="shell-theme.css"') || /--(?:surface|raised|text|muted|edge|brand)\s*:/.test(shell)) {
  violations.push('../extension/sidepanel.html: shell must consume generated application theme tokens');
}

const manifestPath = resolve(process.cwd(), '../extension/manifest.json');
const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
const injectedScripts = (manifest.content_scripts ?? []).flatMap((entry) => entry.js ?? []);
if (injectedScripts.length !== 1 || injectedScripts[0] !== 'island/content.js') {
  violations.push('../extension/manifest.json: browser control must inject Dynamic Island only');
}

if (violations.length) {
  process.stderr.write(`${violations.join('\n')}\n`);
  process.exitCode = 1;
} else {
  process.stdout.write('Visual-system guardrails passed.\n');
}
