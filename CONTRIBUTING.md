# Contributing

Thanks for taking a look. This is a small self-hosted project, so the bar for
contributions is simply: keep it deployable and keep secrets out.

## Ground rules

- **Never commit secrets.** No API keys, passwords, private hostnames, LAN IPs,
  personal domains or local absolute paths. Everything configurable belongs in
  git-ignored env files; templates (`*.example`) are committed instead.
- **One codebase, no private forks.** Config goes through environment variables,
  not through edited constants.
- Keep dependencies in the lockfiles (`pnpm-lock.yaml`, `requirements.txt`).

## Getting set up

```sh
# backend
cd server
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                             # then edit
uvicorn app.main:app --reload --port 8439

# front end
cd app
pnpm install
pnpm dev                                         # http://localhost:8090
```

## Before you open a pull request

- `cd server && pytest` — backend tests pass.
- `cd app && pnpm type && pnpm build` — typecheck and production build pass.
- No new hard-coded hosts, ports or credentials.
- If you change configuration, update `server/.env.example` **and** the
  configuration table in `README.md`.

CI runs the same two jobs on every pull request
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

## Commit and PR style

- Small, focused commits with a plain-language subject line.
- Describe *why* in the body when it is not obvious.
- New behaviour needs a test or a manual verification note in the PR.
- Formatting: `pnpm format` in `app/`; Python follows PEP 8 with 4-space
  indentation.

## Reporting bugs and asking questions

Open an issue with your deployment shape (Docker version, host OS, whether you
use a reverse proxy) and the relevant logs (`docker compose logs --tail=200`).
**Scrub tokens, hostnames and IPs before pasting.** Security issues: see
[SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contribution is licensed under the MIT
license in [LICENSE](LICENSE).
