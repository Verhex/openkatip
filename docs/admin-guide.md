# Administration guide

## Panel accounts and roles

The web interface is Turkish. Bootstrap login is described in
[getting started](getting-started.md). Create named accounts with passwords rather
than sharing the bearer admin token. Accounts must be enabled and their email
domain must be in `KATIP_PANEL_EMAIL_DOMAINS`. Changing a domain allowlist can
invalidate access for existing accounts. Authentication uses email/password;
Microsoft Graph configuration does not enable Entra SSO or MFA.

| Panel label | Stored role | Intended access |
| --- | --- | --- |
| Yönetici | `admin` | Manage accounts, installation settings and all meetings. |
| Kullanıcı | `operator` | Start/manage authorized meetings and perform allowed actions. |
| İzleyici | `viewer` | Read permitted meeting information; cannot manage meetings. |

Meeting ownership and participant identity further restrict non-admin access.
Display names and transcript statements are not authority. Review the users and
meeting assignments together; disabling an account stops subsequent session use.
Panel writes require a session CSRF token. Password changes revoke sessions within
the applicable account scope. Idle and maximum lifetimes are configurable.

## Meetings, documents and presentations

Paste a supported meeting URL, select `tam` or `kayit`, and provide participant
emails as needed. Check that the assigned owner is correct. Monitor worker status,
admission and the live panel; ending the meeting releases its worker. Concurrency,
duration and daily-minute caps are application controls, not provider billing limits.

Upload preparation documents through the meeting's document controls. Supported
parsers include plain text, PDF, DOCX and PPTX formats; inspect
`documents/parser.py` for accepted extensions. Upload size, count, context and
timeout limits are configurable. Do not disable parser isolation for untrusted files.
Documents are untrusted evidence and may contain prompt injection.

PDF presentations require an explicit presentation upload and encrypted storage.
The ordinary document path does not retain the original as a presentation.
An authorized panel confirmation starts screen sharing; platform permission can
still reject it. Remove documents from the same meeting when no longer needed.

## Calendar policy

Set the installation mailbox and Graph credentials, then choose a policy:

| Value | Behavior |
| --- | --- |
| `kapalı` | No calendar fetching/joining through this feature. |
| `onay` | Eligible invitations require review. |
| `güvenilir` | Explicit trusted organizer emails/domains may join automatically; others require review. |
| `hepsi` | Eligible invitations can join automatically; use only after risk review. |

Trust domains and full email addresses explicitly; defaults are empty. Only exact
ASCII domains match. The queue has persistent capacity limits and automatic daily
join budgets. Changed approved invitation details require reapproval, even in
automatic mode. Check pending/rejected/changed invites before adding trust.
Invited attendees do not automatically become owners: enabled matching organizer
accounts and explicit approver ownership determine that assignment.

## Microsoft 365 and Teams

Register an installation-owned Entra application and configure tenant ID, client
ID, client secret and mailbox. Grant application permissions only for enabled Graph
features, with tenant administrator consent: calendar reads use `Calendars.Read`,
meeting-note sending uses `Mail.Send`; scope mailbox access in your tenant policy.
Review `integrations/microsoft.py`, `calendar.py` and `mail.py` against your tenant
configuration. TODO: validate tenant-specific permissions and access policies in
a test tenant; no tenant IDs or ready-to-use enterprise grants are shipped.

The optional Teams bot is separate from the Chromium meeting participant. Configure
`KATIP_TEAMS_BOT_APP_ID`, secret/file and tenant, plus HTTPS public/privacy/terms
URLs. The messaging route is `/api/messages`. Generate a package using:

```sh
python -m katip.teams.package --out katip-teams.zip
```

The package generator reads process environment only, not `.env`. Import the ZIP
through your tenant's Teams app management process and apply appropriate policies.
The manifest supports personal and groupChat scope, not channel meetings, and
requests `OnlineMeeting.ReadBasic.Chat` resource-specific permission. Test signed
activity validation, linked panel identity and meeting ownership. Teams buttons
cannot replace authenticated owner approval. Rebuild when bot IDs or URLs change.
For an existing Teams installation, set `KATIP_TEAMS_APP_PACKAGE_ID` explicitly
to preserve its package identity when migrating from a differently branded build.

## Integrations, usage and deletion

Mail sending is off by default and uses a prepared draft plus explicit authorization.
Set internal-domain policy and inspect recipients before approval. Monday's token
is a reserved setting, not a documented end-to-end integration. Ops Slack/Odoo
configuration is separate; installing both engines does not connect them automatically.

The usage view groups provider/model/region consumption. Values are estimates;
set contract-specific price overrides and reconcile with provider invoices. Unknown
or missing STT rates may appear as zero estimates in Meet; Ops marks unknown cost
explicitly. Set provider-side hard budgets as well as Katip's meeting limits.

Delete meetings through authorized management controls to remove their linked
content and generated files. Use person/identity deletion for participant-related
data requests and verify affected documents, screenshots and reports. Do not infer
that a display-name removal erases all appearances in free text. Configure retention
and debug retention, then test a deletion on sample data. Backups, provider retention,
Ops drafts and external Odoo records require separate retention/deletion procedures.
Preserve only the audit metadata your policy requires, with restricted access.
