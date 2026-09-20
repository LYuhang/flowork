# Flowork rename and existing installations

The application and GitHub repository are now **Flowork**:
<https://github.com/LYuhang/flowork>.

Update an existing checkout's remote without replacing the checkout:

```sh
git remote set-url origin https://github.com/LYuhang/flowork.git
```

The rename updates Web and extension branding, agent identity, API documentation,
user documentation, and future release asset names. The existing icon contains no
product-name lettering and remains the shared Web / extension icon.

## Preserve existing data and integrations

- **Keep the current Docker Compose project name when upgrading.** Compose
  prefixes volumes with the project name. Renaming a checkout directory and then
  using a different project name can start an empty set of volumes. Continue to
  pass the project name already used by that installation; new installations use
  `flowork`. Do not delete or recreate volumes to perform the rename.
- Python import/package names (`vibecanvas_api`, `vibecanvas_engine`), database
  names, existing `VIBECANVAS_*` settings, browser storage keys, and the extension
  public key / ID remain stable. No account or data migration is required.
- Sandbox launcher commands, MCP integration identifiers, extension events,
  signed-token domains, temporary paths, image names, and the OpenFGA erasure
  identity now use the `flowork` name. Restart the complete application stack
  during the upgrade so every component uses the same identifiers, and rerun the
  one-shot OpenFGA erasure bootstrap before processing account deletion.
- `FLOWORK_CONTROL_PLANE_HTTP_PROXY` is the host-side proxy setting.
- The extension download is `/downloads/flowork-extension.zip`; the old
  `/downloads/vibecanvas-extension.zip` URL remains an alias. Reload the extension
  after upgrading its files; its stable ID preserves browser permissions.
- At the owner's request, the old GitHub Releases and their attachments are
  retired after verification. Git history and version tags remain intact.
  Image digests and attestations are not rewritten. New releases use
  `flowork-*` artifact names and the `LYuhang/flowork` repository identity.

The repository directory itself does not need to be renamed. Keeping an existing
working directory also avoids breaking editor paths, running processes and local
deployment scripts.
