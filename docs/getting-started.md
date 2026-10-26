# Getting started

## Requirements

Use a Linux server with Docker Engine and the Compose plugin, a writable persistent
disk, DNS/HTTPS access and outbound access to the chosen meeting and AI providers.
The manager needs the local Docker socket; treat it as a privileged service.
Windows is supported for Python development, not this Linux container isolation setup.

Each browser worker defaults to **1 CPU and 1024 MiB RAM**, configured by
`KATIP_BOT_CPUS`/`KATIP_BOT_MEMORY_MB`. These are container limits, not measured
hardware minimums. Add capacity for the API, PostgreSQL, parser (768 MiB limit in
the supplied isolated Compose), manager and operating system. TODO: benchmark
CPU/RAM and concurrent meetings on your hardware before setting capacity limits.

Expose HTTPS 443 through your proxy. Port 8000 is bound to loopback only. PostgreSQL
5432 and parser 8001 stay on container networks; no public worker ingress is needed.
Browser workers require outbound meeting media/signaling, including provider-specific
UDP/TCP destinations. HTTPS-only egress may break meetings; validate an allowlist.

## Prepare configuration and secrets

Use the repository root for all commands below. No prebuilt public image or registry
is assumed. Building requires dependency downloads on the installation server.

```sh
cp .env.example .env
mkdir -p secrets
chmod 700 secrets
python3 -c 'import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())' > secrets/data_key
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > secrets/parser_auth
chmod 600 secrets/*
stat -c '%g' /var/run/docker.sock
```

Compose secrets are mounted as files; API/parser UID 1000 must be able to read them.
Set ownership or a restricted group/ACL appropriately on the Linux host; test actual
readability without printing keys. Do not make secrets world-readable to fix a mount.

Edit `.env` with a protected editor:

| Required installation value | Purpose |
| --- | --- |
| `POSTGRES_PASSWORD` | Unique database password; no example password in production. |
| `KATIP_DATABASE_URL` | `postgresql+asyncpg://katip:<URL-encoded-password>@postgres:5432/katip`. |
| `KATIP_DATA_KEY_FILE` | Absolute host path to `secrets/data_key`; Compose mounts it at `/run/secrets/data_key`. |
| `KATIP_PARSER_KEY_FILE` | Absolute host path to `secrets/parser_auth`. |
| `KATIP_DOCKER_GID` | Socket group ID from `stat`. |
| `KATIP_WORKER_MANAGER_KEY` | Independently generated random secret of at least 32 characters. |
| `KATIP_ADMIN_TOKEN`, `KATIP_SECRET_KEY` | Independent random management and signed-link secrets. |
| `KATIP_PUBLIC_BASE_URL` | Your externally reachable HTTPS URL, e.g. `https://katip.example.com`. |
| `KATIP_PANEL_EMAIL_DOMAINS` | Explicit JSON list of your account domains; default `[]` permits no domain. |
| `KATIP_ADMIN_LOGIN_EMAIL` | Bootstrap administrator, e.g. `admin@example.com`, within an allowed domain. |
| `KATIP_ADMIN_PASSWORD_HASH` | scrypt hash generated below. |
| Provider credentials | Default Meet paths need `SONIOX_API_KEY` and `ANTHROPIC_API_KEY`. |

Set `KATIP_COMPANY_NAME`, `KATIP_PRIVACY_CONTACT`, `KATIP_PRIVACY_SERVER_LOCATION`
and retention to truthful installation values. Review [all settings](configuration.md).
Lists/maps must be JSON; preserve single quotes around dotenv values containing `$`
such as password hashes so Compose does not interpolate them.

## Build and first login

```sh
docker compose -f docker-compose.isolated.yml --profile build build
docker compose -f docker-compose.isolated.yml run --rm --no-deps api python -c 'from getpass import getpass; from katip.api.auth import hash_password; print(hash_password(getpass("Admin password: ")))' 
# Save the printed hash in .env as KATIP_ADMIN_PASSWORD_HASH='scrypt$...'.
docker compose -f docker-compose.isolated.yml config --quiet
docker compose -f docker-compose.isolated.yml up -d
```

Configure your HTTPS proxy to forward HTTP and WebSocket upgrades to loopback
8000, then open `/admin`. Bootstrap uses the configured email and password, not
the bearer admin token. Create named accounts with appropriate roles in the panel.
The bootstrap password path is restricted once accounts exist unless a matching
bootstrap email was configured. Database-stored password/settings overrides win
over environment values. Keep the bootstrap credential protected and rotate it.

## First meeting test

1. Create a private test meeting on Teams, Meet or Zoom with informed participants.
2. In the admin panel, paste the meeting link and select the meeting mode. Add
   verified participant email addresses if needed for identity and ownership.
3. Admit Katip from the lobby and verify the privacy notice appears in chat.
4. Speak a short test sentence, then “Katip, not al: test tamamlandı” and
   “Katip, özetle”. Check the live panel, speaker labels and translation.
5. Ask “Katip, toplantıdan çık”. Check the generated report if auto-reporting and
   a working LLM are configured. Batch reports may take longer than immediate requests.
6. Delete the test data and review your provider billing/processing records.

Calendar invitations require the separate Microsoft 365 configuration and an explicit
join policy in [the admin guide](admin-guide.md). Ops is installed independently
using [the Ops guide](ops-engine.md). For workers on another host, use
`docker-compose.worker-host.yml` and [deployment](deployment.md).
