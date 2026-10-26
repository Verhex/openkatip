"""Toplantı agent'ı: komutları yürütür, talimat önerilerini panel onayına gönderir.

Ses ve görünen ad yetki vermez; talimatlar doğrulanmış şirket panel hesabıyla onaylanır.
Toplantı oturumunda ses kimliği ve sesli kişisel veri işlemleri kapalıdır. Eski rıza/ses
yardımcıları geçmiş veri ve birim test uyumluluğu için tutulur.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Protocol

from katip import privacy, usage
from katip.agent import brain, llm
from katip.agent.commands import (
    REQUIRES_APPROVAL,
    Command,
    Intent,
    Level,
    classify,
    detect_wake,
    normalize,
    parse_slash,
    parse_voice,
)
from katip.agent.security import ToolGate, validate_tool
from katip.config import get_settings
from katip.logging import get_logger
from katip.pipeline.floor import content_words, is_backchannel, is_stop
from katip.store import repo
from katip.store.models import ANONYMOUS_SPEAKER, Role
from katip.voice.identity import KnownVoice, VoiceIdentifier, VoiceMatch
from katip.voice.tr_normalize import normalize_tr

log = get_logger(__name__)

_PENDING_TTL_S = 90.0
_LISTEN_WINDOW_S = 12.0
# Katip bir cevap verdikten sonra aynı kişinin bu süre içindeki sözleri "Katip" denmeden de
# ona yöneltilmiş sayılır (ör. "istersen aksiyon olarak kaydedeyim" → "evet, kaydet").
# Model, konuşma Katip'e değilse sessiz kalır.
_FOLLOW_UP_S = 20.0
# Agent bağlamındaki transkript: 25 satırlık bloklar, en fazla 800 satır (≈ 80 dk).
_CHUNK_LINES = 25
_CONTEXT_LINES = 800
_CONTEXT_STEP = 200
_CACHE_ACTIVE_S = 600.0

# Cümle yarım kaldıysa ("Katip, karar şu:", "not al ve") devamı beklenir.
_INCOMPLETE_END = re.compile(r"(:|,|\b(şu|şöyle|şunu|şunları|şunlar))\s*$")
# Bunlar ancak cümle noktalamasız bitiyorsa yarım sayılır ("…Word olarak." tamamdır; 8. testte
# 6 sn beklemişti).
_WEAK_END = re.compile(
    r"\b(bunu|karar|not|not al|aksiyon|görev|ve|ile|ama|yani|için|mesela|örneğin|olarak)\s*$"
)
# Deterministik kalması gereken akışlar: model bunları yapamaz (KVKK, güvenlik).
_DETERMINISTIC = frozenset(
    {
        Intent.ENROLL,
        Intent.CONSENT,
        Intent.FORGET,
        Intent.CONFIRM_FORGET,
        Intent.CANCEL,
        Intent.ADMIN_DIRECTIVE,
        Intent.SCREENSHOT,
        Intent.REPLY_MODE,
        Intent.PAUSE,
        Intent.RESUME,
        Intent.LEAVE,
        Intent.LANGUAGE,
        Intent.REPORT,
        Intent.PRESENTATION,
        Intent.NARRATE,
        Intent.READ_SLIDE,
    }
)
_KIND_TR = {"not": "note", "karar": "decision", "aksiyon": "action"}
_KIND_LABEL = {"note": "Not", "decision": "Karar", "action": "Aksiyon"}
_KIND_ACK = {"note": "Not alındı.", "decision": "Karar kaydedildi.", "action": "Aksiyon eklendi."}

HELP_TEXT = (
    'Komutlar — sesli: "Katip, …" · chat: /komut\n'
    "• özetle (/özet) · not al: … (/not) · aksiyon ekle: … (/aksiyon) · karar: … (/karar)\n"
    "• ekran görüntüsü al (/ekran) · kayıt dışı (/dur) · kayda devam (/devam)\n"
    "• sesli cevap ver (/sesli) · yazılı cevap ver (/yazılı)\n"
    "• dili İngilizce yap (/dil en) · soru sor (/sor …) · toplantıdan ayrıl (/ayrıl)\n"
    "• Katılımcı e-postaları ve kişisel veri talepleri: yönetim paneli\n"
    "• talimat: … (yalnızca yönetici, panel onayı ile)"
    "\n• sunumu başlat (panel onayı) · sonraki slayt · önceki slayt · 3. slayta git"
    " · bu slaytı anlat · slaytı oku · sunumu yap · dur · devam et · sunumu bitir (/sunum …)"
)


class AgentHost(Protocol):
    session_id: str
    identifier: VoiceIdentifier | None

    def say(self, text: str, *, channel: str | None = None, record: bool = True) -> None: ...
    def transcript_text(self, last: int = 400) -> str: ...
    def dom_name_for_label(self, label: str | None) -> str | None: ...
    def label_for_name(self, name: str) -> str | None: ...
    def set_target_lang(self, code: str) -> None: ...
    def request_approval(self, approval_id: str, description: str) -> None: ...
    def on_person_forgotten(self, person_id: int, name: str) -> None: ...
    def on_person_enrolled(self, label: str, person_id: int, name: str) -> None: ...
    def set_reply_mode(self, mode: str) -> None: ...
    def set_paused(self, paused: bool) -> None: ...
    def request_screenshot(self, requested_by: str | None) -> None: ...
    def leave_meeting(self) -> None: ...
    def generate_report_now(self) -> None: ...
    async def presentation_action(self, action: str, page: int | None = None) -> dict: ...
    async def presentation_text(self) -> str: ...


@dataclass
class _Turn:
    """Katip'e yöneltilmiş, henüz yürütülmemiş istek: kişi susana kadar parçalar toplanır."""

    label: str | None
    author: str | None
    parts: list[str]
    listening: bool = False  # "Katip." dendikten sonra seslenmesiz gelen istek
    started: float = field(default_factory=time.monotonic)
    last_at: float = field(default_factory=time.monotonic)
    interruption: dict | None = None


@dataclass
class _Pending:
    kind: str  # "enroll" | "forget"
    label: str
    name: str
    at: float = field(default_factory=time.monotonic)
    consented: bool = False
    person_id: int | None = None

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.at > _PENDING_TTL_S


@dataclass
class Approval:
    id: str
    session_id: str
    description: str
    directive: str
    agent: MeetingAgent
    created_at: float = field(default_factory=time.time)
    command: Command | None = None


# Oturumlar arası paylaşılan onay kuyruğu (panel/API buradan onaylar).
APPROVALS: dict[str, Approval] = {}


def _is_generic(name: str | None) -> bool:
    return not name or name.startswith("Konuşmacı ") or name == ANONYMOUS_SPEAKER


# "Katip, ben Deniz, beni kaydet" / "adım Selin Arslan" / "ismim Eren" — büyük harfle başlayan
# en fazla iki kelime. Toplantı arayüzünden isim okunamadığında kaydın tek yolu budur.
_SELF_NAME = re.compile(
    # Anahtar kelime büyük/küçük harf duyarsız (Soniox cümle başını "Ben" yazıyor); isim
    # büyük harfle başlamalı.
    r"\b(?i:ben|benim adım|adım|ismim)\s+"
    r"([A-ZÇĞİÖŞÜ][a-zçğıöşü]+(?:\s+[A-ZÇĞİÖŞÜ][a-zçğıöşü]+)?)"
)
_NOT_NAMES = {"Katip", "Beni", "Sesimi", "Kaydet", "Tanı", "De", "Da"}


def name_from_speech(raw: str) -> str | None:
    m = _SELF_NAME.search(raw)
    if not m:
        return None
    words = [w for w in m.group(1).split() if w not in _NOT_NAMES]
    return " ".join(words) or None


class _Models:
    """Varsayılan model çağrıları; testler kendi sahtesini verir."""

    answer = staticmethod(llm.answer)
    summarize_so_far = staticmethod(llm.summarize_so_far)
    run_turn = staticmethod(brain.run_turn)
    available = staticmethod(brain.available)


class MeetingAgent:
    def __init__(self, host: AgentHost, *, llm_module=None) -> None:
        self.host = host
        self._llm = llm_module or _Models
        self._pending: dict[str, _Pending] = {}  # label → bekleyen akış
        self.notes: list[str] = []
        self.llm_calls = 0
        # Yalnızca "Katip" diyen kişinin bir sonraki cümlesi, seslenmesiz de komut sayılır.
        self._listening: dict[str, float] = {}
        self._turns: dict[str | None, _Turn] = {}
        self._turn_tasks: set[asyncio.Task] = set()
        self._partial_at: dict[str | None, float] = {}
        self._after_reply: list = []
        # Katip'e aldırılan kayıtlar (no → tür, metin): model "3 numaralı aksiyonun terminini
        # değiştir" diyebilsin, aynı kaydı tekrar eklemesin.
        self.records: dict[int, tuple[str, str]] = {}
        # Son cevap süreleri (sn): konuşma bitişinden cevabın çıkmasına kadar.
        self.latencies: deque[float] = deque(maxlen=20)
        self._last_agent_at = -1e9
        self._handled: dict[tuple, float] = {}
        self._interruption_turn: _Turn | None = None

    def _agent_enabled(self) -> bool:
        if get_settings().agent_mode != "llm" or not hasattr(self._llm, "run_turn"):
            return False
        available = getattr(self._llm, "available", None)
        return available() if callable(available) else True

    # ----------------------------------------------------------- girişler
    async def on_offrecord(self, label: str | None, text: str) -> None:
        """Kayıt dışıyken yalnızca sabit komutlar dinlenir; hiçbir şey modele gönderilmez."""
        command = parse_voice(text)
        if command and (
            command.intent in (Intent.RESUME, Intent.LEAVE, Intent.PRIVACY_NOTICE)
            or (command.intent is Intent.PRESENTATION and command.arg == "stop")
        ):
            await self.handle(command, label=label, author=self._speaker(label))

    async def on_utterance(self, label: str | None, text: str) -> None:
        """Her kesinleşmiş konuşma parçası için çağrılır."""
        self._claim_name(label, text)
        if label and (pending := self._pending.get(label)) and not pending.expired:
            # Bot bir onay beklerken "Katip" demeden verilen cevap da geçerli.
            reply = classify(text, "voice", text)
            if reply.intent in (Intent.CONSENT, Intent.CONFIRM_FORGET, Intent.CANCEL):
                await self.handle(reply, label=label, author=pending.name)
                return
        if (turn := self._turns.get(label)) is not None:
            # İstek sahibi konuşmaya devam ediyor: aynı isteğin devamı.
            turn.parts.append(text)
            turn.last_at = time.monotonic()
            return
        # Agent varken "Katip" cümlenin sonunda ya da ortasında da geçebilir ("bunu çevir
        # Katip"); Katip'e mi söylendiğine model karar verir. Kural modunda sıkı eşleşme.
        command = parse_voice(text, strict=not self._agent_enabled())
        if command is None and normalize(text) in {"dur", "devam"}:
            return
        listening = False
        if command is None and label and self._listening.pop(label, 0) > time.monotonic():
            # "Katip." → "Buyurun" → "şu ana kadar ne konuştuk" (seslenmeden, aynı kişi).
            command = classify(text, "voice", text)
            listening = True
        if command is None:
            return
        if command.intent not in _DETERMINISTIC:
            self._pause_presentation(command)
        if command.intent is Intent.PRESENTATION and command.arg == "pause":
            await self.handle(command, label=label, author=self._speaker(label))
            return
        turn = _Turn(label, self._speaker(label), [text], listening=listening)
        if get_settings().agent_turn_wait_s <= 0:
            await self._run_turn(turn)
            return
        # Söz kesmeme: kişi susana kadar bekle, bu arada söylediklerini isteğe ekle.
        self._turns[label] = turn
        task = asyncio.create_task(self._await_turn(label))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    def on_partial(self, label: str | None) -> None:
        """Biri konuşmaya devam ediyor (henüz kesinleşmemiş metin): bekleme uzar."""
        self._partial_at[label] = time.monotonic()

    def begin_interruption(self, label: str | None) -> None:
        turn = self._turns.get(label)
        if turn is None:
            turn = _Turn(label, self._speaker(label), [], listening=True)
            self._turns[label] = turn
            task = asyncio.create_task(self._await_turn(label))
            self._turn_tasks.add(task)
            task.add_done_callback(self._turn_tasks.discard)
        turn.interruption = {
            "finished": False,
            "label": label,
            "offrecord": getattr(self.host, "paused", False),
        }
        self._interruption_turn = turn

    def append_interruption(self, label: str | None, text: str, *, offrecord: bool = False) -> bool:
        turn = self._turns.get(label)
        if turn is None or turn.interruption is None:
            return False
        turn.parts.append(text)
        turn.last_at = time.monotonic()
        if offrecord:
            turn.interruption["offrecord"] = True
        return True

    def finish_interruption(
        self, *, spoken: str, remainder: str, hard: bool, presentation: bool
    ) -> None:
        turn, self._interruption_turn = self._interruption_turn, None
        if turn is not None and turn.interruption is not None:
            turn.interruption.update(
                finished=True,
                spoken=spoken,
                remainder=remainder,
                hard=hard,
                presentation=presentation,
                speaker=turn.author or "bilinmeyen katılımcı",
            )

    def _learn_interruption(self, turn: _Turn) -> None:
        if turn.interruption is None or turn.interruption["hard"]:
            return
        learn = getattr(self.host, "learn_interruption", None)
        if callable(learn):
            learn(turn.label)

    async def _continue_interruption(self, interruption: dict, *, how: str) -> None:
        log.info("konuşmaya devam", how=how)
        if interruption["presentation"]:
            await self.host.presentation_action("resume")
        elif interruption["remainder"]:
            self.host.say(interruption["remainder"], record=False)

    def _turn_ready(self, turn: _Turn, now: float) -> bool:
        settings = get_settings()
        if turn.interruption is not None:
            if not turn.interruption["finished"]:
                return False
            quiet = now - max(turn.last_at, self._partial_at.get(turn.label, 0.0))
            return (
                quiet >= settings.voice_resume_quiet_s
                or now - turn.started >= settings.agent_turn_max_s
            )
        if now - turn.started >= settings.agent_turn_max_s:
            return True
        quiet = now - max(turn.last_at, self._partial_at.get(turn.label, 0.0))
        joined = " ".join(turn.parts)
        rest = normalize(joined) if turn.listening else detect_wake(joined)
        last = turn.parts[-1].rstrip()
        incomplete = (
            not rest
            or last.endswith((":", ","))
            or bool(_INCOMPLETE_END.search(normalize(last)))
            or (not last.endswith((".", "!", "?")) and bool(_WEAK_END.search(normalize(last))))
        )
        needed = settings.agent_incomplete_wait_s if incomplete else settings.agent_turn_wait_s
        if not incomplete and turn.parts[-1].rstrip().endswith("?"):
            needed = min(needed, 1.0)  # soru bitti: daha kısa bekle (gecikme)
        return quiet >= needed

    async def _await_turn(self, label: str | None) -> None:
        try:
            while (turn := self._turns.get(label)) is not None:
                if self._turn_ready(turn, time.monotonic()):
                    self._turns.pop(label, None)
                    await self._run_turn(turn)
                    return
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("istek yürütülemedi", error=str(exc))

    def _speaker(self, label: str | None) -> str | None:
        speaker_name = getattr(self.host, "speaker_name", None)
        return (
            speaker_name(label) if callable(speaker_name) else self.host.dom_name_for_label(label)
        )

    def is_enrolling(self, label: str | None) -> bool:
        """Bu konuşmacı sesli açık rıza verdi ve ses kaydı tamamlanmayı mı bekliyor?"""
        pending = self._pending.get(label) if label else None
        return bool(
            pending and pending.kind == "enroll" and pending.consented and not pending.expired
        )

    async def _run_turn(self, turn: _Turn) -> None:
        raw = " ".join(turn.parts)
        if interruption := turn.interruption:
            if interruption["hard"] and (not raw or is_stop(raw)):
                return
            if not raw or is_backchannel(raw) or not content_words(raw):
                self._learn_interruption(turn)
                if not interruption["hard"]:
                    await self._continue_interruption(interruption, how="auto")
                return
            command = parse_voice(raw) or classify(raw, "voice", raw)
            if getattr(self.host, "paused", False) or interruption.get("offrecord"):
                await self.on_offrecord(turn.label, raw)
                if not interruption["hard"] and getattr(self.host, "paused", False):
                    await self._continue_interruption(interruption, how="auto")
                return
            if command.intent in _DETERMINISTIC:
                if command.intent is Intent.PRESENTATION and command.arg == "resume":
                    self._learn_interruption(turn)
                    if not interruption["presentation"]:
                        await self._continue_interruption(interruption, how="agent")
                        return
                await self.handle(command, label=turn.label, author=turn.author)
                return
            if self._agent_enabled():
                await self._agent(
                    raw,
                    label=turn.label,
                    author=turn.author,
                    source="voice",
                    spoken_at=turn.last_at,
                    interruption=interruption,
                )
            return
        command = (
            classify(raw, "voice", raw)
            if turn.listening
            else parse_voice(raw, strict=not self._agent_enabled())
        )
        if command is None:
            return
        if (
            command.intent in _DETERMINISTIC
            or command.intent is Intent.LISTEN
            or not self._agent_enabled()
        ):
            await self.handle(command, label=turn.label, author=turn.author)
            return
        await self._agent(
            raw, label=turn.label, author=turn.author, source="voice", spoken_at=turn.last_at
        )

    async def on_chat(self, author: str, text: str) -> None:
        label = self.host.label_for_name(author)
        # "/not …" gibi açık komutlar doğrudan; "Katip, …" diye yazılan serbest istekler agent'a.
        command = parse_slash(text)
        if command is None:
            # Çift tırnak içindeki örnekler ("Katip, kayda devam") istek sayılmaz; Türkçe kesme
            # işareti korunur. Agent'a ise mesajın tamamı gider.
            probe = re.sub(r'"[^"]*"|“[^”]*”|«[^»]*»', " ", text)
            command = parse_voice(probe, strict=not self._agent_enabled())
            if command is not None and command.intent not in _DETERMINISTIC:
                self._pause_presentation(command)
            if (
                command is not None
                and command.intent not in _DETERMINISTIC
                and command.intent is not Intent.LISTEN
                and self._agent_enabled()
            ):
                await self._agent(text, label=label, author=author, source="chat")
                return
        if command:
            await self.handle(replace(command, source="chat"), label=label, author=author)

    def _voice_match(self, label: str | None) -> VoiceMatch | None:
        """Bu konuşmacının sesle doğrulanmış kimliği (cümle bazlı, yakın zamanda)."""
        fn = getattr(self.host, "voice_match_for", None)
        if callable(fn):
            return fn(label)
        identifier = self.host.identifier
        return identifier.match_for(label) if (identifier and label) else None

    async def on_voice_match(self, label: str, match: VoiceMatch) -> None:
        """Kişi bu toplantıda ilk kez sesinden tanındı (oturum kişi başına bir kez çağırır)."""
        settings = get_settings()
        identifier = self.host.identifier
        greet_once = callable(getattr(self.host, "voice_match_for", None))
        if (
            settings.greeting == "chat"
            and identifier
            and (greet_once or identifier.mark_greeted(label))
        ):
            first = match.name.split()[0]
            self.host.say(f"Merhaba {first}, tekrar hoş geldin!")

    async def on_speech_observed(self, label: str) -> None:
        """Ses kimliğine yeni konuşma eklendiğinde: bekleyen kayıt tamamlanabilir mi?"""
        pending = self._pending.get(label)
        if pending and pending.kind == "enroll" and pending.consented:
            await self._try_complete_enrollment(pending)

    def _claim_name(self, label: str | None, text: str) -> None:
        """'Ben Deniz' diyen konuşmacı o kişiye aday gösterilir (ses izi varsa 1:1 doğrulama)."""
        spoken = name_from_speech(text) if label else None
        claim = getattr(self.host, "claim_name", None)
        if spoken and callable(claim):
            claim(label, spoken)

    # ------------------------------------------------------------ agent
    async def _agent(
        self,
        request: str,
        *,
        label: str | None,
        author: str | None,
        source: str,
        spoken_at: float | None = None,
        interruption: dict | None = None,
    ) -> None:
        if not self._take_llm_budget():
            self.host.say(
                "Bu toplantı için canlı istek limitine ulaşıldı; toplantı sonu raporu yine "
                "hazırlanacak."
            )
            return
        settings = get_settings()

        gate = ToolGate()
        continue_requested = False

        async def execute(name: str, args: dict) -> str:
            nonlocal continue_requested
            try:
                gate.check(name, args, brain.TOOLS)
            except (ValueError, PermissionError):
                await repo.audit(
                    "meeting-agent",
                    "agent.tool_denied",
                    meeting_id=self.host.session_id,
                    tool=name if isinstance(name, str) and len(name) < 80 else "invalid",
                )
                raise
            if name == "konusmaya_devam_et":
                if interruption is None:
                    return "Hata: devam edilecek bir kesinti yok."
                continue_requested = True
                return "Kalan kısmı sistem cevabından sonra okuyacak; metni tekrar yazma."
            return await self._tool(name, args, author=author)

        t0 = time.monotonic()
        try:
            with usage.meeting(self.host.session_id):
                reply = await self._llm.run_turn(
                    request,
                    speaker=author,
                    source=source,
                    context=await self._turn_context(label, request),
                    executor=execute,
                    reply_mode=getattr(self.host, "reply_mode", "chat"),
                    source_lang=getattr(self.host, "source_lang", "tr"),
                    directives=await repo.list_directives(),
                    company=settings.company_name,
                    **({"interruption": interruption} if interruption is not None else {}),
                )
        except Exception as exc:
            log.warning("agent başarısız", error_type=type(exc).__name__)
            reply = "Şu an bu isteği yerine getiremedim, biraz sonra tekrar dener misin?"
        done = time.monotonic()
        if spoken_at is not None:
            self.latencies.append(round(done - spoken_at, 1))
        log.info(
            "agent süresi",
            model_s=round(done - t0, 2),
            since_speech_s=round(done - spoken_at, 2) if spoken_at else None,
            replied=bool(reply),
        )
        if reply:
            self.host.say(reply)
            if label and source == "voice" and "?" in reply:
                # Yalnızca Katip bir şey sorduysa ("kaydedeyim mi?") cevabı "Katip" demeden
                # beklenir; aksi hâlde başkasına sorulan soruyu cevaplıyordu (9. test).
                self._listening[label] = time.monotonic() + _FOLLOW_UP_S
        after, self._after_reply = self._after_reply, []
        for action in after:
            action()
        if continue_requested and interruption is not None:
            learn = getattr(self.host, "learn_interruption", None)
            if callable(learn) and not interruption["hard"]:
                learn(label)
            await self._continue_interruption(interruption, how="agent")

    async def _past_meetings(self) -> list[str]:
        """Herkes şirket içindeyse doğrulanmış katılımcıların erişebildiği özetleri verir."""
        internal = getattr(self.host, "participants_all_internal", None)
        if not callable(internal) or not internal():
            return []
        emails = getattr(self.host, "verified_internal_emails", None)
        if not callable(emails):
            return []
        return [
            f"{m.started_at:%d.%m.%Y} — {m.report.get('title', '')}: {m.report.get('summary', '')}"
            for m in await repo.recent_reports(
                exclude=self.host.session_id,
                reader_emails=emails(),
                scope_meeting_id=self.host.session_id,
            )
        ]

    def _transcript_window(self) -> str:
        lines_fn = getattr(self.host, "transcript_lines", None)
        if callable(lines_fn):
            return "\n".join(lines_fn(300, timestamps=True))
        return self.host.transcript_text(300)

    def _transcript_blocks(self) -> tuple[list[str], str]:
        """Transkript: 25 satırlık değişmeyen bloklar + son satırlar (önbellek için).

        Kayan pencere önbelleği bozar: pencere yerine toplantının tamamı gönderilir; 800
        satırı aşınca başlangıç 200'er satırlık adımlarla ilerler.
        """
        lines_fn = getattr(self.host, "transcript_lines", None)
        if callable(lines_fn):
            lines = lines_fn(timestamps=True)
        else:
            lines = self.host.transcript_text(300).splitlines()
        start = 0
        if len(lines) > _CONTEXT_LINES:
            start = ((len(lines) - _CONTEXT_LINES) // _CONTEXT_STEP + 1) * _CONTEXT_STEP
        lines = lines[start:]
        complete = len(lines) // _CHUNK_LINES * _CHUNK_LINES
        chunks = ["\n".join(lines[i : i + _CHUNK_LINES]) for i in range(0, complete, _CHUNK_LINES)]
        return chunks, "\n".join(lines[complete:])

    def _context_notes(self) -> list[str]:
        lines = [f"#{no} {_KIND_LABEL[kind]}: {text}" for no, (kind, text) in self.records.items()]
        recorded = {f"{_KIND_LABEL[kind]}: {text}" for kind, text in self.records.values()}
        return lines + [n for n in self.notes if n not in recorded]

    async def _turn_context(self, label: str | None = None, request: str = "") -> brain.TurnContext:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        settings = get_settings()
        from katip.documents.service import document_evidence

        evidence = await document_evidence(self.host.session_id, request)
        past = await self._past_meetings()
        participants = getattr(self.host, "participant_lines", lambda: [])()
        now = datetime.now(ZoneInfo(settings.timezone))
        voice = getattr(self.host, "reply_mode", "chat") == "voice"
        state = {
            "saat": now.strftime("%d.%m.%Y %H:%M"),
            "cevap modu": "sesli" if voice else "yazılı",
            "kayıt": "kayıt dışı" if getattr(self.host, "paused", False) else "kayıtta",
            "çeviri dili": str(getattr(self.host, "target_lang", "-")),
        }
        voice_status = getattr(self.host, "voice_status", None)
        if callable(voice_status):
            state["konuşan kişinin ses kaydı"] = voice_status(label)
        if self.latencies:
            state["son cevap süreleri (konuşma bitişinden cevaba, sn)"] = ", ".join(
                f"{x:g}" for x in list(self.latencies)[-8:]
            )
        # Uyarlamalı önbellek: aktif sohbette (son 10 dk'da başka istek) toplantının tamamı
        # önbelleğe uygun bloklarla gider (sonraki turlar ~%70 ucuz); seyrek isteklerde son
        # 300 satır önbelleksiz gider (tek seferlik önbellek yazmanın 2x fiyatı ödenmez).
        now = time.monotonic()
        active = now - self._last_agent_at <= _CACHE_ACTIVE_S
        self._last_agent_at = now
        if active:
            chunks, tail = self._transcript_blocks()
        else:
            chunks, tail = [], self._transcript_window()
        return brain.TurnContext(
            transcript=tail,
            transcript_chunks=chunks,
            transcript_tail=tail,
            notes=self._context_notes(),
            participants=participants,
            org="\n".join(participants),
            past=past,
            documents=evidence.text,
            document_sources=evidence.sources,
            state=state,
        )

    async def _tool(self, name: str, args: dict, *, author: str | None) -> str:
        validate_tool(name, args, brain.TOOLS)
        match name:
            case "konusmaya_devam_et":
                return "Hata: devam edilecek bir kesinti yok."
            case "kayda_gec":
                kind = _KIND_TR.get(str(args.get("tur", "not")), "note")
                text = str(args.get("metin") or "").strip()
                if not text:
                    raise ValueError("kaydedilecek metin boş")
                extra = ", ".join(
                    x
                    for x in (
                        f"sorumlu: {args['sorumlu']}" if args.get("sorumlu") else "",
                        f"termin: {args['termin']}" if args.get("termin") else "",
                    )
                    if x
                )
                no = await self._save_note(kind, f"{text} ({extra})" if extra else text, author)
                return f"#{no} olarak kaydedildi. İçeriği tekrar etmeden kısaca onay ver."
            case "kaydi_guncelle":
                no = int(args.get("no", 0))
                text = str(args.get("metin") or "").strip()
                if no not in self.records or not text:
                    raise ValueError(f"#{no} numaralı kayıt yok ya da metin boş")
                kind = _KIND_TR.get(str(args.get("tur") or ""), self.records[no][0])
                old = f"{_KIND_LABEL[self.records[no][0]]}: {self.records[no][1]}"
                await repo.update_note(self.host.session_id, no, text, kind)
                self.records[no] = (kind, text)
                self.notes = [f"{_KIND_LABEL[kind]}: {text}" if n == old else n for n in self.notes]
                return f"#{no} güncellendi."
            case "kaydi_sil":
                no = int(args.get("no", 0))
                if no not in self.records:
                    raise ValueError(f"#{no} numaralı kayıt yok")
                kind, text = self.records.pop(no)
                await repo.delete_note(self.host.session_id, no)
                self.notes = [n for n in self.notes if n != f"{_KIND_LABEL[kind]}: {text}"]
                return f"#{no} silindi."
            case "ekran_goruntusu_al":
                self.host.request_screenshot(author)
                return "İstendi; görüntü alınınca sistem onay verecek."
            case "cevap_modu":
                mode = "voice" if args.get("mod") == "sesli" else "chat"
                self.host.set_reply_mode(mode)
                return f"Cevap modu artık {'sesli' if mode == 'voice' else 'yazılı'}."
            case "kayit_disi":
                paused = bool(args.get("acik"))
                self.host.set_paused(paused)
                return "Kayıt dışı açıldı." if paused else "Kayda dönüldü."
            case "rapor_hazirla":
                self.host.generate_report_now()
                return "Ara rapor hazırlanıyor; birkaç dakika içinde panelde PDF ve Word çıkar."
            case "ceviri_dili":
                code = str(args.get("dil_kodu") or "").strip().lower()
                if not re.fullmatch(r"[a-z]{2}", code):
                    raise ValueError(f"geçersiz dil kodu: {code}")
                self.host.set_target_lang(code)
                return f"Çeviri dili: {code}"
            case "kvkk_bilgilendirmesi":
                self._privacy_notice(voice=bool(args.get("sesli")))
                return "Aydınlatma metni chat'e yazıldı" + (
                    " ve sesli okundu." if args.get("sesli") else "."
                )
            case "chate_yaz":
                text = str(args.get("metin") or "").strip()
                if not text:
                    raise ValueError("metin boş")
                try:
                    self.host.say(text, channel="chat")
                except TypeError:  # kanal desteklemeyen basit host (testler)
                    self.host.say(text)
                return "Chat'e yazıldı."
            case "toplantidan_ayril":
                self._after_reply.append(self.host.leave_meeting)
                return "Cevabından sonra toplantıdan ayrılacaksın."
        raise ValueError(f"bilinmeyen araç: {name}")

    async def _save_note(self, kind: str, text: str, author: str | None) -> int:
        no = await repo.add_note(self.host.session_id, kind, text, author)
        self.records[no] = (kind, text)
        self.notes.append(f"{_KIND_LABEL[kind]}: {text}")
        return no

    def _privacy_notice(self, *, voice: bool) -> None:
        """Aydınlatma metni her zaman chat'e yazılı; istenirse ayrıca sesli okunur."""
        try:
            self.host.say(privacy.privacy_notice_chat(), channel="chat")
            if voice:
                self.host.say(privacy.privacy_notice_spoken(), channel="voice", record=False)
        except TypeError:  # kanal desteklemeyen basit host (testler)
            self.host.say(privacy.privacy_notice_chat())

    # ------------------------------------------------------------ yürütme
    async def handle(self, cmd: Command, *, label: str | None, author: str | None) -> None:
        now = time.monotonic()
        self._handled = {key: at for key, at in self._handled.items() if now - at < 8}
        key = (cmd.intent, cmd.arg, normalize(cmd.text), label, author)
        if key in self._handled:
            log.info("yinelenen komut yok sayıldı", intent=cmd.intent, label=label)
            return
        self._handled[key] = now
        already = None
        if cmd.intent is Intent.PAUSE and getattr(self.host, "paused", False):
            already = "Zaten kayıt dışındayız."
        elif cmd.intent is Intent.RESUME and not getattr(self.host, "paused", False):
            already = "Kayıt zaten açık."
        elif cmd.intent is Intent.REPLY_MODE and getattr(self.host, "reply_mode", None) == cmd.arg:
            already = (
                "Zaten sesli cevap veriyorum."
                if cmd.arg == "voice"
                else "Zaten cevaplarımı chat'e yazıyorum."
            )
        if already:
            self.host.say(already)
            return
        log.info("komut", intent=cmd.intent, source=cmd.source, label=label)
        self._pause_presentation(cmd)
        with usage.meeting(self.host.session_id):
            await self._handle(cmd, label=label, author=author)

    def _pause_presentation(self, cmd: Command) -> None:
        playback = getattr(self.host, "presentation", None)
        if playback and not (cmd.intent is Intent.PRESENTATION and cmd.arg == "resume"):
            playback.pause()

    async def _handle(self, cmd: Command, *, label: str | None, author: str | None) -> None:
        try:
            if cmd.level is Level.ADMIN:
                await self._admin(cmd, label, author)
            elif cmd.level is Level.SELF:
                if getattr(self.host, "_voice_enabled", True) is False:
                    self.host.say(
                        "Sesle kimlik tanıma kapalı. Kimlik ve silme işlemleri "
                        "şirket hesabıyla yönetim panelinden yapılır."
                    )
                else:
                    await self._self_service(cmd, label, author)
            else:
                await self._public(cmd, label, author)
        except Exception as exc:
            log.exception("komut başarısız", intent=cmd.intent, error=str(exc))
            self.host.say("Şu an bu isteği yerine getiremedim, biraz sonra tekrar dener misin?")

    # ------------------------------------------------------------ PUBLIC
    async def _public(self, cmd: Command, label: str | None, author: str | None) -> None:
        settings = get_settings()
        match cmd.intent:
            case Intent.HELP:
                self.host.say(HELP_TEXT)
            case Intent.LISTEN:
                if label:
                    self._listening[label] = time.monotonic() + _LISTEN_WINDOW_S
                self.host.say("Buyurun, dinliyorum.")
            case Intent.PRIVACY_NOTICE:
                self._privacy_notice(voice=cmd.source == "voice")
            case Intent.REPORT:
                self.host.generate_report_now()
                self.host.say(
                    "Şu ana kadarki toplantının raporunu hazırlıyorum; birkaç dakika içinde "
                    "panelde PDF ve Word olarak hazır olur. Toplantı bitince son hâli de "
                    "otomatik hazırlanır."
                )
            case Intent.CANCEL:
                if label and self._pending.pop(label, None):
                    self.host.say("Tamam, vazgeçtim.")
            case Intent.NOTE | Intent.ACTION | Intent.DECISION:
                if not cmd.text:
                    self.host.say("Neyi not alayım?")
                    return
                kind = {
                    Intent.NOTE: "note",
                    Intent.ACTION: "action",
                    Intent.DECISION: "decision",
                }[cmd.intent]
                await self._save_note(kind, cmd.text, author)
                # İçerik tekrar edilmez: yalnızca kısa onay.
                self.host.say(_KIND_ACK[kind])
            case Intent.REPLY_MODE:
                mode = cmd.arg or "chat"
                self.host.set_reply_mode(mode)
                self.host.say(
                    "Tamam, bundan sonra sesli cevap vereceğim."
                    if mode == "voice"
                    else "Tamam, bundan sonra cevaplarımı chat'e yazacağım."
                )
            case Intent.SCREENSHOT:
                self.host.request_screenshot(author)
            case Intent.PAUSE:
                self.host.set_paused(True)
                self.host.say(
                    "Kayıt dışı: bu kısmı toplantı kaydına ve raporuna eklemiyorum. "
                    'Devam etmek için "Katip, kayda devam" deyin.'
                )
            case Intent.RESUME:
                self.host.set_paused(False)
                self.host.say("Kayda devam ediyorum.")
            case Intent.LEAVE:
                self.host.say("Toplantıdan ayrılıyorum; rapor birkaç dakika içinde hazır olacak.")
                self.host.leave_meeting()
            case Intent.LANGUAGE:
                if not cmd.arg:
                    self.host.say("Hangi dile çevireyim? Örnek: /dil en")
                    return
                self.host.set_target_lang(cmd.arg)
                self.host.say(f"Tamam, çeviri dili artık: {cmd.arg}")
            case Intent.SUMMARY | Intent.ASK if not self._take_llm_budget():
                self.host.say(
                    "Bu toplantı için soru limitine ulaşıldı; toplantı sonu raporu yine "
                    "hazırlanacak."
                )
            case Intent.SUMMARY:
                text = await self._llm.summarize_so_far(
                    transcript=self.host.transcript_text(),
                    notes=self.notes,
                    directives=await repo.list_directives(),
                    company=settings.company_name,
                )
                self.host.say(text)
            case Intent.ASK:
                past = await self._past_meetings()
                text = await self._llm.answer(
                    cmd.text,
                    transcript=self.host.transcript_text(),
                    notes=self.notes,
                    org="\n".join(getattr(self.host, "participant_lines", lambda: [])()),
                    past=past,
                    directives=await repo.list_directives(),
                    company=settings.company_name,
                )
                self.host.say(text)

    def _take_llm_budget(self) -> bool:
        if self.llm_calls >= get_settings().max_agent_calls_per_meeting:
            return False
        self.llm_calls += 1
        return True

    # -------------------------------------------------------------- SELF
    async def _self_service(self, cmd: Command, label: str | None, author: str | None) -> None:
        identifier = self.host.identifier
        match cmd.intent:
            case Intent.ENROLL:
                if identifier is None:
                    self.host.say("Ses tanıma bu kurulumda kapalı.")
                    return
                known = self._voice_match(label)
                if known is not None:
                    self.host.say(f"Seni zaten tanıyorum, {known.name.split()[0]}.")
                    return
                # Kişinin kendi söylediği ad ("ben Deniz") ekrandaki addan önce gelir:
                # aynı cihazdan konuşan iki kişide ekran başkasını gösterebiliyor (9. test).
                spoken = name_from_speech(cmd.raw)
                if spoken:
                    # Şemadaki kişiyle ya da toplantıdaki tam adla eşleştir:
                    # "Deniz" → "Deniz Kaya": şemadaki tam ad tercih edilir.
                    person = await repo.find_person_by_name(spoken)
                    name = person.display_name if person else self._roster_full_name(spoken)
                else:
                    name = self.host.dom_name_for_label(label) or author
                if label is None or _is_generic(name):
                    self.host.say(
                        'Adını anlayamadım. "Katip, ben Ayşe Yılmaz, beni kaydet" gibi '
                        "adını söyleyerek tekrar dener misin?"
                    )
                    return
                self._pending[label] = _Pending("enroll", label, name)
                self.host.say(privacy.consent_text(name.split()[0]))

            case Intent.CONSENT:
                pending = self._pending.get(label) if label else None
                if not pending or pending.kind != "enroll" or pending.expired:
                    return  # beklenmeyen "onaylıyorum": yok say
                person = await repo.ensure_person(pending.name)
                await repo.add_consent(
                    person.id,
                    "voiceprint",
                    f"{cmd.source}: '{cmd.raw.strip()}' | açık rıza metni sürümü "
                    f"{privacy.CONSENT_VERSION}",
                    self.host.session_id,
                )
                pending.consented, pending.person_id, pending.at = True, person.id, time.monotonic()
                if not await self._try_complete_enrollment(pending):
                    self.host.say("Teşekkürler. Biraz daha konuştukça sesini öğrenip kaydedeceğim.")

            case Intent.FORGET:
                if cmd.source != "voice":
                    self.host.say(
                        'Güvenliğin için "beni unut" komutunu sesli ver; ya da yöneticiden '
                        "panelden silmesini iste."
                    )
                    return
                voice = self._voice_match(label) if identifier else None
                if voice is None:
                    self.host.say(
                        "Seni sesinden tanıyamadım; kayıtlı bir ses izin yoksa silinecek bir "
                        "şey de yok. Yine de silinmesini istediğin kayıtlar için yöneticiye "
                        "başvurabilirsin."
                    )
                    return
                self._pending[label] = _Pending(
                    "forget", label, voice.name, person_id=voice.person_id
                )
                self.host.say(
                    f"{voice.name.split()[0]}, ses izini ve profilini silip geçmiş kayıtlarda "
                    'adını anonimleştireceğim. Emin misin? "Evet, unut" de.'
                )

            case Intent.CONFIRM_FORGET:
                pending = self._pending.get(label) if label else None
                voice = self._voice_match(label) if identifier else None
                if (
                    not pending
                    or pending.kind != "forget"
                    or pending.expired
                    or voice is None
                    or voice.person_id != pending.person_id
                ):
                    return
                self._pending.pop(label, None)
                completed = await repo.forget_person(pending.person_id, requested_by="voice")
                identifier.forget(pending.person_id)
                self.host.on_person_forgotten(pending.person_id, pending.name)
                if not completed:
                    self.host.say(
                        "Silme işlemi henüz tamamlanamadı. Yöneticiye bildirildi; "
                        "kalan kayıtlar otomatik olarak yeniden silinecek."
                    )
                    return
                self.host.say(
                    'Tamam, seni unuttum. Geçmiş kayıtlarda artık "Anonim katılımcı" olarak '
                    "geçiyorsun."
                )

    def _roster_full_name(self, spoken: str) -> str:
        roster = getattr(self.host, "human_roster", lambda: [])()
        wanted = spoken.casefold()
        hits = [
            n for n in roster if n.casefold() == wanted or n.casefold().startswith(wanted + " ")
        ]
        return hits[0] if len(hits) == 1 else spoken

    async def _try_complete_enrollment(self, pending: _Pending) -> bool:
        identifier = self.host.identifier
        if identifier is None or not identifier.ready_to_enroll(pending.label):
            return False
        similarity, minor_share = identifier.split(pending.label)
        settings = get_settings()
        log.info(
            "ses kaydı tutarlılığı",
            label=pending.label,
            cluster_similarity=round(similarity, 3),
            minor_share=round(minor_share, 3),
        )
        if (
            similarity < settings.voice_mixed_max_similarity
            and minor_share >= settings.voice_mixed_min_share
        ):
            identifier.reset(pending.label)
            pending.at = time.monotonic()
            self.host.say(
                f"{pending.name.split()[0]}, kayıt sırasında birden fazla ses duydum; ses izini "
                "kaydetmedim. Lütfen birkaç cümle yalnız sen konuş, yeniden deneyeceğim."
            )
            return False
        centroid = identifier.centroid(pending.label)
        assert pending.person_id is not None and centroid is not None
        await repo.save_voiceprint(
            pending.person_id,
            centroid,
            model=identifier.encoder.name,
            speech_seconds=identifier.speech_seconds(pending.label),
            samples=0,
        )
        person = await repo.get_person(pending.person_id)
        role = person.role if person else Role.MEMBER
        identifier.register(
            pending.label, KnownVoice(pending.person_id, pending.name, role, centroid)
        )
        self._pending.pop(pending.label, None)
        self.host.on_person_enrolled(pending.label, pending.person_id, pending.name)
        self.host.say(f"{pending.name.split()[0]}, sesini kaydettim. Artık seni tanıyacağım.")
        return True

    # ------------------------------------------------------------- ADMIN
    async def _admin(self, cmd: Command, label: str | None, author: str | None) -> None:
        if cmd.intent in {Intent.PRESENTATION, Intent.NARRATE, Intent.READ_SLIDE}:
            action = (cmd.arg or "").partition(":")[0]
            active = bool(getattr(self.host, "presentation_state", {}).get("page"))
            # Paylaşımı başlatmak içeriği herkese gösterir: panel onayı. Onaylanmış sunumda
            # gezinme ve bitirme onaysız; bitirmeyi herkes yapabilir (toplantıdan çık gibi).
            if (
                action == "stop"
                or (active and cmd.intent in {Intent.NARRATE, Intent.READ_SLIDE})
                or (active and action in {"next", "prev", "goto", "pause", "resume"})
            ):
                with usage.meeting(self.host.session_id):
                    await self._run_presentation(cmd)
                return
            if cmd.intent in {Intent.NARRATE, Intent.READ_SLIDE} or action not in {"start", "auto"}:
                self.host.say("Önce sunumu başlatın: Katip, sunumu başlat.")
                return
            pending = [a for a in APPROVALS.values() if a.session_id == self.host.session_id]
            for approval in pending:
                if approval.command and time.time() - approval.created_at > _PENDING_TTL_S:
                    APPROVALS.pop(approval.id, None)
            pending = [a for a in APPROVALS.values() if a.session_id == self.host.session_id]
            if len(pending) >= 20 or any(a.command == cmd for a in pending):
                self.host.say("Sunum isteği zaten bekliyor veya onay kuyruğu dolu.")
                return
            approval = Approval(
                uuid.uuid4().hex[:10],
                self.host.session_id,
                f"Sunum: {cmd.text}",
                "",
                self,
                command=cmd,
            )
            APPROVALS[approval.id] = approval
            self.host.request_approval(approval.id, approval.description)
            self.host.say("Sunum isteğini aldım; yönetim panelinden onay gerekiyor.")
            return
        # Ses/görünen ad/e-posta toplantı içinden geldiği için yetki vermez.
        # Herkes öneri bırakabilir; uygulama yalnız kimliği doğrulanmış panel onayıyla.
        if not cmd.text:
            self.host.say(
                'Talimatını söyler misin? Örnek: "Katip, talimat: raporları İngilizce hazırla"'
            )
            return

        if cmd.intent in REQUIRES_APPROVAL:
            pending = [a for a in APPROVALS.values() if a.session_id == self.host.session_id]
            if any(a.directive == cmd.text for a in pending):
                self.host.say("Bu talimat zaten panel onayı bekliyor.")
                return
            if len(pending) >= 20:
                self.host.say(
                    "Bekleyen talimatlar için önce yönetim panelinden onay veya ret gerekiyor."
                )
                return
            approval = Approval(
                id=uuid.uuid4().hex[:10],
                session_id=self.host.session_id,
                description=f"Yönetici talimatı: {cmd.text}",
                directive=cmd.text,
                agent=self,
            )
            APPROVALS[approval.id] = approval
            self.host.request_approval(approval.id, approval.description)
            await repo.audit(
                "system",
                "directive_proposed",
                meeting_id=self.host.session_id,
                approval_id=approval.id,
            )
            self.host.say("Talimatını aldım; yürürlüğe girmesi için panelden onaylaman gerekiyor.")

    async def apply_approval(self, approval: Approval) -> None:
        if approval.command:
            if time.time() - approval.created_at > _PENDING_TTL_S:
                self.host.say("Sunum isteğinin süresi doldu; komutu tekrar verin.")
                return
            with usage.meeting(self.host.session_id):
                await self._run_presentation(approval.command)
            return
        await repo.add_directive(approval.directive, approval.session_id)
        self.host.say(f"Talimat onaylandı ve kaydedildi: {approval.directive}")

    async def _run_presentation(self, cmd: Command) -> None:
        if cmd.intent in {Intent.NARRATE, Intent.READ_SLIDE}:
            from katip.pipeline.presentation import read_slide

            if getattr(self.host, "paused", False) or getattr(self.host, "mode", "") == "kayit":
                self.host.say("Sesli anlatım kapalı; önce kayda devam edin.", channel="chat")
                return
            text = await self.host.presentation_text()
            if not text:
                self.host.say("Bu slaytta okunabilir metin yok veya sunum başlamadı.")
                return
            if cmd.intent is Intent.READ_SLIDE:
                self.host.say("[tr] " + read_slide(text), channel="voice")
            else:
                playback = self.host.presentation
                try:
                    text = await playback.narration(self.host.presentation_state["page"])
                except ValueError as exc:
                    self.host.say(str(exc))
                    return
                self.host.say("[tr] " + text, channel="voice")
            return
        action, _, page = (cmd.arg or "").partition(":")
        result = await self.host.presentation_action(action, int(page) if page else None)
        if result.get("error"):
            self.host.say(result["error"])
        elif action == "stop":
            self.host.say("Sunum bitti.")
        elif action == "start":
            self.host.say(normalize_tr(f"Sunumu başlatıyorum; {result['pages']} slayt var."))
        elif (
            action in {"next", "prev", "goto"}
            and getattr(self.host, "reply_mode", "voice") == "chat"
        ):
            self.host.say(f"Slayt {result['page']} / {result['pages']}", channel="chat")


async def approve(approval_id: str) -> bool:
    approval = APPROVALS.pop(approval_id, None)
    if approval is None:
        return False
    await approval.agent.apply_approval(approval)
    return True


def reject(approval_id: str) -> bool:
    approval = APPROVALS.pop(approval_id, None)
    if approval is None:
        return False
    approval.agent.host.say(
        "Sunum isteği reddedildi." if approval.command else "Yönetici talimatı reddedildi."
    )
    return True


__all__ = ["APPROVALS", "MeetingAgent", "approve", "reject", "normalize"]
