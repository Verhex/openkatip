"""Sesli cevapların çalınması ve toplantıda söz sırasının izlenmesi."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass, field

from katip import usage
from katip.agent.commands import detect_wake
from katip.config import get_settings
from katip.events import PanelEvent, Segment
from katip.logging import get_logger
from katip.pipeline.floor import content_words, is_echo, overlap_decision, split_sentences
from katip.pipeline.stt.base import Utterance
from katip.store.models import KATIP_SPEAKER
from katip.voice.expressive import display_text, transform_text, tts_text
from katip.voice.tr_normalize import normalize_tr
from katip.voice.tts import TTSEvent, apply_lexicon

log = get_logger(__name__)
_LANG_TAG = re.compile(r"^\s*\[([a-z]{2})\]\s*")
_SPEAK_CHUNK_MS = 200
_SPEAK_CUSHION_MS = 400
# Tarayıcıda sırada bekleyen ses payı. Kısa pay, sunucudaki küçük gecikmelerde boşluk ve
# çıtırtı yapar; cümle sonunda durma ve sert kesme bu paydan etkilenmez.
_SPEAK_LEAD_S = 1.0


def _squash(text: str) -> str:
    """Teams blokları boşluksuz birleştirebilir; yalnızca harf ve rakamlar karşılaştırılır."""
    return "".join(c for c in text.lower() if c.isalnum())


@dataclass
class _Speech:
    sentences: list[str]
    strict: bool
    hard: asyncio.Event = field(default_factory=asyncio.Event)
    soft: bool = False
    interrupted: bool = False
    label: str | None = None
    index: int = 0
    sent: int = 0
    started: bool = False
    stop_sent: bool = False
    ambient: set[str | None] = field(default_factory=set)
    growth: dict[str, tuple[float, int]] = field(default_factory=dict)
    decisions: dict[str, tuple[str, str]] = field(default_factory=dict)
    boundaries: list[int] = field(default_factory=list)
    playback: list[tuple[int, float, float]] = field(default_factory=list)
    soft_position: float | None = None
    stopped_position: float | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def played(self, rate: int) -> float:
        now = time.monotonic()
        position = 0.0
        for end, start_at, end_at in self.playback:
            if now < end_at:
                position = max(position, end - max(0, end_at - max(now, start_at)) * 2 * rate)
                break
            position = end
        if self.stopped_position is not None:
            position = self.stopped_position
        self.sent = sum(boundary <= position for boundary in self.boundaries)
        self.index = min(self.sent, max(0, len(self.sentences) - 1))
        return position


def _timestamp_key(text: str) -> str:
    return "".join(c for c in display_text(text) if c.isalnum() or c in ".?!…")


class SentenceTimestamps:
    def __init__(self, speech: _Speech, rate: int):
        self.speech, self.rate = speech, rate
        self.characters = ""
        self.times: list[float] = []
        self.keys = [_timestamp_key(sentence) for sentence in speech.sentences]

    def feed(self, timestamps: dict) -> None:
        for character, end in zip(
            timestamps["characters"], timestamps["character_end_times_seconds"], strict=True
        ):
            self.characters += character
            self.times.extend([end] * len(character))
        # Soniox boşlukları normalleştirir, emoji gibi işaretleri atar; etiketler aynen gelir.
        positions = []
        in_tag = False
        key = ""
        for index, character in enumerate(self.characters):
            if character == "[":
                in_tag = True
            elif character == "]":
                in_tag = False
            elif not in_tag and (character.isalnum() or character in ".?!…"):
                key += character
                positions.append(index)
        expected = ""
        for index, part in enumerate(self.keys):
            expected += part
            if len(expected) > len(key) or not key.startswith(expected):
                break
            if index >= len(self.speech.boundaries) and expected:
                seconds = self.times[positions[len(expected) - 1]]
                self.speech.boundaries.append(round(seconds * self.rate) * 2)


@contextlib.asynccontextmanager
async def _tts_stream(tts, text: str, language: str):
    if hasattr(tts, "stream"):
        async with tts.stream(language=language) as stream:
            await stream.send(text, end=True)
            yield stream
    else:
        # Eski sağlayıcılarda zaman damgası yok: tek üretim, yumuşak kesintide doğal bitiş.
        chunks = tts.synthesize(text, language=language)
        closed = False

        async def close():
            nonlocal closed
            if not closed and hasattr(chunks, "aclose"):
                closed = True
                await chunks.aclose()

        class LegacyStream:
            async def cancel(self):
                pass

            async def __aiter__(self):
                try:
                    while True:
                        try:
                            yield TTSEvent(await anext(chunks))
                        except StopAsyncIteration:
                            return
                finally:
                    await close()

        try:
            yield LegacyStream()
        finally:
            await close()


class SpeechPlayback:
    def say(
        self, text: str, *, channel: str | None = None, record: bool = True, url: str | None = None
    ) -> None:
        """Cevabı panele ve transkripte yazar; toplantıya moduna göre ya tamamen sesli ya
        tamamen yazılı iletir.

        `channel` verilirse mod yerine o kanal kullanılır (ör. aydınlatma metni hep chat'e).
        Metin "[en] …" gibi bir dil etiketiyle başlıyorsa o dilde seslendirilir.
        """
        lang = self.source_lang
        if m := _LANG_TAG.match(text):
            lang, text = m.group(1), text[m.end() :]
        audible = tts_text(text)
        text = display_text(text)
        if not text.strip():
            return
        if record:
            self._publish(PanelEvent(kind="note", text=text, url=url))
            self._record_reply(text)
        if self.mode == "kayit":
            channel = "chat"  # sessiz kayıt: hiçbir şey sesli söylenmez
        if (channel or self.reply_mode) == "voice":
            self._spawn(self._speak(audible, lang))
        else:
            self._chat(text)

    def _record_reply(self, text: str) -> None:
        """Katip'in söylediği de toplantının parçası: transkripte ve rapora "Katip" olarak girer."""
        if self.paused:
            return  # kayıt dışıyken Katip'in söyledikleri de kayda geçmez
        self._reply_count += 1
        base = self._first_stream_at
        at = max(0.0, time.monotonic() - base) if base is not None else 0.0
        segment = Segment(
            id=f"katip-{self.session_id}-{self._reply_count}",
            session_id=self.session_id,
            speaker=KATIP_SPEAKER,
            speaker_label="katip",
            start_s=at,
            end_s=at,
            text=text,
            lang=self.source_lang,
            is_final=True,
        )
        self.segments[segment.id] = segment
        self._seg_wall[segment.id] = time.time()
        self._publish(PanelEvent(kind="segment", segment=segment))
        self._store(segment)

    def channel_busy(self) -> bool:
        """Son birkaç saniyede biri konuştu mu (kesinleşmemiş metin geldi mi)?"""
        window = get_settings().voice_busy_window_s
        return time.monotonic() - self._last_partial_at < window

    def _chat(self, text: str) -> None:
        now = time.monotonic()
        while self._sent_chat and now - self._sent_chat[0][0] > 600:
            self._sent_chat.popleft()
        self._sent_chat.append((now, _squash(text)))
        self.chat_router.send(text)

    def _browser_chat(self, text: str) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self.outbox.put_nowait({"kind": "chat_send", "text": text})

    def set_reply_mode(self, mode: str) -> None:
        if mode not in ("chat", "voice"):
            raise ValueError(f"geçersiz cevap modu: {mode}")
        if mode == "voice" and self.mode == "kayit":
            raise ValueError("sessiz kayıt modunda sesli cevap kapalı")
        self.reply_mode = mode
        self._publish(PanelEvent(kind="mode", text=mode))
        log.info("cevap modu", session_id=self.session_id, mode=mode)

    async def _speak(self, text: str, lang: str | None = None, *, strict: bool = False) -> bool:
        lang = lang or self.source_lang
        settings = get_settings()
        async with self._speech_lock:
            self._speech_interrupted = False
            waiting = time.monotonic()
            deadline = waiting + settings.voice_floor_wait_s
            # Kısmi metinlerin gelişi için olay yok; kısa aralıklarla boşluğu denetle.
            while self.channel_busy() and time.monotonic() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
            if self.channel_busy():
                log.info(
                    "ses kanalı meşgul, yine de konuşuluyor",
                    waited_s=round(time.monotonic() - waiting, 2),
                )
            if self._tts is None:
                try:
                    from katip.pipeline.session import _build_tts

                    self._tts = _build_tts()
                except Exception as exc:
                    # Ses üretilemiyorsa cevap kaybolmasın: bu mesaj chat'e gider.
                    log.warning(
                        "sesli cevap verilemiyor, chat'e yazılıyor", error_type=type(exc).__name__
                    )
                    if not strict:
                        self._chat(display_text(text))
                    return False

            def pronounce(value):
                value = apply_lexicon(value, settings.tts_lexicon)
                return normalize_tr(value) if lang == "tr" and value.strip() else value

            spoken = transform_text(tts_text(text), pronounce)
            speech = _Speech(split_sentences(display_text(spoken)), strict)
            self._speech = speech
            self._speak_task = asyncio.current_task()
            rate = self._tts.sample_rate
            audio_bytes = 0
            synthesized_bytes = 0
            playback_until = 0.0
            step = rate * 2 * _SPEAK_CHUNK_MS // 1000
            cushion = rate * 2 * _SPEAK_CUSHION_MS // 1000
            first = True
            timeline = SentenceTimestamps(speech, rate)
            buffer = bytearray()
            changed = speech.changed
            done = False
            error = None
            reader = None
            soft_stopped = False
            t0 = time.monotonic()

            async def receive(stream):
                nonlocal synthesized_bytes, done, error
                try:
                    iterator = aiter(stream)
                    while True:
                        try:
                            async with asyncio.timeout(
                                min(
                                    settings.provider_attempt_timeout_s,
                                    settings.provider_deadline_s,
                                )
                            ):
                                event = await anext(iterator)
                        except StopAsyncIteration:
                            break
                        synthesized_bytes += len(event.audio)
                        if event.timestamps:
                            timeline.feed(event.timestamps)
                        buffer.extend(event.audio)
                        changed.set()
                except Exception as exc:
                    error = exc
                finally:
                    done = True
                    changed.set()

            # Mikrofon açılışı sağlayıcı beklemesiyle örtüşsün; ortam etiketleri yine ilk
            # ses paketi gönderildiği anda belirlenir.
            await self.outbox.put({"kind": "speak_start", "sample_rate": rate})
            # Tarayıcı ilk sesi speak_start'tan en erken 0,3 sn sonra çalar (capture.js).
            playback_until = time.monotonic() + 0.3

            async def wait_audio():
                item = asyncio.create_task(changed.wait())
                stop = asyncio.create_task(speech.hard.wait())
                try:
                    await asyncio.wait((item, stop), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in (item, stop):
                        task.cancel()
                    await asyncio.gather(item, stop, return_exceptions=True)

            async def pace(until):
                remaining = until - time.monotonic()
                if remaining > 0 and not speech.hard.is_set():
                    try:
                        await asyncio.wait_for(speech.hard.wait(), remaining)
                    except TimeoutError:
                        pass

            async def push(pcm):
                nonlocal audio_bytes, playback_until, first
                if speech.hard.is_set():
                    return
                if first:
                    first = False
                    now = time.monotonic()
                    speech.ambient = self._ambient_labels | {
                        label
                        for label, at in self._label_activity.items()
                        if now - at <= settings.voice_ambient_window_s
                    }
                    speech.started = True
                    log.info(
                        "ses üretimi başladı",
                        session_id=self.session_id,
                        lang=lang,
                        first_audio_ms=round((now - t0) * 1000),
                    )
                await self._send_audio(pcm, rate)
                audio_bytes += len(pcm)
                # Tarayıcının çalma sırası: parça, öncekinin bittiği ya da geldiği an çalar.
                start_at = max(playback_until, time.monotonic())
                playback_until = start_at + len(pcm) / (2 * rate)
                speech.playback.append((audio_bytes, start_at, playback_until))
                self._speech_until = playback_until

            try:
                async with _tts_stream(self._tts, spoken, lang) as stream:
                    reader = asyncio.create_task(receive(stream))
                    try:
                        while not speech.hard.is_set():
                            changed.clear()
                            speech.played(rate)
                            target = None
                            if speech.soft:
                                position = max(speech.soft_position or 0, audio_bytes)
                                target = next(
                                    (
                                        end
                                        for end in speech.boundaries
                                        if end >= position and end > 0
                                    ),
                                    None,
                                )
                            if target is not None and audio_bytes >= target:
                                soft_stopped = True
                                await stream.cancel()
                                break
                            boundary_ready = any(
                                audio_bytes < end <= audio_bytes + len(buffer)
                                for end in speech.boundaries
                            )
                            if error and len(buffer) < step and not boundary_ready:
                                raise error
                            if done and not buffer:
                                break
                            if not buffer:
                                await wait_audio()
                                continue
                            # Bilinen cümle sınırının ötesini o cümle çalınmadan sıraya koyma.
                            if audio_bytes in speech.boundaries and audio_bytes > 0:
                                await pace(playback_until)
                                speech.played(rate)
                                if speech.soft or speech.hard.is_set():
                                    continue
                            available = len(buffer)
                            next_end = next(
                                (end for end in speech.boundaries if end > audio_bytes), None
                            )
                            if not done and not boundary_ready and available < step:
                                await wait_audio()
                                continue
                            if (
                                not done
                                and not boundary_ready
                                and time.monotonic() >= playback_until
                                and available < cushion
                            ):
                                await wait_audio()
                                continue
                            limit = min(step, available)
                            if next_end is not None:
                                limit = min(limit, next_end - audio_bytes)
                            if target is not None:
                                limit = min(limit, target - audio_bytes)
                            await push(bytes(buffer[:limit]))
                            del buffer[:limit]
                            await pace(playback_until - _SPEAK_LEAD_S)
                        if speech.hard.is_set():
                            await stream.cancel()
                        else:
                            if not audio_bytes:
                                raise ValueError("TTS boş cümle döndürdü")
                            await self.outbox.put({"kind": "speak_end"})
                            await pace(playback_until + 0.4)
                            if speech.hard.is_set():
                                await stream.cancel()
                            speech.played(rate)
                            if (
                                done
                                and not buffer
                                and not soft_stopped
                                and not speech.hard.is_set()
                            ):
                                speech.sent = len(speech.sentences)
                    except BaseException:
                        with contextlib.suppress(Exception):
                            await stream.cancel()
                        raise
                    finally:
                        reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
                return bool(audio_bytes) and not (speech.soft or speech.hard.is_set())
            except asyncio.CancelledError:
                self._barge_in()
                raise
            except Exception as exc:
                log.warning(
                    "sesli cevap üretilemedi, chat'e yazılıyor", error_type=type(exc).__name__
                )
                self._barge_in()
                # Gönderilmiş cümleleri tekrar etme; yalnızca henüz gönderilmeyenler chat'e.
                submitted = 0
                if audio_bytes:
                    submitted = (
                        sum(end < audio_bytes for end in speech.boundaries) + 1
                        if speech.boundaries or hasattr(self._tts, "stream")
                        else len(speech.sentences)
                    )
                remainder = " ".join(speech.sentences[submitted:])
                if not strict and remainder:
                    self._chat(remainder)
                return False
            finally:
                if reader is not None:
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                if self._persist and synthesized_bytes:
                    self._spawn(usage.record_tts(self.session_id, synthesized_bytes / (2 * rate)))
                self._speak_task = None
                self._speech = None
                # Bundan sonra await yok: sunum önce duraklama durumuna geçer, ardından
                # kesinti turu uyanabilir. Temizlik sırasında sunumu yeniden başlatmasın.
                if speech.interrupted:
                    self._speech_interrupted = True
                    remainder = " ".join(speech.sentences[speech.sent :])
                    if strict and self.presentation.narrating and not speech.hard.is_set():
                        self.presentation.remainder = (self.presentation.position(), remainder)
                    self.agent.finish_interruption(
                        spoken=" ".join(speech.sentences[: speech.sent]),
                        remainder=remainder,
                        hard=speech.hard.is_set(),
                        presentation=strict,
                    )

    async def _send_audio(self, pcm: bytes, rate: int) -> None:
        import base64

        await self.outbox.put(
            {"kind": "speak_chunk", "sample_rate": rate, "pcm": base64.b64encode(pcm).decode()}
        )

    def _observe_floor(self, utt: Utterance) -> None:
        speech = self._speech
        if speech is None or not speech.started:
            return
        text = utt.text
        wake = detect_wake(text) is not None
        position = speech.played(self._tts.sample_rate)
        # Çalmakta olan cümlenin yankısı da mikrofona dönebilir.
        spoken = " ".join(speech.sentences[: speech.index + 1])
        kind, reason = overlap_decision(
            text,
            spoken=spoken,
            is_final=utt.is_final,
            wake=wake,
            min_words=5 if speech.strict else 3,
        )
        if kind != "hard" and is_echo(text, spoken):
            self._echo_ids.add(utt.id)
            kind, reason = "ignore", "echo"
        if utt.speaker_label in speech.ambient and not wake and kind != "hard" and reason != "echo":
            kind, reason = "ignore", "ambient"
        words = len(content_words(text))
        now = time.monotonic()
        length = len(text)
        previous = speech.growth.get(utt.id)
        if previous is None:
            speech.growth[utt.id] = (now, length)
        elif length > previous[1] and reason not in {"ambient", "echo", "backchannel"}:
            speech.growth[utt.id] = (previous[0], length)
            if words >= 2 and now - previous[0] >= get_settings().voice_interrupt_sustain_s:
                if kind != "hard":
                    kind, reason = "soft", "sustained"
        if speech.decisions.get(utt.id) != (kind, reason):
            speech.decisions[utt.id] = (kind, reason)
            log.info(
                "söz kesme kararı",
                session_id=self.session_id,
                kind=kind,
                reason=reason,
                label=utt.speaker_label,
                words=words,
                final=utt.is_final,
                sentence_index=speech.index + 1,
                sentence_total=len(speech.sentences),
            )
        if kind == "ignore" or (speech.interrupted and kind != "hard"):
            return
        if not speech.interrupted:
            speech.interrupted = True
            speech.label = utt.speaker_label
            self.agent.begin_interruption(utt.speaker_label)
        log.info(
            "konuşma kesildi",
            kind=kind,
            label=utt.speaker_label,
            sentence_index=speech.index + 1,
            sentence_total=len(speech.sentences),
            words=words,
        )
        if kind == "hard":
            self._barge_in()
        else:
            speech.soft = True
            speech.soft_position = position
            speech.changed.set()

    def _barge_in(self) -> None:
        """Sesi hemen kes; sunumun çalıştığı görevi iptal etme."""
        speech = self._speech
        if speech is not None:
            speech.stopped_position = speech.played(self._tts.sample_rate)
            speech.hard.set()
        if (speech is not None and not speech.stop_sent) or (
            speech is None and time.monotonic() < self._speech_until
        ):
            if speech is not None:
                speech.stop_sent = True
            self._speech_until = 0.0
            self.outbox.put_nowait({"kind": "speak_stop"})

    def learn_interruption(self, label: str | None) -> None:
        self._noisy_interruptions[label] += 1
        if self._noisy_interruptions[label] >= 2:
            self._ambient_labels.add(label)
