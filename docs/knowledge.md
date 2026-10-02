# Knowledge packages

Knowledge stores reusable notes and reference material as versioned file
packages. Each package is an ordinary directory tree rather than a proprietary
index format, so people and Agents can understand the same source files.

## Package structure

Every package has a `README.md` at its root. The README should state the
package's purpose and scope, outline its directory structure, and identify the
role of important files. Subdirectories and filenames are otherwise chosen by
the user. A package may contain text, source code, PDF and Office documents,
images, audio, video, and other relevant files.

For example:

```text
agent-evaluation/
├── README.md
├── notes/
│   ├── evaluation-methods.md
│   └── open-questions.md
├── sources/
│   ├── benchmark-paper.pdf
│   └── framework-comparison.xlsx
└── media/
    └── architecture.png
```

Raw files are authoritative. A package remains usable even when none of its
files are indexed. When search acceleration is useful, the platform may build
a disposable derived view appropriate to the file type: for example,
section-aware text for Markdown, page-aware text for PDF, or no text index at
all for an opaque binary. These derived views never replace or reshape the
files in the package.

The Web application therefore presents a package as a file browser: choose a
file in the directory tree and read its original content in the main pane.
Index maintenance is automatic and is not part of the normal user workflow.
The content pane selects a viewer from the file itself: Markdown uses a
document reading layout, source files use syntax highlighting with line
numbers and folding, and images, audio, video, and PDF use native media
surfaces. Supported Office documents use dedicated document viewers. Unsupported binary
formats remain clearly identified and available for download.

Use **Upload knowledge** to create a package from a complete local folder or
ZIP archive. The importer preserves nested paths, accepts a single outer folder
used only for transport, and verifies that `README.md` is at the logical root
before creating anything. It also rejects path traversal, duplicate paths,
encrypted or non-regular ZIP entries, oversized files, and oversized packages.

Choose **Edit** to open the shared package draft. File actions support creating
UTF-8 text files, editing text up to 2 MiB, uploading or replacing any supported
file, and deleting non-root files. Folder upload and deletion remain available
in the tree. `README.md` can be edited or replaced, but cannot be deleted.

All these file changes are saved to the draft. **New version → Publish** makes
one complete snapshot available to Agents and schedules its search indexing.
Closing the editor preserves saved draft changes. The version selector opens
immutable historical files; drafts and history never become search inputs.
The Overview tab contains identity, timestamps, and current indexing state.

Knowledge history is encrypted in PostgreSQL independently of indexed-file GC.
Migration 154 creates its RLS-protected snapshot table. Existing installations
retain the current publication lazily on first version access or modification;
previous versions that were never retained cannot be reconstructed. An Agent's
explicit whole-package CLI publication also preserves history and supersedes
the shared unpublished draft. Browser edits carry a draft hash and reject stale
writes with HTTP 409, including a concurrent Agent publication. There is no new
runtime service or queue component.

## Working with the Agent

Activate `/knowledge` only when a conversation needs to read or maintain the
Knowledge library. The Agent can:

- list packages in the active organization;
- materialize a package in the current Chat workspace;
- read `README.md`, then progressively list and open relevant files, using
  derived search only when it helps;
- prepare a new package locally and publish it;
- publish a complete replacement package as a new version; and
- delete a package only after an explicit user request.

The Agent's built-in commands move complete packages between platform storage and the
Chat sandbox. Ordinary filesystem tools handle reading, searching, editing,
and reorganizing local files. This keeps file operations transparent and
prevents the Knowledge integration from duplicating the Agent's file tools.

CLI publication replaces the entire file tree and increments the current version;
files absent from the submitted package are removed. The CLI upload has no
expected-version guard: concurrent CLI publications serialize, and the last
commit wins. Browser draft editing and publication require their captured hash. Fetch the
latest package and reconcile changes before publishing to avoid overwriting
someone else's work. Search indexing is asynchronous; a pending or failed index
does not mean the saved files need to be uploaded again.

By default, each fetched version is materialized in its own versioned local
directory. This prevents files removed in a newer package version from being
mistaken for current content in a reused workspace.

## Sharing and ownership

Knowledge packages shared directly with the current account appear under
**Shared with me** in the Web application. A share grants a resource-level role
without changing the package owner or provenance. The `/knowledge` command's
package catalog remains scoped to the active organization; use the Web
application's shared view to access a package owned outside that scope.
