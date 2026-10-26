# Providers and data processing

These are implemented code paths, not a statement of current commercial model
availability or contractual residency. No live provider validation is bundled.
Confirm model IDs, keys, regional enablement, prices, retention and data-processing
terms for your account before real meetings.

| Engine / service | Implemented adapters | Credentials and region |
| --- | --- | --- |
| Meet LLM | Anthropic, Anthropic via Bedrock, Anthropic via Vertex | `ANTHROPIC_API_KEY`; AWS SDK IAM/profile and EU region; Google ADC/project with `eu` region. |
| Meet STT | Soniox (default), AssemblyAI, Deepgram | `SONIOX_API_KEY`, `ASSEMBLYAI_API_KEY`, `DEEPGRAM_API_KEY`; Soniox default `us`, optional `eu`; AssemblyAI `eu`. |
| Meet TTS | Soniox real-time | Uses Soniox key and endpoint region selection. |
| Ops LLM | Claude, OpenAI, DeepSeek | Individual credential files referenced in executors.yaml. DeepSeek requires explicit base URL and public/internal data only. |

Meet's OpenAI abstraction has no implemented adapter. Ops OpenAI is implemented;
the engines do not share provider registries. Ops xAI remains unavailable even if
enabled in configuration. Legacy local speaker-embedding code is not active
biometric recognition in MeetingSession.

## Meet selection and fallback

Default live and report chains are `["anthropic"]`, with the current source model
`claude-sonnet-5-5`. Report and live model settings are independent. The default
STT provider is Soniox; an empty `stt_chain` uses `stt_provider`. Other providers
are enabled only when explicitly added. Example:

```dotenv
KATIP_LLM_LIVE_CHAIN='["anthropic","bedrock","vertex"]'
KATIP_LLM_REPORT_CHAIN='["anthropic","bedrock","vertex"]'
KATIP_STT_CHAIN='["soniox","assemblyai"]'
KATIP_BEDROCK_REGION=eu-central-1
KATIP_VERTEX_REGION=eu
KATIP_SONIOX_REGION=eu
```

Bedrock and Vertex SDK extras are optional (`.[providers]`); configure server-side
SDK credentials. Bedrock uses the runtime client with model prefix or per-workload
full-ID overrides, not a different client selected by prefix. Bedrock has no batch
path and validates JSON locally; Vertex uses its implemented structured response
path. Cloud fallback omits unsupported thinking/effort parameters and converts
incompatible signed tool histories. TODO: verify account/model-specific support.

Retryable connectivity, timeout, rate-limit and service failures can advance a
chain; authentication, refusal and schema errors do not authorize arbitrary fallback.
Provider deadlines and per-attempt limits bound retries. LLM selections are sticky
per meeting/workload. STT switching replays a bounded in-memory PCM buffer with
timestamp remapping and overlap removal; real-language boundary quality needs
installation testing. AssemblyAI uses LLM translation of finalized text instead
of native translation. TTS interruption falls back to chat only when less than
100 ms of synthesized audio was sent; it does not duplicate a partly spoken reply.

End-of-meeting Anthropic reports can use batches. After the configured wait limit,
generation falls back to an immediate call. Interim report requests are immediate.

## Data processing and regions

Audio leaves the worker for the server and selected STT service; transcript and
document context can reach LLM services; response text goes to TTS when enabled.
Changing hosting location does not change all of those processing locations.
Soniox EU selection rewrites STT/TTS endpoint hosts and requires provider-side EU
enablement. Bedrock accepts EU-prefixed regions when selected and Vertex is
restricted to `eu`; these code checks are not legal guarantees about all processing.
Deepgram and direct Anthropic processing terms must be assessed separately.

Provider failover may change the processor for the same meeting. Approve every
configured chain member in your data inventory and contracts. Check any cross-border
transfer basis, subprocessors, training/retention settings and participant notice
with your organization's responsible advisors. An endpoint name is not proof of
KVKK/GDPR compliance. Configure truthful notice wording and a rights contact.

Keys remain on the control plane; browser workers do not receive provider credentials.
Use restricted provider accounts, budgets and separate keys per installation.
Meet usage estimates support provider-specific overrides; missing rates are not
evidence of free service. Ops prices require all five configured rate components
for a known total. [Configuration](configuration.md) and [Ops](ops-engine.md).
