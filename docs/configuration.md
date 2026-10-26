# Configuration reference

This inventory is derived from `src/katip/config.py::Settings`. Types, literal choices
and defaults below are the source defaults. Every field is optional syntactically;
credentials and installation values are required for the features you enable.

Settings load `.env` in the working directory, with process environment taking
precedence. Names are case-insensitive. Fields normally use `KATIP_`; API-key
validation aliases use the exact names shown below without that prefix. Unknown
settings are ignored. Lists and maps use JSON, not comma-separated strings, except
`KATIP_MAIL_INTERNAL_DOMAINS`. Use single-quoted dotenv values for JSON and scrypt
hashes to avoid shell/Compose interpolation. Null defaults should normally be
represented by omitting the variable, rather than a literal string `null`.

Editable non-secret panel settings persist in `app_settings` and override the
environment at startup. Changing `.env` alone does not replace an existing panel
override. New meetings use new defaults; active sessions may retain their state.

Panel email domains and trusted calendar organizer domains default to `[]`.
Configure them explicitly per installation; domain trust is exact, ASCII-only,
with no wildcard or implicit subdomain match. An empty panel list permits no
domain. Calendar `hepsi` still accepts only structurally eligible invitations;
it does not bypass URL checks or reapproval of a changed approved invitation.
The legacy autojoin switch is used only if the explicit join policy is absent.


## LLM, speech providers and failover

| Environment variable | Type | Default | Description |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | `str` | `""` | Anthropic credential; used only on the server. |
| `KATIP_LLM_MODEL` | `str` | `"claude-sonnet-5-5"` | Report and general LLM model identifier; confirm account availability. |
| `KATIP_AGENT_MODEL` | `str` | `"claude-sonnet-5-5"` | Live agent model identifier. |
| `KATIP_AGENT_EFFORT` | `Literal['low', 'medium', 'high']` | `"low"` | Live Anthropic reasoning effort. |
| `KATIP_AGENT_THINKING` | `bool` | `true` | Enable adaptive thinking on supported live calls. |
| `KATIP_REPORT_THINKING` | `bool` | `true` | Enable thinking on supported report calls. |
| `KATIP_REPORT_BATCH` | `bool` | `true` | Use Anthropic batches for end-of-meeting reports; interim reports remain immediate. |
| `KATIP_REPORT_BATCH_MAX_WAIT_S` | `int` | `1200` | Batch wait limit in seconds before immediate generation fallback. |
| `KATIP_LLM_LIVE_CHAIN` | `list[Literal['anthropic', 'bedrock', 'vertex']]` | `["anthropic"]` | Ordered nonempty live LLM provider chain; duplicates rejected. |
| `KATIP_LLM_REPORT_CHAIN` | `list[Literal['anthropic', 'bedrock', 'vertex']]` | `["anthropic"]` | Ordered nonempty report LLM provider chain; duplicates rejected. |
| `KATIP_STT_CHAIN` | `list[Literal['soniox', 'assemblyai', 'deepgram']]` | `[]` | Ordered speech provider chain; empty uses stt_provider. |
| `KATIP_PROVIDER_DEADLINE_S` | `float` | `60.0` | Total live provider time budget in seconds; positive. |
| `KATIP_PROVIDER_ATTEMPT_TIMEOUT_S` | `float` | `20.0` | Initial provider attempt limit in seconds; positive. |
| `KATIP_REPORT_DEADLINE_S` | `float` | `600.0` | Total report call time budget in seconds; positive. |
| `KATIP_REPORT_ATTEMPT_TIMEOUT_S` | `float` | `120.0` | Initial report attempt limit in seconds; positive. |
| `KATIP_BEDROCK_REGION` | `str` | `"eu-central-1"` | AWS runtime region; must start with eu- when Bedrock is selected. |
| `KATIP_BEDROCK_AWS_PROFILE` | `str` | `""` | Optional AWS profile name; otherwise use the SDK credential chain. |
| `KATIP_BEDROCK_INFERENCE_PROFILE` | `Literal['eu']` | `"eu"` | Legacy EU inference-profile selection retained for compatibility. |
| `KATIP_BEDROCK_MODEL_PREFIX` | `str` | `"eu.anthropic."` | Prefix added to the configured model for Bedrock runtime. |
| `KATIP_BEDROCK_MODEL_IDS` | `dict[str, str]` | `{}` | Per-workload complete model IDs overriding the prefix: live, translation, report. |
| `KATIP_VERTEX_PROJECT` | `str` | `""` | Google Cloud project for the Vertex adapter; credentials use ADC. |
| `KATIP_VERTEX_REGION` | `Literal['eu']` | `"eu"` | Vertex multi-region; only eu is accepted. |
| `KATIP_PROVIDER_LLM_PRICES` | `dict[str, dict[str, float]]` | `{}` | Provider/model/region pricing overrides in USD per million tokens. |
| `KATIP_PROVIDER_STT_PRICES` | `dict[str, float]` | `{}` | USD per audio hour, keyed by provider/model/region. |
| `ASSEMBLYAI_API_KEY` | `str` | `""` | AssemblyAI credential for the optional speech adapter. |
| `KATIP_ASSEMBLYAI_REGION` | `Literal['eu']` | `"eu"` | AssemblyAI region; only eu is accepted. |

## Agent turns and spoken-response floor

| Environment variable | Type | Default | Description |
| --- | --- | --- | --- |
| `KATIP_AGENT_MODE` | `Literal['llm', 'rules']` | `"llm"` | llm uses tool calls; rules uses deterministic commands. Missing LLM key also limits agent behavior. |
| `KATIP_AGENT_TURN_WAIT_S` | `float` | `1.5` | Quiet wait after an addressed utterance in seconds. |
| `KATIP_AGENT_INCOMPLETE_WAIT_S` | `float` | `6.0` | Longer wait for an apparently unfinished addressed utterance. |
| `KATIP_AGENT_TURN_MAX_S` | `float` | `120.0` | Maximum duration of an addressed turn before handling. |
| `KATIP_VOICE_BUSY_WINDOW_S` | `float` | `1.2` | Recent-speech window used by spoken-response floor control. |
| `KATIP_VOICE_FLOOR_WAIT_S` | `float` | `2.0` | Wait for an opportunity to speak. |
| `KATIP_VOICE_AMBIENT_WINDOW_S` | `float` | `1.5` | Ambient speech window used by floor control. |
| `KATIP_VOICE_INTERRUPT_SUSTAIN_S` | `float` | `1.2` | Sustained interruption duration before speech is interrupted. |
| `KATIP_VOICE_RESUME_QUIET_S` | `float` | `1.2` | Quiet interval before resuming spoken output. |

## Content, reports and legacy voice identity

| Environment variable | Type | Default | Description |
| --- | --- | --- | --- |
| `KATIP_COMPANY_NAME` | `str or None` | `null` | Optional installation company name for reports and notices; privacy fallback is Şirket. |
| `KATIP_DATA_DIR` | `str` | `"data"` | Persistent meeting files directory, including reports and requested screenshots. |
| `KATIP_DATA_KEY` | `str` | `""` | Base64-encoded 32-byte content master key; keep outside source control. |
| `KATIP_DATA_KEY_FILE` | `str` | `""` | Content-key file path; takes precedence over data_key. |
| `KATIP_DATA_KEY_PREVIOUS` | `str` | `""` | Previous base64 master key during rewrapping; never remove until rotation is verified. |
| `KATIP_REQUIRE_ENCRYPTION` | `bool` | `false` | Fail closed when a valid meeting-content key is unavailable. |
| `KATIP_REPORT_LANG` | `str` | `"Türkçe"` | Natural-language report language. |
| `KATIP_REPORT_INCLUDE_TRANSCRIPT` | `bool` | `true` | Include transcript in the Word report attachment. |
| `KATIP_REPORT_TRANSCRIPT_PDF` | `bool` | `false` | Also include transcript in the PDF report. |
| `KATIP_TIMEZONE` | `str` | `"Europe/Istanbul"` | Time zone used to display report times; server timestamps use UTC. |
| `KATIP_AUTO_REPORT` | `bool` | `true` | Generate an end-of-meeting report automatically. |
| `KATIP_VOICE_ENABLED` | `bool` | `false` | Legacy voice-identity switch; MeetingSession does not enable biometrics. |
| `KATIP_VOICE_MODEL` | `str` | `"wespeaker-resnet34-lm"` | Legacy speaker-embedding model identifier. |
| `KATIP_VOICE_MATCH_THRESHOLD` | `float` | `0.55` | Legacy minimum embedding similarity for a match. |
| `KATIP_VOICE_MATCH_MARGIN` | `float` | `0.08` | Legacy separation required between candidate matches. |
| `KATIP_VOICE_MIN_ENROLL_S` | `float` | `8.0` | Legacy minimum voice enrollment audio in seconds. |
| `KATIP_GREETING` | `str` | `"chat"` | Legacy recognized-person greeting mode: chat or off. |
| `KATIP_VOICE_MODEL_PATH` | `str or None` | `null` | Optional local legacy embedding model path; otherwise model cache is under data_dir. |

## Reply and speech synthesis

| Environment variable | Type | Default | Description |
| --- | --- | --- | --- |
| `KATIP_REPLY_MODE` | `Literal['chat', 'voice']` | `"chat"` | Default chat or voice replies; recording mode limits voice/translation. |
| `KATIP_TTS_MODEL` | `str` | `"tts-rt-v2"` | Soniox real-time speech synthesis model identifier. |
| `KATIP_TTS_VOICE` | `str` | `"Rania"` | Soniox voice identifier; availability depends on provider account. |
| `KATIP_TTS_WS_URL` | `str` | `"wss://tts-rt.soniox.com/tts-websocket"` | Soniox speech synthesis WebSocket URL; EU mode rewrites the host. |
| `KATIP_TTS_SAMPLE_RATE` | `int` | `48000` | Synthesized audio sample rate in Hz. |
| `KATIP_TTS_NORMALIZER` | `Literal['auto', 'ntr', 'katip']` | `"auto"` | Turkish normalization backend: auto, ntr or katip; failures fall back locally. |
| `KATIP_TTS_LEXICON` | `dict[str, str]` | `{}` | Installation pronunciation map applied before TTS; empty by default. |

## Administration, Microsoft and calendar

| Environment variable | Type | Default | Description |
| --- | --- | --- | --- |
| `KATIP_ADMIN_TOKEN` | `str` | `""` | Bearer credential for management API; empty disables bearer-authorized management. |
| `KATIP_ADMIN_PASSWORD_HASH` | `str` | `""` | Bootstrap panel scrypt hash; database override takes precedence. |
| `KATIP_PANEL_EMAIL_DOMAINS` | `list[str]` | `[]` | Exact allowed panel account domains; empty by default, configure explicitly. |
| `KATIP_ADMIN_LOGIN_EMAIL` | `str` | `""` | Explicit bootstrap login email within an allowed domain. |
| `KATIP_M365_TENANT_ID` | `str` | `""` | Microsoft Entra tenant identifier for Graph integration. |
| `KATIP_M365_CLIENT_ID` | `str` | `""` | Microsoft application/client identifier for Graph integration. |
| `KATIP_M365_CLIENT_SECRET` | `str` | `""` | Microsoft Graph application secret. |
| `KATIP_M365_MAILBOX` | `str` | `""` | Installation calendar/mail mailbox address; empty by default. |
| `KATIP_MAIL_ENABLED` | `bool` | `false` | Enable approved meeting-note email sending. |
| `KATIP_MAIL_INTERNAL_DOMAINS` | `str` | `""` | Comma-separated exact domains used by internal-mail checks; empty uses configured identity rules. |
| `KATIP_TEAMS_BOT_APP_ID` | `str` | `""` | Microsoft Bot application UUID for Teams messaging. |
| `KATIP_TEAMS_BOT_SECRET` | `str` | `""` | Teams bot client secret; keep out of repository and logs. |
| `KATIP_TEAMS_BOT_SECRET_FILE` | `str` | `""` | Teams bot secret file path. |
| `KATIP_TEAMS_BOT_TENANT_ID` | `str` | `""` | Tenant accepted by Teams bot authentication. |
| `KATIP_TEAMS_NOTIFY` | `bool` | `true` | Enable Teams notifications where the integration is configured. |
| `KATIP_TEAMS_APP_PACKAGE_ID` | `str` | `""` | Explicit Teams package UUID; otherwise derived from bot app ID using a neutral namespace. |
| `KATIP_TEAMS_PRIVACY_URL` | `str` | `""` | HTTPS privacy policy URL in generated Teams manifest; falls back to public base /gizlilik. |
| `KATIP_TEAMS_TERMS_URL` | `str` | `""` | HTTPS terms URL in generated Teams manifest; falls back to public base /gizlilik. |
| `KATIP_CALENDAR_AUTOJOIN` | `bool` | `false` | Legacy switch: selects güvenilir only when calendar_join_policy is not explicitly set. |
| `KATIP_CALENDAR_JOIN_POLICY` | `Literal['kapalı', 'onay', 'güvenilir', 'hepsi']` | `"kapalı"` | kapalı disables, onay requires review, güvenilir permits trusted organizers, hepsi permits eligible invitations. |
| `KATIP_CALENDAR_DAILY_AUTOJOINS` | `int` | `20` | Maximum automatic calendar joins per day, 0–1000. |
| `KATIP_CALENDAR_MAX_PENDING` | `int` | `200` | Persistent calendar queue cap, 1–1000; default 200. |
| `KATIP_CALENDAR_POLL_S` | `int` | `60` | Calendar polling interval, 30–3600 seconds. |
| `KATIP_CALENDAR_ORGANIZER_DOMAINS` | `list[str]` | `[]` | Exact trusted ASCII domains, no wildcard or subdomain inheritance; empty by default, at most 100. |
| `KATIP_CALENDAR_ORGANIZER_EMAILS` | `list[str]` | `[]` | Normalized trusted full email addresses; empty by default, at most 200. |
| `KATIP_ADMIN_IDLE_MINUTES` | `int` | `60` | Panel idle session lifetime in minutes. |
| `KATIP_ADMIN_SESSION_HOURS` | `int` | `12` | Maximum panel session age in hours. |
| `KATIP_ADMIN_COOKIE_SECURE` | `bool` | `true` | Send panel cookies only over HTTPS; keep true outside isolated local development. |
| `KATIP_CLIENT_IP_HEADER` | `Literal['x-forwarded-for', 'cf-connecting-ip']` | `"x-forwarded-for"` | Client-IP strategy: last x-forwarded-for value, or trusted-proxy cf-connecting-ip. |
| `KATIP_CLIENT_IP_TRUSTED_PROXIES` | `list[str]` | `["127.0.0.1","::1"]` | Exact proxy IPs trusted in Cloudflare mode; configure actual TCP peers. |
| `KATIP_SECRET_KEY` | `str` | `""` | Signing secret for links; if empty derives from admin_token. Set explicitly and back it up securely. |
| `KATIP_SHARE_LINK_DAYS` | `float` | `7.0` | Signed panel/report link validity in days. |
| `KATIP_PUBLIC_BASE_URL` | `str` | `""` | Public HTTPS origin used for generated links; set explicitly on-premises. |
| `KATIP_VOICEPRINT_KEY` | `str` | `""` | Legacy base64 32-byte AES key for voiceprints, separate from meeting-content keys. |

## Privacy, retention and usage limits

| Environment variable | Type | Default | Description |
| --- | --- | --- | --- |
| `KATIP_PRIVACY_NOTICE_ON_JOIN` | `Literal['chat', 'voice', 'off']` | `"chat"` | Post notice in chat, read it in voice too, or off; default chat. |
| `KATIP_PRIVACY_CONTACT` | `str` | `""` | Installation privacy/rights contact; empty falls back to the organizer. |
| `KATIP_PRIVACY_SERVER_LOCATION` | `str` | `"Katip şu an için yurt dışındaki sunucularda çalışmaktadır"` | Truthful Turkish server-location sentence; cannot be empty or contain control characters. |
| `KATIP_RETENTION_TEXT` | `str` | `""` | Human-readable retention wording; empty derives from retention_days. |
| `KATIP_RETENTION_DAYS` | `int` | `365` | Meeting-data retention days; 0 disables automatic deletion. |
| `KATIP_DEBUG_RETENTION_DAYS` | `int` | `7` | Separate diagnostic artifact retention in days. |
| `KATIP_POST_REPORT_TO_CHAT` | `bool` | `true` | Post a signed report link in the meeting chat when ready. |
| `KATIP_AUTO_DIAGNOSE` | `bool` | `false` | Save meeting HTML/screenshots routinely; join failures can still create diagnostics when false. |
| `KATIP_MAX_MEETING_MINUTES` | `int` | `90` | Maximum bot duration in tam meeting mode. |
| `KATIP_DEFAULT_MODE` | `Literal['tam', 'kayit']` | `"tam"` | tam for interactive agent, voice and translation; kayit for quiet transcript/report mode. |
| `KATIP_RECORD_MAX_MINUTES` | `int` | `600` | Maximum bot duration in kayit mode. |
| `KATIP_RECORD_IDLE_LEAVE_MINUTES` | `int` | `60` | Silence timeout for leaving in kayit mode. |
| `KATIP_ALONE_LEAVE_MINUTES` | `int` | `5` | Leave when no other visible participant and no speech for this duration. |
| `KATIP_STT_ROTATE_MINUTES` | `int` | `280` | Rotate a Soniox streaming connection before its duration limit. |
| `KATIP_MAX_CONCURRENT_BOTS` | `int` | `1` | Application meeting concurrency cap; size hardware separately. |
| `KATIP_DAILY_MEETING_MINUTES` | `int` | `660` | Daily cumulative bot-minute budget. |
| `KATIP_MAX_AGENT_CALLS_PER_MEETING` | `int` | `60` | Live LLM request cap per meeting. |
| `KATIP_PRESENTATION_NARRATIONS_PER_MEETING` | `int` | `80` | Presentation narration request cap per meeting; nonnegative. |
| `KATIP_IDLE_LEAVE_MINUTES` | `int` | `15` | Interactive-mode silence timeout; 0 disables. |
| `KATIP_VOICE_MIXED_MAX_SIMILARITY` | `float` | `0.5` | Legacy threshold for separating mixed enrollment voice clusters. |
| `KATIP_VOICE_SEGMENT_MIN_S` | `float` | `2.0` | Legacy minimum segment duration for voice matching. |
| `KATIP_VOICE_MIXED_MIN_SHARE` | `float` | `0.25` | Legacy minimum share of the smaller voice cluster. |
| `KATIP_INGEST_LOCAL_ONLY` | `bool` | `false` | Accept ingest only from local clients in the legacy co-located topology. |

## Speech recognition and language

| Environment variable | Type | Default | Description |
| --- | --- | --- | --- |
| `KATIP_STT_PROVIDER` | `Literal['soniox', 'assemblyai', 'deepgram']` | `"soniox"` | Speech provider used when stt_chain is empty. |
| `SONIOX_API_KEY` | `str` | `""` | Soniox STT/TTS credential. |
| `KATIP_SONIOX_MODEL` | `str` | `"stt-rt-v5"` | Soniox streaming transcription model identifier. |
| `KATIP_SONIOX_WS_URL` | `str` | `"wss://stt-rt.soniox.com/transcribe-websocket"` | Soniox transcription WebSocket URL; rewritten for EU selection. |
| `KATIP_SONIOX_REGION` | `Literal['us', 'eu']` | `"us"` | us or eu endpoints; EU requires provider-side account enablement. |
| `KATIP_SONIOX_MAX_ENDPOINT_DELAY_MS` | `int` | `1000` | Speech endpointing delay in milliseconds; provider supports 500–3000. |
| `KATIP_STT_TERMS` | `list[str]` | `[]` | Installation vocabulary hints, e.g. product terms; JSON array. |
| `DEEPGRAM_API_KEY` | `str` | `""` | Deepgram credential, used only when explicitly selected. |
| `KATIP_SOURCE_LANG` | `str` | `"tr"` | Primary speech language code. |
| `KATIP_TARGET_LANG` | `str` | `"en"` | Translation target language code; empty disables translation. |
| `KATIP_SAMPLE_RATE` | `int` | `16000` | Capture PCM sample rate in Hz; keep pipeline format consistent. |
| `KATIP_FRAME_MS` | `int` | `20` | Capture frame size in milliseconds; frame_samples is derived, not an environment variable. |

## Infrastructure, workers, parser and bot

| Environment variable | Type | Default | Description |
| --- | --- | --- | --- |
| `KATIP_DATABASE_URL` | `str` | `"sqlite+aiosqlite:///data/katip.db"` | SQLAlchemy async database URL: SQLite or PostgreSQL with asyncpg. |
| `KATIP_API_HOST` | `str` | `"0.0.0.0"` | API bind address for launchers that read this setting; Compose uses its explicit uvicorn command. |
| `KATIP_API_PORT` | `int` | `8000` | API port for launchers; Compose explicitly binds 8000. |
| `KATIP_INGEST_WS_URL` | `str` | `"ws://localhost:8000/ws/ingest"` | Browser worker ingest WebSocket endpoint. |
| `KATIP_BOT_BACKEND` | `Literal['local', 'docker', 'remote']` | `"local"` | local subprocess, docker manager in process, or remote manager lease queue. |
| `KATIP_BOT_IMAGE` | `str` | `"katip-worker:local"` | Browser worker image tag; build it before launching meetings. |
| `KATIP_BOT_IMAGE_DIGEST` | `str` | `""` | Optional required worker image digest; pin after validating an image. |
| `KATIP_BOT_NETWORK` | `str` | `"katip-workers"` | Docker network joined by dynamic browser workers. |
| `KATIP_BOT_WORKER_INGEST_URL` | `str` | `"ws://api:8000/ws/ingest"` | API ingest URL reachable from dynamic workers. |
| `KATIP_BOT_MEMORY_MB` | `int` | `1024` | Per-worker memory limit, 256–16384 MiB. |
| `KATIP_BOT_CPUS` | `float` | `1.0` | Per-worker CPU quota, 0.25–16. |
| `KATIP_BOT_BLOCK_PRIVATE_NETWORK` | `bool` | `true` | Block browser requests to private networks; complements host firewall rules. |
| `KATIP_WORKER_MANAGER_KEYS` | `dict[str, str]` | `{}` | Manager-ID to secret mapping on API; independent secret per manager. |
| `KATIP_WORKER_LEASE_S` | `int` | `60` | Worker lease duration, 30–300 seconds. |
| `KATIP_WORKER_QUEUE_TIMEOUT_S` | `int` | `300` | Maximum queued-job wait, 30–3600 seconds. |
| `KATIP_BOT_DISCONNECT_TIMEOUT_S` | `int` | `60` | Worker disconnect grace period, 15–300 seconds. |
| `KATIP_WORKER_API_URL` | `str` | `""` | Control API URL used by a remote manager; HTTPS required except configured internal hostname. |
| `KATIP_WORKER_API_INTERNAL_HOSTNAME` | `str` | `""` | Single explicitly permitted internal Compose HTTP hostname. |
| `KATIP_WORKER_MANAGER_ID` | `str` | `""` | Remote manager identity, matching API manager mapping. |
| `KATIP_WORKER_MANAGER_KEY` | `str` | `""` | Remote manager credential; use a random value of at least 32 characters. |
| `KATIP_DOCUMENT_MAX_BYTES` | `int` | `8388608` | Upload limit in bytes, 1024–16777216. |
| `KATIP_DOCUMENT_MAX_PER_MEETING` | `int` | `8` | Maximum preparation documents per meeting, 1–20. |
| `KATIP_DOCUMENT_CONTEXT_CHARS` | `int` | `24000` | Maximum document context characters passed to live answers, 2000–60000. |
| `KATIP_DOCUMENT_PARSER_TIMEOUT_S` | `int` | `20` | Document parsing timeout, 1–60 seconds. |
| `KATIP_PARSER_URL` | `str` | `""` | Isolated parser service URL; configure for uploaded untrusted documents. |
| `KATIP_PARSER_KEY` | `str` | `""` | Parser authentication credential. |
| `KATIP_PARSER_KEY_FILE` | `str` | `""` | Parser authentication file path, preferred for Compose. |
| `KATIP_BOT_DISPLAY_NAME` | `str` | `"Katip"` | Installation-configurable browser and full Teams display name; default Katip. |
| `KATIP_BOT_SANDBOX` | `Literal['auto', 'on', 'off']` | `"on"` | on fails closed; auto may fall back without Chromium sandbox; off disables it. |
| `KATIP_BOT_SITE_ISOLATION` | `bool` | `true` | Enable Chromium site isolation; keep true in production. |
| `KATIP_BOT_HEADLESS` | `bool` | `false` | Run browser headless; worker image normally supplies a virtual display. |
| `KATIP_BOT_JOIN_TIMEOUT_S` | `int` | `900` | Maximum wait to join/admit bot from meeting lobby in seconds. |
| `MONDAY_API_TOKEN` | `str` | `""` | Reserved Monday integration credential; no complete public Monday workflow is shipped. |

## Secrets and installation files

Keep `.env`, master keys, Graph/Teams credentials and Ops secret files outside
version control. Limit filesystem access to service users and backup operators;
avoid dumping resolved Compose configuration into logs. `_FILE` settings read
individual files for data keys, Teams and parser secrets. Do not mount `.env` or
provider keys into browser workers. The API explicitly ignores `.env` in bot-token mode.
AWS credentials and Google ADC belong only on the server that runs those adapters.

`KATIP_BOT_TOKEN` is a short-lived worker capability generated by the manager,
not a persistent Settings field. `RAILWAY_PUBLIC_DOMAIN` remains a legacy code
fallback for absolute URLs; on-premises installations should use
`KATIP_PUBLIC_BASE_URL` explicitly. Ops has separate file-based settings and
`KATIP_OPS_CONFIG` / `KATIP_CONFIG_DIR`; see [Ops](ops-engine.md).

For the isolated Compose topology, also set the interpolation inputs below.
They are not additional fields in the Python Settings class.

| Compose input | Purpose |
| --- | --- |
| `POSTGRES_PASSWORD` | Required unique PostgreSQL password. |
| `KATIP_DOCKER_GID` | Required local socket group for manager access. |
| `KATIP_WORKER_NETWORK` | Docker network name, default `katip-workers`. |
| `KATIP_DATA_KEY_FILE`, `KATIP_PARSER_KEY_FILE` | Host secret paths; services use fixed container paths. |
| `KATIP_WORKER_MANAGER_KEY` | Shared with API's `single-host` manager mapping by Compose. |

Production overrides in `docker-compose.isolated.yml` set remote workers,
required encryption, parser service addresses and safety switches. Treat the
resolved container environment as the runtime configuration, not the example
file alone. [Deployment](deployment.md) explains key rotation and storage.
