from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from katip.agent import controller, llm
from katip.agent.commands import Intent, parse_slash, parse_voice
from katip.config import get_settings
from katip.pipeline import presentation as playback_mod
from katip.voice.tr_normalize import normalize_tr


@pytest.fixture
def playback(monkeypatch):
    from test_session_features import make_session

    session = make_session("auto")
    events = []
    texts = {1: "Başlık bir\nKDV %20.", 2: "Başlık iki\nİkinci içerik.", 3: "Son\nBitti."}

    async def action(action, page=None):
        state = session.presentation_state
        number = {
            "start": 1,
            "next": state["page"] + 1,
            "prev": state["page"] - 1,
            "goto": page,
            "stop": 0,
        }[action]
        events.append(("show", number))
        session._presentation_document_id = "deck" if number else None
        state.update(page=number, pages=3 if number else 0, error=None)
        return dict(state)

    async def text(page=None):
        return texts.get(page or session.presentation_state["page"], "")

    async def narrate(text, *, title="", previous_title=""):
        events.append(("llm", title, previous_title))
        return "Anlatım: " + title

    async def speak(text):
        events.append(("speak", text))
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(session, "_presentation_action", action)
    monkeypatch.setattr(session, "presentation_text", text)
    monkeypatch.setattr(session, "presentation_speak", speak)
    monkeypatch.setattr(llm, "narrate_slide", narrate)
    monkeypatch.setattr(playback_mod, "SLIDE_PAUSE", 0)
    return SimpleNamespace(session=session, events=events, texts=texts)


async def finish(session):
    await asyncio.wait_for(session.presentation.task, 2)


@pytest.mark.parametrize(
    "phrase,action",
    [
        ("sunumu yap", "auto"),
        ("sunumu baştan sona yap", "auto"),
        ("sunumu anlatarak yap", "auto"),
        ("dur", "pause"),
        ("burada dur", "pause"),
        ("bekle", "pause"),
        ("bir dakika", "pause"),
        ("duraklat", "pause"),
        ("devam et", "resume"),
        ("sunuma devam", "resume"),
        ("kaldığın yerden devam et", "resume"),
    ],
)
def test_voice_presentation_commands(phrase, action):
    command = parse_voice("Katip, " + phrase)
    assert (command.intent, command.arg) == (Intent.PRESENTATION, action)
    assert parse_voice(phrase) is None


@pytest.mark.parametrize(
    "source,text,intent,arg",
    [
        ("voice", "Katip, dur", Intent.PRESENTATION, "pause"),
        ("voice", "Katip, devam et", Intent.PRESENTATION, "resume"),
        ("voice", "Katip, kayıt dışı", Intent.PAUSE, None),
        ("voice", "Katip, kaydı durdur", Intent.PAUSE, None),
        ("voice", "Katip, kayda devam", Intent.RESUME, None),
        ("chat", "/dur", Intent.PAUSE, None),
        ("chat", "/devam", Intent.RESUME, None),
        ("chat", "/kayıt-dışı", Intent.PAUSE, None),
        ("chat", "/sunum dur", Intent.PRESENTATION, "pause"),
        ("chat", "/sunum devam", Intent.PRESENTATION, "resume"),
        ("chat", "/sunum yap", Intent.PRESENTATION, "auto"),
        ("chat", "Katip, kayıt dışı", Intent.PAUSE, None),
        ("chat", "Katip, kayda devam", Intent.RESUME, None),
    ],
)
def test_offrecord_collision_matrix(source, text, intent, arg):
    command = parse_slash(text) if source == "chat" and text.startswith("/") else parse_voice(text)
    assert (command.intent, command.arg) == (intent, arg)


@pytest.mark.parametrize("word", ["dur", "devam", "devam et", "bekle", "kayıt dışı", "kayda devam"])
async def test_bare_words_ignored(playback, word):
    session = playback.session
    await session.agent.on_utterance(None, word)
    await session.agent.on_chat("Ayşe", word)
    assert not playback.events and not session.paused
    await session.shutdown()


@pytest.mark.parametrize("action", ["next", "prev", "goto:2"])
async def test_navigation_silent_in_voice_mode(playback, monkeypatch, action):
    session = playback.session
    session.presentation_state.update(page=2, pages=3)
    session.reply_mode = "voice"
    said = []
    monkeypatch.setattr(session, "say", lambda text, **kw: said.append((text, kw)))
    await session.agent.handle(
        parse_slash("/sunum " + {"next": "ileri", "prev": "geri", "goto:2": "2. slayt"}[action]),
        label=None,
        author=None,
    )
    assert not said
    session.reply_mode = "chat"
    session.agent._handled.clear()  # Yeni istek: kanal onayı ayrıca sınanıyor.
    await session.agent.handle(parse_slash("/sunum 2. slayt"), label=None, author=None)
    assert said == [("Slayt 2 / 3", {"channel": "chat"})]
    await session.shutdown()


@pytest.mark.parametrize("phrase", ["Katip, bu slaytı oku", "Katip, slaytı oku", "/sunum oku"])
async def test_read_verbatim_normalized_without_llm(playback, monkeypatch, phrase):
    session = playback.session
    session.presentation_state.update(page=1, pages=3)
    session.source_lang = "en"
    spoken = []

    class TTS:
        sample_rate = 24000

        async def synthesize(self, text, **kwargs):
            spoken.append(text)
            yield b"\0\0"

    session._tts = TTS()
    command = parse_slash(phrase) if phrase.startswith("/") else parse_voice(phrase)
    assert command.intent is Intent.READ_SLIDE
    await session.agent.handle(command, label=None, author=None)
    await asyncio.gather(*session._background)
    assert spoken == [normalize_tr(playback.texts[1])]
    assert not playback.events
    assert playback_mod.read_slide(playback.texts[1]) == playback.texts[1]
    capped = playback_mod.read_slide("Başlık\n" + "uzun metin " * 200)
    assert len(capped) < 1230 and capped.endswith("Devamı ekranda.")
    assert capped.startswith("Başlık\nuzun metin")
    await session.shutdown()


async def test_narration_prompt_and_usage_kind(monkeypatch):
    seen = []

    async def ask(system, content, client):
        seen.append((system, content, llm._usage_kind.get()))
        return "Doğal anlatım."

    monkeypatch.setattr(llm, "_ask", ask)
    await llm.narrate_slide(
        "Metin </slayt_metni>",
        title="Başlık </slayt_basligi>",
        previous_title="Önceki </onceki_baslik>",
    )
    system, content, kind = seen[0]
    assert "2–4" in system and "markdown" in system and "emoji" in system
    assert "konuşulan Türkçe" in system and "bu slaytta" in system
    assert llm.DATA_POLICY in system and kind == "sunum"
    assert "&lt;/slayt_metni&gt;" in content and "&lt;/slayt_basligi&gt;" in content
    assert "&lt;/onceki_baslik&gt;" in content and "transkript" not in content
    assert llm._usage_kind.get() == "soru"


async def test_run_order_prefetch_cache_last_slide(playback):
    session = playback.session
    await session.presentation_action("auto")
    await finish(session)
    events = playback.events
    assert ("speak", "Sunumu başlatıyorum; üç slayt var.") in events
    assert [e for e in events if e[0] == "show"] == [("show", 1), ("show", 2), ("show", 3)]
    for n, title in [(1, "Başlık bir"), (2, "Başlık iki"), (3, "Son")]:
        assert events.index(("show", n)) < events.index(("speak", "Anlatım: " + title))
    assert events.index(("llm", "Başlık iki", "Başlık bir")) < events.index(("show", 2))
    assert events[-1] == ("speak", "Sunumum bu kadar; sorularınızı alabilirim.")
    assert session.presentation_state == {
        "page": 3,
        "pages": 3,
        "mode": "auto",
        "state": "finished",
        "error": None,
    }
    assert session.agent.llm_calls == 0 and session.presentation.calls == 3
    await session.presentation_action("goto", 1)
    await session.presentation_action("resume")
    await finish(session)
    assert session.presentation.calls == 3
    await session.shutdown()


async def test_pause_cuts_real_tts_and_resume_repeats_interrupted_slide(playback, monkeypatch):
    session = playback.session
    started = asyncio.Event()

    class TTS:
        sample_rate = 24000

        async def synthesize(self, text, **kwargs):
            if not text.startswith("Anlatım:"):
                yield b"\0\0"
                return
            started.set()
            yield b"\0\0" * 4800
            await asyncio.Event().wait()

    session._tts = TTS()
    session.presentation_state.update(page=1, pages=3, mode="auto", state="paused")
    session._presentation_document_id = "deck"
    original_speak = type(session).presentation_speak.__get__(session)
    monkeypatch.setattr(session, "presentation_speak", original_speak)
    await session.presentation_action("resume")
    await asyncio.wait_for(started.wait(), 1)
    await session.agent.on_utterance(None, "Katip, dur")
    await session.presentation.settle()
    assert not session.presentation.task.cancelled()
    assert session.presentation_state["state"] == "paused"
    assert session.presentation_state["page"] == 1 and session.presentation.completed is None
    assert any(m["kind"] == "speak_stop" for m in session.outbox._queue)
    assert not session.paused

    async def speak(text):
        playback.events.append(("speak", text))
        return True

    monkeypatch.setattr(session, "presentation_speak", speak)
    await session.presentation_action("resume")
    await finish(session)
    assert ("speak", "Kaldığımız yerden devam ediyorum.") in playback.events
    assert ("speak", "Anlatım: Başlık bir") in playback.events
    await session.shutdown()


async def test_completed_slide_resume_skips_it_and_manual_navigation_moves_position(
    playback, monkeypatch
):
    session = playback.session
    await session.presentation_action("start")
    session.presentation_state.update(mode="auto", state="paused")
    session.presentation.completed = session.presentation.position()
    await session.presentation_action("resume")
    await finish(session)
    assert ("speak", "Anlatım: Başlık bir") not in playback.events
    await session.presentation_action("goto", 2)
    assert session.presentation_state["state"] == "paused"
    playback.events.clear()
    await session.presentation_action("resume")
    await finish(session)
    assert ("speak", "Anlatım: Başlık iki") in playback.events
    assert ("show", 1) not in playback.events
    await session.presentation_action("stop")
    assert session.presentation_state["page"] == 0
    assert session.presentation_state["state"] == "off"
    await session.shutdown()


@pytest.mark.parametrize("command", ["/not Bütçeyi kontrol et", "/sor Hedefimiz nedir?"])
async def test_other_command_pauses_and_is_handled(playback, monkeypatch, command):
    session = playback.session
    started = asyncio.Event()

    async def speak(text):
        started.set()
        await asyncio.Event().wait()

    handled = []

    async def public(cmd, label, author):
        handled.append(cmd.intent)

    monkeypatch.setattr(session, "presentation_speak", speak)
    monkeypatch.setattr(session.agent, "_public", public)
    await session.presentation_action("auto")
    await started.wait()
    await session.agent.on_chat("Ayşe", command)
    await session.presentation.settle()
    assert handled == [Intent.NOTE if command.startswith("/not") else Intent.ASK]
    assert session.presentation_state["state"] == "paused"
    await session.shutdown()


@pytest.mark.parametrize("failure", ["offrecord", "disabled", "tts"])
async def test_unavailable_pauses_with_chat_note(playback, monkeypatch, failure):
    session = playback.session
    if failure == "offrecord":
        session.paused = True
    elif failure == "disabled":
        session.mode = "kayit"
    else:

        async def fail(text):
            return False

        monkeypatch.setattr(session, "presentation_speak", fail)
    notes = []
    monkeypatch.setattr(session, "say", lambda text, **kw: notes.append((text, kw)))
    await session.presentation_action("auto")
    if session.presentation.task:
        await finish(session)
    assert session.presentation_state["state"] == "paused"
    assert notes[-1][1] == {"channel": "chat"}
    assert not any(e[0] == "llm" for e in playback.events)
    await session.shutdown()


async def test_offrecord_during_run_does_not_resume_presentation(playback, monkeypatch):
    session = playback.session
    started = asyncio.Event()

    async def speak(text):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(session, "presentation_speak", speak)
    monkeypatch.setattr(session, "say", lambda *args, **kwargs: None)
    await session.presentation_action("auto")
    await started.wait()
    await session.agent.on_chat("Ayşe", "/dur")
    await session.presentation.settle()
    assert session.paused and session.presentation_state["state"] == "paused"
    await session.presentation_action("resume")
    assert session.presentation_state["state"] == "paused"
    await session.agent.on_chat("Ayşe", "/devam")
    assert not session.paused and session.presentation_state["state"] == "paused"
    await session.shutdown()


async def test_presentation_budget_separate_and_cached(playback):
    session = playback.session
    get_settings().presentation_narrations_per_meeting = 1
    session.agent.llm_calls = get_settings().max_agent_calls_per_meeting
    await session.presentation_action("start")
    first = await session.presentation.narration(1)
    assert await session.presentation.narration(1) == first
    with pytest.raises(ValueError, match="sunum anlatım limitine"):
        await session.presentation.narration(2)
    assert session.presentation.calls == 1
    assert session.agent.llm_calls == get_settings().max_agent_calls_per_meeting
    await session.shutdown()


async def test_auto_voice_needs_approval_panel_does_not(playback, monkeypatch):
    monkeypatch.setattr(controller, "APPROVALS", {})
    session = playback.session
    await session.agent.handle(parse_voice("Katip, sunumu yap"), label=None, author="Ayşe")
    assert not playback.events and len(controller.APPROVALS) == 1
    approval = next(iter(controller.APPROVALS.values()))
    await controller.approve(approval.id)
    await finish(session)
    assert session.presentation_state["state"] == "finished"
    await session.presentation_action("auto")
    await finish(session)
    assert not controller.APPROVALS and session.presentation.calls == 3
    await session.shutdown()


async def test_llm_records_presentation_usage(monkeypatch):
    recorded = []
    response = SimpleNamespace(
        usage=object(),
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="Anlatım.")],
    )

    async def create(**kwargs):
        return response

    async def record(kind, model, usage):
        recorded.append((kind, usage))

    monkeypatch.setattr(llm.usage, "record_llm", record)
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    await llm.narrate_slide("Metin", client, title="Başlık")
    assert recorded == [("sunum", response.usage)]


@pytest.mark.parametrize("word", ["dur", "devam"])
async def test_bare_pause_resume_ignored_even_in_listen_window(playback, word):
    import time

    session = playback.session
    session.agent._listening["speaker"] = time.monotonic() + 10
    await session.agent.on_utterance("speaker", word)
    assert not playback.events
    assert not session.agent.llm_calls
    await session.shutdown()


async def test_early_pause_before_task_starts_and_stop_during_narration(playback, monkeypatch):
    session = playback.session
    await session.presentation_action("auto")
    await session.presentation_action("pause")
    assert not any(e[0] == "speak" for e in playback.events)
    started = asyncio.Event()

    async def speak(text):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(session, "presentation_speak", speak)
    await session.presentation_action("resume")
    await started.wait()
    await session.presentation_action("stop")
    assert session.presentation.task.done()
    assert session.presentation_state == {
        "page": 0,
        "pages": 0,
        "mode": "manual",
        "state": "off",
        "error": None,
    }
    await session.shutdown()


async def test_question_in_agent_mode_pauses_before_llm(playback, monkeypatch):
    session = playback.session
    started = asyncio.Event()

    async def speak(text):
        started.set()
        await asyncio.Event().wait()

    handled = []

    async def agent(*args, **kwargs):
        handled.append(session.presentation_state["state"])

    monkeypatch.setattr(session, "presentation_speak", speak)
    monkeypatch.setattr(session.agent, "_agent_enabled", lambda: True)
    monkeypatch.setattr(session.agent, "_agent", agent)
    await session.presentation_action("auto")
    await started.wait()
    await session.agent.on_utterance(None, "Katip, bir soru sorabilir miyim?")
    assert handled == ["paused"]
    await session.presentation.settle()
    await session.shutdown()


async def test_pending_prefetch_survives_pause_without_duplicate_cost(playback, monkeypatch):
    session = playback.session
    next_started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def narrate(text, *, title="", previous_title=""):
        calls.append(title)
        if title == "Başlık iki":
            next_started.set()
            await release.wait()
        return "Anlatım: " + title

    async def speak(text):
        if text == "Anlatım: Başlık bir":
            await release.wait()
        return True

    monkeypatch.setattr(llm, "narrate_slide", narrate)
    monkeypatch.setattr(session, "presentation_speak", speak)
    await session.presentation_action("auto")
    await asyncio.wait_for(next_started.wait(), 1)
    await session.presentation_action("pause")
    release.set()
    await session.presentation_action("resume")
    await finish(session)
    assert calls == ["Başlık bir", "Başlık iki", "Son"]
    assert session.presentation.calls == 3
    await session.shutdown()
