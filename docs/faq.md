# Frequently asked questions

## Is everything local?

The control plane, storage and worker containers run on your server. Configured
STT, TTS and language models are external services. See [providers](providers.md).
No fully local STT/LLM replacement is shipped as the default.

## Which meetings work?

The browser adapters target Teams, Google Meet and Zoom. Guest admission, chat,
audio and presentation permissions vary. UI changes can require adapter updates;
test your actual tenant before relying on unattended joins.

## Why did the calendar bot not join?

Calendar joining defaults to disabled. Check Graph configuration, mailbox, eligible
URL, policy, trusted organizer list, review queue, daily budget and lobby admission.
Changing an already approved invitation may require a new approval.

## Why can nobody log in after copying the example?

No account domain is trusted by default and no password is shipped. Set explicit
panel domains, bootstrap email and scrypt hash. Use HTTPS with secure cookies and
remember that stored panel settings/passwords override environment defaults.

## Does Katip record audio or recognize voices?

Normal meeting sessions do not persist raw audio. They process audio in memory
and may send it to providers. Transcripts and requested screenshots can be stored.
MeetingSession disables biometric voice recognition; legacy code/settings remain.

## Does “kayıt dışı” delete earlier notes?

It pauses new note capture for that part of the meeting. It does not erase earlier
content, backups or other participants' recordings. Ask the operator about deletion.

## Why is the report late or the cost missing?

End reports may use batches and wait up to the configured timeout before immediate
fallback. Missing model credentials or provider errors can also prevent generation.
Usage prices are estimates: missing Meet STT rates may appear as zero, while Ops
unknown totals are explicitly unknown. Reconcile with actual provider invoices.

## Does Katip Full automatically turn meetings into Odoo tasks?

No. Both engines are included, but the event contract is a definition, not a running
dispatcher. Ops actions follow their own policy and owner approval process.

## Does Ops dry-run avoid all effects?

`--fake` avoids remote services. Real dry-run can make read-only API calls and paid
model calls; both modes can write local drafts, ledger and approval state. Use
separate state/work directories for testing. Publication remains manual.

## May I use the code commercially?

MIT permits commercial use subject to its notice requirements. Dependencies retain
their own licenses. The Katip name/logo are not licensed by MIT; see the README
trademark note. No compliance certification or service warranty is provided.
