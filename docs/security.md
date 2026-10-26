# Security model

## Threat summary

Protect meeting content, identities, documents, approval state, signing/encryption
keys and external API credentials from unauthorized participants, leaked links,
malicious documents/transcripts, compromised browser sessions and accidental
operator actions. Host administrators, the Docker manager, configured provider
accounts and installation policy documents are trusted. A host compromise is outside
the per-container boundary; harden and patch the host separately.

## Access and isolation

Panel passwords are scrypt hashes. Random session tokens are hashed in the database;
cookies use HttpOnly/SameSite controls, secure transport by default and CSRF on
mutations. Account roles combine with meeting access checks. A display name or
spoken claim cannot authorize an administrative side effect. Rate limits include
process-local login failure protection; restarting resets that counter.

Bearer admin APIs are powerful and separate from panel sessions. Signed panel/report
links provide scoped bearer access until expiry and must not be publicly posted.
Teams integration verifies activities and linked account permissions; Graph uses
installation application credentials. Entra SSO/MFA is not implemented panel login.

Browser workers receive short-lived meeting capabilities and no server keys.
Keep Chromium sandboxing, site isolation and non-root/container restrictions enabled.
Use the isolated parser for untrusted documents. Restrict manager Docker access,
host networking and IPv6 as described in [deployment](deployment.md). Container
configuration alone is not proof of host egress enforcement.

## Prompt injection and actions

Meeting speech, chat, uploaded documents, GitHub/Odoo evidence and GEO answers are
untrusted. Text that requests policy changes or declares itself an administrator
does not grant authority. The Meet controller gates sensitive actions and panel
approvals. Ops separates deterministic orchestration from tool-capable research
and tool-less synthesis, restricts tool grants, validates JSON, checks policy/claims
and snapshots approval actions. Edited actions are checked again before application.
Only the configured Slack owner can decide Ops approvals.

These layers reduce risk without proving that model output is correct. Claims
heuristics do not establish factual entailment. Review recipients, generated content,
Odoo record IDs and proposed edits. Never add arbitrary shell/SQL/HTTP execution
tools to an untrusted summarization step. A prompt delimiter alone is not a defense.

## Secrets, storage and privacy

Supply provider credentials only to the engine that needs them. Keep key files
outside source control, with service-specific permissions and separate backups.
Enable required content encryption and test rotation/restoration. Some metadata,
account state, Ops SQLite/work files and provider-side data are not covered by Meet
content encryption. Encrypt disks/backups and restrict logs accordingly.

Normal sessions do not persist raw meeting audio. An in-memory buffer supports
provider switching. Transcripts, screenshots, report files and diagnostics still
contain sensitive data. Configure retention, informed participant access and
deletion handling; the shipped notice requires installation review. Neither MIT
licensing nor on-premises hosting certifies regulatory compliance.

## Reporting

Use the private process in [SECURITY.md](../SECURITY.md). TODO: the release owner
must replace its placeholder reporting address before publication. Do not send
live credentials, personal transcripts or private documents in a public issue.
