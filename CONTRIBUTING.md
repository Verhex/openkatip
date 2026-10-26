# Contributing

Read the [developer guide](docs/developer-guide.md) and discuss substantial changes
in an issue before building them. Keep pull requests focused on a concrete problem,
describe resulting behavior and include relevant offline test/lint results.

Use Python 3.12+, preserve Turkish UI characters, Turkish comments in Meet and
English comments in Ops. Put organization settings in config, never fixtures with
real names, domains, IDs, transcripts or customer data. Never commit credentials,
private deployment details or generated runtime state. Use separate fake providers
and avoid live network calls in tests.

Run both suites for changes spanning the engines, and ruff for affected Python.
Security or effectful changes need meaningful permission/policy tests. Keep public
docs and `.env.example` aligned with configuration changes. Changes are contributed
under the repository's MIT license; retain third-party notices.

Follow the [code of conduct](CODE_OF_CONDUCT.md). Report vulnerabilities privately
using [SECURITY.md](SECURITY.md), not in a public issue.
