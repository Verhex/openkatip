"""Türkçe TTS normalizasyonu: sesten bağımsız, doğru sayı/tutar/tarih okunuşu."""

import pytest

from katip.voice.tr_normalize import normalize_tr, number_words, ordinal_words


@pytest.mark.parametrize(
    ("n", "words"),
    [
        (0, "sıfır"),
        (7, "yedi"),
        (10, "on"),
        (19, "on dokuz"),
        (100, "yüz"),
        (101, "yüz bir"),
        (1000, "bin"),
        (1001, "bin bir"),
        (2026, "iki bin yirmi altı"),
        (42500, "kırk iki bin beş yüz"),
        (1_000_000, "bir milyon"),
        (1_250_000, "bir milyon iki yüz elli bin"),
        (3_000_000_000, "üç milyar"),
    ],
)
def test_number_words(n, words) -> None:
    assert number_words(n) == words


@pytest.mark.parametrize(
    ("n", "words"),
    [
        (1, "birinci"),
        (2, "ikinci"),
        (3, "üçüncü"),
        (4, "dördüncü"),
        (6, "altıncı"),
        (9, "dokuzuncu"),
        (10, "onuncu"),
        (40, "kırkıncı"),
        (100, "yüzüncü"),
        (1000, "bininci"),
    ],
)
def test_ordinals_follow_vowel_harmony(n, words) -> None:
    assert ordinal_words(n) == words


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        # Canlı denemede bazı seslerin "virgül sıfır sıfır" diye okuduğu cümle.
        ("42.500,00 ₺ tutarındaki faturanız", "kırk iki bin beş yüz lira tutarındaki faturanız"),
        ("21.000 TL", "yirmi bir bin lira"),
        ("₺1.250,75", "bin iki yüz elli lira yetmiş beş kuruş"),
        ("30 USD ve €15,50", "otuz dolar ve on beş avro elli sent"),
        ("oran %87,5", "oran yüzde seksen yedi buçuk"),
        ("% 12 arttı", "yüzde on iki arttı"),
        ("son tarih 15.10.2026", "son tarih on beş Ekim iki bin yirmi altı"),
        ("saat 14:30'da", "saat on dört otuzda"),
        ("saat 09:05", "saat dokuz sıfır beş"),
        ("3. çeyrek", "üçüncü çeyrek"),
        ("1.250.000 kullanıcı", "bir milyon iki yüz elli bin kullanıcı"),
        ("0,05 puan", "sıfır virgül sıfır beş puan"),
        ("0532 123 45 67", "sıfır beş yüz otuz iki yüz yirmi üç kırk beş altmış yedi"),
    ],
)
def test_normalize_sentences(text, spoken) -> None:
    assert normalize_tr(text) == spoken


def test_sentence_end_number_is_not_ordinal() -> None:
    assert normalize_tr("Toplam 40.") == "Toplam kırk."
    assert normalize_tr("Yıl 2026. Sonra") == "Yıl iki bin yirmi altı. Sonra"


def test_text_without_numbers_is_unchanged() -> None:
    s = "Deniz Bey, Selin Hanım ve Ayşe Yılmaz sonuçları paylaşacak."
    assert normalize_tr(s) == s
