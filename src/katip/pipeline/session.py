"""Oturum orkestrasyonu: karışık ses → STT (+çeviri) → kişi → panel, agent, kayıt.

Toplantı başına tek STT akışı açılır. Meet uzak sesi 3 sanal track'te gönderir ve
bu track'lerin kaynağı en yüksek sesle konuşanlara göre sürekli değişir; yani
track = kişi değildir. Üstelik Soniox stream süresini faturaladığı için track başına
ayrı akış maliyeti katlar. Tarayıcı track'leri tek akışta karıştırır, konuşmacıları
Soniox'un diarization'ı ayırır.

Konuşmacı DOM konuşma göstergesinden eşleştirilir; belirsizse "Konuşmacı N" kalır.
Katılımcı toplantıya özel e-posta listesine veya paneldeki seçime bağlanır.
Ses kimliği kapalıdır. Eski ses kimliği yardımcıları eski veri/test uyumluluğu için
tutulur; toplantı oturumu model yüklemez veya biyometrik iz çıkarmaz.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

from katip import usage
from katip.agent.controller import MeetingAgent
from katip.async_utils import LoopLocal, raise_if_cancelling, wait_tasks
from katip.config import get_settings
from katip.events import PanelEvent, RosterUpdate, Segment, SessionState, Translation
from katip.identity import ParticipantIdentity, company_email, participant_name
from katip.logging import get_logger
from katip.pipeline.presentation import PresentationHost, PresentationPlayback
from katip.pipeline.speakers import SpeakerMapper
from katip.pipeline.speech import SpeechPlayback, _Speech, _squash
from katip.pipeline.stt.base import StreamExpired, STTProvider, Utterance, build_provider
from katip.pipeline.translate import Translator
from katip.providers import get_registry
from katip.providers.audio import ReplayBuffer, deduplicate
from katip.providers.resilience import transient
from katip.store import repo
from katip.store.models import ANONYMOUS_SPEAKER, KATIP_SPEAKER
from katip.teams.routing import ChatRouter
from katip.voice.identity import KnownVoice, VoiceIdentifier
from katip.voice.ring import AudioRing

log = get_logger(__name__)

# Toplantı sonu raporları arka planda üretilir; referans tutulmazsa görev GC'ye gider.
_report_tasks: LoopLocal[set[asyncio.Task]] = LoopLocal(set)
_SHUTDOWN_TIMEOUT_S = 10.0
_CANCEL_TIMEOUT_S = 1.0

_LATE_NOTICE_GAP_S = 120.0
_QUEUE_MAX_FRAMES = 400  # ~25 sn (1024 örnek/frame @16 kHz); dolarsa en eski düşer
_MAX_STT_RESTARTS = 5


def _build_tts():
    return get_registry().tts()


def _build_encoder():
    from katip.voice.encoder import WeSpeakerEncoder

    return WeSpeakerEncoder()


class MeetingSession(SpeechPlayback, PresentationHost):
    def __init__(
        self,
        session_id: str,
        *,
        source_lang: str | None = None,
        target_lang: str | None = None,
        stt: STTProvider | None = None,
        persist: bool = True,
        voice: bool | None = None,
        mode: str | None = None,
    ) -> None:
        settings = get_settings()
        self._persist = persist
        self.session_id = session_id
        # "tam": agent + ses + çeviri. "kayit": gün boyu sessiz kayıt (ses ve çeviri kapalı).
        self.mode = mode or settings.default_mode
        self.source_lang = source_lang or settings.source_lang
        if target_lang is not None:
            self.target_lang = target_lang or None
        else:
            self.target_lang = None if self.mode == "kayit" else settings.target_lang
        self.state = SessionState.PENDING
        self.segments: dict[str, Segment] = {}

        self._stt = stt or build_provider()
        self._provider_registry = get_registry()
        self._stt_index = 0
        self._replay_ring = ReplayBuffer(settings.sample_rate)
        self._replay_frames: list[bytes] = []
        self._replaying = False
        self._final_samples = 0
        self._replay_boundary = 0.0
        self._replay_previous = ""
        self._next_stream_sample: int | None = None
        self._stt_probe = None
        self._audio_cursor = 0
        self._last_audio_loss = -1e9
        # Sağlayıcı çeviriyi kendisi vermiyorsa kesinleşen parçaları Claude çevirir.
        self._translator = (
            None
            if self._stt.translates
            else Translator(source_lang=self.source_lang, target_lang=self.target_lang)
        )
        self._speakers = SpeakerMapper()
        self._audio: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_QUEUE_MAX_FRAMES)
        self._ring = AudioRing(settings.sample_rate)
        self._stt_task: asyncio.Task | None = None
        self._shutdown_task: asyncio.Task | None = None
        self._closing = False
        self._restart_stt = False
        self._stream_started_at = time.monotonic()
        # Akışlar arası süreklilik (Soniox 300 dk'da yenilenir, dil değişince yeniden açılır):
        # zamanlar ilk akışa göre mutlak, konuşmacı etiketleri akış başına ayrı numaralanır.
        self._first_stream_at: float | None = None
        self._stream_no = 0
        self._stream_offset = 0.0
        self._label_map: dict[tuple[int, str], str] = {}
        self._label_counter = 0
        self._seg_stream: dict[str, tuple[int, float]] = {}
        self._alone_since: float | None = None
        self._claims: dict[str, str] = {}  # etiket → kendini tanıttığı ad
        # Cümle bazlı kimlik: cümle anındaki "konuşuyor" kutucuğu, sesle doğrulanan kişi.
        self._seg_dom: dict[str, str | None] = {}
        self._seg_person: dict[str, int] = {}
        self._label_voice: dict[str, tuple] = {}  # etiket → (son doğrulanan kişi, zaman)
        self._greeted_people: set[int] = set()
        self._enroll_segments: dict[str, list[str]] = defaultdict(list)
        self._subscribers: set[asyncio.Queue[PanelEvent]] = set()
        self._roster: list[str] = []
        self._background: set[asyncio.Task] = set()
        # Kesinleşen segmentler sırayla, tek yazıcı görevle depoya yazılır.
        self._persist_queue: asyncio.Queue[tuple[Segment, int | None] | None] = asyncio.Queue()
        self._writer: asyncio.Task | None = None
        # Kesinleşen parçalar için ses kimliği + agent işçisi.
        self._final_queue: asyncio.Queue[tuple | None] = asyncio.Queue()
        self._final_worker: asyncio.Task | None = None
        self._label_segments: dict[str, list[str]] = defaultdict(list)
        self._suppressed_names: set[str] = set()

        # Bota gidecek komutlar (chat'e yaz, konuş); ingest soketi bunları sayfaya iletir.
        self.outbox: asyncio.Queue[dict] = asyncio.Queue(maxsize=2000)
        self.presentation_state = {
            "page": 0,
            "pages": 0,
            "mode": "manual",
            "state": "off",
            "error": None,
        }
        self.presentation = PresentationPlayback(self)
        self._presentation_lock = asyncio.Lock()
        self._presentation_waiters: dict[str, asyncio.Future] = {}
        self._presentation_document_id: str | None = None
        self._presentation_pending_document: tuple[str, str] | None = None
        self._speech_lock = asyncio.Lock()
        self._speak_task: asyncio.Task | None = None
        self._tts = None
        self.reply_mode: str = "chat" if self.mode == "kayit" else settings.reply_mode
        # Kayıt dışı: konuşmalar yazıya dökülmez, saklanmaz, çevrilmez; yalnızca
        # "Katip, kayda devam" komutu için dinlenir.
        self.paused = False
        self._offrecord_ids: set[str] = set()
        self._offrecord_done: set[str] = set()
        self._screenshot_waiters: dict[str, tuple[asyncio.Future, str | None]] = {}
        self._screenshot_count = 0
        self._created_at = time.monotonic()
        self._diagnosed = False
        self._last_speech = time.monotonic()
        # Son kesinleşmemiş konuşma anı: kanal meşgul mü (sesli cevap araya girmesin)?
        self._last_partial_at = 0.0
        self._reply_count = 0
        # Toplantıdaki katılımcılar: ad → e-posta (şemadan eşleşirse)
        self._participants: dict[str, str | None] = {}
        self._identities: dict[str, tuple[ParticipantIdentity, str]] = {}
        self._participant_people: dict[str, int] = {}
        self._identity_lock = asyncio.Lock()
        self._internal: dict[str, bool] = {}  # katılımcı şirket şemasında mı
        self._announced = False
        self._last_interim: tuple[float, int] | None = None
        self._interim_task: asyncio.Task | None = None
        # Toplantıda çalınan son sesli cevabın biteceği an (tarayıcı sesi tamponlayıp çalıyor).
        self._speech_until = 0.0
        # Kendi chat mesajlarımız: toplantı chat'inden geri okununca komut sanılmasın.
        self._sent_chat: deque[tuple[float, str]] = deque()
        self._seen_chat: dict[tuple[str, str], float] = {}
        self._speech: _Speech | None = None
        self._speech_interrupted = False
        self._label_activity: dict[str | None, float] = {}
        self._ambient_labels: set[str | None] = set()
        self._noisy_interruptions: dict[str | None, int] = defaultdict(int)
        self._echo_ids: set[str] = set()
        self.chat_router = ChatRouter(session_id, self._browser_chat, self._spawn)
        # Kesinleşen her parçanın duvar saati: agent "14:12'de ne konuştuk" sorusunu cevaplasın.
        self._seg_wall: dict[str, float] = {}
        self._last_late_notice = 0.0

        self.identifier: VoiceIdentifier | None = None
        # Eski Railway değişkeni veya çağrıdaki voice=True özelliği yeniden açamaz.
        # voice parametresi eski çağıranlarla uyumluluk için tutulur.
        self._voice_enabled = False
        self._voice_ready: asyncio.Task | None = None
        self.agent = MeetingAgent(self)

    # ------------------------------------------------------------- abonelik
    def subscribe(self) -> asyncio.Queue[PanelEvent]:
        q: asyncio.Queue[PanelEvent] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[PanelEvent]) -> None:
        self._subscribers.discard(q)

    def _publish(self, event: PanelEvent) -> None:
        for q in list(self._subscribers):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(event)

    def final_segments(self) -> list[Segment]:
        return sorted((s for s in self.segments.values() if s.is_final), key=lambda s: s.start_s)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # ------------------------------------------------------------ bot olayları
    def set_state(self, state: SessionState, detail: str | None = None) -> None:
        first_active = state == SessionState.ACTIVE and self.state != SessionState.ACTIVE
        self.state = state
        self._publish(PanelEvent(kind="status", state=state, text=detail))
        log.info("oturum durumu", session_id=self.session_id, state=state, detail=detail)
        if first_active and not self._diagnosed:
            self._diagnosed = True
            if get_settings().auto_diagnose:
                self._spawn(self._auto_diagnose())
            self._spawn(self._announce())
            self._last_speech = time.monotonic()
            self._spawn(self._idle_watchdog())

    @property
    def max_minutes(self) -> int:
        s = get_settings()
        return s.record_max_minutes if self.mode == "kayit" else s.max_meeting_minutes

    def _idle_limit_s(self) -> float:
        s = get_settings()
        minutes = s.record_idle_leave_minutes if self.mode == "kayit" else s.idle_leave_minutes
        return minutes * 60

    async def _idle_watchdog(self) -> None:
        """Dakikada bir: uzun sessizlikte ya da toplantıda yalnız kalınca ayrıl; Soniox
        bağlantısını 300 dk sınırından önce yenile."""
        interval = min(60.0, self._idle_limit_s() / 4 or 60.0)
        while self.state == SessionState.ACTIVE:
            await asyncio.sleep(interval)
            if self._watchdog_tick(time.monotonic()):
                return

    def _watchdog_tick(self, now: float) -> bool:
        """Bir bekçi turu; toplantıdan ayrılma başlatıldıysa True."""
        s = get_settings()
        silent_for = now - self._last_speech
        limit = self._idle_limit_s()
        if limit > 0 and silent_for >= limit:
            log.info("uzun sessizlik, toplantıdan ayrılıyor", session_id=self.session_id)
            self.say(
                "Bir süredir kimse konuşmuyor; toplantıdan ayrılıyorum. Rapor birkaç dakika "
                "içinde hazır olacak."
            )
            self.leave_meeting()
            return True
        alone_s = s.alone_leave_minutes * 60
        if (
            alone_s > 0
            and self._alone_since is not None
            and now - self._alone_since >= alone_s
            and silent_for >= alone_s
        ):
            log.info("toplantıda kimse kalmadı, ayrılıyor", session_id=self.session_id)
            self.leave_meeting()
            return True
        rotate_s = s.stt_rotate_minutes * 60
        if getattr(self._stt, "name", None) == "assemblyai":
            # AssemblyAI üç saatlik sınırı aşmadan aynı sağlayıcıda yenilenir.
            rotate_s = min(rotate_s or 170 * 60, 170 * 60)
        if (
            rotate_s > 0
            and self._stt_task is not None
            and not self._stt_task.done()
            and now - self._stream_started_at >= rotate_s
        ):
            # Akışı kapatıp hemen yeniden aç (dil değişimiyle aynı yol); kısa bir boşluk olur.
            log.info("stt akışı yenileniyor (bağlantı süresi sınırı)", session_id=self.session_id)
            self._restart_stt = True
            with contextlib.suppress(asyncio.QueueFull):
                self._audio.put_nowait(None)
        return False

    async def _announce(self) -> None:
        """KVKK: toplantıya girince aydınlatma metni chat'e yazılır (istenirse sesli de okunur)."""
        from katip.privacy import privacy_notice_chat, privacy_notice_spoken

        mode = get_settings().privacy_notice_on_join
        if mode == "off":
            return
        await asyncio.sleep(8)  # chat paneli açılsın (4 sn'de kutu henüz hazır değildi)
        if self._announced:
            return
        self._announced = True
        self.say(privacy_notice_chat(), channel="chat")
        if mode == "voice" and self.mode != "kayit":
            self.say(privacy_notice_spoken(), channel="voice", record=False)
        self._last_late_notice = time.monotonic()

    async def _auto_diagnose(self) -> None:
        # Toplantıya girildikten biraz sonra sayfa teşhisini kaydet: platform arayüzü
        # değiştiğinde (isimler, chat) seçicileri gerçek sayfadan düzeltebilmek için.
        await asyncio.sleep(20)
        with contextlib.suppress(asyncio.QueueFull):
            self.outbox.put_nowait({"kind": "diagnose"})

    def push_audio(self, frame: bytes) -> None:
        if self._closing:
            return
        self._replay_ring.append(frame)
        # STT akışı ilk ses geldiğinde açılır: bot lobide beklerken fatura işlemez.
        if self._stt_task is None:
            self._stt_task = asyncio.create_task(self._run_stt())
            if self._voice_enabled and self._voice_ready is None:
                self._voice_ready = self._spawn(self._init_voice())
        if self._audio.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                dropped = self._audio.get_nowait()
                if dropped:
                    self._audio_cursor += len(dropped) // 2
            self._audio_loss("Ses kuyruğu doldu; bazı ses kareleri kaybolmuş olabilir.")
        self._audio.put_nowait(frame)

    def update_roster(self, roster: RosterUpdate) -> None:
        bot = participant_name(get_settings().bot_display_name)
        people = [n for n in roster.participants if participant_name(n) != bot]
        active = [n for n in roster.active_speakers if n in people]
        roster = roster.model_copy(update={"participants": people, "active_speakers": active})
        self._roster = people
        if people:
            self._alone_since = None
        elif self._alone_since is None:
            self._alone_since = time.monotonic()
        self._speakers.observe(active)
        self._publish(PanelEvent(kind="roster", roster=roster))
        new = [n for n in people if n not in self._participants]
        if new and self._announced and get_settings().privacy_notice_on_join != "off":
            now = time.monotonic()
            if now - self._last_late_notice >= _LATE_NOTICE_GAP_S:
                from katip.privacy import late_joiner_notice

                self._last_late_notice = now
                self.say(late_joiner_notice(), channel="chat")
        if new:
            for name in new:
                self._participants[name] = None
            self._spawn(self._register_participants(new))
        if roster.identities:
            self._spawn(self.add_identities(roster.identities, "meeting"))

    async def add_identities(self, identities: list[ParticipantIdentity], source: str) -> None:
        async with self._identity_lock:
            for identity in identities:
                previous = self._identities.get(identity.email)
                # Panelde düzeltilen kimliği takvim yenilemesi geri değiştirmesin.
                if previous and previous[1] == "panel" and source != "panel":
                    continue
                self._identities[identity.email] = (identity, source)
                if self._persist:
                    await repo.save_meeting_identity(self.session_id, identity, source)
            await self._register_participants_unlocked(list(self._participants))

    async def bind_identity(self, name: str, identity: ParticipantIdentity) -> None:
        async with self._identity_lock:
            self._identities[identity.email] = (identity, "panel")
            if self._persist:
                await repo.save_meeting_identity(self.session_id, identity, "panel")
            await self._bind_identity(name, identity, "panel")

    async def _bind_identity(self, name: str, identity: ParticipantIdentity, source: str) -> None:
        self._participants[name] = identity.email
        # Takvim daveti görünen adla eşleşebilir; geçmiş raporları açmak için ayrıca
        # panel doğrulaması veya toplantıdan gelen doğrudan e-posta gerekir.
        self._internal[name] = source == "panel" and company_email(
            identity.email, get_settings().panel_email_domains
        )
        if self._persist:
            person = await repo.bind_participant(self.session_id, name, identity, source)
            self._participant_people[name] = person.id
        for sid, seg in list(self.segments.items()):
            if seg.speaker == name and seg.speaker_email != identity.email:
                updated = seg.model_copy(update={"speaker_email": identity.email})
                self.segments[sid] = updated
                if name in self._participant_people:
                    self._seg_person[sid] = self._participant_people[name]
                self._publish(PanelEvent(kind="segment", segment=updated))
                if updated.is_final:
                    self._store(updated)

    async def _register_participants(self, names: list[str]) -> None:
        async with self._identity_lock:
            await self._register_participants_unlocked(names)

    async def _register_participants_unlocked(self, names: list[str]) -> None:
        """Yalnız toplantının e-posta listesinden eşleştir; şirket şemasından mail tahmin etme."""
        people = await repo.list_people() if self._persist else []
        saved = await repo.list_participants(self.session_id) if self._persist else []
        for name in names:
            try:
                # Panelde seçilmiş bağ korunur; eklenen davetliler bu seçimi değiştirmez.
                binding = next(
                    (p for p in saved if p.name == name and p.identity_source == "panel"), None
                )
                if binding and binding.email:
                    continue
                candidates = []
                for identity, source in self._identities.values():
                    person = next((p for p in people if p.email == identity.email), None)
                    if participant_name(name) in (
                        participant_name(identity.name),
                        identity.email,
                    ) or (person and person.matches_name(participant_name(name))):
                        candidates.append((identity, source))
                if len(candidates) == 1:
                    await self._bind_identity(name, *candidates[0])
                else:
                    self._participants[name] = None
                    self._internal[name] = False
                    self._participant_people.pop(name, None)
                    if self._persist:
                        await repo.upsert_participant(self.session_id, name, None, None)
                    for sid, seg in list(self.segments.items()):
                        if seg.speaker == name and seg.speaker_email:
                            updated = seg.model_copy(update={"speaker_email": None})
                            self.segments[sid] = updated
                            self._seg_person.pop(sid, None)
                            self._publish(PanelEvent(kind="segment", segment=updated))
                            if updated.is_final:
                                self._store(updated)
            except Exception as exc:
                log.warning("katılımcı kaydedilemedi", error=str(exc))

    def participants_all_internal(self) -> bool:
        """Gözlenen herkes şirket e-postasına bağlı mı? Harici/belirsiz kişiye geçmiş verilmez."""
        return bool(self._roster) and all(self._internal.get(n, False) for n in self._roster)

    def verified_internal_emails(self) -> set[str]:
        """Yalnızca güncel katılımcıların doğrulanmış şirket e-postaları."""
        return {
            email
            for name in self._roster
            if self._internal.get(name, False)
            and (email := self._participants.get(name))
            and company_email(email, get_settings().panel_email_domains)
        }

    def participant_lines(self) -> list[str]:
        """Agent bağlamı için: "Ad Soyad <e-posta>" (konuşanlar da dahil)."""
        names = dict(self._participants)
        for seg in self.final_segments():
            if seg.speaker and seg.speaker != KATIP_SPEAKER and seg.speaker not in names:
                names[seg.speaker] = None
        return [f"{n} <{e}>" if e else n for n, e in names.items()]

    def on_chat(self, author: str, text: str) -> None:
        self.chat_router.receive(author, text, self._queue_browser_chat)

    def _queue_browser_chat(self, author: str, text: str) -> None:
        bot = get_settings().bot_display_name.casefold()
        # Teams yazarı "Katip (Guest)" gibi gösterebiliyor; metin de satır sonları
        # değişmiş hâlde geri okunuyor (8. test: Katip kendi notunu istek sandı).
        now = time.monotonic()
        while self._sent_chat and now - self._sent_chat[0][0] > 600:
            self._sent_chat.popleft()
        key = _squash(text)
        own = any(
            key == sent or (min(len(key), len(sent)) >= 20 and (key in sent or sent in key))
            for _, sent in self._sent_chat
        )
        if author.strip().casefold().startswith(bot) or own:
            return  # kendi mesajımız
        self._seen_chat = {k: at for k, at in self._seen_chat.items() if now - at < 30}
        identity = (_squash(author), key)
        if identity in self._seen_chat:
            return
        self._seen_chat[identity] = now
        self._ensure_final_worker()
        self._final_queue.put_nowait(("chat", author, text))

    # ------------------------------------------------------------- ses kimliği
    async def _init_voice(self) -> None:
        from katip.voice import crypto

        settings = get_settings()
        if not crypto.available():
            # Biyometrik veri şifresiz saklanmaz: anahtar yoksa ses tanıma hiç çalışmaz.
            log.warning("ses kimliği kapalı: KATIP_VOICEPRINT_KEY tanımlı değil ya da geçersiz")
            return
        try:
            encoder = await asyncio.to_thread(_build_encoder)
        except Exception as exc:
            log.warning("ses kimliği kapalı: model yüklenemedi", error=str(exc))
            return
        known = []
        try:
            known = [
                KnownVoice(pid, name, role, vec)
                for pid, name, role, vec in await repo.load_voiceprints(encoder.name)
            ]
        except Exception as exc:
            log.warning("ses izleri yüklenemedi", error=str(exc))
        self.identifier = VoiceIdentifier(
            encoder=encoder,
            sample_rate=settings.sample_rate,
            known=known,
            match_threshold=settings.voice_match_threshold,
            match_margin=settings.voice_match_margin,
            min_enroll_s=settings.voice_min_enroll_s,
        )
        log.info("ses kimliği hazır", session_id=self.session_id, known=len(known))

    def human_roster(self) -> list[str]:
        return list(self._roster)

    def claim_name(self, label: str | None, spoken: str) -> None:
        """Konuşmacı kendini tanıttı: toplantıdaki ya da ses kaydı olan bir kişiyle eşleşirse
        o kişiye aday gösterilir. ("Ben Ankara'dan…" gibi eşleşmeyen sözler yok sayılır.)"""
        if not label:
            return
        wanted = spoken.casefold()
        pool = list(self._roster) + [
            k.name for k in (self.identifier.known if self.identifier else [])
        ]
        for name in pool:
            n = name.casefold()
            if n == wanted or n.startswith(wanted + " "):
                self._claims[label] = name
                return

    def _segment_candidates(self, label: str | None, dom: str | None = None) -> set[int]:
        """Bu cümle kimin olabilir: toplantıda bulunan (katılımcı listesinde görünen), kendini
        tanıtan ya da o an konuşuyor görünen ve ses izi için rıza vermiş kişiler.

        Aday yoksa cümlenin sesinden hiçbir iz çıkarılmaz. Veritabanındaki, toplantıda olmayan
        kişilerle karşılaştırma yapılmaz.
        """
        if self.identifier is None or not self.identifier.known:
            return set()
        names = list(self._roster)
        if claimed := self._claims.get(label):
            names.append(claimed)
        if dom:
            names.append(dom)
        out = set()
        for name in names:
            wanted = name.casefold()
            for known in self.identifier.known:
                k = known.name.casefold()
                if k == wanted or wanted.startswith(k + " ") or k.startswith(wanted + " "):
                    out.add(known.person_id)
        return out

    def voice_match_for(self, label: str | None, max_age_s: float = 180.0):
        """Bu etiketin son cümlesinde sesle doğrulanan kişi (yakın zamanda doğrulandıysa)."""
        entry = self._label_voice.get(label) if label else None
        if entry and time.monotonic() - entry[1] <= max_age_s:
            return entry[0]
        return None

    def speaker_name(self, label: str | None) -> str | None:
        """Yeterli kanıtla eşleşen kişi; tek kutucuk görünmesi isim doğrulaması değildir."""
        if match := self.voice_match_for(label):
            return match.name
        if label and (ids := self._label_segments.get(label)):
            current = self.segments.get(ids[-1])
            if current is not None:
                # Keep agent attribution aligned with the sentence-level conflict check.
                return current.speaker
        return self._display_name(label)

    def voice_status(self, label: str | None) -> str:
        if self.identifier is None:
            return "ses tanıma kapalı"
        if match := self.voice_match_for(label):
            return f"sesinden tanındı: {match.name}"
        if self.agent.is_enrolling(label):
            return "ses kaydı sürüyor (rıza alındı, birkaç saniye daha konuşması gerekiyor)"
        return "ses izi yok ya da henüz doğrulanmadı"

    def _display_name(self, label: str | None, dom: str | None = None) -> str | None:
        # Acoustic labels never establish a person's identity.
        # Until platform account IDs are available, require an explicit panel email binding.
        email = self._participants.get(dom) if dom else None
        identity = self._identities.get(email) if email else None
        bound = bool(identity and identity[1] == "panel")
        name = dom if bound else (f"Konuşmacı {label}" if label is not None else None)
        if name and name.casefold() in self._suppressed_names:
            return ANONYMOUS_SPEAKER
        return name

    def _person_id(self, segment_id: str) -> int | None:
        return self._seg_person.get(segment_id)

    # ----------------------------------------------------------------- STT
    async def _frames(self) -> AsyncIterator[bytes]:
        frames, self._replay_frames = self._replay_frames, []
        self._replaying = bool(frames)
        try:
            for frame in frames:
                self._ring.append(frame)
                yield frame
        finally:
            self._replaying = False
        while True:
            frame = await self._audio.get()
            if frame is None:
                return
            # Tampon, STT'ye giden sesi birebir tutar → parça zamanları kaymadan kesilir.
            self._ring.append(frame)
            self._audio_cursor += len(frame) // 2
            yield frame

    def _audio_loss(self, text: str) -> None:
        if time.monotonic() - self._last_audio_loss < 5:
            return
        self._last_audio_loss = time.monotonic()
        log.warning("STT ses kaybı olasılığı", session_id=self.session_id)
        self._publish(PanelEvent(kind="error", text=text))

    def _prepare_replay(self) -> None:
        start, frames, lost = self._replay_ring.replay(self._final_samples)
        self._next_stream_sample = start
        self._replay_frames = frames
        self._audio_cursor = self._replay_ring.total
        self._replay_boundary = self._final_samples / get_settings().sample_rate
        self._replay_previous = " ".join(
            s.text for s in self.final_segments()[-10:] if s.speaker != KATIP_SPEAKER
        )
        ended = False
        # Tampondaki anlık görüntü bekleyen kareleri de içerir; iki kez gönderilmez.
        while not self._audio.empty():
            ended = self._audio.get_nowait() is None or ended
        if ended:
            self._audio.put_nowait(None)
        for key, segment in list(self.segments.items()):
            if not segment.is_final:
                # Panel upsert'i önceki sağlayıcının yarım parçasını temizler.
                segment = segment.model_copy(update={"text": "", "is_final": True})
                self._publish(PanelEvent(kind="segment", segment=segment))
                del self.segments[key]
        if lost:
            self._audio_loss(
                "STT kesintisi ses tamponunu aştı; konuşmanın bir bölümü kaybolmuş olabilir."
            )

    def _switch_stt(self, exc: Exception, *, record_failure: bool = True) -> bool:
        chain = self._provider_registry.stt_chain
        if not transient(exc):
            return False
        current = getattr(self._stt, "name", chain[self._stt_index])
        if record_failure:
            if len(chain) > 1:
                self._provider_registry.breaker(current, "stt").failure()
            self._provider_registry.observed[current] = False
        self._stt_probe = None
        for offset in range(1, len(chain)):
            index = (self._stt_index + offset) % len(chain)
            breaker = self._provider_registry.breaker(chain[index], "stt")
            if not breaker.acquire():
                continue
            try:
                provider = self._provider_registry.stt(chain[index])
            except Exception:
                breaker.release()
                raise
            self._stt_probe = breaker
            self._stt_index = index
            self._stt = provider
            self._provider_registry.sticky[(self.session_id, "stt")] = index
            if not provider.translates and self._translator is None:
                self._translator = Translator(
                    source_lang=self.source_lang, target_lang=self.target_lang
                )
            self._prepare_replay()
            self._publish(
                PanelEvent(kind="note", text=f"Yazıya dökme sağlayıcısı değişti: {chain[index]}")
            )
            log.warning(
                "STT sağlayıcısı değişti", session_id=self.session_id, provider=chain[index]
            )
            return True
        return False

    async def _run_stt(self) -> None:
        restarts = 0
        while True:
            if len(self._provider_registry.stt_chain) > 1 and self._stt_probe is None:
                name = getattr(
                    self._stt, "name", self._provider_registry.stt_chain[self._stt_index]
                )
                breaker = self._provider_registry.breaker(name, "stt")
                if not breaker.acquire():
                    if self._switch_stt(ConnectionError("Devre açık"), record_failure=False):
                        continue
                    if self.state == SessionState.ENDED:
                        return
                    await asyncio.sleep(5)
                    self._prepare_replay()
                    continue
                self._stt_probe = breaker
                self._provider_registry.sticky[(self.session_id, "stt")] = self._stt_index
            self._stream_started_at = time.monotonic()
            if self._first_stream_at is None:
                self._first_stream_at = self._stream_started_at
            self._stream_no += 1
            self._speakers.reset_labels()
            self._stream_offset = self._stream_started_at - self._first_stream_at
            if self._next_stream_sample is not None:
                self._stream_offset = self._next_stream_sample / get_settings().sample_rate
                self._next_stream_sample = None
            elif len(self._provider_registry.stt_chain) > 1:
                self._stream_offset = self._audio_cursor / get_settings().sample_rate
                # İlk akışta kuyruğa giren ilk kare sıfırdan başlar.
                if self._stream_no == 1:
                    self._stream_offset = 0.0
            self._ring.mark_stream_start()
            try:
                await self._stt_stream()
                if self._stt_probe is not None:
                    self._stt_probe.success()
                    self._stt_probe = None
                if hasattr(self._stt, "name"):
                    self._provider_registry.observed[self._stt.name] = True
                if self._restart_stt:  # dil değişti: yeni ayarlarla yeniden aç
                    self._restart_stt = False
                    continue
                return  # akış düzgün kapandı (ses bitti)
            except asyncio.CancelledError:
                if self._stt_probe is not None:
                    self._stt_probe.release()
                    self._stt_probe = None
                raise
            except StreamExpired:
                if self._stt_probe is not None:
                    self._stt_probe.release()
                    self._stt_probe = None
                self._prepare_replay()
                if self._closing:
                    return
                continue
            except Exception as exc:
                raise_if_cancelling()
                restarts += 1
                log.warning(
                    "STT akışı koptu", session_id=self.session_id, error_type=type(exc).__name__
                )
                self._publish(PanelEvent(kind="error", text="Yazıya dökme bağlantısı kesildi."))
                if not transient(exc) and self._stt_probe is not None:
                    self._stt_probe.release()
                    self._stt_probe = None
                if not self._closing and self._switch_stt(exc):
                    restarts = 0
                    continue
                if len(self._provider_registry.stt_chain) > 1:
                    self._prepare_replay()
                single_or_permanent = len(self._provider_registry.stt_chain) == 1 or not transient(
                    exc
                )
                if (
                    (single_or_permanent and restarts > _MAX_STT_RESTARTS)
                    or self.state == SessionState.ENDED
                    or self._closing
                ):
                    return
                await asyncio.sleep(min(2**restarts, 15))

    async def _stt_stream(self) -> None:
        if hasattr(self._stt, "audio_backlog"):
            self._stt.audio_backlog = lambda: self._replaying or self._audio.qsize() > 5
        try:
            async for utt in self._stt.stream(
                self._frames(), language=self.source_lang, target_language=self.target_lang
            ):
                if self._replay_boundary:
                    normalized = replace(
                        utt,
                        start_s=utt.start_s + self._stream_offset,
                        end_s=utt.end_s + self._stream_offset,
                    )
                    normalized = deduplicate(
                        normalized, self._replay_boundary, self._replay_previous
                    )
                    if normalized is None:
                        continue
                    utt = replace(
                        normalized,
                        start_s=normalized.start_s - self._stream_offset,
                        end_s=normalized.end_s - self._stream_offset,
                    )
                if self._stt_probe is not None:
                    self._stt_probe.success()
                    self._stt_probe = None
                if utt.is_final:
                    self._final_samples = max(
                        self._final_samples,
                        round((utt.end_s + self._stream_offset) * get_settings().sample_rate),
                    )
                self._on_utterance(utt)
        finally:
            # Soniox akış süresini (sessizlik dahil) faturalar: süreyi kaydet. Oturum kapanırken
            # iptal edilmesin diye arka plan görevleriyle değil, rapor görevleriyle tutulur.
            if self._persist and (self._closing or not asyncio.current_task().cancelling()):
                seconds = time.monotonic() - self._stream_started_at
                task = asyncio.create_task(
                    usage.record_stt(
                        self.session_id,
                        seconds,
                        provider=getattr(self._stt, "name", "soniox"),
                        model=getattr(self._stt, "model", get_settings().soniox_model),
                        region=getattr(self._stt, "region", get_settings().soniox_region),
                    )
                )
                tasks = _report_tasks.get()
                tasks.add(task)
                task.add_done_callback(tasks.discard)

    def _map_label(self, raw: str | None) -> str | None:
        """Soniox etiketleri her akışta "1"den başlar: sonraki akışlarda yeni numara verilir."""
        if raw is None:
            return None
        key = (self._stream_no, raw)
        if key not in self._label_map:
            if self._stream_no <= 1:
                label = raw
            else:
                self._label_counter += 1
                label = str(self._label_counter)
            self._label_map[key] = label
            if label.isdigit():
                self._label_counter = max(self._label_counter, int(label))
        return self._label_map[key]

    def _on_utterance(self, utt: Utterance) -> None:
        utt = replace(
            utt,
            speaker_label=self._map_label(utt.speaker_label),
            start_s=utt.start_s + self._stream_offset,
            end_s=utt.end_s + self._stream_offset,
        )
        self._seg_stream.setdefault(utt.id, (self._stream_no, self._stream_offset))
        if not utt.is_final and utt.text:
            self._last_partial_at = time.monotonic()
            self.agent.on_partial(utt.speaker_label)
        if utt.text:
            self._label_activity[utt.speaker_label] = time.monotonic()
            self._observe_floor(utt)
        if self.paused or utt.id in self._offrecord_ids:
            # Kayıt dışı: panele, depoya, çeviriye ve ses kimliğine gitmez. Yalnızca komut
            # algılama için ("Katip, kayda devam") agent'a iletilir.
            if utt.id not in self._offrecord_ids:
                self._offrecord_ids.add(utt.id)
            if utt.is_final and utt.id not in self._offrecord_done and utt.id not in self._echo_ids:
                self._offrecord_done.add(utt.id)
                self._ensure_final_worker()
                self._final_queue.put_nowait(("offrecord", utt.speaker_label, utt.text))
            return
        base = self._first_stream_at or self._stream_started_at
        t0 = base + utt.start_s
        t1 = base + utt.end_s
        previous = self.segments.get(utt.id)
        if utt.is_final and (previous is None or not previous.is_final):
            if utt.id not in self._seg_dom:
                self._seg_dom[utt.id] = self._speakers.name_at(t0, t1)
            dom = self._seg_dom[utt.id]
            candidate = self._display_name(utt.speaker_label, dom)
            log.info(
                "speaker_account_binding",
                session_id=self.session_id,
                label=utt.speaker_label,
                mapped=bool(dom and candidate == dom),
                candidate_hash=hashlib.sha256((candidate or "").encode()).hexdigest()[:10],
            )

        if previous and previous.is_final:
            # Translation/final retransmissions must not learn twice or rename old speech.
            speaker = previous.speaker
        else:
            speaker = self._display_name(utt.speaker_label, self._seg_dom.get(utt.id))
        segment = Segment(
            id=utt.id,
            session_id=self.session_id,
            speaker=speaker,
            speaker_email=self._participants.get(speaker),
            speaker_label=utt.speaker_label,
            start_s=utt.start_s,
            end_s=utt.end_s,
            text=utt.text,
            lang=utt.lang or self.source_lang,
            is_final=utt.is_final,
            translation=utt.translation or None,
        )
        previous = self.segments.get(utt.id)
        self.segments[utt.id] = segment
        if speaker in self._participant_people:
            self._seg_person[segment.id] = self._participant_people[speaker]

        if (
            previous is None
            or previous.text != segment.text
            or previous.is_final != segment.is_final
        ):
            self._publish(PanelEvent(kind="segment", segment=segment))

        newly_final = segment.is_final and (previous is None or not previous.is_final)
        if segment.is_final and (
            newly_final
            or previous.translation != segment.translation
            or previous.speaker != segment.speaker
        ):
            self._store(segment)

        if newly_final:
            self._last_speech = time.monotonic()
            self._seg_wall[segment.id] = time.time()
            if utt.speaker_label:
                self._label_segments[utt.speaker_label].append(segment.id)
            self._ensure_final_worker()
            self._final_queue.put_nowait(("speech", segment))

        if utt.translation and (
            previous is None or previous.translation != segment.translation or newly_final
        ):
            # Çeviri metni değişmese de parça kesinleşince "final" olarak yeniden yayınla;
            # yoksa panelde kısmi (italik) kalıyordu.
            self._publish_translation(segment.id, utt.translation, final=utt.is_final)
        elif self._translator and newly_final and utt.text:
            self._spawn(self._translate(segment))

    def _publish_translation(self, segment_id: str, text: str, *, final: bool) -> None:
        self._publish(
            PanelEvent(
                kind="translation",
                translation=Translation(
                    segment_id=segment_id,
                    session_id=self.session_id,
                    text=text,
                    lang=self.target_lang,
                    is_final=final,
                ),
            )
        )

    async def _translate(self, segment: Segment) -> None:
        assert self._translator is not None
        with usage.meeting(self.session_id):
            text = await self._translator.translate(segment.text)
        if not text:
            return
        stored = self.segments.get(segment.id)
        if stored is not None:
            updated = stored.model_copy(update={"translation": text})
            self.segments[segment.id] = updated
            self._store(updated)
        self._publish_translation(segment.id, text, final=True)

    # -------------------------------------------- kesinleşen parça işçisi
    def _ensure_final_worker(self) -> None:
        if self._final_worker is None:
            self._final_worker = asyncio.create_task(self._final_loop())

    async def _final_loop(self) -> None:
        while (job := await self._final_queue.get()) is not None:
            try:
                if job[0] == "chat":
                    if await self.chat_router.accepts_browser_input():
                        await self.agent.on_chat(job[1], job[2])
                elif job[0] == "offrecord":
                    if not self.agent.append_interruption(job[1], job[2], offrecord=True):
                        await self.agent.on_offrecord(job[1], job[2])
                else:
                    await self._process_final(job[1])
            except Exception as exc:
                raise_if_cancelling()
                log.exception("parça işlenemedi", session_id=self.session_id, error=str(exc))

    async def _process_final(self, segment: Segment) -> None:
        label = segment.speaker_label
        if segment.speaker in self._participants:
            email = self._participants[segment.speaker]
            updated = self.segments[segment.id].model_copy(update={"speaker_email": email})
            self.segments[segment.id] = updated
            if segment.speaker in self._participant_people:
                self._seg_person[segment.id] = self._participant_people[segment.speaker]
            if updated != segment:
                self._publish(PanelEvent(kind="segment", segment=updated))
            self._store(updated)
        dom = self._seg_dom.pop(segment.id, None)
        stream_no, offset = self._seg_stream.pop(segment.id, (self._stream_no, self._stream_offset))
        if self.identifier and label and stream_no == self._stream_no:

            def audio():
                return self._ring.slice_stream(segment.start_s - offset, segment.end_s - offset)

            if self.agent.is_enrolling(label):
                # Rıza verildi: kayıt için bu kişinin sesi biriktirilir.
                await asyncio.to_thread(self.identifier.observe, label, audio(), enrolling=True)
                self._enroll_segments[label].append(segment.id)
                await self.agent.on_speech_observed(label)
            else:
                candidates = self._segment_candidates(label, dom)
                long_enough = segment.end_s - segment.start_s >= get_settings().voice_segment_min_s
                if candidates and long_enough:
                    match = await asyncio.to_thread(
                        self.identifier.identify_segment, audio(), candidates
                    )
                    if match:
                        await self._apply_identity(segment, label, match)
        if segment.id not in self._echo_ids:
            await self.agent.on_utterance(label, segment.text)

    async def _apply_identity(self, segment: Segment, label: str, match) -> None:
        """Sesle doğrulanan kişiyi yalnızca bu cümleye yazar (etiketin tamamına değil)."""
        self._label_voice[label] = (match, time.monotonic())
        self._seg_person[segment.id] = match.person_id
        name = ANONYMOUS_SPEAKER if match.name.casefold() in self._suppressed_names else match.name
        current = self.segments.get(segment.id)
        if current is not None and current.speaker != name:
            updated = current.model_copy(update={"speaker": name})
            self.segments[segment.id] = updated
            self._publish(PanelEvent(kind="segment", segment=updated))
        if self._persist:
            with contextlib.suppress(Exception):
                await repo.set_segment_person([segment.id], match.person_id, name)
        if match.person_id not in self._greeted_people:
            self._greeted_people.add(match.person_id)
            await self.agent.on_voice_match(label, match)

    # ------------------------------------------------ AgentHost arayüzü
    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if paused:
            self.presentation.pause("Kayıt dışı: sunum duraklatıldı.")
        self._publish(PanelEvent(kind="paused", text="on" if paused else "off"))
        log.info("kayıt dışı" if paused else "kayda devam", session_id=self.session_id)

    # ------------------------------------------------ ekran görüntüsü
    def request_screenshot(self, requested_by: str | None) -> None:
        """Bottan ekran görüntüsü ister; görüntü runner tarafından HTTP ile geri gönderilir."""
        request_id = uuid.uuid4().hex[:12]
        future = asyncio.get_running_loop().create_future()
        self._screenshot_waiters[request_id] = (future, requested_by)
        with contextlib.suppress(asyncio.QueueFull):
            self.outbox.put_nowait({"kind": "screenshot", "request_id": request_id})
        self._spawn(self._screenshot_timeout(request_id, future))

    async def _screenshot_timeout(self, request_id: str, future: asyncio.Future) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=20)
        except TimeoutError:
            self._screenshot_waiters.pop(request_id, None)
            self.say("Ekran görüntüsünü alamadım, tekrar dener misiniz?")

    async def on_screenshot(self, request_id: str, jpeg: bytes) -> int | None:
        entry = self._screenshot_waiters.pop(request_id, None)
        if entry is None:
            return None  # zaman aşımına uğramış ya da bilinmeyen istek
        future, requested_by = entry
        self._screenshot_count += 1
        number = self._screenshot_count
        at_s = time.monotonic() - self._created_at
        path = Path(get_settings().data_dir) / "screenshots" / self.session_id / f"{number:03d}.jpg"
        from katip.store.crypto import write_file

        await write_file(self.session_id, path, jpeg)
        asset_id = None
        if self._persist:
            try:
                asset_id = await repo.add_asset(
                    self.session_id, "screenshot", number, str(path), at_s, requested_by
                )
            except Exception as exc:
                log.warning("ekran görüntüsü kaydedilemedi", error=str(exc))
        self._publish(
            PanelEvent(
                kind="screenshot",
                text=f"Ekran görüntüsü {number}",
                url=f"/api/meetings/{self.session_id}/screenshots/{number}",
            )
        )
        if not future.done():
            future.set_result(number)
        self.say(f"Ekran görüntüsünü aldım ({number}. görüntü).")
        if get_registry().enabled():
            self._spawn(self._caption_screenshot(asset_id, number, jpeg))
        return number

    async def _caption_screenshot(self, asset_id: int | None, number: int, jpeg: bytes) -> None:
        from katip.agent import llm

        recent = " ".join(s.text for s in self.final_segments()[-3:])
        try:
            with usage.meeting(self.session_id if self._persist else None):
                caption = await llm.describe_image(jpeg, context=recent)
        except Exception as exc:
            log.warning("ekran görüntüsü tarif edilemedi", error=str(exc))
            return
        if not caption:
            return
        self.agent.notes.append(f"Ekran görüntüsü {number}: {caption}")
        if asset_id is not None:
            with contextlib.suppress(Exception):
                await repo.set_asset_caption(asset_id, caption)

    # ------------------------------------------------------------ ayrılma
    def leave_meeting(self) -> None:
        """Botu toplantıdan çıkarır ve oturumu kapatır (rapor üretimi tetiklenir)."""
        if self._interim_task is not None and not self._interim_task.done():
            # Final raporu zaten üretilecek: yarımdaki ara rapor boşa para harcamasın.
            self._interim_task.cancel()
        settings = get_settings()
        if (
            settings.post_report_to_chat
            and self.state == SessionState.ACTIVE
            and settings.auto_report
        ):
            # Rapor bot çıktıktan sonra üretilir; linki (imzalı, süreli) çıkmadan önce chat'e yaz.
            from katip.api.security import absolute_url, view_link_params

            path = f"/api/meetings/{self.session_id}/report.pdf"
            q = view_link_params(self.session_id)
            self.say(
                f"Toplantı notları hazır olunca bu linklerden açılır (genellikle birkaç "
                f"dakika; {settings.share_link_days:g} gün geçerli):\n"
                f"PDF: {absolute_url(path)}?{q} adresinde.\n"
                f"Word: {absolute_url(path[:-4] + '.docx')}?{q} adresinde.",
                channel="chat",
            )
        task = asyncio.create_task(self._leave())
        tasks = _report_tasks.get()
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _leave(self) -> None:
        from katip.bot.supervisor import supervisor

        # Veda mesajı iletilsin: sesli cevap üretilip toplantıda çalınması bitene kadar bekle
        # (sabit 4 sn bekleme cümleyi yarıda kesiyordu, 8. test). En fazla 30 sn.
        await asyncio.sleep(1.5)
        deadline = time.monotonic() + 30
        # Bilinçli yoklama: çalma bitişi zamana bağlı (tarayıcıdaki tampon), bekleyecek olay yok.
        while time.monotonic() < deadline and (  # noqa: ASYNC110
            self._speech_lock.locked()
            or (self._speak_task is not None and not self._speak_task.done())
            or time.monotonic() < self._speech_until
        ):
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.5)
        await supervisor.stop(self.session_id)
        await registry.drop(self.session_id)

    def transcript_text(self, last: int = 400, *, timestamps: bool = False) -> str:
        return "\n".join(self.transcript_lines(last, timestamps=timestamps))

    def transcript_lines(self, last: int | None = None, *, timestamps: bool = False) -> list[str]:
        segs = self.final_segments()
        if last is not None:
            segs = segs[-last:]
        if not timestamps:
            return [f"{s.speaker or 'Bilinmeyen'}: {s.text}" for s in segs]
        # Agent için: "[14:12:05] Ad: metin" (yerel saat) — "14:12'de ne konuştuk", "buradan
        # sonrasını not al" gibi istekler saat ve sıra bağlamıyla çözülebilsin.
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(get_settings().timezone)

        def stamp(s: Segment) -> str:
            wall = self._seg_wall.get(s.id)
            if wall is None:
                return f"+{int(s.start_s // 60):02d}:{int(s.start_s % 60):02d}"
            return datetime.fromtimestamp(wall, tz).strftime("%H:%M:%S")

        return [f"[{stamp(s)}] {s.speaker or 'Bilinmeyen'}: {s.text}" for s in segs]

    def dom_name_for_label(self, label: str | None) -> str | None:
        return self._speakers.name_for(label)

    def label_for_name(self, name: str) -> str | None:
        wanted = name.strip().casefold()
        for label in self._label_segments:
            if (self._display_name(label) or "").casefold() == wanted:
                return label
            if (self._speakers.name_for(label) or "").casefold() == wanted:
                return label
        return None

    def set_target_lang(self, code: str) -> None:
        if code == self.target_lang:
            return
        self.target_lang = code
        if self._translator:
            self._translator.target_lang = code
        elif self._stt_task and not self._stt_task.done():
            # Soniox çeviri dilini akış açılırken alır: akışı kapatıp yeni dille aç.
            self._restart_stt = True
            with contextlib.suppress(asyncio.QueueFull):
                self._audio.put_nowait(None)

    def request_approval(self, approval_id: str, description: str) -> None:
        self._publish(PanelEvent(kind="approval", approval_id=approval_id, text=description))

    def on_person_forgotten(self, person_id: int, name: str) -> None:
        """Bu toplantıda da adını bir daha gösterme; görünen parçaları anonimleştir."""
        affected = [n for n, pid in self._participant_people.items() if pid == person_id]
        for display in {name, *affected}:
            self._suppressed_names.add(display.casefold())
            email = self._participants.pop(display, None)
            if email:
                self._identities.pop(email, None)
            self._participant_people.pop(display, None)
            self._internal[display] = False
        for sid, seg in list(self.segments.items()):
            if seg.speaker and seg.speaker.casefold() == name.casefold():
                seg = seg.model_copy(update={"speaker": ANONYMOUS_SPEAKER, "speaker_email": None})
                self.segments[sid] = seg
                self._seg_person.pop(sid, None)
                self._publish(PanelEvent(kind="segment", segment=seg))

    def on_person_enrolled(self, label: str, person_id: int, name: str) -> None:
        """Yalnızca kayıt sırasında söylenen cümleler bu kişiye yazılır (etiketin tamamı değil)."""
        from katip.voice.identity import VoiceMatch

        seg_ids = self._enroll_segments.pop(label, [])
        for sid in seg_ids:
            seg = self.segments.get(sid)
            if seg and seg.speaker != name:
                seg = seg.model_copy(update={"speaker": name})
                self.segments[sid] = seg
                self._publish(PanelEvent(kind="segment", segment=seg))
            self._seg_person[sid] = person_id
        self._label_voice[label] = (VoiceMatch(person_id, name, "member", 1.0), time.monotonic())
        self._greeted_people.add(person_id)
        if self._persist and seg_ids:
            self._spawn(repo.set_segment_person(seg_ids, person_id, name))

    # ------------------------------------------------------------- kalıcılık
    def _store(self, segment: Segment) -> None:
        if not self._persist:
            return
        if self._writer is None:
            self._writer = asyncio.create_task(self._write_loop())
        self._persist_queue.put_nowait((segment, self._person_id(segment.id)))

    async def _write_loop(self) -> None:
        while (item := await self._persist_queue.get()) is not None:
            segment, person_id = item
            segment = self.segments.get(segment.id, segment)
            person_id = self._person_id(segment.id)
            try:
                await repo.save_segment(self.session_id, segment, person_id)
            except Exception as exc:  # depo düşse bile canlı akış sürmeli
                raise_if_cancelling()
                log.warning("segment kaydedilemedi", session_id=self.session_id, error=str(exc))

    # ----------------------------------------------------------------- bitiş
    async def shutdown(self) -> None:
        self._closing = True
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._shutdown(), name=f"session-shutdown:{self.session_id}"
            )
        try:
            pending = await wait_tasks(
                {self._shutdown_task}, budget_s=_SHUTDOWN_TIMEOUT_S, label="oturum"
            )
            if not pending and not self._shutdown_task.cancelled():
                self._shutdown_task.result()
        finally:
            tasks = {
                task
                for task in (
                    self._shutdown_task,
                    self._stt_task,
                    self._final_worker,
                    self._writer,
                    *self._background,
                    *getattr(self.agent, "_turn_tasks", ()),
                )
                if task is not None and task is not asyncio.current_task()
            }
            try:
                await wait_tasks(
                    tasks, budget_s=_CANCEL_TIMEOUT_S, label="oturum işçileri", cancel=True
                )
            finally:
                self._provider_registry.forget(self.session_id)
                if self.state not in (SessionState.ENDED, SessionState.FAILED):
                    self.set_state(SessionState.ENDED)

    async def _shutdown(self) -> None:
        await self.presentation.close()
        if self._stt_task is not None and not self._stt_task.done():
            # Ses sonu işareti: sağlayıcı kalan final'leri gönderip akışı kapatır.
            if self._audio.full():
                self._audio.get_nowait()
                log.warning("kapanış için ses kuyruğu boşaltıldı", session_id=self.session_id)
            self._audio.put_nowait(None)
            await wait_tasks({self._stt_task}, budget_s=5, label="STT")
        if self._final_worker is not None:
            self._final_queue.put_nowait(None)
            await wait_tasks({self._final_worker}, budget_s=5, label="son parçalar")
        await self.chat_router.flush()
        for task in list(self._background):
            task.cancel()
        final_state = self.state if self.state == SessionState.FAILED else SessionState.ENDED
        if self.state != SessionState.ENDED:
            self.set_state(SessionState.ENDED)
        # Hiç ses almamış oturum (ör. bot katılamadı) toplantı kaydına dokunmaz.
        if self._persist and (self._stt_task is not None or self.segments):
            await self._finalize_record(final_state)

    async def _finalize_record(self, state: SessionState) -> None:
        if self._writer is not None:
            self._persist_queue.put_nowait(None)
            await wait_tasks({self._writer}, budget_s=5, label="segment kaydı")
        try:
            await repo.finish_meeting(self.session_id, state.value)
        except Exception as exc:
            log.warning("toplantı kaydı kapatılamadı", session_id=self.session_id, error=str(exc))
            return
        settings = get_settings()
        if settings.auto_report and get_registry().enabled("report") and self.final_segments():
            from katip.reports.service import track_report

            task = asyncio.create_task(self._report())
            track_report(self.session_id, task, batch=settings.report_batch)
            tasks = _report_tasks.get()
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    async def _report(self, *, interim: bool = False) -> None:
        from katip.reports.service import generate_report

        try:
            # Her seferinde yeniden özetle: ara rapordan sonra konuşma devam etmiş olabilir.
            docs = await generate_report(
                self.session_id,
                resummarize=True,
                batch=not interim and get_settings().report_batch,
                interim=interim,
            )
        except Exception as exc:
            log.warning(
                "rapor üretilemedi", session_id=self.session_id, error_type=type(exc).__name__
            )
            self._publish(PanelEvent(kind="error", text="Rapor üretilemedi."))
            return
        finally:
            if self.state in (SessionState.ENDED, SessionState.FAILED):
                self._provider_registry.forget(self.session_id)
        if docs:
            from katip.api.security import absolute_url, view_link_params

            label = "Ara rapor" if interim else "Toplantı raporu"
            path = f"/api/meetings/{self.session_id}/report.pdf"
            self._publish(PanelEvent(kind="note", text=f"{label} hazır (PDF / Word).", url=path))
            if get_settings().post_report_to_chat and self.state == SessionState.ACTIVE:
                # Toplantı sürüyorsa imzalı, süreli linki chat'e yaz (toplantı bittiyse bot çıktı).
                q = view_link_params(self.session_id)
                days = get_settings().share_link_days
                self.say(
                    f"{label} hazır ({days:g} gün geçerli):\n"
                    f"PDF: {absolute_url(path)}?{q} adresinde.\n"
                    f"Word: {absolute_url(path[:-4] + '.docx')}?{q} adresinde.",
                    channel="chat",
                )

    def generate_report_now(self) -> None:
        """Toplantı sürerken ara rapor (konuşmalar önce depoya yazılsın diye kısa gecikme).

        Son 2 dakikada üretilmiş ve o zamandan beri konuşma eklenmemişse yeniden üretilmez
        (10. testte aynı rapor art arda üretildi); önceki link geçerli kalır.
        """
        now = time.monotonic()
        count = len(self.final_segments())
        last = self._last_interim
        if last and now - last[0] < 120 and count == last[1]:
            log.info("ara rapor atlandı: yeni konuşma yok", session_id=self.session_id)
            return
        self._last_interim = (now, count)

        async def run() -> None:
            await asyncio.sleep(2)
            await self._report(interim=True)

        task = asyncio.create_task(run())
        from katip.reports.service import track_report

        track_report(self.session_id, task)
        tasks = _report_tasks.get()
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        self._interim_task = task


class SessionRegistry:
    """Süreç içindeki aktif oturumlar."""

    def __init__(self) -> None:
        self._sessions: dict[str, MeetingSession] = {}

    def create(
        self,
        session_id: str,
        *,
        source_lang: str | None = None,
        target_lang: str | None = None,
        mode: str | None = None,
    ) -> MeetingSession:
        try:
            session = MeetingSession(
                session_id, source_lang=source_lang, target_lang=target_lang, mode=mode
            )
        except BaseException:
            get_registry().forget(session_id)
            raise
        self._sessions[session_id] = session
        return session

    def get_or_create(self, session_id: str) -> MeetingSession:
        return self._sessions.get(session_id) or self.create(session_id)

    def get(self, session_id: str) -> MeetingSession | None:
        return self._sessions.get(session_id)

    def all(self) -> list[MeetingSession]:
        return list(self._sessions.values())

    async def drop(self, session_id: str) -> None:
        # Oturum kapanış bitene kadar kayıtta kalır: silme kontrolleri onu görmeli.
        session = self._sessions.get(session_id)
        try:
            if session:
                await session.shutdown()
                self._sessions.pop(session_id, None)
        finally:
            get_registry().forget(session_id)


registry = SessionRegistry()
