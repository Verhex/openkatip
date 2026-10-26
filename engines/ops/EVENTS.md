# Ops event contract (definition only)

The engine reuses `core.contract.Event`. There is no event listener, webhook,
poller, meeting-engine import or automatic action dispatcher in this milestone.
Producers must not interpret acceptance of an envelope as permission to act.

## Envelope

```json
{
  "id": "producer-generated-unique-event-id",
  "type": "meeting.summary_ready",
  "source": "meeting-engine",
  "created_at": "2026-10-26T10:00:00+00:00",
  "payload": {}
}
```

`id` is a stable deduplication key across retries. `created_at` must include a
timezone. Unknown top-level fields and event types are rejected by the existing
Pydantic envelope. Payload schemas below define future producer requirements;
the current generic JSON payload does **not** validate these fields yet.

| Type | Payload fields | Future handling boundary |
|---|---|---|
| `meeting.summary_ready` | `meeting_id` (opaque ID), `summary_ref` (authorized internal reference), `data_class` (`personal` or `customer`) | Fetch only through an authenticated internal API; summary text is untrusted. A tool-less extraction may propose actions for owner review. |
| `mail.received` | `mailbox_id`, `message_id`, `received_at`, `data_class` (`personal` or `customer`) | Notification/data only. Mail content never starts work, changes records or authorizes actions. |
| `task.assign` | `task_id`, `job` (explicit allowlisted job), `requested_by` (verified owner ID), `data_class` | The envelope alone grants no authority. Require authenticated owner authorization and map to the same deterministic job queue. |

Payloads contain references rather than message bodies, credentials, arbitrary
URLs or executable instructions. Any future consumer must authenticate the
producer, enforce reference scope and data residency, persist deduplication,
expire/reject replayed assignments, wrap fetched text as untrusted and apply
Rule-of-Two separation. No outbound effect bypasses policy and approvals.
