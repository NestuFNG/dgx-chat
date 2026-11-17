# DGX Chat

[简体中文](README.zh-CN.md) | English

DGX Chat is a self-hosted, multi-device chat front end for a DGX inference gateway,
built on the open-source [deepseek-ai/chat-demo](https://github.com/deepseek-ai/chat-demo)
project (MIT License). The upstream UI, streaming protocol handling and rendering
pipeline are kept intact; this fork adds a runtime-configurable API origin, account
login/registration against the DGX Chat backend, and cloud session synchronisation.

## Getting started

Node.js 22.12+ and pnpm are required.

```sh
pnpm install
pnpm dev        # http://localhost:8090
```

Build the static site:

```sh
pnpm build      # or: pnpm build:win   (same cross-platform Node script)
pnpm type       # TypeScript check
```

The output is written to `dist/`. `scripts/build.mjs` removes `dist/` and runs rspack
with `NODE_ENV=production`; it works in PowerShell, cmd, bash and zsh alike.

## Server address (runtime configurable)

The API origin is resolved on **every request**, so changing it takes effect immediately:

1. `localStorage['dgxchat:origin']` — set from the login screen or Settings → Server / Account.
2. Otherwise the default: the build-time address from `DGX_API_ORIGIN` when the builder set one,
   else — **in a browser only** — the page's own origin when the bundle is served over http(s)
   by the backend itself, else empty. The Capacitor app never falls back to the page origin, so
   distributed native builds still require the user to enter the address.
   Self-hosters can bake their own address in: `DGX_API_ORIGIN=http://their-host:8439 pnpm build`.
   With no stored, no baked and no browser same-origin address, API calls fail with
   "Set the server address first" instead of falling back to any host.

## Accounts and cloud sync

- On first launch, create the account on the login screen (`POST /auth/register`).
  On an empty server the first account becomes the administrator.
- The login token is stored in `localStorage['dgxchat:token']`.
- When logged in, sessions and attachment blobs are written through to
  `PUT /api/sessions/{id}` and `PUT /api/files/{local_id}`; the server list is pulled on
  login/boot and full sessions are fetched lazily when opened.
- IndexedDB remains the offline-first cache: the UI never blocks on the network, failed
  sync only logs, and the app renders without the backend.
- "Continue with a local API Key" on the login screen keeps the upstream direct mode:
  enter an API Key in Settings and chats stay on this device. This mode is stored in
  `localStorage['dgxchat:skip-login']`.

## Local mock backend (optional, development only)

`scripts/mock-backend.mjs` implements the `/auth/*` and `/api/*` endpoints in memory so the
login, session sync and attachment flows can be exercised without a real backend:

```sh
pnpm mock     # http://127.0.0.1:8787
```

It also answers `GET /models` and a minimal `POST /anthropic/v1/messages` SSE stream so chat
requests can be pointed at it from Settings. Registration is invite-only after the first
account; the mock's invite code is `DGX-INVITE-2026`. The script is not part of the build.

## Known limitations (v1)

- **Remote deletions are propagated from tombstones, with caveats.** Incremental pulls call
  `GET /api/sessions/sync?since=` and delete locally the ids listed in `deleted`; the cursor is
  persisted per account. Remaining limits: the first pull (no cursor yet, e.g. right after
  upgrading or on a new device) is a full pull and does not consume historical tombstones, so
  previously deleted sessions can linger locally (they are no longer re-uploaded); if the backend
  has no sync endpoint the client falls back to the plain list and no tombstones are applied.
- **Last-write-wins with client clocks.** The server keeps the copy with the newer `updatedAt`;
  device clock skew can therefore make an older edit look newer. The pull path never stamps a
  newer remote timestamp onto a stale local body.
- **`/api/me/settings` is not synced.** Theme, language, read-aloud voice and the thinking/search
  switches remain per browser profile.
- **Account isolation is per-account IndexedDB** (`dgxchat:local`, `dgxchat:user-<id>`).
  The pre-scoping database `chat-demo` is deliberately left orphaned and is never migrated into
  whichever account logs in next; a console warning is logged if it still exists.
- **Pending sync jobs are discarded on account switch.** A deletion queued immediately before
  logout may not reach the server and the session can reappear when that account logs in again.
- **Attachment uploads** are queued and retried (up to 4 attempts), never throw into the UI, and
  are skipped when already pushed. A permanently failed upload is only logged.

## License

MIT. Upstream copyright notice and license text are retained in [LICENSE](LICENSE);
see [NOTICE](NOTICE) for attribution and a summary of modifications.
