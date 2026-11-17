# DGX Chat — architecture and interface contract (SPEC v1)

> Design notes for this repository: how the pieces fit together and exactly what
> the backend must implement for the bundled web/Android client to work.
> Deployment steps live in [README.md](README.md) and
> [docs/self-hosting.md](docs/self-hosting.md).

## 0. Verified baseline

| Item | Value |
|---|---|
| Upstream inference | any OpenAI-compatible `/chat/completions` endpoint (a local vLLM / llama.cpp / Ollama / LiteLLM gateway by default) |
| Reasoning stream | `choices[0].delta.reasoning` (thinking) and `choices[0].delta.content` (answer); `data: …` frames terminated by `[DONE]` |
| Non-streaming reasoning | `choices[0].message.reasoning` |
| Usage | only present at the end of a stream when `stream_options.include_usage=true`; always present when non-streaming |
| Server runtime | Python 3.12 in Docker, SQLite (WAL) |
| Client stack | React + rspack, **Anthropic Messages-compatible protocol** (not OpenAI) |

The client calls these endpoints — the backend must implement them as written:

```
GET    {ORIGIN}/models                                 Authorization: Bearer <key>
POST   {ORIGIN}/anthropic/v1/messages                  x-api-key: <key>       (SSE)
POST   {ORIGIN}/anthropic/v1/files                     x-api-key + anthropic-beta: files-api-2025-04-14
DELETE {ORIGIN}/anthropic/v1/files/{file_id}
POST   {ORIGIN}/v1/tools/asr                           Authorization: Bearer   (multipart file)
POST   {ORIGIN}/v1/tools/tts                            Authorization: Bearer   (json -> raw PCM)
```

In the upstream demo `ORIGIN` is the build-time constant `__API_ORIGIN__`; this
fork resolves it at **runtime** instead (see `app/src/services/api/origin.ts`).

## 1. Components

```
dgxchat/
  server/            FastAPI backend (Docker image)
  app/               chat-demo fork: runtime ENDPOINT/KEY + login + cloud sync
  android/           Capacitor shell that produces the APK
  deploy/            optional SSH deployment helper + example config
  scripts/           one-command local/docker deployment
  tools/             operator tools (SSH helper, end-to-end acceptance client)
```

## 2. Backend interface contract (server/)

### 2.1 Authentication
- `POST /auth/register` `{username,password}` → `{token,user}`; **the first
  registration on an empty database automatically becomes admin**; later
  registrations need an admin-issued invite code.
- `POST /auth/login` `{username,password}` → `{token,user:{id,username,role,createdAt}}`
- `GET /auth/me` → user
- `POST /auth/logout`
- Token: random 32-byte hex, only its sha256 is stored; **never expires**
  (revoke it from the admin page).
- Every `/anthropic/*`, `/v1/tools/*`, `/models`, `/api/*` request accepts:
  - `Authorization: Bearer <token>`
  - `x-api-key: <token>`
  - `Authorization: Bearer <username>:<password>` (so a user can paste credentials)
- Unauthorized → `{"error":{"message":"...","type":"authentication_error"}}` / 401.

### 2.2 Compatibility layer
- `GET /models` → `{"object":"list","data":[{"id":"deepseek-flash",...},{"id":"deepseek-v4-pro",...}]}`
  (the client's `ChatModel` type only has `deepseek-flash` / `deepseek-v4-pro`,
  so client ids are mapped to whatever upstream model you configure)
- `POST /anthropic/v1/messages`: see §3
- `POST /anthropic/v1/files` (multipart: `purpose?`, `file`) → `{id,type:"file",size_bytes,created_at,filename,mime_type}`, stored at `data/files/<user>/<id>`, metadata in the database
- `DELETE /anthropic/v1/files/{id}`
- `POST /v1/tools/asr` (multipart `file`) → `{"text": "..."}`; upstream failure → 4xx/5xx + `{"error":{...}}`
- `POST /v1/tools/tts` (`{text, voice_id, format:"pcm", filter_markdown}`) → `audio/pcm`, **24 kHz mono s16le**, markdown stripped (code blocks/tables/quotes/long formulas)

### 2.3 Cloud sync API (client-only)
```
GET    /api/sessions              -> [{id,title,createdAt,updatedAt}]           (SessionSummary[])
GET    /api/sessions/{id}         -> full Session object
PUT    /api/sessions/{id}         body=Session  idempotent upsert (last write wins by updatedAt)
DELETE /api/sessions/{id}
GET    /api/files/{local_id}      -> attachment bytes (indexed by the client's local id)
PUT    /api/files/{local_id}      body=raw bytes (any Content-Type) idempotent
GET    /api/me/settings           -> {tts:{...},asr:{...},model:{...},...}
PUT    /api/me/settings
```
The Session structure must match `app/src/services/chat/types.ts` exactly; the
backend stores it **verbatim** and never interprets the message internals.

### 2.4 Administration
```
GET    /admin/users                       -> list
POST   /admin/users {username,password,role}
DELETE /admin/users/{id}
POST   /admin/users/{id}/reset-password
GET    /admin/stats
```
`role=admin` only.

### 2.5 Storage
- SQLite (`data/dgxchat.db`), WAL mode: `users / tokens / sessions / session_summaries / files / user_settings / usage_log`
- Attachments and exports under `data/files/`
- Sessions are isolated per user; `GET /api/sessions` only returns your own.

## 3. Anthropic Messages → OpenAI translation rules

Request `{model, system, messages[], thinking:{type}, tools[], safety_check, title?}`

### 3.0 🔴 System prompt is a hard constraint
The official chat-demo system prompt is the constant in
`src/services/chat/store.ts`:

```
该助手为DeepSeek，由深度求索公司创造。
```

**It must stay byte-identical.**
- Client: the `SYSTEM_PROMPT` constant must not change by a single character —
  no "DGX Chat", no extra company/role/style instructions, no date prefix.
- Backend: the `system` field is passed through as the OpenAI
  `{role:"system"}` message content **as-is** — no prepend/append/trim/rewrite/
  merge, and no extra system message of its own (`latest_reminder` is handled
  separately, see below).
- Verification: capture one real request, print the `system` string and diff it
  against the constant; record the result in `server/README.md`.

1. `system` → first `{role:"system"}`, **byte-identical**
2. `role:"latest_reminder"` → folded into a system hint (`reminder: ...`) or
   prepended to the next user turn
3. user content as a string → as-is; as a block array → concatenate `text`
   blocks; `image.source.file_id` → read from disk, convert to a data URI
   (`image_url`); `document` → anything text-decodable (text/*, json, csv, md,
   source code) becomes `<document name=...>` text, everything else is ignored
4. assistant → concatenate `text` blocks only; drop `thinking` history;
   ignore `server_tool_use` / `web_fetch_tool_result` (read_link) — the local
   model has no network access
5. Trailing assistant message (Prefill/continue) → either append `继续：` to the
   last user turn, or pass it as an assistant prefix (`continue_final_message`,
   preferred when the upstream supports it)
6. `thinking.type==='enabled'` → try `chat_template_kwargs:{thinking:true}` or
   `enable_thinking:true`; when **disabled** it must really be off (see the
   measured results in `server/README.md`)
7. `deepseek_web_search` / `deepseek_read_url` in `tools`: the local gateway
   cannot do this → silently ignore (no error) and say so in an
   `x-dgx-notice` response header
8. `title:{enabled:true}` → fire a separate short request in parallel and put
   the result in `message_delta.delta.title` (≤30 chars)

SSE event sequence (must match the client's `parseEvent.ts` exactly):
```
event: message_start        data: {"type":"message_start","message":{"id":"<uuid>","model":"<client model>","usage":{...}}}
event: content_block_start  data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"","signature":""}}
event: content_block_delta  data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"..."}}
event: content_block_stop   data: {"type":"content_block_stop","index":0}
event: content_block_start  data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}
event: content_block_delta  data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"..."}}
event: content_block_stop   data: {"type":"content_block_stop","index":1}
event: message_delta        data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null,"title":"?"},"usage":{...}}
event: message_stop         data: {"type":"message_stop"}
```
- **no** thinking block when thinking is disabled
- upstream error → `event: error` + `data: {"error":{"message":...}}`
- client disconnects → close the upstream connection immediately
- `: ping` comment frames may be interleaved as keep-alives

## 3.5 Web search / link reading

When "smart search" is on, the client declares:
```json
"tools": [{"type":"deepseek_web_search","name":"deepseek_web_search"},
          {"type":"deepseek_read_url","name":"deepseek_read_url"}]
```
and expects the server to emit `server_tool_use` / `web_search_tool_result` /
`web_fetch_tool_result` blocks in the SSE stream. The client's `parseEvent.ts`
validates the fields strictly — shapes must match exactly (see
`app/src/services/api/types.ts`).

Implementation strategy (configurable via `SEARCH_MODE`):

- `SEARCH_MODE=official` (highest fidelity, needs `DEEPSEEK_API_KEY`): forward the
  whole request to DeepSeek's Anthropic-compatible endpoint and **relay the SSE
  events verbatim** (only replacing `message_start` ids and skipping frames that
  cannot be parsed). Search, deep search, image search, citations and link
  reading then behave exactly like the official app.
- `SEARCH_MODE=dgx` (default): the local model writes the answer; the backend
  provides retrieval:
  1. fetch results from the official endpoint (with the `deepseek_web_search`
     tool, non-streaming) or from a pluggable provider
     (brave / tavily / searxng / google_cse);
  2. emit **fully compliant** `server_tool_use` + `web_search_tool_result`
     blocks (`id` like `srvtoolu_s_<n>_<32hex>`, `site_icon` from a favicon
     service, `encrypted_content` empty string, `page_age` may be null);
  3. fold the snippets into the local model's context and ask it to cite
     sources as `[n]`; emit `citations_delta` in the text stream when needed.
- No key and no provider: **degrade gracefully** — emit no tool blocks, answer
  normally, and set `x-dgx-search: unavailable`.

`deepseek_read_url`: fetch URLs found in this turn's user message (8s timeout,
body truncated to 8k chars) and emit
`server_tool_use(kind='read_link', input={action:'open_page',url})` +
`web_fetch_tool_result` with
`content:{type:'web_fetch_result',url,content:{type:'document',source:{type:'text',media_type:'text/plain',data},title},published_at}`.
History may contain `read_link` calls and results; the backend must accept and
ignore or reuse them.

## 4. Client behaviour

1. `API_ORIGIN` is runtime-configurable: `localStorage['dgxchat:origin']` ??
   `__API_ORIGIN__` (default `http://localhost:8439`); the settings panel has a
   "server address" field with validation.
2. Settings panel gains a "server / account" section: address, token/API key,
   login/logout, status.
3. New login gate (show the login screen when not authenticated).
4. Cloud sync: `saveSession/renameSession/deleteSession` in
   `services/chat/db.ts` write through to `PUT/DELETE /api/sessions/*`; on boot
   `GET /api/sessions` pulls the list and `GET /api/sessions/{id}` the full body;
   attachment blobs go through `/api/files/{local_id}`.
5. Branding: title, `index.html`, icons, Chinese default.

## 5. Android
- Capacitor 6, `appId=com.dgxchat.app`, `appName=DGX Chat`, `webDir=../app/dist`
- cleartext HTTP allowed while there is no TLS (see `network_security_config.xml`)
- permissions: INTERNET, ACCESS_NETWORK_STATE, RECORD_AUDIO, MODIFY_AUDIO_SETTINGS
- signing: your own keystore via `android/keystore/keystore.properties` or
  `DGX_KEYSTORE_*` environment variables (never committed); `assembleRelease`
- output: `android/app/build/outputs/apk/release/app-release.apk`

## 6. Deployment
- port **8439**, `docker compose up -d`, `restart: unless-stopped`
- data volume: `data/` next to the compose file
- environment variables: see `server/.env.example` and the configuration table in
  [README.md](README.md)
