#!/usr/bin/env node
/**
 * Launch the unmodified official Playwright MCP with a short-lived remote-CDP
 * capability supplied through the private Runtime environment. The secret is
 * removed from process.env before the upstream server starts and never appears
 * in the command line or MCP tool catalog.
 */

const fs = require("node:fs");
const path = require("node:path");

const isMetadataCommand = process.argv.includes("--version") || process.argv.includes("--help");
if (!isMetadataCommand) {
  // This launcher runs inside a minimal gVisor rootfs. The process uid maps to
  // root, but `/root` intentionally does not exist. Pin every Playwright cache
  // and socket lookup to the sandbox-local tmpfs before loading Playwright so
  // upstream os.homedir()/XDG defaults cannot escape that contract.
  const runtimeHome = "/tmp/flowork-playwright-mcp";
  const runtimeCache = path.join(runtimeHome, "cache");
  fs.mkdirSync(runtimeCache, { recursive: true, mode: 0o700 });
  process.env.HOME = runtimeHome;
  process.env.XDG_CACHE_HOME = runtimeCache;
  process.env.TMPDIR = "/tmp";

  const endpoint = String(process.env.FLOWORK_PLAYWRIGHT_CDP_ENDPOINT || "").trim();
  const bearer = String(process.env.FLOWORK_PLAYWRIGHT_CDP_BEARER || "").trim();
  delete process.env.FLOWORK_PLAYWRIGHT_CDP_ENDPOINT;
  delete process.env.FLOWORK_PLAYWRIGHT_CDP_BEARER;
  if (!/^wss?:\/\//i.test(endpoint)) {
    console.error("FLOWORK_PLAYWRIGHT_CDP_ENDPOINT must be an absolute ws(s) URL");
    process.exit(2);
  }
  if (!bearer || /[\r\n]/.test(bearer)) {
    console.error("FLOWORK_PLAYWRIGHT_CDP_BEARER is missing or invalid");
    process.exit(2);
  }
  process.argv.push(
    "--cdp-endpoint",
    endpoint,
    "--cdp-header",
    `Authorization: Bearer ${bearer}`,
  );
}

const packagePath = require.resolve("@playwright/mcp/package.json");
require(path.join(path.dirname(packagePath), "cli.js"));
