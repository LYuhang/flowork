"use strict";

const fs = require("node:fs/promises");
const path = require("node:path");
const { BrowserCommandError, writeArtifact } = require("./browser-files.cjs");

class PrivateBrowserFiles {
  constructor(root) { this.root = root; this.files = new Map(); }

  async save(filename, content, grantId, tabId) {
    if (!filename || filename !== path.basename(filename) || [".", ".."].includes(filename) || /[\\\x00-\x1f]/.test(filename))
      throw new BrowserCommandError("invalid_private_filename", "Cookie --file must be a filename, not a path. Use the private path returned by the command.");
    if (!this.root) throw new BrowserCommandError("private_storage_unavailable", "Private Cookie storage is unavailable.");
    const directory = await fs.lstat(this.root);
    if (!directory.isDirectory() || directory.isSymbolicLink() || (directory.mode & 0o077) !== 0)
      throw new BrowserCommandError("private_storage_invalid", "Private Cookie storage is not a protected directory.");
    const artifact = await writeArtifact(path.join(this.root, filename), content);
    this.files.set(artifact.file, { grantId, tabId });
    return { file: artifact.file, bytes: artifact.bytes, sensitive: true, persistence: "temporary",
      hint: "Use this exact path for authenticated requests in this turn. Do not print, copy to the workspace, preview or share Cookie contents. The managed file is removed on permission revocation or when the turn ends." };
  }

  async revoke(grantId) {
    for (const [file, record] of [...this.files]) {
      if (grantId !== undefined && record.grantId !== grantId) continue;
      // Unlink only exact managed files, never traverse an Agent-provided tree.
      await fs.unlink(file).catch(error => { if (error.code !== "ENOENT") throw error; });
      this.files.delete(file);
    }
  }
}

module.exports = { PrivateBrowserFiles };
