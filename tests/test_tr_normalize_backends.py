"""Toplantı corpus'u, ünlü uyumu ve hata halinde yerel kurallara dönüş."""

from __future__ import annotations

import builtins
import json
import runpy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from katip.config import Settings, get_settings
from katip.voice import tr_normalize as tr
from katip.voice.tts import apply_lexicon

CASES = json.loads((Path(__file__).parent / "data/tr_normalize_cases.json").read_text("utf-8"))


@pytest.fixture(params=["auto", "ntr", "katip"])
def backend(request, monkeypatch):
    monkeypatch.setenv("KATIP_TTS_NORMALIZER", request.param)
    get_settings.cache_clear()
    if request.param != "katip" and tr._normalizer is None:
        pytest.skip("normalizer-tr kurulu değil")
    return request.param


def test_corpus_has_required_size_and_independent_expectations():
    assert len(CASES) >= 120
    assert len({case["input"] for case in CASES}) == len(CASES)
    assert all(set(case) == {"input", "expected", "requires", "note"} for case in CASES)
    assert all(case["requires"] in {"any", "ntr"} and case["note"] for case in CASES)


@pytest.mark.parametrize("case", CASES, ids=[case["input"] for case in CASES])
def test_meeting_corpus(case, backend):
    spoken = tr.normalize_tr(case["input"])
    if backend == "katip" and case["requires"] == "ntr":
        # IBAN'ın yerel yedekte korunması kabul edilebilir; parçalı sayı dönüşümü değildir.
        assert spoken == case["input"]
    else:
        assert spoken == case["expected"]
    assert not any(broken in spoken for broken in ("liraden", "kuruşlik", "bir.0"))


# Son sözcüklerin tümü; beklenen ekler normalleştiriciden bağımsız yazılmıştır.
# Sıra: bulunma, ayrılma, belirtme, yönelme, tamlayan, araç, sıra sayısı.
ENDINGS = [
    (0, "sıfır", "da dan ı a ın la ıncı"),
    (1, "bir", "de den i e in le inci"),
    (2, "iki", "de den yi ye nin yle nci"),
    (3, "üç", "te ten ü e ün le üncü"),
    (4, "dört", "te ten ü e ün le üncü"),
    (5, "beş", "te ten i e in le inci"),
    (6, "altı", "da dan yı ya nın yla ncı"),
    (7, "yedi", "de den yi ye nin yle nci"),
    (8, "sekiz", "de den i e in le inci"),
    (9, "dokuz", "da dan u a un la uncu"),
    (10, "on", "da dan u a un la uncu"),
    (20, "yirmi", "de den yi ye nin yle nci"),
    (30, "otuz", "da dan u a un la uncu"),
    (40, "kırk", "ta tan ı a ın la ıncı"),
    (50, "elli", "de den yi ye nin yle nci"),
    (60, "altmış", "ta tan ı a ın la ıncı"),
    (70, "yetmiş", "te ten i e in le inci"),
    (80, "seksen", "de den i e in le inci"),
    (90, "doksan", "da dan ı a ın la ıncı"),
    (100, "yüz", "de den ü e ün le üncü"),
    (1000, "bin", "de den i e in le inci"),
    (10**6, "bir milyon", "da dan u a un la uncu"),
    (10**9, "bir milyar", "da dan ı a ın la ıncı"),
    (10**12, "bir trilyon", "da dan u a un la uncu"),
]


@pytest.mark.parametrize(("number", "word", "endings"), ENDINGS)
@pytest.mark.parametrize("index", range(7))
def test_all_numeral_endings(number, word, endings, index, backend):
    ending = endings.split()[index]
    softened = word[:-1] + "d" if number == 4 and index in {2, 3, 4, 6} else word
    expected = softened + ending
    if index == 6:
        assert tr.ordinal_words(number) == expected
        source = f"{number}. gündem"
    else:
        source = f"{number}'{ending} gündem"
    assert tr.normalize_tr(source) == expected + " gündem"


@pytest.mark.parametrize("word", ["kap", "ağaç", "dört", "kırk", "raf", "şah", "ses", "beş"])
def test_hard_consonants(word):
    # Sayılar dışındaki tutar/birim sonlarında da aynı ek uyumu uygulanır.
    expected_vowel = "a" if word in {"kap", "ağaç", "kırk", "raf", "şah"} else "e"
    assert tr._attach_suffix(word, "de") == word + "t" + expected_vowel


@pytest.mark.parametrize("backend", ["auto", "ntr", "katip"])
def test_setting_switch(backend, monkeypatch):
    monkeypatch.setenv("KATIP_TTS_NORMALIZER", backend)
    assert Settings(_env_file=None).tts_normalizer == backend
    normalizer = Mock()
    normalizer.normalize.return_value = SimpleNamespace(
        segments=[SimpleNamespace(kind="cardinal", text="beş")]
    )
    monkeypatch.setattr(tr, "_normalizer", normalizer)
    assert tr.normalize_tr("5") == "beş"
    if backend == "katip":
        normalizer.normalize.assert_not_called()
    else:
        normalizer.normalize.assert_called_once_with(
            "5", ambiguity_policy="preserve", deadline_ms=50
        )


@pytest.mark.parametrize("backend", ["auto", "ntr"])
@pytest.mark.parametrize(
    ("source", "spoken"),
    [
        ("2,5'a", "iki buçuğa"),
        ("%1,5'luk", "yüzde bir buçukluk"),
        ("3,5 kg", "üç buçuk kilogram"),
        ("2,5 milyon dolar", "iki buçuk milyon dolar"),
    ],
)
def test_bucuk_is_spoken_before_normalizer_tr(backend, source, spoken, monkeypatch):
    monkeypatch.setenv("KATIP_TTS_NORMALIZER", backend)
    get_settings.cache_clear()
    normalizer = Mock()
    normalizer.normalize.return_value = SimpleNamespace(
        segments=[
            SimpleNamespace(kind="verbatim", start_byte=0, end_byte=len(spoken.encode("utf-8")))
        ],
    )
    monkeypatch.setattr(tr, "_normalizer", normalizer)
    assert tr.normalize_tr(source) == spoken
    normalizer.normalize.assert_called_once_with(
        spoken, ambiguity_policy="preserve", deadline_ms=50
    )


def test_default_and_invalid_setting(monkeypatch):
    monkeypatch.delenv("KATIP_TTS_NORMALIZER", raising=False)
    assert Settings(_env_file=None).tts_normalizer == "auto"
    monkeypatch.setenv("KATIP_TTS_NORMALIZER", "fallback")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("backend", ["auto", "ntr"])
@pytest.mark.parametrize("failure", [RuntimeError("özel toplantı metni"), TimeoutError("gizli")])
def test_exception_and_timeout_fallback_logs_once(backend, failure, monkeypatch):
    monkeypatch.setenv("KATIP_TTS_NORMALIZER", backend)
    normalizer = Mock()
    normalizer.normalize.side_effect = failure
    monkeypatch.setattr(tr, "_normalizer", normalizer)
    monkeypatch.setattr(tr, "_warned", False)
    logger = Mock()
    monkeypatch.setattr(tr, "log", logger)
    source = "1.0 sürümünde 1.250,50 TL'lik paket 25 TL'den ucuz."
    expected = (
        "bir nokta sıfır sürümünde bin iki yüz elli lira elli kuruşluk "
        "paket yirmi beş liradan ucuz."
    )
    for _ in range(2):
        assert tr.normalize_tr(source) == expected
    logger.warning.assert_called_once_with("tts_normalizer_katip_fallback")
    assert normalizer.normalize.call_count == 2


def test_native_deadline_fallback(monkeypatch):
    ntr = pytest.importorskip("normalizer_tr")
    failure = ntr.NormalizationError("limit_exceeded", "gizli", limit_kind="deadline")
    monkeypatch.setenv("KATIP_TTS_NORMALIZER", "ntr")
    monkeypatch.setattr(tr, "_normalizer", Mock(normalize=Mock(side_effect=failure)))
    assert tr.normalize_tr("25 TL'den") == "yirmi beş liradan"


@pytest.mark.parametrize("backend", ["auto", "ntr"])
def test_missing_import_uses_katip(backend, monkeypatch):
    monkeypatch.setenv("KATIP_TTS_NORMALIZER", backend)
    monkeypatch.setattr(tr, "_normalizer", None)
    monkeypatch.setattr(tr, "_warned", False)
    logger = Mock()
    monkeypatch.setattr(tr, "log", logger)
    assert tr.normalize_tr("25 TL'den") == "yirmi beş liradan"
    assert tr.normalize_tr("1.0 sürümünde") == "bir nokta sıfır sürümünde"
    logger.warning.assert_called_once_with("tts_normalizer_katip_fallback")


@pytest.mark.parametrize("failure", [ImportError, OSError, RuntimeError])
def test_import_and_constructor_failure(failure, monkeypatch):
    original_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "normalizer_tr":
            if failure is ImportError:
                raise ImportError("normalizer_tr")
            return SimpleNamespace(Normalizer=Mock(side_effect=failure("yerel modül")))
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    namespace = runpy.run_path(tr.__file__)
    assert namespace["_normalizer"] is None
    assert namespace["normalize_tr"]("25 TL'den") == "yirmi beş liradan"


def test_one_reusable_normalizer_across_threads(monkeypatch):
    if tr._normalizer is None:
        pytest.skip("normalizer-tr kurulu değil")
    monkeypatch.setenv("KATIP_TTS_NORMALIZER", "ntr")
    original = tr._normalizer
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(tr.normalize_tr, ["25 TL'den"] * 16))
    assert results == ["yirmi beş liradan"] * 16
    assert tr._normalizer is original


def test_lexicon_before_normalization(backend):
    spoken = apply_lexicon(
        "Example API'si 1.0 sürümünde.", {"Example": "Eksampıl", "API": "ey pi ay"}
    )
    assert tr.normalize_tr(spoken) == "Eksampıl ey pi aysi bir nokta sıfır sürümünde."


@pytest.mark.parametrize("language", ["tr", "en"])
async def test_session_language_and_lexicon_order(language, backend, monkeypatch):
    from katip.pipeline import session as session_mod
    from katip.pipeline import speech as speech_mod
    from katip.pipeline.stt.base import STTProvider

    class NoSTT(STTProvider):
        translates = True

        async def stream(self, audio, *, language, target_language=None):
            if False:
                yield

    seen = []

    class FakeTTS:
        sample_rate = 24000

        async def synthesize(self, text, *, language):
            seen.append((text, language))
            yield b"\0\0" * 240

    monkeypatch.setenv("KATIP_TTS_LEXICON", '{"API": "ey pi ay"}')
    get_settings.cache_clear()
    spy = Mock(wraps=tr.normalize_tr)
    monkeypatch.setattr(speech_mod, "normalize_tr", spy)
    monkeypatch.setattr(session_mod, "_build_tts", FakeTTS)
    session = session_mod.MeetingSession("normalizer", stt=NoSTT(), persist=False, voice=False)
    await session._speak("API 1.0", lang=language)
    if language == "tr":
        spy.assert_called_once_with("ey pi ay 1.0")
        assert seen == [("ey pi ay bir nokta sıfır", "tr")]
    else:
        spy.assert_not_called()
        assert seen == [("ey pi ay 1.0", "en")]


@pytest.mark.parametrize(
    "source",
    ["31.02.2026 tarihinde", "Q4 API CEO KPI", "abc123", "v2.5.1", "192.168.1.1", "IIII. yüzyıl"],
)
def test_ambiguous_or_invalid_identifiers_are_preserved(source, backend):
    assert tr.normalize_tr(source) == source


def test_long_number_does_not_interrupt_tts(backend):
    source = "9" * 5000
    assert tr.normalize_tr(source) == source


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("TL'ye geçelim.", "Türk lirasına geçelim."),
        ("TL'den vazgeçmeyelim.", "Türk lirasından vazgeçmeyelim."),
        ("KDV'ye dahil.", "katma değer vergisine dahil."),
    ],
)
def test_abbreviation_suffixes(source, expected, backend):
    assert tr.normalize_tr(source) == expected
