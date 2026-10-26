# Developer guide

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/katip` | Meet API, pipeline, providers, browser adapters, persistence and reports. |
| `web` | Turkish admin, account and live panel assets; bundled font licenses. |
| `engines/ops/src/katip_ops` | Independent operations engine. |
| `engines/ops/config` | Neutral settings/executor examples and runtime-document templates. |
| `docker*`, `engines/ops/compose.ops.yml` | Container and deployment definitions. |
| `tests`, `engines/ops/tests` | Offline tests, isolated fixtures and mocked external APIs. |
| `scripts` | Installation checks, migration, diagnostics and public export. |

## Local setup

Python 3.12 or newer is required. Use separate virtual environments for Meet and Ops.
The following are installation instructions for your development machine; dependencies
are pinned in lockfiles, and browser installation requires an explicit download.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m pip install -e '.[dev]'
python -m playwright install chromium
PYTHONPATH=src python -m uvicorn katip.api.main:app --host 127.0.0.1 --port 8000
```

On Windows, use `py -3.12 -m venv .venv`, then `.venv\Scripts\Activate.ps1`;
use `requirements.windows.lock` and `$env:PYTHONPATH = "$PWD/src"`. Chromium bot
execution normally needs Linux audio/display support; use Linux containers for
end-to-end meetings. Local HTTP development may require secure-cookie disabling
in a private test environment. Do not transfer that setting to production.

For Ops, create its own venv under `engines/ops`, install its requirements lock
and editable development extras from that directory, and use `PYTHONPATH=src`.
Runtime config is described in [Ops](ops-engine.md).

## Tests and lint

From the repository root:

```sh
PYTHONPATH=src python -m pytest -q -p no:cacheprovider
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
```

From `engines/ops`, with its own environment:

```sh
PYTHONPATH=src python -m pytest -q -p no:cacheprovider
python -m ruff check src tests
python -m ruff format --check src tests
```

Set `$env:PYTHONPATH` first for equivalent PowerShell commands. Tests mock external
providers; do not add real keys or network dependencies. Browser/subprocess tests
need OS permission to create processes; report sandbox failures separately from
assertion failures. Tests must use explicit fake organization settings rather than
depending on production domain defaults. Use neutral fixtures only.

## Conventions and extension points

Comments and docstrings in `src/katip` use Turkish; those in Ops use English.
User-facing Turkish must preserve ç, ğ, ı, İ, ö, ş and ü. Use UTF-8 and avoid mojibake.
All organization defaults, IDs, domains, policy, brand pronunciation and deployment
settings belong in configuration. Keep secrets out of logs and exceptions.

- **Meeting platform:** implement the adapter contract in `bot/platforms/base.py`,
  register URL routing, update host/network validation and add offline DOM/behavior
  tests. Validate lobby, audio, roster, chat and presentation behavior on a test tenant.
- **Meet provider:** implement `providers/interfaces.py`, wire registry/capabilities
  and configured chain selection, usage/health classification and transient-error
  handling. Test fallback, cancellation, shutdown and missing credentials.
- **Ops job:** add its deterministic `jobs` implementation, `runtime_files.SOPS`
  entry and SOP, runner/CLI/Slack allowlists, task/data class, read-tool grants,
  routing, scheduler if needed and meaningful policy/approval tests.
- **Ops executor:** implement the base executor contract and registry mapping;
  enforce availability and data-class exclusions on every fallback. Adapters return
  tool requests; job code executes only explicitly granted typed tools.

## Release and export

The public tree is exported from this repository; do all product edits here.
`.gitattributes` omits private deployment, decisions, research and audit material.
The export script uses `git archive HEAD`, so uncommitted changes cannot be exported.
After review and a separately authorized release commit, run from a clean checkout:

```sh
bash scripts/export-openkatip.sh ../openkatip
```

The target must be absent or empty and outside the source checkout. The script
refuses tracked/untracked changes, checks export-ignore exclusions and scans the
archive with the source-owned private denylist. It creates no repository or remote.
Inspect third-party notices, license, docs, local links, config coverage and both
suites before distributing an archive. A public export intentionally omits the
private denylist; downstream re-export requires an operator-supplied denylist file.
TODO: complete screenshot, security-contact and trademark-policy placeholders.
