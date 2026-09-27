"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const { PrivateBrowserFiles } = require("./browser-private-files.cjs");

test("Cookie files remain private, never appear in stdout and are removed by grant", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "browser-private-test-"));
  const files = new PrivateBrowserFiles(root);
  try {
    const saved = await files.save("cookies.json", '{"value":"PRIVATE_SECRET"}', "grant", "tab");
    assert.ok(!JSON.stringify(saved).includes("PRIVATE_SECRET"));
    assert.equal(saved.persistence, "temporary");
    assert.equal((await fs.stat(saved.file)).mode & 0o777, 0o600);
    assert.match(await fs.readFile(saved.file, "utf8"), /PRIVATE_SECRET/);
    await files.save("second.json", "another", "other-grant", "tab");
    await files.revoke("grant");
    assert.deepEqual(await fs.readdir(root), ["second.json"]);
    await files.revoke();
    assert.deepEqual(await fs.readdir(root), []);
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});

test("Cookie output cannot target a workspace path, symlink or insecure directory", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "browser-private-path-test-"));
  const files = new PrivateBrowserFiles(root);
  try {
    for (const filename of ["/data/cookies.json", "../cookies", "a/b", ".", ".."]) {
      await assert.rejects(files.save(filename, "secret", "grant", "tab"), { code: "invalid_private_filename" });
    }
    await fs.symlink(path.join(root, "other"), path.join(root, "link"));
    await assert.rejects(files.save("link", "secret", "grant", "tab"), { code: "file_exists" });
    await fs.chmod(root, 0o755);
    await assert.rejects(files.save("cookies", "secret", "grant", "tab"), { code: "private_storage_invalid" });
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});
