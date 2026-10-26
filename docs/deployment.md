# Production deployment

Use `docker-compose.isolated.yml` as the single-server production starting point.
The older `docker-compose.yml` is a development topology with example database
credentials and exposed database/Redis ports; do not publish it as production.
`docker-compose.worker-host.yml` supports a separate worker host. Prepare keys
and bootstrap access using [getting started](getting-started.md).

## TLS, proxy and network isolation

Terminate HTTPS at your managed reverse proxy or tunnel and forward WebSockets.
Bind upstream port 8000 only to loopback or a private authenticated transport.
Keep secure panel cookies enabled. Set the public URL explicitly. The API image
uses an explicit Uvicorn command; configure proxy headers and trusted peers
together. The default client-IP strategy uses the last X-Forwarded-For value;
your proxy must replace untrusted incoming forwarding headers. Cloudflare mode
accepts CF-Connecting-IP only from explicitly configured peer IPs. When using
that mode, disable automatic Uvicorn peer rewriting (`--no-proxy-headers`).

Keep PostgreSQL, parser, manager and Docker socket off public networks. The manager
can control the host Docker daemon: restrict host administrators and do not share
its socket with API, parser, browser workers or Ops. The parser has a private
network, read-only root and resource limits. Use per-manager random secrets;
remote managers require HTTPS except for the explicit internal Compose hostname.

Dynamic browser containers have one-meeting capabilities, non-root execution,
capability drops, seccomp, read-only filesystem/tmpfs and bounded resources.
Keep `bot_sandbox=on`, site isolation and private-network blocking enabled.
Local subprocess mode shares a host trust boundary and is intended for trusted
development. It is not equivalent to container isolation.

## Worker egress firewall

The supplied worker network permits internet access. Docker networking and browser
request filtering alone do not prove network isolation. Before real meetings,
enforce host firewall rules for the dynamic worker network: deny host/management,
RFC1918, loopback, link-local/metadata, IPv6 local ranges and other container
networks; allow only the API ingest destination/port needed by workers and reviewed
meeting-provider egress. Apply equivalent IPv4 and IPv6 rules. Avoid a blanket
private-network exception. Meeting media may use UDP, so validate each platform
on the target server. TODO: maintain an installation-owned tested firewall policy;
no universal provider IP list is shipped. Ops uses its separate Squid allowlist.

## Persistent data and backups

Back up the PostgreSQL database and `server-data` volume as a consistent set.
Database metadata and files are linked; a database-only backup is incomplete.
Include deployment configuration, image digests, the encryption installation
identity, master keys and signed-link secrets in the recovery plan. Store keys
separately from encrypted backups with tightly controlled recovery access.
Use PostgreSQL-native backups or stop writers for a consistent filesystem snapshot.
Test a full isolated restore and meeting report access before relying on it.

Ops state is separate: stop its process or use SQLite's backup API, preserving
WAL consistency; retain its config and work directory. Its state and drafts do
not inherit Meet's content encryption or retention automatically.

## Content encryption and key rotation

Set `KATIP_REQUIRE_ENCRYPTION=true`. Supply a base64-encoded random 32-byte
`KATIP_DATA_KEY_FILE`; the file overrides inline `KATIP_DATA_KEY`. Meeting data
uses envelope AES-256-GCM, per-meeting keys, installation/meeting/field associated
data and encrypted files. Some metadata, access accounts, logs and Ops state remain
outside this envelope; use disk/database encryption and access controls too.

For rotation, stop meeting writes and take a recoverable backup. Create a new
32-byte master key, retain the old key as `KATIP_DATA_KEY_PREVIOUS`, and configure
the new current key/file. Run `python -m katip.store.rotate_key` using the same
database, data directory and installation identity. The migration rewraps meeting
keys and handles legacy formats; background migration also retries errors.
Verify migration health, archived meetings and restored backups before removing
the previous key. Retain old keys for backups that still need them. Never replace
an unreadable key with an empty value or disable encryption to bypass errors.
Lost keys cannot be recovered by resetting a panel password.

Changing `KATIP_SECRET_KEY` affects signed links. Plan reissuance; do not assume
old shares remain valid. Voiceprint keys are separate legacy credentials.

## Retention and upgrades

Meeting retention defaults to 365 days; diagnostics default to 7 days. A retention
value of 0 disables automatic meeting deletion. Align the notice text, backup
retention and provider contracts with actual behavior. Requested screenshots and
join-failure diagnostics require particular review. Administrative meeting deletion
and participant data deletion are separate operations; see [admin guide](admin-guide.md).

Before upgrades, record deployed image tags/digests, config and schema version;
back up, build reviewed source, test in staging, then restart services. Do not
assume schema changes can be reversed by selecting an older image. Roll back
with a matching database/files snapshot when needed. Provider model availability
must be checked for your accounts. For Ops, reconcile pending approvals and any
partial external writes before restart/retry; image rollback does not undo Odoo writes.
