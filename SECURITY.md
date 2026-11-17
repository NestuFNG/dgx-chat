# Security policy

DGX Chat is a self-hosted application: whoever runs it is responsible for its
host, its network exposure and its data. That said, vulnerabilities in *this
code* are worth reporting.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
("Security" tab → "Report a vulnerability") on this repository.

If that is unavailable, open a normal issue that says only that you have a
security report and how to reach you — no details, no proof of concept, no
affected URLs.

Please include:

- what you found and why it is a problem,
- the version or commit you tested,
- reproduction steps or a minimal proof of concept,
- whether the finding is already public.

You can expect an acknowledgement within about a week. This is a volunteer
project, so please allow reasonable time before disclosing publicly.

## Scope

In scope:

- the backend in `server/` (authentication, sessions, file handling, admin
  endpoints, request parsing),
- the front end in `app/` (token storage, origin handling, unsafe rendering),
- the deployment scripts in `scripts/` and `deploy/`,
- the Android shell in `android/` (WebView configuration, cleartext policy).

Out of scope:

- misconfiguration of your own deployment (open port, no TLS, `REGISTRATION_MODE=open`
  on a public host, unchanged defaults),
- weaknesses of the inference gateway or model you point it at,
- the upstream [deepseek-ai/chat-demo](https://github.com/deepseek-ai/chat-demo)
  code itself (report those upstream — but tell us if our fork makes it worse),
- denial of service by sheer request volume,
- findings that require an already-compromised host or admin account.

## What is known and accepted

These are documented limitations rather than vulnerabilities:

- rate limiting is per-process and in-memory, so it does not survive restarts
  and is not shared between replicas;
- session tokens never expire unless `TOKEN_TTL_DAYS` is set (they can be
  revoked from the admin page);
- `CORS_ALLOW_ORIGINS=*` is permitted, and is the default, for home-network use;
- the data volume is not encrypted at rest;
- the Android build permits cleartext HTTP so that LAN deployments work.

## Handling secrets

Never commit `.env`, `deploy/.env.deploy`, `tools/nas.env`, keystores or tokens —
they are git-ignored for that reason. If you do commit one, rotate it: removing
it from the tip of the branch is not enough.
