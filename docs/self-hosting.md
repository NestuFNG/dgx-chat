# Self-hosting DGX Chat — operator notes

This is the generic version of the deployment notes. It deliberately contains
no hostnames, no IP addresses and no credentials: fill in your own.

> If you keep private notes (real host, real admin account, invite codes), put
> them in `DEPLOY_INFO.md` at the repository root — it is git-ignored.

## 1. What is running where

```
Browser / Android app
   │  (DeepSeek-compatible protocol + account token)
   ▼
your-server:8439            ← users / sessions / attachments live here
   ├─ answer text → your OpenAI-compatible inference gateway
   │                (primary DGX_BASE_URL, optional fallbacks)
   └─ web search / ASR / TTS → DeepSeek official API (optional key)
```

The answer text always comes from your own gateway. The DeepSeek key, if you
set one, is only used for retrieval, speech-to-text and text-to-speech.

## 2. Files that are never committed

| Path | What it is |
|---|---|
| `.env` (next to `docker-compose.yml`) | runtime configuration incl. API keys |
| `deploy/.env.deploy` | the same, for the optional SSH deploy helper |
| `deploy/.env.deploy.example` | template — safe to commit |
| `tools/nas.env` | SSH host/user/password for your deployment machine |
| `tools/.admin_token.txt`, `tools/.e2e_token.txt` | operator test tokens |
| `android/keystore/*` | release signing key + passwords |
| `DEPLOY_INFO.md` | this project's private operator note |
| `server/.env`, `server/.env.local-test` | local backend environment |

## 3. First start

```sh
./scripts/quickstart.sh          # Linux / macOS / NAS
pwsh ./scripts/quickstart.ps1    # Windows
```

The script creates `.env` if it is missing, starts the stack and waits for
`/health`. Then open `http://<host>:8439/` and register — **the first account
becomes the administrator**.

Manual path once `.env` exists:

```sh
cd server && docker compose up -d --build
docker compose logs -f
```

## 4. Configuration

See the table in [../README.md](../README.md#configuration). The four values
almost everyone changes:

```ini
DGX_BASE_URL=http://your-gateway:8041/v1
DGX_MODEL_MAP={"deepseek-flash":"your-model","deepseek-v4-pro":"your-model"}
DEEPSEEK_API_KEY=                 # optional; search/TTS/ASR only
REGISTRATION_MODE=invite          # first user is always allowed
```

After editing `.env`: `docker compose up -d` (add `--build` only when code
changed).

## 5. Day-to-day operations

```sh
docker compose ps                 # status
docker compose logs --tail=200    # logs
docker compose restart            # restart
docker compose down               # stop
```

Health check:

```sh
curl -s http://127.0.0.1:8439/health
```

If someone is locked out, `python deploy/reset_password.py --user <name>
--list` can list accounts and reset a password straight in the container's
database (needs `tools/nas.env` or the equivalent environment variables):

```sh
python deploy/reset_password.py --list
python deploy/reset_password.py --user admin              # random new password
python deploy/reset_password.py --user admin --password '...'
```

## 6. Backup

Everything worth keeping is in the data directory mounted at `/data`:

- `dgxchat.db` — accounts, sessions, invites, usage counters
- `files/` — uploaded attachments

Copy those two paths somewhere safe on a schedule. There is no separate export
format; SQLite + the files directory is the whole state.

## 7. HTTPS and remote access

The app allows plain HTTP so a LAN deployment works out of the box. When you
have a certificate:

1. reverse-proxy `https://chat.example.com` → `127.0.0.1:8439` (nginx, Caddy,
   Traefik, or whatever your host provides);
2. set `DGXCHAT_TRUST_PROXY=1` in `.env` so client IPs are taken from
   `X-Forwarded-For`;
3. point the app's **server address** setting at the new URL — no rebuild needed.

For access from outside without opening ports, a mesh VPN (Tailscale, WireGuard,
ZeroTier) is the least risky option.

## 8. Upgrading

```sh
git pull
./scripts/quickstart.sh          # rebuilds, keeps data
```

Data lives outside the image, so rebuilding never touches it. Pin the image tag
(`dgxchat:1.0.0`) if you prefer to upgrade deliberately.

## 9. Known limitations (v1)

1. Switching accounts on one device keeps the data separated (per-account
   IndexedDB), but databases left over from older versions are orphaned, not
   migrated.
2. Deleting a session on one device does not remove it on other devices until
   those devices delete it locally — there is no remote-delete broadcast yet.
3. Sessions are last-write-wins based on client timestamps.
4. Rate limits are per-process, in-memory counters: they do not survive a
   restart and are not shared across replicas.
5. No layout recognition for PDFs or Word documents — only text-decodable
   content (plain text, code, CSV/Markdown, tables) is sent to the model.
6. Reasoning ("thinking") content is only shown when the upstream gateway
   streams a reasoning channel; some proxies drop it.
