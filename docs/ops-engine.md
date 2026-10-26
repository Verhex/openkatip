# Katip Ops

Ops is an independent Python 3.12 engine for Slack and Odoo. It runs four jobs:
weekly content (`icerik`), public pipeline research (`pipeline`), assistant visibility
measurement (`geo`) and Friday review (`cuma`). Output text is Turkish. It does not
consume meeting events or automatically publish content. [EVENTS.md](../engines/ops/EVENTS.md)
defines future integration envelopes only; there is no event listener or dispatcher.

## Concepts and effect boundaries

- A **task** labels its type, data class, source and requester. Code orchestrates
  steps; the LLM cannot select arbitrary workflows or change permissions.
- An **executor** is a configured provider/model adapter. It returns text or typed
  tool requests, not external effects. Routing filters data class and availability.
- **Policy** gates drafts, personal data, prohibited/customer terms, claims and
  allowed action kinds. These checks are conservative heuristics, not fact verification.
- An **approval** snapshots a proposed action with digests, owner and expiry.
  Only the configured Slack owner may approve, edit or reject. The applier rechecks
  edited payloads. Mandatory sensitive actions cannot be weakened by routine rules.
- The **ledger** persists task status, executor/model usage, costs, prompt digest
  and sizes in SQLite WAL; it does not store raw prompts. Work files and Odoo still
  contain business data and need independent access/retention controls.

Research steps have explicit read-tool grants. Untrusted evidence is summarized
without tools; Odoo writes are not exposed to the model. Approval is a separate
effect boundary. Ledger completion does not provide exactly-once remote delivery:
an Odoo write and subsequent chatter note are separate operations. Reconcile
ambiguous or partial success before retrying.

## Installation configuration

Create an installation-owned directory outside the repository. Copy
`engines/ops/config/settings.example.toml` to `settings.toml`,
`executors.example.yaml` to `executors.yaml` and `config/examples/*` into the same
directory. Replace neutral examples with reviewed business policy. Structure:

```text
config/
  settings.toml
  executors.yaml
  OPS.md
  VOICE.md
  ICP.md
  claims.md
  sops/
    weekly-content-pack.md
    pipeline-research.md
    growth-report.md
    friday-review.md
  growth/geo-questions.md
state/ops.sqlite3
work/
  content/ideas/  content/drafts/  content/approved/  content/published/
  pipeline/  growth/raw/  reviews/
```

Set `KATIP_OPS_CONFIG` to the engine configuration directory (default `config`
relative to the process). Set `KATIP_CONFIG_DIR` to the runtime-document directory;
both may name the same path. `--config` changes engine settings only, not document
root. Relative database, work and credential paths resolve against engine config.
Secrets are read from individual files; do not put key values in TOML/YAML.
Use service-specific filesystem permissions. Neutral templates are starter policy,
not evidence that your business has any particular capability.

Runtime documents are read anew for each generation. `OPS.md`, the job's SOP,
`VOICE.md`, `ICP.md` and `claims.md` form the stable system context; Claude can cache
it. An optional installation `DECISIONS.md` can supply a content evidence diff.
Do not put secrets in that document: excerpts can be sent to providers.

`ICP.md` needs a parseable `criterion | 0–N | description` scoring table with
maxima summing to 10. Segment IDs use `S1`, `S2`, etc. Pipeline code rotates the
segment by ISO week and requires public source evidence. `VOICE.md` can define a
LinkedIn word range in a table. Claims rows use `C-001 | claim | evidence | status |
last checked`; only `public` claims authorize the heuristic gate. Citation presence
does not establish that a claim supports a sentence. GEO questions use `G-001`
table rows; `G-050` and later and brand-containing questions are excluded from
the unprompted baseline. Raw assistant answers remain untrusted measurement data.

## settings.toml reference

Defaults below describe `katip_ops.config`, not credentials for a real installation.

| Field / section | Type and default | Meaning |
| --- | --- | --- |
| `owner_slack_id` | required nonempty string | Single verified owner; replace placeholder with your user ID. |
| `database_path` | path, `state/ops.sqlite3` | Ledger and approvals database. |
| `approval_ttl_hours` | positive number, 72 | Approval validity. |
| `timezone` | string, `Europe/Istanbul` | Current validator accepts only this timezone. |
| `ops_dir` | path, `.` | Confined work-file root; example uses `../work`. |
| `dry_run` | bool, true | Disable external application of proposed Odoo/file actions; local job drafts still write. |
| `content_excluded_repos` | string array, empty | GitHub repository names/full names excluded from evidence. |
| `max_tool_rounds` | integer 1–10, 3 | Per-generation granted-tool round cap. |
| `github.org` | string, empty | Installation GitHub organization; required by check-config. |
| `github.token_file` | path, `secrets/github_token` | Restricted read-only token for selected organization repositories. |
| `geo.mention_names` | array, empty | Own brand/product terms for deterministic mention checks. |
| `geo.citation_domains` | array, empty | Own domains for citation scoring. |
| `geo.competitors` | array, empty | Approved comparator names. |
| `geo.assistants` | array, empty | Explicit executor names to query. |
| `odoo.base_url` | string, `http://odoo-test:8069` | Trusted internal test Odoo; use HTTPS for remote transport. |
| `odoo.db` | string, `katip_agent_test` | Dedicated test database; replace per installation. |
| `odoo.key_file` | path, `secrets/odoo_api_key_test` | JSON-2 bearer key for a restricted application user. |
| `odoo.timeout_seconds` | positive number, 30 | Odoo call timeout. |
| `slack.slack_bot_token` | path, `secrets/slack_bot_token` | Bot OAuth token file. |
| `slack.slack_app_token` | path, `secrets/slack_app_token` | Socket Mode app token file. |
| `slack.proxy` | string or null, null | Explicit proxy; otherwise HTTPS_PROXY/HTTP_PROXY, including lowercase variants. |
| `slack.channels` | map, empty | Required keys: ops, ops_approvals, pipeline, social; IDs must begin C/G and be alphanumeric. |
| `policy.banned_terms` | array, empty | Terms forbidden in drafts. |
| `policy.customer_names` | array, empty | Private customer names also prohibited in public content. |
| `policy.personal_email_domains` | array, empty | Extend built-in personal email detection. |
| `policy.allowed_kinds` | array, empty | Explicit action-kind allowlist. |
| `policy.approval_rules.stage` | 1 or 2, 1 | Stage 2 permits configured routine automation only where mandatory policy allows. |
| `policy.approval_rules.stage1` | map kind → bool, empty | Stage 1 approval requirements; missing kind requires approval. |
| `policy.approval_rules.routine_kinds` | array, empty | Stage 2 routine action candidates. |
| `policy.approval_rules.always_approval` | array, empty | Additional kinds always requiring approval. |
| `schedules.<job>.day_of_week` | mon–sun, required | Day for that configured schedule. |
| `schedules.<job>.hour` | 0–23, 10 | Local scheduled hour. |
| `schedules.<job>.minute` | 0–59, 0 | Scheduled minute. |
| `schedules.<job>.enabled` | bool, true | Disable a job schedule without removing the job. |

Source defaults: pipeline Tuesday 10:00, icerik Wednesday 10:00, geo Thursday 10:00,
cuma Friday 16:00. Custom schedule maps replace the model map, so include each
desired job explicitly. The scheduler coalesces missed runs with one instance and
a one-hour misfire grace. Run only one active scheduler per installation/state.

## executors.yaml reference and residency

Top-level `executors` maps installation names to provider definitions. `routing`
has a required nonempty `default` list and optional `routes` map. Exact task routes
win, then the first declared matching glob, then default. Fallback preserves data
class restrictions. Only explicitly retryable failures advance after SDK retries;
refusals and policy failures do not become a different provider attempt.

| Executor field | Type / source default | Meaning |
| --- | --- | --- |
| `provider` | claude/openai/deepseek/xai, required | Adapter family. xAI remains unavailable. |
| `model` | nonempty string, required | Account-valid model ID; Haiku is rejected. |
| `enabled` | bool, false | Explicit activation. Missing key makes that executor unavailable. |
| `key_file` | path, required | Credential file. |
| `base_url` | string or null | Required explicitly for DeepSeek; review endpoint trust. |
| `allowed_data_classes` | required array | public/internal/personal/customer allowlist. |
| `max_tokens` | positive integer, 4096 | Output/thinking budget as supported by adapter; examples use 16000. |
| `max_retries` | nonnegative integer, 2 | Provider SDK retry count. |
| `timeout_seconds` | positive number, 60 | Provider request timeout. |
| `thinking` | adaptive/off, off | Claude thinking configuration. |
| `effort` | low/medium/high/xhigh/max, high | Supported model effort. |
| `server_fallback` | bool, false | Claude beta server-side model fallback; validate account support and data policy before enabling. |
| `web_search_type` | string, `web_search_20260209` | Claude server search tool version used when a job explicitly grants web. |

`prices` maps exact model IDs to nonnegative `input`, `output`, `cache_read`,
`cache_write` (USD/million tokens), and `web_search` (USD/thousand searches).
All five rates must be present to compute known USD; no rates are assumed.
The example enables Claude routes and leaves OpenAI/DeepSeek disabled. Confirm
model availability before use; the examples are not a live availability guarantee.

DeepSeek can receive **public and internal only**; personal/customer classes are
rejected in validation and routing, including fallback. Caller classification must
be correct: changing a label does not anonymize CRM or personal evidence. This rule
is a product boundary, not proof of legal residency for other providers. Pipeline
research requires Claude server search; Friday review's first candidate must be
an Opus model. OpenAI/DeepSeek GEO answers currently have no web search tool;
do not compare them as equivalent search coverage.

## Slack application

Create an installation-owned Slack app using the neutral manifest at
`engines/ops/config/slack-app.example.yaml` (also shown by its comments). Enable
Socket Mode, interactivity, `/katip` and the `message.im` bot event. Required bot
scopes are `chat:write`, `commands`, `im:history` and `im:write`. Create an app-level
token with `connections:write`; store both tokens as separate secret files.
Invite the bot to the configured channels and replace placeholder conversation
IDs and owner user ID. Reinstall the app after permission changes. No inbound
public webhook port is required by Socket Mode.

Owner commands are `/katip durum`, `/katip çalıştır pipeline`,
`/katip çalıştır icerik`, `/katip çalıştır geo`, `/katip çalıştır cuma`,
`/katip fikir <metin>` and `/katip yardım`. The same text without `/katip` works
in the verified owner's direct message. Non-owner requests are refused; channel
messages and bot/app messages do not enqueue jobs. Approval cards offer approve,
edit and reject, with server-side ownership, expiry and snapshot checks.

## Odoo user, database and JSON-2 key

Use an Odoo version supporting JSON-2 (the deployment example expects Odoo 19).
Create a dedicated application user with only the CRM/project models and record
rules needed for enabled typed tools. Create that user's API key and store it in
`odoo.key_file`. Calls use `Authorization: Bearer …` and `X-Odoo-Database`; no
username/password is passed by the client. The base URL is trusted installation
config, never taken from model text. Do not use a global administrator key.

Begin with a separate database and filestore. The PostgreSQL role serving test
Odoo must not have access to production databases or create roles/databases.
Configure `db_name`, `dbfilter`, `list_db=False`, worker/cron limits, addons and
data paths in the mounted Odoo config. Supply its database credentials separately.
Test access denial to production; matching names alone do not isolate databases.

The Compose example attaches Odoo to an existing PostgreSQL Docker network and
requires `ODOO_IMAGE` (approved tag/digest), `ODOO_DB_NETWORK`, `ODOO_TEST_FILESTORE`
and `ODOO_ADDONS_DIR`. It supplies no PostgreSQL server, database initialization
or safe universal Odoo configuration. TODO: prepare and test the Odoo database,
service user and image-specific entrypoint behavior for your installation.

## Squid egress and deployment

`engines/ops/compose.ops.yml` places the agent on internal networks with no published
ports. Squid is the outbound proxy, separate from the internal Odoo connection.
The shipped allowlist includes Anthropic, Slack domains and GitHub; OpenAI/DeepSeek
are commented out. Enable only configured providers. CONNECT is restricted to
443, unsafe destinations/ports are blocked and access logs omit URLs/query strings.
The proxy environment includes NO_PROXY for the internal Odoo service.

The Compose file uses `/opt/katip/ops/{config,state,work}` and individual read-only
secret mounts under `/opt/katip/secrets/ops/`. Prepare directories and permissions
for UID/GID 10001, secret read access and writable state/work before launch. Copy
neutral templates to config, supply Odoo's protected config/filestore/addons and
the external DB network. Choose reviewed image tags/digests for agent, Squid and Odoo.

When enabling another provider, also add its key file as a read-only individual
mount and its API hostname to the Squid allowlist. Restart Squid and recreate the
agent. Test intended HTTPS access, denied arbitrary hosts/ports, direct IPv4/IPv6
egress denial, host/metadata denial and Odoo network separation on the target host.
Internal Docker networks do not replace host firewall verification.

## Validation, scheduler and dry-run

From `engines/ops` with its environment and `PYTHONPATH=src`:

```sh
export KATIP_OPS_CONFIG=/absolute/path/to/config
export KATIP_CONFIG_DIR=/absolute/path/to/config
python -m katip_ops check-config
python -m katip_ops dry-run icerik --fake
python -m katip_ops dry-run pipeline
python -m katip_ops serve
```

Equivalent Compose commands after preparing host mounts and interpolation values:

```sh
docker compose -f compose.ops.yml config --quiet
docker compose -f compose.ops.yml build ops-agent
docker compose -f compose.ops.yml run --rm --no-deps ops-agent check-config
docker compose -f compose.ops.yml run --rm --no-deps ops-agent dry-run icerik --fake
docker compose -f compose.ops.yml up -d
```

`check-config` is offline: it validates documents, channels, required policy and
routes, not credentials, connectivity or live model availability. `--fake` replaces
every remote client. Without it, dry-run performs real read-only GitHub/Odoo calls
and paid provider calls, but prints Slack payloads instead of posting them and
starts no approval listeners. Both modes may write local drafts, ledger and pending
approvals. Use separate state/work paths and restrict terminal logs.

CLI aliases `content`, `growth`, `friday` correspond to `icerik`, `geo`, `cuma`.
`serve` runs Slack Socket Mode, a serialized job worker and scheduler in one process.
Manual queue contents are in memory and do not survive restart. Ledger and approvals
persist, but Slack card locations do not: reconcile old approvals and re-card the
same approval record rather than approving stale cards or duplicating proposals.

Review trial outputs and pending approvals before switching `dry_run=false`.
Approved content moves into `content/approved/`; publication is still manual.
Edits feed `content/voice-feedback.md`; rejection does not delete the draft.
Monitor JSON metadata logs and known/unknown ledger cost. Rotate provider/Slack/Odoo
keys independently, recreate services after file-mount replacement, and revoke old
keys only after verifying the new ones. Back up consistent SQLite state, config and
work files, with secrets separately. Rolling back images cannot undo remote writes.
