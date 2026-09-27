# Chat feedback and sharing

Assistant messages expose Copy, Helpful, Not helpful and Share once their
stable message IDs are persisted. Votes are optimistic but roll back on failure.
Clicking the selected vote clears it; selecting the other replaces it. Reloads
restore the current rating, and all message rows share one feedback query.

Migration `130` adds three owner/tenant-isolated tables:

- `chat_message_feedback`: current nullable `up`/`down` rating, unique by user,
  chat and message, with the original turn ID for analysis.
- `chat_feedback_events`: changed-state history. Retrying an identical PUT does
  not append another event. Rows cascade when the user/chat/message is deleted.
- `chat_public_shares`: a 256-bit random capability token hash, encrypted
  creation-time snapshot, owner/chat/message scope, created/revoked/expiry times.
  One active link per scope is reused; revoking and creating again produces a
  new URL and fresh snapshot. Both share types currently have no expiry.

The dialog beside New Chat manages conversation and single-response links.
Single-response sharing copies immediately and shows a green check for 2.5 s.
Sharing a conversation includes its persisted user/assistant text, not only the
history window currently loaded in the browser. A response share includes only
that assistant message and uses a generic title. Later turns are never added to
an existing snapshot. Attachments, tool payloads and hidden control messages are
excluded; the text itself may still contain sensitive information, so the dialog
asks owners to review it. Snapshots are limited to 10,000 stored messages / 4 MiB.

Links use `VIBECANVAS_PUBLIC_URL` (including a deployment subpath), resolved when
returned rather than saved as a domain in the database. If unset, the browser
uses its origin and detected mount path. A changed domain needs a redirect from
the old domain for already-copied links to continue working.

`/share/:token` is a standalone read-only route, without login, composer or app
sidebar. Anyone possessing the link may read it. The API checks revocation,
expiry, chat soft deletion and owner status on every read; a narrowly scoped RLS
capability policy admits only the matching snapshot. Snapshots reuse the chat's
encrypted-content key. Public responses are no-store/noindex/no-referrer; the
page disables image fetching/private file previews and periodically revalidates.
Revocation cannot erase copies or screenshots someone already made. Avoid logging
full share URLs at the reverse proxy because the token grants access.

Implementation follows the snapshot/revocation model described in
[OpenAI's sharing documentation](https://help.openai.com/en/articles/7925741-sharing-conversations-and-scheduled-tasks-in-chatgpt).

Validation: `api/tests/test_chat_engagement.py` exercises real PostgreSQL FORCE
RLS, owner checks, encrypted snapshots, idempotency, revocation, expiry, deletion
and prefixed URLs. `chat-engagement.test.tsx` covers vote restoration/rollback,
copy feedback, public rendering and image/file-link restrictions.
