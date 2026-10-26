# Katip Ops

Independent Python 3.12 operations engine: Slack Socket Mode, typed Odoo JSON-2
tools, Claude/OpenAI/DeepSeek executors, owner approvals, SQLite ledger and four
scheduled jobs. It has no imports from the meeting engine. Katip Full deploys both
engines separately; [EVENTS.md](EVENTS.md) defines future integration envelopes only.

Start with the detailed [public Ops guide](../../docs/ops-engine.md). Copy
`config/settings.example.toml` and `config/executors.example.yaml` into your own
configuration directory as `settings.toml`/`executors.yaml`; copy neutral runtime
documents from `config/examples/`. All organization policy is installation-owned.

Set `KATIP_OPS_CONFIG` for engine files and `KATIP_CONFIG_DIR` for runtime documents.
Relative credential, database and work paths resolve against the engine config
directory. Provider keys are individual files, not environment key values.

With the engine environment active and `PYTHONPATH=src`, from this directory:

```sh
python -m katip_ops check-config
python -m katip_ops dry-run content --fake
python -m katip_ops serve
python -m pytest -q -p no:cacheprovider
python -m ruff check src tests
```

Only `--fake` substitutes all remote clients. Real dry-run can make paid model
calls and read-only GitHub/Odoo requests; both modes write local state/drafts.
Use separate state/work directories for trials. Dry-run never starts approval
decision listeners. Review pending state before enabling live effects.

Policy checks, tool grants and approvals are separate layers. Model output and
external evidence never grant permission to write. The applier rechecks edits;
partial remote writes require reconciliation before retry. The ledger records
prompt digest/size and costs, not raw prompt text. Unknown prices remain unknown.
See [architecture](../../docs/architecture.md) and [security](../../docs/security.md).
