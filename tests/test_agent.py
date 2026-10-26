"""Agent akışları: ses kaydı (rıza), beni unut, yönetici talimatı (3 sinyal), not, soru.

Gerçek depo (geçici SQLite) + sahte ses kodlayıcı + sahte LLM ile.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from sqlalchemy import select
from test_voice import SR, FakeEncoder, voice_of

from katip import privacy
from katip.agent import brain, controller
from katip.agent.controller import MeetingAgent
from katip.store import repo
from katip.store.db import session_scope
from katip.store.models import AuditLog, Consent, Role
from katip.store.org import OrgChart, apply_org_chart
from katip.voice.identity import KnownVoice, VoiceIdentifier


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def answer(self, question, **kw):
        self.calls.append(("answer", {"question": question, **kw}))
        return f"cevap: {question}"

    async def summarize_so_far(self, **kw):
        self.calls.append(("summary", kw))
        return "özet: bütçe konuşuldu"


class FakeHost:
    session_id = "m1"

    def __init__(self, identifier: VoiceIdentifier | None, dom: dict[str, str]) -> None:
        self.identifier = identifier
        self.dom = dom
        self.said: list[str] = []
        self.approvals: list[tuple[str, str]] = []
        self.forgotten: list[tuple[int, str]] = []
        self.enrolled: list[tuple[str, int, str]] = []
        self.lang = "en"

    def say(self, text, **kw):
        self.said.append(text)

    def transcript_text(self, last=400):
        return "Ayşe: bütçe cuma"

    def dom_name_for_label(self, label):
        return self.dom.get(label, f"Konuşmacı {label}") if label else None

    def label_for_name(self, name):
        return next((lbl for lbl, n in self.dom.items() if n == name), None)

    def set_target_lang(self, code):
        self.lang = code

    def request_approval(self, approval_id, description):
        self.approvals.append((approval_id, description))

    def on_person_forgotten(self, person_id, name):
        self.forgotten.append((person_id, name))

    def on_person_enrolled(self, label, person_id, name):
        self.enrolled.append((label, person_id, name))

    reply_mode = "chat"
    paused = False
    screenshots: list = []
    left = False

    def set_reply_mode(self, mode):
        self.reply_mode = mode

    def set_paused(self, paused):
        self.paused = paused

    def request_screenshot(self, requested_by):
        self.screenshots = [*self.screenshots, requested_by]

    def leave_meeting(self):
        self.left = True

    reports = 0

    def generate_report_now(self):
        self.reports += 1


def speak(
    ident: VoiceIdentifier, label: str, amplitude: int, seconds: float, *, enrolling: bool = False
) -> None:
    # Testlerde konuşmacı, kayıtlı herkese aday gösterilmiş sayılır (ekranda adı görünüyor).
    candidates = {k.person_id for k in ident.known}
    ident.observe(
        label,
        np.full(int(seconds * SR), amplitude, np.int16),
        candidates=candidates,
        enrolling=enrolling,
    )


@pytest.fixture(autouse=True)
def _clear_approvals():
    controller.APPROVALS.clear()
    yield
    controller.APPROVALS.clear()


async def test_enrollment_requires_spoken_consent_then_enough_speech(database) -> None:
    ident = VoiceIdentifier(FakeEncoder(), SR)
    host = FakeHost(ident, {"1": "Ayşe Yılmaz"})
    agent = MeetingAgent(host, llm_module=FakeLLM())

    await agent.on_utterance("1", "Katip, sesimi tanı")
    assert "biyometrik" in host.said[-1] and "onaylıyorum" in host.said[-1]

    # "Katip" demeden verilen cevap da rıza sayılır (bot bekliyordu).
    await agent.on_utterance("1", "Onaylıyorum")
    assert "Biraz daha konuştukça" in host.said[-1]
    ayse = await repo.find_person_by_name("Ayşe Yılmaz")
    async with session_scope() as s:
        consent = await s.scalar(select(Consent).where(Consent.person_id == ayse.id))
    assert consent is not None and consent.evidence.startswith("voice: 'Onaylıyorum'")
    assert privacy.CONSENT_VERSION in consent.evidence  # hangi rıza metnine onay verildi
    assert agent.is_enrolling("1")

    speak(ident, "1", 3, 3.0, enrolling=True)
    speak(ident, "1", 3, 3.0, enrolling=True)
    speak(ident, "1", 3, 3.0, enrolling=True)  # 9 sn ≥ 8 sn
    await agent.on_speech_observed("1")
    assert "sesini kaydettim" in host.said[-1]
    assert host.enrolled == [("1", ayse.id, "Ayşe Yılmaz")]
    stored = await repo.load_voiceprints("fake")
    assert [pid for pid, *_ in stored] == [ayse.id]


async def test_unexpected_consent_is_ignored(database) -> None:
    host = FakeHost(VoiceIdentifier(FakeEncoder(), SR), {"1": "Ali"})
    agent = MeetingAgent(host, llm_module=FakeLLM())
    await agent.on_utterance("1", "Katip onaylıyorum")
    assert host.said == []
    assert await repo.find_person_by_name("Ali") is None


async def test_forget_requires_voice_match_and_confirmation(database) -> None:
    person = await repo.ensure_person("Mehmet Kaya")
    await repo.add_consent(person.id, "voiceprint", "test", "m0")
    await repo.save_voiceprint(person.id, voice_of(5), model="fake", speech_seconds=9, samples=1)

    ident = VoiceIdentifier(
        FakeEncoder(), SR, known=[KnownVoice(person.id, "Mehmet Kaya", "member", voice_of(5))]
    )
    host = FakeHost(ident, {"2": "Mehmet Kaya", "3": "Başkası"})
    agent = MeetingAgent(host, llm_module=FakeLLM())
    speak(ident, "2", 5, 4.5)  # Mehmet'in sesi tanındı
    speak(ident, "3", 6, 4.5)  # tanınmayan biri

    # Başkası "beni unut" diyerek Mehmet'i sildiremez.
    await agent.on_utterance("3", "Katip beni unut")
    assert "tanıyamadım" in host.said[-1]

    # Ayrı bir isteğin yetki kontrolü; kısa süreli komut tekilleştirmesinden bağımsız.
    agent._handled.clear()
    await agent.on_utterance("2", "Katip, beni unut")
    assert "Emin misin" in host.said[-1]
    # Teyidi başka bir ses veremez.
    await agent.on_utterance("3", "Evet, unut")
    assert await repo.get_person(person.id) is not None

    await agent.on_utterance("2", "Evet, unut")
    assert await repo.get_person(person.id) is None
    assert host.forgotten == [(person.id, "Mehmet Kaya")]
    assert ident.match_for("2") is None
    assert "seni unuttum" in host.said[-1]


async def test_forget_via_chat_is_refused(database) -> None:
    host = FakeHost(VoiceIdentifier(FakeEncoder(), SR), {"1": "Ali"})
    agent = MeetingAgent(host, llm_module=FakeLLM())
    await agent.on_chat("Ali", "/unut")
    assert "sesli" in host.said[-1]


@pytest.mark.parametrize("reply_mode", ["voice", "chat"])
async def test_partial_forget_truthful_reply_and_biometrics_removed(
    database, monkeypatch, reply_mode
):
    from unittest.mock import AsyncMock

    person = await repo.ensure_person("Mehmet Kaya")
    await repo.add_consent(person.id, "voiceprint", "test", "m0")
    await repo.save_voiceprint(person.id, voice_of(5), model="fake", speech_seconds=9, samples=1)
    await repo.set_report("m1", {"summary": "Mehmet Kaya"}, {})
    monkeypatch.setattr(repo, "_erase_meeting_person", AsyncMock(side_effect=RuntimeError))
    ident = VoiceIdentifier(
        FakeEncoder(), SR, known=[KnownVoice(person.id, "Mehmet Kaya", "member", voice_of(5))]
    )
    host = FakeHost(ident, {"2": "Mehmet Kaya"})
    host.reply_mode = reply_mode
    agent = MeetingAgent(host, llm_module=FakeLLM())
    speak(ident, "2", 5, 4.5)
    await agent.on_utterance("2", "Katip, beni unut")
    await agent.on_utterance("2", "Evet, unut")
    assert "henüz tamamlanamadı" in host.said[-1]
    assert "Yöneticiye bildirildi" in host.said[-1]
    assert "seni unuttum" not in host.said[-1]
    assert (await repo.get_person(person.id)).erasure_pending
    assert await repo.enrolled_person_ids() == set()
    assert ident.match_for("2") is None


async def _admin_setup():
    await apply_org_chart(
        OrgChart.from_yaml(
            "people:\n  - name: Deniz Kaya\n    role: admin\n    aliases: [Deniz]\n"
            "  - name: Ayşe Yılmaz\n"
        )
    )
    boss = await repo.find_person_by_name("Deniz")
    ayse = await repo.find_person_by_name("Ayşe Yılmaz")
    known = [
        KnownVoice(boss.id, boss.display_name, Role.ADMIN, voice_of(1)),
        KnownVoice(ayse.id, ayse.display_name, Role.MEMBER, voice_of(4)),
    ]
    return boss, ayse, VoiceIdentifier(FakeEncoder(), SR, known=known)


async def test_admin_directive_needs_panel_approval(database) -> None:
    boss, _, ident = await _admin_setup()
    host = FakeHost(ident, {"1": "Deniz", "2": "Ayşe Yılmaz"})
    agent = MeetingAgent(host, llm_module=FakeLLM())
    speak(ident, "1", 1, 4.5)
    speak(ident, "2", 4, 4.5)

    await agent.on_utterance("1", "Katip, talimat: raporları İngilizce hazırla")
    assert len(host.approvals) == 1 and "panelden onaylaman" in host.said[-1]
    assert await repo.list_directives() == []  # onay gelmeden yürürlüğe girmez

    approval_id = host.approvals[0][0]
    assert await controller.approve(approval_id) is True
    assert await repo.list_directives() == ["raporları ingilizce hazırla"]
    assert "onaylandı" in host.said[-1]


async def test_spoofed_name_or_voice_cannot_apply_a_directive(database) -> None:
    _, _, ident = await _admin_setup()
    # 2 numaralı etiket: sesi Ayşe'ye ait ama ekranda adını "Deniz" yapmış.
    host = FakeHost(ident, {"2": "Deniz"})
    agent = MeetingAgent(host, llm_module=FakeLLM())
    speak(ident, "2", 4, 4.5)
    await agent.on_utterance("2", "Katip talimat: herkese mail at")
    assert "panelden onaylaman" in host.said[-1] and len(host.approvals) == 1
    assert await repo.list_directives() == []

    # 1 numaralı etiket: sesi yöneticiye benziyor (ör. klon) ama ekrandaki hesap başka.
    host.dom["1"] = "Misafir"
    speak(ident, "1", 1, 4.5)
    agent._handled.clear()
    await agent.on_utterance("1", "Katip talimat: herkese mail at")
    assert "zaten panel onayı" in host.said[-1] and len(host.approvals) == 1
    assert await repo.list_directives() == []

    async with session_scope() as s:
        proposals = list(
            await s.scalars(select(AuditLog).where(AuditLog.action == "directive_proposed"))
        )
    assert len(proposals) == 1


async def test_note_summary_and_question(database) -> None:
    llm = FakeLLM()
    host = FakeHost(None, {"1": "Ayşe"})
    agent = MeetingAgent(host, llm_module=llm)

    await agent.on_utterance("1", "Katip, not al: demo tarihi 3 Ekim")
    assert host.said[-1] == "Not alındı."
    notes = await repo.list_notes("m1")
    assert [(n.kind, n.text, n.author) for n in notes] == [("note", "demo tarihi 3 ekim", "Ayşe")]

    await agent.on_chat("Ayşe", "/özet")
    assert host.said[-1] == "özet: bütçe konuşuldu"

    await agent.on_utterance("1", "Katip, geçen hafta bütçe için ne demiştik?")
    kind, kw = llm.calls[-1]
    assert kind == "answer" and "bütçe" in kw["question"] and kw["transcript"] == "Ayşe: bütçe cuma"
    assert kw["notes"] == ["Not: demo tarihi 3 ekim"]

    await agent.on_chat("Ayşe", "/dil de")
    assert host.lang == "de"


async def test_llm_budget_per_meeting(database, monkeypatch) -> None:
    monkeypatch.setenv("KATIP_MAX_AGENT_CALLS_PER_MEETING", "2")
    from katip.config import get_settings

    get_settings.cache_clear()
    llm = FakeLLM()
    host = FakeHost(None, {"1": "Ayşe"})
    agent = MeetingAgent(host, llm_module=llm)
    for _ in range(3):
        agent._handled.clear()  # İstek limiti, sekiz saniyelik tekilleştirmeden bağımsız.
        await agent.on_chat("Ayşe", "/özet")
    assert len(llm.calls) == 2
    assert "soru limitine" in host.said[-1]
    # Not almak LLM gerektirmez; limitten etkilenmez.
    await agent.on_chat("Ayşe", "/not yine de not al")
    assert host.said[-1] == "Not alındı."


async def test_meeting_minutes_today_caps_stale_meetings(database) -> None:
    from datetime import UTC, datetime, timedelta

    await repo.upsert_meeting("old", started_at=datetime.now(UTC) - timedelta(hours=10))
    minutes = await repo.meeting_minutes_today()
    # Hiç kapanmamış kayıt en fazla max_meeting_minutes + 5 sayılır (bugün başladıysa).
    assert minutes <= 605  # en uzun mod (sessiz kayıt 600 dk) + 5


async def test_mode_screenshot_pause_decision_leave(database) -> None:
    host = FakeHost(None, {"1": "Ayşe"})
    agent = MeetingAgent(host, llm_module=FakeLLM())

    await agent.on_utterance("1", "Katip, sesli cevap ver")
    assert host.reply_mode == "voice" and "sesli cevap" in host.said[-1]
    await agent.on_chat("Ayşe", "/yazılı")
    assert host.reply_mode == "chat" and "chat'e yazacağım" in host.said[-1]

    await agent.on_utterance("1", "Katip, ekran görüntüsü al")
    assert host.screenshots == ["Ayşe"]

    await agent.on_utterance("1", "Katip, kayıt dışı konuşalım")
    assert host.paused is True and "Kayıt dışı" in host.said[-1]
    await agent.on_utterance("1", "Katip, kayda devam")
    assert host.paused is False

    await agent.on_utterance("1", "Katip, karar: fiyatlar yüzde on artacak")
    assert host.said[-1] == "Karar kaydedildi."
    notes = await repo.list_notes("m1")
    assert [(n.kind, n.text) for n in notes] == [("decision", "fiyatlar yüzde on artacak")]

    await agent.on_utterance("1", "Katip, toplantıdan ayrılabilirsin")
    assert host.left is True


async def test_bare_wake_word_opens_listen_window(database) -> None:
    llm = FakeLLM()
    host = FakeHost(None, {"1": "Deniz"})
    agent = MeetingAgent(host, llm_module=llm)

    await agent.on_utterance("1", "Katip.")
    assert host.said[-1] == "Buyurun, dinliyorum."
    # Aynı kişi, "Katip" demeden: komut olarak işlenir.
    await agent.on_utterance("1", "Şu ana kadar ne konuştuk?")
    assert llm.calls[-1][0] == "summary"
    # Pencere tek kullanımlık: sonraki sohbet cümlesi komut sayılmaz.
    before = len(host.said)
    await agent.on_utterance("1", "Tamam arkadaşlar devam edelim")
    assert len(host.said) == before


async def test_listen_window_is_per_speaker(database) -> None:
    host = FakeHost(None, {"1": "Deniz", "2": "Selin"})
    agent = MeetingAgent(host, llm_module=FakeLLM())
    await agent.on_utterance("1", "Katip.")
    before = len(host.said)
    await agent.on_utterance("2", "Not al bunu da")  # başka konuşmacı: pencere onun değil
    assert len(host.said) == before


async def test_privacy_notice(database, monkeypatch) -> None:
    monkeypatch.setenv("KATIP_COMPANY_NAME", "Example")
    from katip.config import get_settings

    get_settings.cache_clear()
    host = FakeHost(None, {"1": "Deniz"})
    agent = MeetingAgent(host, llm_module=FakeLLM())
    await agent.on_utterance("1", "Katip, KVKK bilgilendirmesi yap.")
    # Aydınlatma metni chat'e yazılı gider; sesli istendiği için ayrıca sesli okunur.
    chat, spoken = host.said[-2], host.said[-1]
    assert "Example adına yapay zekâ asistanı Katip" in chat
    assert "KVKK md. 11" in chat and "kayıt dışı" in chat and "/gizlilik" in chat
    assert "Sesle kimlik tanıma yapılmaz" in chat and "geliştirme aşamasında" in chat
    assert "yurt dışındaki sunucularda" in chat and "/ayrıl" in chat
    # Sağlayıcı ve barındırma adları toplantıya yazılmaz.
    for name in ("Anthropic", "Soniox", "Railway"):
        assert name not in chat and name not in spoken
    assert "Example adına" in spoken and "/gizlilik" not in spoken  # link sesli okunmaz
    assert "toplantıdan çık" in spoken and "geliştirme aşamasında" in spoken
    page = privacy.notice_page_html()
    assert "yurt dışındaki sunucularda" in page and "toplantıdan çık" in page
    assert not any(name in page for name in ("Anthropic", "Soniox", "Railway"))


async def test_any_participant_can_remove_katip_as_notice_promises(database) -> None:
    """Aydınlatma metni itiraz edenin Katip'i çıkarabileceğini söyler; tanınmayan kişi de."""
    host = FakeHost(None, {"1": "Deniz"})
    agent = MeetingAgent(host, llm_module=FakeLLM())
    await agent.on_utterance("1", "Katip, kayıt dışı")
    assert host.paused
    await agent.on_utterance("7", "Katip, toplantıdan çık.")  # eşleşmemiş, hesabı yok
    assert host.left


async def test_consent_text_is_separate_from_notice() -> None:
    """KVKK 2026/347: açık rıza aydınlatmadan ayrı; alternatif ve geri alma belirtilir."""
    text = privacy.consent_text("Ayşe")
    assert "ayrı bir açık rıza" in text and "zorunda değilsin" in text
    assert "beni unut" in text and "onaylıyorum" in text
    assert "aydınlatma" not in text.lower()


async def test_enroll_uses_spoken_name_when_screen_name_unknown(database) -> None:
    """Teams'te ekrandaki isim okunamadığında: "Katip, ben Deniz, beni kaydet"."""
    await apply_org_chart(
        OrgChart.from_yaml("people:\n  - name: Deniz Kaya\n    aliases: [Deniz]\n")
    )
    ident = VoiceIdentifier(FakeEncoder(), SR)
    host = FakeHost(ident, {})  # DOM adı yok → "Konuşmacı 1"
    agent = MeetingAgent(host, llm_module=FakeLLM())

    await agent.on_utterance("1", "Katip, ben Deniz. Beni kaydet.")
    assert "biyometrik" in host.said[-1] and host.said[-1].startswith("Deniz,")
    await agent.on_utterance("1", "Onaylıyorum")
    for _ in range(3):
        speak(ident, "1", 2, 3.0, enrolling=True)
    await agent.on_speech_observed("1")
    boss = await repo.find_person_by_name("Deniz Kaya")
    assert host.enrolled == [("1", boss.id, "Deniz Kaya")]  # şemadaki tam adla


async def test_enroll_without_any_name_asks_for_it(database) -> None:
    host = FakeHost(VoiceIdentifier(FakeEncoder(), SR), {})
    agent = MeetingAgent(host, llm_module=FakeLLM())
    await agent.on_utterance("1", "Katip, beni kaydet")
    assert "adını söyleyerek" in host.said[-1]


async def test_interrupted_meetings_get_closed_and_reported(database, monkeypatch) -> None:
    from datetime import UTC, datetime

    from katip.api import main
    from katip.events import Segment
    from katip.reports import service

    started = datetime(2026, 9, 30, 7, 0, tzinfo=UTC)
    await repo.upsert_meeting("kesik", started_at=started)
    for i in range(3):
        await repo.save_segment(
            "kesik",
            Segment(
                id=f"k{i}",
                session_id="kesik",
                start_s=i * 10,
                end_s=i * 10 + 5,
                text="deneme",
                lang="tr",
                is_final=True,
            ),
        )
    assert await repo.meetings_missing_report() == ["kesik"]

    produced: list[str] = []

    async def fake_generate(meeting_id, **kw):
        produced.append(meeting_id)
        return {}

    monkeypatch.setattr(service, "generate_report", fake_generate)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(main.asyncio, "sleep", lambda *_: real_sleep(0))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("KATIP_AUTO_REPORT", "true")
    from katip.config import get_settings

    get_settings.cache_clear()
    await main._catch_up_reports()

    assert produced == ["kesik"]
    meeting = await repo.get_meeting("kesik")
    assert meeting.state == "interrupted"
    ended = meeting.ended_at if meeting.ended_at.tzinfo else meeting.ended_at.replace(tzinfo=UTC)
    assert (ended - started).total_seconds() == 25  # son konuşmanın bitişi


def test_name_from_speech_handles_sentence_case() -> None:
    from katip.agent.controller import name_from_speech

    assert name_from_speech("Katip. Ben Deniz. Beni kaydet.") == "Deniz"
    assert name_from_speech("Katip, benim adım Selin Arslan") == "Selin Arslan"
    assert name_from_speech("Katip, ben de katılıyorum") is None


async def test_already_enrolled_person_is_not_reenrolled(database) -> None:
    ident = VoiceIdentifier(
        FakeEncoder(), SR, known=[KnownVoice(7, "Deniz Kaya", "admin", voice_of(2))]
    )
    host = FakeHost(ident, {})
    agent = MeetingAgent(host, llm_module=FakeLLM())
    speak(ident, "1", 2, 4.5)
    await agent.on_utterance("1", "Katip, ben Deniz, beni kaydet")
    assert host.said[-1] == "Seni zaten tanıyorum, Deniz."


async def test_report_command_triggers_interim_report(database) -> None:
    host = FakeHost(None, {"1": "Deniz"})
    agent = MeetingAgent(host, llm_module=FakeLLM())
    await agent.on_utterance("1", "Katip, bu toplantıyı dokümante et.")
    assert host.reports == 1 and "raporunu hazırlıyorum" in host.said[-1]


# ------------------------------------------------------------------ agent (Claude araçları)
class FakeBrain(FakeLLM):
    """Claude yerine: isteğe göre araç çağırır, cevap metnini döner."""

    def __init__(self, script) -> None:
        super().__init__()
        self.script = script  # (istek) -> [(araç, girdi)], cevap
        self.turns: list[dict] = []

    def available(self):
        return True

    async def run_turn(self, request, *, speaker, source, context, executor, **kw):
        self.turns.append(
            {"request": request, "speaker": speaker, "source": source, "ctx": context}
        )
        tools, reply = self.script(request)
        for name, args in tools:
            await executor(name, args)
        return reply


async def test_free_form_request_goes_to_agent_and_uses_tools(database) -> None:
    def script(request):
        assert "bir kenara yaz" in request
        return [
            ("kayda_gec", {"tur": "aksiyon", "metin": "Teklifi gönder", "sorumlu": "Ahmet"}),
            ("kayda_gec", {"tur": "karar", "metin": "Lansman 15 Kasım"}),
        ], "Kaydedildi."

    brain_ = FakeBrain(script)
    host = FakeHost(None, {"1": "Ayşe"})
    agent = MeetingAgent(host, llm_module=brain_)
    await agent.on_utterance(
        "1", "Katip, bunları bir kenara yaz: Ahmet teklifi göndersin, lansman 15 Kasım."
    )
    assert host.said == ["Kaydedildi."]
    assert brain_.turns[0]["speaker"] == "Ayşe" and brain_.turns[0]["source"] == "voice"
    notes = await repo.list_notes("m1")
    assert [(n.kind, n.text, n.author) for n in notes] == [
        ("action", "Teklifi gönder (sorumlu: Ahmet)", "Ayşe"),
        ("decision", "Lansman 15 Kasım", "Ayşe"),
    ]
    # Sonraki isteklerde model kaydedilenleri bağlamda görür.
    assert agent.notes == ["Aksiyon: Teklifi gönder (sorumlu: Ahmet)", "Karar: Lansman 15 Kasım"]


async def test_model_cannot_drive_operational_controls(database) -> None:
    order = []
    host = FakeHost(None, {"1": "Ayşe"})
    host.say = lambda text: order.append(("say", text))
    host.leave_meeting = lambda: order.append(("leave",))

    def script(request):
        return [
            ("cevap_modu", {"mod": "sesli"}),
            ("ekran_goruntusu_al", {}),
            ("ceviri_dili", {"dil_kodu": "de"}),
            ("toplantidan_ayril", {}),
        ], "Görüşmek üzere."

    agent = MeetingAgent(host, llm_module=FakeBrain(script))
    await agent.on_utterance("1", "Katip, bütçeyi açıklar mısın?")
    assert host.reply_mode == "chat" and not host.screenshots and host.lang == "en"
    assert len(order) == 1 and order[0][0] == "say"
    assert "yerine getiremedim" in order[0][1]


async def test_security_flows_never_go_to_the_model(database) -> None:
    brain_ = FakeBrain(lambda r: ([], "olmamalı"))
    host = FakeHost(VoiceIdentifier(FakeEncoder(), SR), {"1": "Ayşe Yılmaz"})
    agent = MeetingAgent(host, llm_module=brain_)
    await agent.on_utterance("1", "Katip, sesimi tanı")
    await agent.on_utterance("1", "Katip, beni unut")
    await agent.on_utterance("1", "Katip, talimat: raporları İngilizce yaz")
    assert brain_.turns == []
    assert "biyometrik" in host.said[0]


async def test_chat_free_text_to_katip_goes_to_agent_slash_stays_direct(database) -> None:
    brain_ = FakeBrain(lambda r: ([], "Bütçe cuma günü konuşuldu."))
    host = FakeHost(None, {"1": "Ayşe"})
    agent = MeetingAgent(host, llm_module=brain_)
    await agent.on_chat("Ayşe", "Katip bütçe ne zaman konuşuldu?")
    assert brain_.turns[0]["source"] == "chat" and host.said[-1] == "Bütçe cuma günü konuşuldu."
    await agent.on_chat("Ayşe", "/not demo 3 Ekim")
    assert len(brain_.turns) == 1 and host.said[-1] == "Not alındı."


async def test_silent_agent_reply_says_nothing(database) -> None:
    host = FakeHost(None, {"1": "Ayşe"})
    agent = MeetingAgent(host, llm_module=FakeBrain(lambda r: ([], "")))
    await agent.on_utterance("1", "Katip bugün çok çalıştı, değil mi arkadaşlar?")
    assert host.said == []


async def test_agent_waits_until_speaker_stops_and_merges_continuation(
    database, monkeypatch
) -> None:
    """Söz kesmeme: "Katip, karar şu:" deyip duraklayan kişinin devamı beklenir ve birleşir."""
    monkeypatch.setenv("KATIP_AGENT_TURN_WAIT_S", "0.3")
    monkeypatch.setenv("KATIP_AGENT_INCOMPLETE_WAIT_S", "0.8")
    from katip.config import get_settings

    get_settings.cache_clear()
    brain_ = FakeBrain(
        lambda r: ([("kayda_gec", {"tur": "karar", "metin": "Fiyat artışı"})], "Karar kaydedildi.")
    )
    host = FakeHost(None, {"1": "Ayşe"})
    agent = MeetingAgent(host, llm_module=brain_)

    await agent.on_utterance("1", "Katip, karar şu:")
    await asyncio.sleep(0.5)  # yarım cümle: kısa bekleme (0,3) yetmez
    assert brain_.turns == []
    agent.on_partial("1")  # hâlâ konuşuyor
    await agent.on_utterance("1", "fiyatlar yüzde on artacak.")
    await asyncio.sleep(0.1)
    assert brain_.turns == []  # az önce konuştu, bekleniyor
    await asyncio.sleep(0.6)
    assert [t["request"] for t in brain_.turns] == ["Katip, karar şu: fiyatlar yüzde on artacak."]
    assert host.said == ["Karar kaydedildi."]


async def test_agent_updates_and_deletes_records_instead_of_duplicating(database) -> None:
    steps = iter(
        [
            (
                [("kayda_gec", {"tur": "aksiyon", "metin": "Teklifleri karşılaştır"})],
                "Aksiyon eklendi.",
            ),
            (
                [("kayda_gec", {"tur": "aksiyon", "metin": "Teklifleri karşılaştır"})],
                "Aksiyon eklendi.",
            ),
        ]
    )
    brain_ = FakeBrain(lambda r: next(steps))
    host = FakeHost(None, {"1": "Ayşe"})
    agent = MeetingAgent(host, llm_module=brain_)
    await agent.on_utterance("1", "Katip, aksiyon ekle: teklifleri karşılaştır")
    await agent.on_utterance("1", "Katip, aksiyon ekle: teklifleri karşılaştır")
    first, dup = list(agent.records)
    # Model sonraki turda #no'larla günceller ve mükerreri siler.
    await agent._tool(
        "kaydi_guncelle",
        {"no": first, "metin": "Teklifleri karşılaştır (termin: 1 Ekim)"},
        author="Ayşe",
    )
    await agent._tool("kaydi_sil", {"no": dup}, author="Ayşe")
    notes = await repo.list_notes("m1")
    assert [(n.id, n.text) for n in notes] == [(first, "Teklifleri karşılaştır (termin: 1 Ekim)")]
    assert agent._context_notes() == [f"#{first} Aksiyon: Teklifleri karşılaştır (termin: 1 Ekim)"]
    with pytest.raises(ValueError):
        await agent._tool("kaydi_sil", {"no": 999}, author="Ayşe")


async def test_follow_up_without_wake_word_reaches_agent(database) -> None:
    replies = iter(["Aksiyon olarak kaydedeyim mi?", "Aksiyon eklendi."])
    brain_ = FakeBrain(lambda r: ([], next(replies)))
    host = FakeHost(None, {"1": "Ayşe", "2": "Mehmet"})
    agent = MeetingAgent(host, llm_module=brain_)
    await agent.on_utterance("1", "Katip, Teams chat'ine rapor atabilir misin?")
    await agent.on_utterance("2", "Bence de kaydedelim")  # başka kişi: takip penceresi onun değil
    assert len(brain_.turns) == 1
    await agent.on_utterance("1", "Evet, aksiyon olarak kaydet.")  # "Katip" demeden
    assert [t["request"] for t in brain_.turns][-1] == "Evet, aksiyon olarak kaydet."


async def test_wake_word_at_end_reaches_agent_in_llm_mode(database) -> None:
    brain_ = FakeBrain(lambda r: ([], "[en] Today's meeting is about sales."))
    host = FakeHost(None, {"1": "Ayşe"})
    agent = MeetingAgent(host, llm_module=brain_)
    await agent.on_utterance("1", "Bugünkü toplantının konusu satış. Bunu çevir Katip.")
    assert brain_.turns and host.said == ["[en] Today's meeting is about sales."]


async def test_off_record_speech_never_reaches_the_model(database) -> None:
    brain_ = FakeBrain(lambda r: ([], "olmamalı"))
    host = FakeHost(None, {"1": "Ayşe"})
    agent = MeetingAgent(host, llm_module=brain_)
    await agent.on_offrecord("1", "Katip, maaş zammını yüzde otuz olarak not al")
    assert brain_.turns == [] and host.said == []
    await agent.on_offrecord("1", "Katip, kayda devam")
    assert host.paused is False and brain_.turns == []


async def test_past_meetings_are_shared_only_when_everyone_is_internal(database) -> None:
    from datetime import UTC, datetime

    await repo.upsert_meeting("eski", started_at=datetime(2026, 9, 1, tzinfo=UTC))
    await repo.set_report("eski", {"title": "Maaş planı", "summary": "Gizli"}, {})
    host = FakeHost(None, {"1": "Ayşe"})
    agent = MeetingAgent(host, llm_module=FakeBrain(lambda r: ([], "")))
    host.participants_all_internal = lambda: False  # harici misafir var
    assert (await agent._turn_context("1")).past == []
    host.participants_all_internal = lambda: True
    host.verified_internal_emails = lambda: {"ayse@example.com"}
    assert (await agent._turn_context("1")).past == []
    account = await repo.set_panel_account("ayse@example.com", "hash", role="operator")
    await repo.upsert_meeting(host.session_id, owner_account_id=account.id)
    for index in range(6):
        await repo.upsert_meeting(
            f"private-{index}", report={"title": "İzinsiz", "summary": "Gizli"}
        )
    await repo.set_meeting_grant("eski", account.id, "read")
    past = (await agent._turn_context("1")).past
    assert len(past) == 1 and "Maaş planı" in past[0]
    await repo.disable_panel_account(account.id)
    assert (await agent._turn_context("1")).past == []
    await repo.set_panel_account("ayse@example.com", "hash", role="operator")
    await repo.remove_meeting_grant("eski", account.id)
    assert (await agent._turn_context("1")).past == []


def test_record_only_turns_always_get_the_standard_ack() -> None:
    from types import SimpleNamespace as NS

    from katip.agent.brain import _standard_ack

    two = [NS(name="kayda_gec", input={"tur": "aksiyon"})] * 2
    assert _standard_ack(two, "İki aksiyonu ekliyorum.") == "Aksiyon eklendi."
    mixed = [
        NS(name="kayda_gec", input={"tur": "not"}),
        NS(name="kayda_gec", input={"tur": "karar"}),
    ]
    assert _standard_ack(mixed, "ekliyorum") == "Kaydedildi."
    other = [NS(name="kayda_gec", input={"tur": "not"}), NS(name="cevap_modu", input={})]
    assert _standard_ack(other, "Sesli moda geçtim, not aldım.") == "Sesli moda geçtim, not aldım."
    update = [NS(name="kaydi_guncelle", input={"no": 2, "metin": "x"})]
    assert _standard_ack(update, "Kaydı güncelleyeceğim.") == "Kayıt güncellendi."
    update_and_add = [*update, NS(name="kayda_gec", input={"tur": "not"})]
    assert _standard_ack(update_and_add, "Güncelledim ve ekledim.") == "Güncelledim ve ekledim."


async def test_agent_can_post_to_chat(database) -> None:
    host = FakeHost(None, {"1": "Ayşe"})
    agent = MeetingAgent(host, llm_module=FakeBrain(lambda r: ([], "")))
    assert (
        await agent._tool("chate_yaz", {"metin": "1. Teklif\n2. Sunum"}, author="Ayşe")
        == "Chat'e yazıldı."
    )
    assert host.said == ["1. Teklif\n2. Sunum"]


def test_sentence_with_final_punctuation_is_not_treated_as_incomplete() -> None:
    import time as _t

    from katip.agent.controller import MeetingAgent, _Turn

    agent = MeetingAgent(FakeHost(None, {}), llm_module=FakeLLM())
    now = _t.monotonic()
    done = _Turn(
        "1", "A", ["Katip, toplantı notlarını oluştur. PDF'e Word olarak."], last_at=now - 1.6
    )
    open_ = _Turn("1", "A", ["Katip, bunu aksiyon olarak"], last_at=now - 1.6)
    colon = _Turn("1", "A", ["Katip, karar şu."], last_at=now - 1.6)
    assert agent._turn_ready(done, now)  # noktayla bitti: kısa bekleme yeter
    assert not agent._turn_ready(open_, now)  # yarım: devamı beklenir
    assert not agent._turn_ready(colon, now)  # "şu" her zaman yarım


async def test_no_follow_up_when_katip_did_not_ask(database) -> None:
    brain_ = FakeBrain(lambda r: ([], "Ekranda görev planı görünüyor."))
    host = FakeHost(None, {"1": "Ayşe"})
    agent = MeetingAgent(host, llm_module=brain_)
    await agent.on_utterance("1", "Katip, ekranda ne var?")
    await agent.on_utterance("1", "Arkada ne çalışıyor?")  # başkasına soruldu
    assert len(brain_.turns) == 1


async def test_spoken_name_wins_over_screen_name_on_enroll(database) -> None:
    ident = VoiceIdentifier(FakeEncoder(), SR)
    host = FakeHost(ident, {"1": "Mehmet Demir"})  # ekran başkasını gösteriyor
    host.human_roster = lambda: ["Deniz Kaya", "Mehmet Demir"]
    agent = MeetingAgent(host, llm_module=FakeLLM())
    await agent.on_utterance("1", "Katip, ben Deniz, sesimi kaydet.")
    assert host.said[-1].startswith("Deniz,")  # "Murat," değil


async def test_mixed_voices_block_enrollment(database, monkeypatch) -> None:
    ident = VoiceIdentifier(FakeEncoder(), SR)
    host = FakeHost(ident, {"1": "Ayşe Yılmaz"})
    agent = MeetingAgent(host, llm_module=FakeLLM())
    await agent.on_utterance("1", "Katip, sesimi tanı")
    await agent.on_utterance("1", "Onaylıyorum")
    speak(ident, "1", 3, 4.5, enrolling=True)  # bir ses
    speak(ident, "1", -7, 4.5, enrolling=True)  # başka bir ses (aynı etikete düştü)
    await agent.on_speech_observed("1")
    assert host.enrolled == [] and "birden fazla ses" in host.said[-1]
    assert ident.speech_seconds("1") == 0.0  # geçici izler atıldı, kayıt baştan


async def test_transcript_is_cached_only_during_active_conversation(database) -> None:
    host = FakeHost(None, {"1": "Ayşe"})
    lines = [f"[14:00:{i % 60:02d}] Ayşe: satır {i}" for i in range(60)]
    host.transcript_lines = lambda last=None, timestamps=False: lines[-last:] if last else lines
    agent = MeetingAgent(host, llm_module=FakeBrain(lambda r: ([], "")))
    first = await agent._turn_context("1")  # seyrek istek: önbelleksiz son satırlar
    assert first.transcript_chunks == [] and first.transcript_tail.endswith("satır 59")
    second = await agent._turn_context("1")  # hemen ardından: önbelleğe uygun bloklar
    assert len(second.transcript_chunks) == 2 and second.transcript_tail.endswith("satır 59")
    blocks = brain.TurnContext(**{**second.__dict__}).render_blocks("<istek/>")
    cached = [b for b in blocks if "cache_control" in b]
    assert len(cached) == 1 and cached[0]["cache_control"]["ttl"] == "1h"
