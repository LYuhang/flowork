// npm 11.19 bundles vulnerable dependencies even after npm install overrides.
// Replace only these reviewed packages with verified, compatible upstream releases.
// Remove this step once npm ships all fixes; scans still cover the build stage.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');
const packages = [
  ['ip-address', '10.5.0', '35e23227dfeca9179f03f899a9e3a21faf542a8079821bce95d5620642d75873'],
  ['tar', '7.5.22', 'b792c2d1c7fc770910522ca1ffc29eee02ee38de4fa3a01e7832eb705879c6c6'],
  ['brace-expansion', '5.0.11', '67bb5a1b4d4a8ff497d845a0b891ffe6b7233aea2202641315cec88d0fff15eb'],
  ['undici', '6.28.1', 'e18191aac9c0ff43dac7fe9b10b7041a22d07addb7b66a6e8ac14a52a5b69b74'],
];
async function main() {
  const root = path.resolve(process.argv[2]);
  if (JSON.parse(fs.readFileSync(path.join(root, 'package.json'))).name !== 'npm') {
    throw new Error('Expected an npm package directory');
  }
  for (const [name, version, digest] of packages) {
    const response = await fetch(`https://registry.npmjs.org/${name}/-/${name}-${version}.tgz`);
    if (!response.ok) throw new Error(`Download failed: ${name} HTTP ${response.status}`);
    const bytes = Buffer.from(await response.arrayBuffer());
    if (createHash('sha256').update(bytes).digest('hex') !== digest) {
      throw new Error(`Checksum mismatch: ${name}`);
    }
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'flowork-npm-'));
    try {
      const archive = path.join(temporary, 'package.tgz');
      fs.writeFileSync(archive, bytes);
      execFileSync('tar', ['-xzf', archive, '-C', temporary]);
      const source = path.join(temporary, 'package');
      const manifest = JSON.parse(fs.readFileSync(path.join(source, 'package.json')));
      if (manifest.name !== name || manifest.version !== version) throw new Error(`Unexpected package: ${name}`);
      const destination = path.join(root, 'node_modules', name);
      fs.rmSync(destination, { recursive: true, force: true });
      fs.cpSync(source, destination, { recursive: true });
      console.log(`npm_dependency_updated=${name}@${version}`);
    } finally {
      fs.rmSync(temporary, { recursive: true, force: true });
    }
  }
}
main().catch((error) => { console.error(error.message); process.exitCode = 1; });
