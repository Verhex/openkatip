# Architecture and boundaries

Katip is one repository with independent Meet and Ops engines. Katip Full means
operating both; there is no automatic action bridge. Python package names remain
`katip` and `katip_ops`; the public distribution is `openkatip`.

## Meet components and data flow

1. FastAPI serves the Turkish admin/account/live web interfaces, authenticated
   management routes, signed sharing links, worker coordination and WebSockets.
2. The session pipeline receives PCM and roster/chat events from a browser worker,
   streams speech to a configured STT provider and publishes segments/translations.
3. The command/controller layer applies command authorization and LLM tool calls;
   document retrieval adds bounded preparation context, treated as untrusted data.
4. Report generation summarizes notes, decisions and actions and renders Word/PDF.
   Graph integrations poll the installation mailbox and send approved email.
5. SQLAlchemy stores meeting state in SQLite or PostgreSQL; persistent files hold
   generated artifacts. Configured envelope encryption protects meeting content.

The browser worker uses Playwright/Chromium platform adapters for Teams, Meet and
Zoom. It captures audio, meeting chat and visible speaker/participant information;
it can play TTS and share explicitly uploaded presentation pages. Platform admission
and identities depend on platform metadata; a display name is not proof of authority.

## Containers in isolated Compose

| Component | Trust and network boundary |
| --- | --- |
| API | Holds database/provider credentials and content keys; loopback HTTP published for proxy; joins control/worker/management/parser networks. |
| Manager | Holds Docker socket and manager credential, not server data volume; launches/leases dynamic per-meeting workers. |
| Browser worker | Per-meeting capability only; no database/server credentials; seccomp/non-root/resource limits; meeting-network egress must be firewalled. |
| Parser | Untrusted document extraction, private parser network, no internet or server volume, read-only root and bounded resources. |
| PostgreSQL | Private control network and persistent volume; never public. |

Control, management and parsing networks are internal. The worker network is not
an internet firewall by itself. A separate worker-host deployment can reduce the
blast radius further, but requires authenticated control connectivity and host
network policy. Local bot mode shares the server trust boundary.

## Ops components and flow

Socket Mode and APScheduler enqueue deterministic allowlisted jobs in one asyncio
process. Runtime documents supply installation policy/voice/claims. The router
filters executors by task type, availability and data class, then handles retryable
fallback. Executors return text/tool proposals, never directly execute tools.
Jobs grant typed read tools, validate output and policy, and create explicit action
proposals. Owner-only approval snapshots are persisted; the applier revalidates
edited proposals before allowed Odoo/file effects.

The SQLite WAL ledger stores task IDs, status, classification, executor, usage,
cost and prompt digest/size rather than raw prompts. Work files can still contain
personal or customer data. Squid restricts outbound HTTPS to configured hosts;
an internal Odoo network is separate. Remote writes and local approval state are
not one atomic transaction: reconciliation is required after ambiguous failures.

## Security and integration limits

Meet bearer APIs, cookie sessions, signed links, worker tokens and parser credentials
serve different scopes. Do not substitute one credential for another. Protect
signed URLs as secrets and do not place them in public logs. The privileged manager
and host remain trusted. External providers remain processors outside the on-prem
boundary. Prompt delimiters are only one layer; permissions, typed tools, code policy
and human approval enforce effects. [Security](security.md) covers the threat model.

`engines/ops/EVENTS.md` defines future event types. There is no listener, webhook,
poller or meeting import that consumes those events in the current engine.
