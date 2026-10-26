"""Sağlayıcı geçişleri: yalnız sahte istemciler, ağ ve ücretli çağrı yok."""

import asyncio
import json
import socket
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import anthropic
import httpx2
import pytest
from pydantic import ValidationError
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from katip import usage
from katip.config import Settings, get_settings
from katip.pipeline.session import MeetingSession, SessionRegistry
from katip.pipeline.stt.assemblyai import AssemblyAIError, AssemblyAISTT
from katip.pipeline.stt.base import StreamExpired, STTProvider, Utterance
from katip.providers import Capabilities, ProviderRegistry, get_registry
from katip.providers.audio import ReplayBuffer, deduplicate
from katip.providers.llm import ClaudeProvider
from katip.providers.resilience import CircuitBreaker, transient
from katip.reports.summarize import MeetingReport, summarize_meeting
from katip.store import repo

MODEL = "claude-haiku-5-5"
PARAMS = {"model": MODEL, "max_tokens": 20, "messages": [{"role": "user", "content": "Soru"}]}
REPORT = dict(
    title="Plan",
    summary="Özet",
    key_points=[],
    decisions=[],
    action_items=[],
    open_questions=[],
    participants=[],
)


def response(text="Cevap", reason="end_turn"):
    return NS(
        content=[NS(type="text", text=text)],
        stop_reason=reason,
        usage=NS(input_tokens=10, output_tokens=3),
    )


def status_error(status):
    r = httpx2.Response(status, request=httpx2.Request("POST", "https://sahte.invalid"))
    return anthropic.APIStatusError("Sahte hata", response=r, body={})


def fake_registry():
    s = get_settings()
    s.llm_live_chain = ["anthropic", "bedrock", "vertex"]
    s.llm_report_chain = ["anthropic", "bedrock", "vertex"]
    registry = get_registry()
    for name, region in (("anthropic", "api"), ("bedrock", "eu-central-1"), ("vertex", "eu")):
        # Ret yedeği beta ucundan gider; aynı sahte çağrı iki uçta da kullanılır.
        create = AsyncMock(return_value=response())
        client = NS(messages=NS(create=create), beta=NS(messages=NS(create=create)))
        registry.clients[name] = ClaudeProvider(name, region, client, Capabilities())
    return registry


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionError(),
        socket.gaierror(),
        TimeoutError(),
        status_error(429),
        status_error(500),
        status_error(529),
    ],
)
async def test_outages_switch_and_stick_per_meeting_and_workload(failure):
    r = fake_registry()
    first = r.clients["anthropic"].client.messages.create
    first.side_effect = failure
    with usage.meeting("a"):
        await r.llm().create(**PARAMS)
        first.side_effect = None
        await r.llm().create(**PARAMS)
        assert first.await_count == 1
        await r.llm("report").create(**PARAMS)
        assert first.await_count == 2
    with usage.meeting("b"):
        await r.llm().create(**PARAMS)
    assert first.await_count == 3
    assert r.clients["bedrock"].client.messages.create.await_count == 2
    assert (
        r.clients["bedrock"].client.messages.create.call_args.kwargs["model"]
        == f"eu.anthropic.{MODEL}"
    )


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_request_and_auth_errors_do_not_switch(status):
    r = fake_registry()
    r.clients["anthropic"].client.messages.create.side_effect = status_error(status)
    with usage.meeting("a"), pytest.raises(anthropic.APIStatusError):
        await r.llm().create(**PARAMS)
    r.clients["bedrock"].client.messages.create.assert_not_awaited()
    assert r.sticky[("a", "live")] == 0


async def test_refusal_does_not_switch():
    r = fake_registry()
    r.clients["anthropic"].client.messages.create.return_value = response(reason="refusal")
    with usage.meeting("a"):
        assert (await r.llm().create(**PARAMS)).stop_reason == "refusal"
    r.clients["bedrock"].client.messages.create.assert_not_awaited()


def test_breaker_window_and_single_half_open_probe():
    now = [0.0]
    b = CircuitBreaker(lambda: now[0])
    for n in (0, 10, 20):
        now[0] = n
        assert b.acquire()
        b.failure()
        if n < 20:
            b.success()
    assert not b.acquire()
    now[0] = 79.9
    assert not b.acquire()
    now[0] = 80
    assert b.acquire() and not b.acquire()
    b.failure()
    now[0] = 140
    assert b.acquire()
    b.success()
    assert b.acquire() and b.acquire()
    now[0] = 200
    b.failure()
    now[0] = 231
    b.failure()
    assert len(b.errors) == 1


async def test_open_circuit_skips_primary_for_new_meeting():
    r = fake_registry()
    b = r.breaker("anthropic", "live")
    for _ in range(3):
        b.failure()
    with usage.meeting("c"):
        await r.llm().create(**PARAMS)
    r.clients["anthropic"].client.messages.create.assert_not_awaited()


async def test_one_overall_deadline_with_attempt_limit():
    r = fake_registry()
    # Pay, Windows'ta zamanlayıcının erken tetiklenmesini (~15 ms) kapsar.
    r.settings.provider_deadline_s = 0.3
    r.settings.provider_attempt_timeout_s = 0.2

    async def slow(**kw):
        await asyncio.sleep(1)

    for p in r.clients.values():
        p.client.messages.create.side_effect = slow
    with pytest.raises(TimeoutError):
        await r.llm().create(**PARAMS)
    assert r.clients["anthropic"].client.messages.create.await_count == 1
    assert r.clients["bedrock"].client.messages.create.await_count == 1
    r.clients["vertex"].client.messages.create.assert_not_awaited()


async def test_report_without_batches_or_structured_output_repairs_once(database):
    r = fake_registry()
    r.settings.llm_report_chain = ["bedrock", "vertex"]
    create = r.clients["bedrock"].client.messages.create
    create.side_effect = [response("{}"), response(json.dumps(REPORT))]
    with usage.meeting("rapor"):
        result = await summarize_meeting([], batch=True)
    assert result == MeetingReport(**REPORT)
    assert create.await_count == 2
    assert "Şemaya" in create.call_args.kwargs["messages"][-1]["content"]
    row = await repo.get_meeting_usage("rapor")
    assert row.providers["bedrock/claude-sonnet-5-5/eu-central-1"]["llm_calls"] == 2
    r.clients["vertex"].client.messages.create.assert_not_awaited()


async def test_invalid_json_after_one_repair_does_not_switch():
    r = fake_registry()
    r.settings.llm_report_chain = ["bedrock", "vertex"]
    r.clients["bedrock"].client.messages.create.return_value = response("{}")
    with pytest.raises(ValidationError):
        await summarize_meeting([])
    assert r.clients["bedrock"].client.messages.create.await_count == 2
    r.clients["vertex"].client.messages.create.assert_not_awaited()


async def test_structured_report_outage_degrades_to_json():
    r = fake_registry()
    p = r.clients["anthropic"]
    p.capabilities = Capabilities(batches=True, structured_output=True)
    p.client.beta = NS(messages=NS(parse=AsyncMock(side_effect=status_error(529))))
    r.clients["bedrock"].client.messages.create.return_value = response(json.dumps(REPORT))
    with usage.meeting("a"):
        assert await summarize_meeting([]) == MeetingReport(**REPORT)
        assert r.sticky[("a", "report")] == 1


async def test_usage_records_provider_model_region_and_list_prices(database):
    r = fake_registry()
    with usage.meeting("u"):
        for p in r.clients.values():
            answer = await p.create(**PARAMS)
            await usage.record_llm("soru", MODEL, answer.usage)
    row = await repo.get_meeting_usage("u")
    assert set(row.providers) == {f"{p.name}/{MODEL}/{p.region}" for p in r.clients.values()}
    assert len({totals["llm_usd"] for totals in row.providers.values()}) == 1
    r.settings.provider_llm_prices[f"bedrock/{MODEL}/eu-central-1"] = dict(
        input=2, output=10, cache_write=2.5, cache_read=0.2
    )
    assert usage.llm_cost(
        MODEL, response().usage, provider="bedrock", region="eu-central-1"
    ) > usage.llm_cost(MODEL, response().usage)


def test_default_config_and_health_are_offline():
    s = Settings(_env_file=None)
    r = ProviderRegistry(s)
    assert r.chain("live") == r.chain("report") == ["anthropic"]
    assert r.stt_chain == ["soniox"]
    assert s.agent_model == s.llm_model == "claude-sonnet-5-5"
    h = r.health()
    assert h["chains"]["stt"] == ["soniox"]
    assert h["providers"]["anthropic"] == dict(
        configured=False, ok=False, detail="Yapılandırılmadı"
    )
    assert not r.clients


@pytest.mark.parametrize(
    "changes",
    [
        {"vertex_region": "global"},
        {"assemblyai_region": "us"},
        {"llm_live_chain": ["bedrock"], "bedrock_region": "us-east-1"},
    ],
)
def test_eu_constraints(changes):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **changes)


def test_installed_sdk_classes_and_constructors(monkeypatch):
    for name in (
        "AsyncAnthropicBedrockMantle",
        "AsyncAnthropicVertex",
        "AsyncAnthropicFoundry",
        "AsyncAnthropicBedrock",
    ):
        assert hasattr(anthropic, name)
    seen = []

    def fake(**kw):
        seen.append(kw)
        return NS()

    monkeypatch.setattr(anthropic, "AsyncAnthropicBedrock", fake)
    monkeypatch.setattr(anthropic, "AsyncAnthropicVertex", fake)
    s = Settings(_env_file=None, bedrock_aws_profile="katip", vertex_project="katip-eu")
    r = ProviderRegistry(s)
    r.llm_provider("bedrock")
    r.llm_provider("vertex")
    assert seen[0]["aws_region"] == "eu-central-1" and seen[0]["aws_profile"] == "katip"
    assert seen[1]["region"] == "eu" and seen[1]["project_id"] == "katip-eu"
    assert all("max_retries" not in kw and "timeout" not in kw for kw in seen)


class NoSTT(STTProvider):
    translates = True

    async def stream(self, audio, *, language, target_language=None):
        if False:
            yield


async def test_tts_mid_utterance_failure_does_not_duplicate_spoken_answer():
    class BrokenTTS:
        sample_rate = 24000

        async def synthesize(self, text, *, language):
            yield b"\0\0" * 5000
            raise ConnectionError("Sahte kesinti")

    s = MeetingSession("ses", stt=NoSTT(), persist=False)
    s._tts = BrokenTTS()
    await s._speak("Example raporu hazır.")
    out = [s.outbox.get_nowait() for _ in range(s.outbox.qsize())]
    assert any(x["kind"] == "speak_chunk" for x in out)
    assert out[-1] == {"kind": "speak_stop"}
    assert not any(x["kind"] == "chat_send" for x in out)


def test_sample_indexed_ring_and_overlap_deduplication():
    ring = ReplayBuffer(10, seconds=3)
    ring.append(b"\1\0" * 20)
    ring.append(b"\2\0" * 20)
    start, frames, lost = ring.replay(5)
    assert start == 10 and lost
    assert b"".join(frames) == b"\1\0" * 10 + b"\2\0" * 20
    utt = Utterance("b", "ekip, yeni karar", True, 1, 3)
    assert deduplicate(utt, 2, "Merhaba ekip").text == "yeni karar"
    assert deduplicate(Utterance("b", "ekip", True, 1, 2), 2, "ekip") is None


async def test_stt_switch_replays_normalizes_deduplicates_and_renumbers(monkeypatch):
    s_settings = get_settings()
    s_settings.stt_chain = ["soniox", "assemblyai"]
    r = get_registry()
    seen = []

    class Primary(NoSTT):
        name = "soniox"

        async def stream(self, audio, **kw):
            await anext(audio)
            yield Utterance("a", "Merhaba ekip", True, 0, 1.5, "1")
            yield Utterance("yarım", "yeni", False, 1.5, 3, "1")
            raise ConnectionError("Sahte kesinti")

    class Fallback(NoSTT):
        name = "assemblyai"
        translates = False

        async def stream(self, audio, **kw):
            seen.extend([frame async for frame in audio])
            yield Utterance("tekrar", "ekip", True, 0, 0.5, "A")
            yield Utterance("b", "ekip yeni karar", True, 0.2, 2, "A")

    monkeypatch.setattr(r, "stt", lambda name: Fallback())
    s = MeetingSession("stt", stt=Primary(), target_lang="", persist=False)
    panel = s.subscribe()
    s.push_audio(b"\1\0" * (16000 * 3))
    await s._audio.put(None)
    await s._stt_task
    assert b"".join(seen) == b"\1\0" * (16000 * 2)
    assert s.segments["b"].text == "yeni karar"
    assert s.segments["b"].start_s == 1.5 and s.segments["b"].end_s == 3
    assert s.segments["b"].speaker_label != s.segments["a"].speaker_label
    assert "tekrar" not in s.segments and "yarım" not in s.segments
    assert s._translator is not None and r.sticky[("stt", "stt")] == 1
    assert any(e.kind == "note" for e in list(panel._queue))
    await s.shutdown()


def test_stt_buffer_loss_is_visible():
    s = MeetingSession("kayıp", stt=NoSTT(), persist=False)
    panel = s.subscribe()
    s._replay_ring.append(b"\0\0" * (16000 * 31))
    s._prepare_replay()
    assert panel.get_nowait().kind == "error"


async def test_stream_switch_deduplicates_emitted_prefix():
    r = fake_registry()

    def streaming(pieces, failure=None):
        @asynccontextmanager
        async def manager(**kw):
            async def source():
                for piece in pieces:
                    yield piece
                if failure:
                    raise failure

            yield NS(text_stream=source(), get_final_message=AsyncMock(return_value=response()))

        return manager

    r.clients["anthropic"].client.messages.stream = streaming(["Hello "], ConnectionError())
    r.clients["bedrock"].client.messages.stream = streaming(["Hel", "lo world"])
    with usage.meeting("çeviri"):
        async with r.llm("translation").stream(**PARAMS) as stream:
            assert "".join([p async for p in stream.text_stream]) == "Hello world"
            assert await stream.get_final_message() is not None
    assert r.sticky[("çeviri", "translation")] == 1


def test_assemblyai_eu_protocol_and_speaker_word_groups(monkeypatch):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "sahte")
    get_settings.cache_clear()
    p = AssemblyAISTT()
    assert "streaming.eu.assemblyai.com/v3/ws" in p._url()
    assert "speech_model=universal-3-6-pro" in p._url()
    assert "speaker_labels=true" in p._url() and "format_turns" not in p._url()
    assert "language_detection=true" in p._url()
    turn = dict(
        type="Turn",
        turn_order=1,
        transcript="Merhaba hello",
        end_of_turn=False,
        speaker_label="A",
        words=[
            dict(text="Merhaba", start=0, end=400, speaker="A"),
            dict(text="hello", start=400, end=800, speaker="B"),
        ],
    )
    partials = p.turn(turn, "akış", "tr")
    turn["end_of_turn"] = True
    finals = p.turn(turn, "akış", "tr")
    assert [u.id for u in partials] == [u.id for u in finals]
    assert [u.speaker_label for u in finals] == ["A", "B"]
    assert finals[-1].end_s == 0.8 and all(u.is_final for u in finals)
    assert not p.translates and p.capabilities.diarization


async def test_assemblyai_wire_batches_frames_and_terminates(monkeypatch):
    from katip.pipeline.stt import assemblyai

    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "sahte")
    get_settings.cache_clear()
    seen = {}
    sent = []
    terminated = asyncio.Event()

    class Socket:
        async def send(self, data):
            sent.append(data)
            if isinstance(data, str) and json.loads(data)["type"] == "Terminate":
                terminated.set()

        async def __aiter__(self):
            yield json.dumps({"type": "Begin", "id": "sahte"})
            await terminated.wait()
            yield json.dumps(
                dict(
                    type="Turn",
                    turn_order=0,
                    transcript="Merhaba",
                    end_of_turn=True,
                    speaker_label="A",
                    words=[dict(text="Merhaba", start=0, end=120)],
                )
            )
            yield json.dumps({"type": "Termination"})

    @asynccontextmanager
    async def connect(url, **kw):
        seen.update(url=url, **kw)
        yield Socket()

    async def audio():
        for _ in range(6):
            yield b"\1\0" * 320

    monkeypatch.setattr(assemblyai.websockets, "connect", connect)
    out = [u async for u in AssemblyAISTT().stream(audio(), language="tr")]
    assert out[0].text == "Merhaba" and out[0].is_final
    binary = [s for s in sent if isinstance(s, bytes)]
    assert [len(chunk) for chunk in binary] == [3200, 1600]
    assert b"".join(binary).startswith(b"\1\0" * (320 * 6))
    assert seen["additional_headers"] == {"Authorization": "sahte"}
    assert "sahte" not in seen["url"]
    assert json.loads(sent[-1]) == {"type": "Terminate"}


def test_worker_environment_excludes_new_provider_secrets(monkeypatch):
    from katip.bot.supervisor import bot_environment

    secrets = (
        "ASSEMBLYAI_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_BEARER_TOKEN_BEDROCK",
    )
    for key in secrets:
        monkeypatch.setenv(key, "sahte")
    assert not (set(bot_environment("bot")) & set(secrets))


@pytest.mark.parametrize("status", [400, 401, 403])
def test_stt_request_errors_do_not_switch(status):
    from katip.pipeline.stt.soniox import SonioxError

    get_settings().stt_chain = ["soniox", "assemblyai"]
    s = MeetingSession("ret", stt=NoSTT(), persist=False)
    assert not s._switch_stt(SonioxError(status, "Sahte hata"))
    assert s._stt_index == 0


async def test_batch_outage_result_switches_to_normal_report():
    r = fake_registry()
    p = r.clients["anthropic"]
    p.capabilities = Capabilities(batches=True, structured_output=True)

    async def results(batch_id):
        async def entries():
            yield NS(result=NS(type="errored", error=NS(error=NS(type="overloaded_error"))))

        return entries()

    p.client.messages.batches = NS(
        create=AsyncMock(return_value=NS(id="sahte")),
        retrieve=AsyncMock(return_value=NS(processing_status="ended")),
        results=results,
    )
    r.clients["bedrock"].client.messages.create.return_value = response(json.dumps(REPORT))
    assert await summarize_meeting([], batch=True) == MeetingReport(**REPORT)
    r.clients["bedrock"].client.messages.create.assert_awaited_once()


async def test_interrupted_translation_does_not_store_partial_text(monkeypatch):
    from katip.pipeline.translate import Translator

    r = fake_registry()

    @asynccontextmanager
    async def broken(**kw):
        async def pieces():
            yield "Hello "
            raise ConnectionError()

        yield NS(text_stream=pieces())

    r.settings.llm_live_chain = ["anthropic"]
    r.clients["anthropic"].client.messages.stream = broken
    translator = Translator()
    assert await translator.translate("Merhaba dünya") == ""
    assert not translator._history


async def test_report_repair_budget_is_shared_across_outage_switch():
    r = fake_registry()
    r.settings.llm_report_chain = ["bedrock", "vertex"]
    r.clients["bedrock"].client.messages.create.side_effect = [response("{}"), status_error(529)]
    r.clients["vertex"].client.messages.create.return_value = response("{}")
    with pytest.raises(ValidationError):
        await summarize_meeting([])
    assert r.clients["bedrock"].client.messages.create.await_count == 2
    r.clients["vertex"].client.messages.create.assert_awaited_once()


@pytest.mark.parametrize(
    "code,retry", [(3005, True), (3009, True), (3006, False), (3007, False), (1008, False)]
)
def test_assemblyai_documented_outages_and_request_errors(code, retry):
    assert transient(AssemblyAIError(code)) is retry


async def test_stt_normal_expiry_replays_on_same_provider():
    get_settings().stt_chain = ["assemblyai"]

    class Expiring(NoSTT):
        name = "assemblyai"
        calls = 0

        async def stream(self, audio, **kw):
            self.calls += 1
            if self.calls == 1:
                await anext(audio)
                yield Utterance("a", "eski", True, 0, 1, "A")
                raise StreamExpired()
            assert len(b"".join([f async for f in audio])) == 16000 * 3
            yield Utterance("b", "yeni", True, 0.5, 1.5, "A")

    provider = Expiring()
    s = MeetingSession("yenileme", stt=provider, persist=False, target_lang="")
    s.push_audio(b"\0\0" * (16000 * 2))
    await s._audio.put(None)
    await s._stt_task
    assert provider.calls == 2 and s._stt is provider and s._stt_index == 0
    assert s.segments["b"].start_s == 1 and s.segments["b"].end_s == 2
    await s.shutdown()


def test_registry_settings_reach_every_audio_adapter():
    s = Settings(
        _env_file=None,
        SONIOX_API_KEY="sahte",
        ASSEMBLYAI_API_KEY="sahte",
        DEEPGRAM_API_KEY="sahte",
        soniox_region="eu",
    )
    registry = ProviderRegistry(s)
    for name in ("soniox", "assemblyai", "deepgram"):
        assert registry.stt(name)._settings is s
    assert registry.tts()._settings is s


@pytest.mark.parametrize(
    "code,retry",
    [(1006, True), (1011, True), (1012, True), (1013, True), (1008, False), (3006, False)],
)
def test_websocket_close_codes_are_not_http_statuses(code, retry):
    assert transient(ConnectionClosedError(Close(code, ""), None)) is retry
    assert transient(ConnectionClosedError(None, None))


async def test_stalled_tts_gap_stops_without_duplicating_audio():
    get_settings().provider_deadline_s = 0.05

    class StalledTTS:
        sample_rate = 24000

        async def synthesize(self, text, *, language):
            yield b"\0\0" * 5000
            await asyncio.sleep(1)

    s = MeetingSession("ses-süre", stt=NoSTT(), persist=False)
    s._tts = StalledTTS()
    await s._speak("Tam cevap.")
    out = [s.outbox.get_nowait() for _ in range(s.outbox.qsize())]
    assert out[-1] == {"kind": "speak_stop"}
    assert not any(x["kind"] == "chat_send" for x in out)
    assert s._speech_until == 0


@pytest.mark.parametrize("workload,limit_s", [("live", 60), ("report", 600)])
async def test_workload_options_preserve_installed_sdk_defaults(workload, limit_s):
    r = ProviderRegistry(Settings(_env_file=None, ANTHROPIC_API_KEY="sahte"))
    base = r.llm_provider("anthropic")
    try:
        assert base.client.max_retries == 2
        assert base.client.timeout.read == 600
        attempt = r.for_attempt("anthropic", None, workload, True, limit_s)
        assert attempt.client.max_retries == 2
        assert attempt.client.timeout.read == limit_s
        assert attempt.client.timeout.connect == 5
        early = r.for_attempt("anthropic", None, workload, False, 20)
        assert early.client.max_retries == 0 and early.client.timeout.read == 20
        assert base.client.max_retries == 2 and base.client.timeout.read == 600
    finally:
        await base.client.close()


@pytest.mark.parametrize("failure", [429, "connection"])
@pytest.mark.parametrize("use_fallback", [False, True])
async def test_single_or_last_provider_sdk_absorbs_transient_blip(
    monkeypatch, failure, use_fallback
):
    calls = []

    async def handle(request):
        calls.append(request)
        if len(calls) == 1:
            if failure == "connection":
                raise httpx2.ConnectError("Sahte kesinti", request=request)
            return httpx2.Response(429, json={"error": {"type": "rate_limit_error"}})
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": MODEL,
                "content": [{"type": "text", "text": "Cevap"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    transport = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    client = anthropic.AsyncAnthropic(api_key="sahte", http_client=transport)
    monkeypatch.setattr(anthropic.AsyncAnthropic, "_calculate_retry_timeout", lambda *a: 0)
    r = ProviderRegistry(Settings(_env_file=None))
    r.clients["anthropic"] = ClaudeProvider("anthropic", "api", client, Capabilities())
    if use_fallback:
        r.settings.llm_report_chain = ["bedrock", "anthropic"]
        r.clients["bedrock"] = ClaudeProvider(
            "bedrock",
            "eu",
            NS(messages=NS(create=AsyncMock(side_effect=ConnectionError()))),
            Capabilities(),
        )
    try:
        answer = await r.llm("report").create(**PARAMS)
        assert answer.content[0].text == "Cevap" and len(calls) == 2
    finally:
        await client.close()


async def test_sdk_retry_wait_cannot_exceed_overall_deadline(monkeypatch):
    calls = []

    async def handle(request):
        calls.append(request)
        return httpx2.Response(429, json={"error": {"type": "rate_limit_error"}})

    transport = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    client = anthropic.AsyncAnthropic(api_key="sahte", http_client=transport)
    # SDK 30 sn beklemek ister; genel süre sınırı (0,3 sn) bunu kesmelidir. Sınır, 50 ms'nin
    # altında deneme başlatılmayan payın üstünde kalmalı. Dış süre yalnız takılma korumasıdır.
    monkeypatch.setattr(anthropic.AsyncAnthropic, "_calculate_retry_timeout", lambda *a: 30)
    r = ProviderRegistry(Settings(_env_file=None, report_deadline_s=0.3))
    r.clients["anthropic"] = ClaudeProvider("anthropic", "api", client, Capabilities())
    try:
        started = time.monotonic()
        async with asyncio.timeout(10):
            with pytest.raises(TimeoutError):
                await r.llm("report").create(**PARAMS)
        assert time.monotonic() - started < 5
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.parametrize("structured", [False, True])
async def test_report_uses_long_workload_budget(structured):
    r = fake_registry()
    r.settings.llm_report_chain = ["anthropic"]
    r.settings.provider_attempt_timeout_s = r.settings.provider_deadline_s = 0.01
    r.settings.report_deadline_s = 0.2

    async def answer(**kw):
        await asyncio.sleep(0.03)
        return response(json.dumps(REPORT))

    r.clients["anthropic"].client.messages.create.side_effect = answer
    if structured:
        assert await summarize_meeting([], batch=False) == MeetingReport(**REPORT)
    else:
        await r.llm("report").create(**PARAMS)


async def test_batch_timeout_keeps_long_normal_report_budget(monkeypatch):
    r = fake_registry()
    r.settings.llm_report_chain = ["anthropic"]
    r.settings.provider_attempt_timeout_s = r.settings.provider_deadline_s = 0.01
    r.settings.report_deadline_s = 0.2
    r.clients["anthropic"].capabilities = Capabilities(batches=True)
    monkeypatch.setattr(
        "katip.reports.summarize._summarize_batch", AsyncMock(side_effect=TimeoutError())
    )

    async def answer(**kw):
        await asyncio.sleep(0.03)
        return response(json.dumps(REPORT))

    r.clients["anthropic"].client.messages.create.side_effect = answer
    assert await summarize_meeting([], batch=True) == MeetingReport(**REPORT)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("chain", [["anthropic"], ["anthropic", "bedrock"]])
async def test_single_and_last_provider_ignore_shared_open_circuits(chain, streaming):
    r = fake_registry()
    r.settings.llm_live_chain = chain
    for name in chain:
        for _ in range(3):
            r.breaker(name, "translation" if streaming else "live").failure()

    @asynccontextmanager
    async def stream(**kw):
        async def pieces():
            yield "Cevap"

        yield NS(text_stream=pieces())

    r.clients[chain[-1]].client.messages.stream = stream
    with usage.meeting("yeni"):
        if streaming:
            async with r.llm("translation").stream(**PARAMS) as out:
                assert [p async for p in out.text_stream] == ["Cevap"]
        else:
            await r.llm().create(**PARAMS)
            r.clients[chain[-1]].client.messages.create.assert_awaited_once()


async def test_single_provider_errors_do_not_create_breakers():
    r = fake_registry()
    r.settings.llm_live_chain = ["anthropic"]
    r.clients["anthropic"].client.messages.create.side_effect = ConnectionError()
    for _ in range(4):
        with pytest.raises(ConnectionError):
            await r.llm().create(**PARAMS)
    assert not r.breakers
    assert r.clients["anthropic"].client.messages.create.await_count == 4


@pytest.mark.parametrize("kind", ["overloaded_error", "api_error", "rate_limit_error"])
async def test_sse_http_200_error_body_switches_provider(kind):
    r = fake_registry()
    exc = status_error(200)
    exc.body = {"type": "error", "error": {"type": kind, "message": "Sahte kesinti"}}

    @asynccontextmanager
    async def broken(**kw):
        async def pieces():
            yield "Hello "
            raise exc

        yield NS(text_stream=pieces())

    @asynccontextmanager
    async def backup(**kw):
        async def pieces():
            yield "Hello world"

        yield NS(text_stream=pieces())

    r.clients["anthropic"].client.messages.stream = broken
    r.clients["bedrock"].client.messages.stream = backup
    async with r.llm("translation").stream(**PARAMS) as out:
        assert "".join([p async for p in out.text_stream]) == "Hello world"


async def test_installed_sdk_sse_error_has_http_200():
    client = anthropic.AsyncAnthropic(api_key="sahte")
    response = httpx2.Response(
        200,
        request=httpx2.Request("POST", "https://sahte.invalid"),
        content=b'event: error\ndata: {"error":{"type":"overloaded_error"}}\n\n',
    )
    try:
        stream = anthropic.AsyncStream(cast_to=dict, response=response, client=client)
        with pytest.raises(anthropic.APIStatusError) as caught:
            await anext(stream)
        assert caught.value.status_code == 200 and transient(caught.value)
    finally:
        await client.close()


@pytest.mark.parametrize("failed", [False, True])
async def test_shutdown_forgets_provider_selections_even_on_failure(monkeypatch, failed):
    r = get_registry()
    s = MeetingSession("bitti", stt=NoSTT(), persist=False)
    r.sticky[(s.session_id, "live")] = 0
    r.sticky[(s.session_id, "stt")] = 0
    if failed:
        monkeypatch.setattr(s, "_shutdown", AsyncMock(side_effect=RuntimeError("Sahte hata")))
        with pytest.raises(RuntimeError):
            await s.shutdown()
    else:
        await s.shutdown()
    assert not r.health()["active"]


def test_failed_session_creation_forgets_selections(monkeypatch):
    r = get_registry()
    r.sticky[("başlamadı", "live")] = 0

    def failed_session(*a, **kw):
        raise RuntimeError("Sahte başlangıç hatası")

    monkeypatch.setattr("katip.pipeline.session.MeetingSession", failed_session)
    with pytest.raises(RuntimeError):
        SessionRegistry().create("başlamadı")
    assert not r.health()["active"]


async def test_replayed_audio_is_available_for_speaker_timing():
    s = MeetingSession("ses-hizası", stt=NoSTT(), persist=False)
    s._ring.append(b"\1\0" * 16000)
    s._replay_ring.append(b"\2\0" * 16000)
    s._prepare_replay()
    s._ring.mark_stream_start()
    await s._audio.put(None)
    assert b"".join([f async for f in s._frames()]) == b"\2\0" * 16000
    assert s._ring.slice_stream(0, 1).tobytes() == b"\2\0" * 16000
    assert s._audio_cursor == 16000
    await s.shutdown()


async def test_stt_fallback_wraps_to_recovered_primary(monkeypatch):
    get_settings().stt_chain = ["soniox", "assemblyai"]
    r = get_registry()
    calls = []

    class Provider(NoSTT):
        def __init__(self, name):
            self.name = name

        async def stream(self, audio, **kw):
            calls.append(self.name)
            if len(calls) < 3:
                raise ConnectionError()
            async for _frame in audio:
                pass
            yield Utterance("final", "Sonuç", True, 0, 1)

    monkeypatch.setattr(r, "stt", Provider)
    s = MeetingSession("dönüş", stt=Provider("soniox"), persist=False)
    s.push_audio(b"\1\0" * 16000)
    await s._audio.put(None)
    await s._stt_task
    assert calls == ["soniox", "assemblyai", "soniox"]
    assert s._stt_index == 0 and "final" in s.segments
    await s.shutdown()


async def test_stt_recovers_after_more_than_restart_limit(monkeypatch):
    import katip.pipeline.session as session_module

    get_settings().stt_chain = ["soniox", "assemblyai"]
    r = get_registry()
    now, calls = [0.0], []
    # Kurtarma öncesi bütün devrelerin açılıp soğumasını da sınar.
    for name in r.stt_chain:
        r.breakers[(name, "stt")] = CircuitBreaker(lambda: now[0])

    async def sleep(delay):
        now[0] += delay

    class Provider(NoSTT):
        def __init__(self, name):
            self.name = name

        async def stream(self, audio, **kw):
            calls.append(self.name)
            if len(calls) <= session_module._MAX_STT_RESTARTS + 2:
                raise ConnectionError()
            async for _frame in audio:
                pass
            yield Utterance("düzeldi", "Sonuç", True, 0, 1)

    monkeypatch.setattr(session_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(r, "stt", Provider)
    s = MeetingSession("uzun-kesinti", stt=Provider("soniox"), persist=False)
    s.push_audio(b"\0\0" * 16000)
    await s._audio.put(None)
    await s._stt_task
    assert len(calls) > session_module._MAX_STT_RESTARTS + 2
    assert "düzeldi" in s.segments and now[0] >= 60
    await s.shutdown()


@pytest.mark.parametrize("chain", [["soniox"], ["soniox", "assemblyai"]])
async def test_stt_permanent_error_keeps_restart_behavior(monkeypatch, chain):
    get_settings().stt_chain = chain
    calls = []

    class Recovering(NoSTT):
        name = "soniox"

        async def stream(self, audio, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("Sahte kalıcı hata")
            async for _frame in audio:
                pass
            if False:
                yield

    monkeypatch.setattr("katip.pipeline.session.asyncio.sleep", AsyncMock())
    s = MeetingSession("yeniden", stt=Recovering(), persist=False)
    s.push_audio(b"\0\0" * 16000)
    await s._audio.put(None)
    await s._stt_task
    assert len(calls) == 2
    if len(chain) == 1:
        assert not get_registry().breakers
    await s.shutdown()


@pytest.mark.parametrize("name", ["bedrock", "vertex"])
@pytest.mark.parametrize(
    "model,thinking",
    [
        ("claude-haiku-5-5", "disabled"),
        ("claude-sonnet-5-5", "between_tools"),
        ("claude-opus-5-5", "adaptive"),
    ],
)
def test_cloud_capabilities_sanitize_thinking_tool_loops(name, model, thinking):
    p = ClaudeProvider(name, "eu", NS(), Capabilities())
    params = {
        **PARAMS,
        "model": model,
        "thinking": {"type": thinking},
        "output_config": {"effort": "high"},
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Gizli", "signature": "imza"},
                    {"type": "tool_use", "id": "t", "name": "not", "input": {"text": "Not"}},
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t", "content": "Alındı"}],
            },
        ],
        "tools": [{"name": "not", "input_schema": {"type": "object"}}],
    }
    args = p.params(params)
    assert "output_config" not in args and "thinking" not in args
    assert all(b["type"] == "text" for m in args["messages"] for b in m["content"])
    assert "Alındı" in args["messages"][1]["content"][0]["text"]
    assert args["tools"] == params["tools"]
    assert params["messages"][0]["content"][0]["type"] == "thinking"


def test_anthropic_verified_capabilities_preserve_parameters():
    p = ClaudeProvider(
        "anthropic",
        "api",
        NS(),
        Capabilities(
            output_config=True,
            thinking_config=True,
            thinking_signatures=True,
        ),
    )
    params = {**PARAMS, "thinking": {"type": "between_tools"}, "output_config": {"effort": "low"}}
    assert p.params(params) == params


@pytest.mark.parametrize("workload", ["live", "translation", "report"])
def test_bedrock_model_prefix_and_workload_full_id_override(workload):
    r = fake_registry()
    r.settings.bedrock_model_prefix = "anthropic."
    p = r.for_attempt("bedrock", None, workload, True, 600)
    assert p.params(PARAMS)["model"] == f"anthropic.{MODEL}"
    r.settings.bedrock_model_ids[workload] = "arn:aws:bedrock:eu:test-profile"
    p = r.for_attempt("bedrock", None, workload, True, 600)
    assert p.params(PARAMS)["model"] == r.settings.bedrock_model_ids[workload]


@pytest.mark.parametrize("first_chunk", [None, b"\0\0" * 100])
async def test_tts_first_or_tiny_audio_timeout_posts_chat(first_chunk):
    get_settings().provider_deadline_s = 0.01
    closed = []

    class StalledTTS:
        sample_rate = 24000

        async def synthesize(self, text, **kw):
            try:
                if first_chunk:
                    yield first_chunk
                await asyncio.sleep(1)
            finally:
                closed.append(True)

    s = MeetingSession("ilk-ses", stt=NoSTT(), persist=False)
    s._tts = StalledTTS()
    await s._speak("Cevap.")
    assert any(m["kind"] == "chat_send" for m in list(s.outbox._queue))
    assert closed
    await s.shutdown()


async def test_tts_playback_backpressure_does_not_consume_provider_timeout(monkeypatch):
    get_settings().provider_deadline_s = 0.01

    class TTS:
        sample_rate = 24000

        async def synthesize(self, text, **kw):
            for _ in range(4):
                yield b"\0\0" * 4800

    s = MeetingSession("uzun-ses", stt=NoSTT(), persist=False)
    s._tts = TTS()
    original = s._send_audio

    async def slow_send(*args):
        await asyncio.sleep(0.02)
        await original(*args)

    monkeypatch.setattr(s, "_send_audio", slow_send)
    await s._speak("Uzun cevap.")
    out = list(s.outbox._queue)
    assert out[-1]["kind"] == "speak_end"
    assert not any(m["kind"] == "chat_send" for m in out)
    await s.shutdown()


@pytest.mark.parametrize("replay,interval", [(False, 0.1), (True, 0.08)])
@pytest.mark.parametrize("initial_wait", [0, 5])
async def test_assemblyai_deadline_pacer_accounts_for_send_time(
    monkeypatch,
    replay,
    interval,
    initial_wait,
):
    from katip.pipeline.stt import assemblyai

    now, sent = [0.0], []
    terminated = asyncio.Event()
    monkeypatch.setattr(assemblyai, "time", NS(monotonic=lambda: now[0]))

    async def sleep(delay):
        now[0] += delay

    class Socket:
        async def send(self, data):
            if isinstance(data, bytes):
                sent.append((now[0], len(data)))
                now[0] += 0.03
            else:
                terminated.set()

        async def __aiter__(self):
            await terminated.wait()
            yield json.dumps({"type": "Termination"})

    @asynccontextmanager
    async def connect(*a, **kw):
        yield Socket()

    async def audio():
        now[0] += initial_wait
        for _ in range(4):
            yield b"\0\0" * 1600

    monkeypatch.setattr(assemblyai.asyncio, "sleep", sleep)
    monkeypatch.setattr(assemblyai.websockets, "connect", connect)
    p = AssemblyAISTT(settings=Settings(_env_file=None, ASSEMBLYAI_API_KEY="sahte"))
    p.audio_backlog = lambda: replay
    assert [u async for u in p.stream(audio(), language="tr")] == []
    assert [t for t, _ in sent] == pytest.approx([initial_wait + i * interval for i in range(4)])
    assert all(size == 3200 for _, size in sent)


@pytest.mark.parametrize("sending_finished", [False, True])
async def test_assemblyai_silence_timeout_is_normal_only_after_audio_end(
    monkeypatch, sending_finished
):
    from katip.pipeline.stt import assemblyai
    from katip.providers import wire

    original_timeout = asyncio.timeout
    monkeypatch.setattr(wire.asyncio, "timeout", lambda _: original_timeout(0.01))

    class Socket:
        async def send(self, data):
            pass

        async def __aiter__(self):
            if not sending_finished:
                raise TimeoutError("Sahte alıcı hatası")
            yield json.dumps({"type": "Begin"})
            await asyncio.Event().wait()

    @asynccontextmanager
    async def connect(*a, **kw):
        yield Socket()

    async def audio():
        if not sending_finished:
            await asyncio.Event().wait()
        if False:
            yield

    monkeypatch.setattr(assemblyai.websockets, "connect", connect)
    p = AssemblyAISTT(settings=Settings(_env_file=None, ASSEMBLYAI_API_KEY="sahte"))
    if sending_finished:
        assert [u async for u in p.stream(audio(), language="tr")] == []
    else:
        with pytest.raises(TimeoutError):
            _ = [u async for u in p.stream(audio(), language="tr")]


@pytest.mark.parametrize(
    "code,expected", [(3005, AssemblyAIError), (3008, StreamExpired), (3009, AssemblyAIError)]
)
async def test_assemblyai_protocol_close_codes_are_classified(monkeypatch, code, expected):
    from katip.pipeline.stt import assemblyai

    class Socket:
        async def send(self, data):
            pass

        async def __aiter__(self):
            raise ConnectionClosedError(Close(code, ""), None)
            yield

    @asynccontextmanager
    async def connect(*a, **kw):
        yield Socket()

    async def audio():
        await asyncio.Event().wait()
        yield

    monkeypatch.setattr(assemblyai.websockets, "connect", connect)
    p = AssemblyAISTT(settings=Settings(_env_file=None, ASSEMBLYAI_API_KEY="sahte"))
    with pytest.raises(expected):
        _ = [u async for u in p.stream(audio(), language="tr")]


@pytest.mark.parametrize("terminated", [False, True])
async def test_soniox_tts_requires_documented_termination(monkeypatch, terminated):
    import base64

    from katip.voice import tts

    class Socket:
        async def send(self, data):
            pass

        async def __aiter__(self):
            yield json.dumps({"audio": base64.b64encode(b"\0\0" * 100).decode(), "audio_end": True})
            if terminated:
                yield json.dumps({"terminated": True})

    @asynccontextmanager
    async def connect(*a, **kw):
        yield Socket()

    monkeypatch.setattr(tts.websockets, "connect", connect)
    p = tts.SonioxTTS(settings=Settings(_env_file=None, SONIOX_API_KEY="sahte"))
    if terminated:
        assert len(b"".join([c async for c in p.synthesize("Cevap", language="tr")])) == 200
    else:
        with pytest.raises(ConnectionError):
            _ = [c async for c in p.synthesize("Cevap", language="tr")]


async def test_wire_silence_after_audio_end_is_normal_end_for_every_provider(monkeypatch):
    """Ses bittikten sonra sunucu susarsa (Soniox dahil) akış hata vermeden biter."""
    from katip.providers import wire

    original_timeout = asyncio.timeout
    monkeypatch.setattr(wire.asyncio, "timeout", lambda _: original_timeout(0.01))

    class Socket:
        async def __aiter__(self):
            yield "son"
            await asyncio.Event().wait()

    async def pump():
        return None

    task = asyncio.create_task(pump())
    await task
    assert [m async for m in wire.messages(Socket(), task)] == ["son"]


@pytest.mark.parametrize(
    ("name", "model", "beta"),
    [
        ("anthropic", "claude-sonnet-5-5", True),
        ("anthropic", "claude-haiku-5-5", False),
        ("vertex", "claude-sonnet-5-5", False),
        ("bedrock", "claude-sonnet-5-5", False),
    ],
)
def test_create_adds_server_fallback_only_on_claude_api(name, model, beta):
    response = NS(usage=None)
    plain, betas = AsyncMock(return_value=response), AsyncMock(return_value=response)
    client = NS(messages=NS(create=plain), beta=NS(messages=NS(create=betas)))
    p = ClaudeProvider(name, "eu", client, Capabilities())
    asyncio.run(p.create(**{**PARAMS, "model": model}))
    used, unused = (betas, plain) if beta else (plain, betas)
    used.assert_awaited_once()
    unused.assert_not_awaited()
    kwargs = used.await_args.kwargs
    assert ("fallbacks" in kwargs) is beta and ("betas" in kwargs) is beta
    if beta:
        assert kwargs["fallbacks"] == "default"
        assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
