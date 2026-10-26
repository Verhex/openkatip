from __future__ import annotations

import pytest

from katip.pipeline.floor import is_echo, overlap_kind, split_sentences


def test_turkish_sentences_and_abbreviations():
    text = "Dr. Ayşe ve Prof. Ali konuştu. 3. slayt hazır! Sn. Av. Ece anlatsın?"
    assert split_sentences(text) == [
        "Dr. Ayşe ve Prof. Ali konuştu.",
        "3. slayt hazır!",
        "Sn. Av. Ece anlatsın?",
    ]
    assert split_sentences("Örn. bu ürün vb. seçenekler vs. örnekler. Sonraki cümle burada.") == [
        "Örn. bu ürün vb. seçenekler vs. örnekler.",
        "Sonraki cümle burada.",
    ]
    assert split_sentences("No. 3. belge bkz. ekler burada. İkinci uzun cümle burada.") == [
        "No. 3. belge bkz. ekler burada.",
        "İkinci uzun cümle burada.",
    ]


def test_short_merge_and_long_split():
    assert split_sentences("Evet. Bütçe raporunu bugün hazırlayacağım. Tamam.") == [
        "Evet. Bütçe raporunu bugün hazırlayacağım. Tamam."
    ]
    text = "Bu uzun anlatım " * 9 + ", ayrıca " + "diğer konular " * 10 + "; son bilgi burada."
    pieces = split_sentences(text)
    assert len(pieces) >= 2
    assert all(len(piece) <= 180 for piece in pieces)
    assert " ".join(pieces) == text.strip()
    assert split_sentences("Birinci uzun cümle burada…\nİkinci uzun cümle burada!") == [
        "Birinci uzun cümle burada…",
        "İkinci uzun cümle burada!",
    ]


@pytest.mark.parametrize(
    "text,final,expected",
    [
        ("dur", False, "ignore"),
        ("dur", True, "hard"),
        ("durum", True, "ignore"),
        ("Katip dur", False, "hard"),
        ("Katip bir dakika", False, "hard"),
        ("tamam yeter", True, "hard"),
        ("sus artık", True, "hard"),
        ("tamam tamam", True, "hard"),
        ("hı hı evet", False, "ignore"),
        ("hmm tamam", True, "ignore"),
        ("tamam", True, "ignore"),
        ("teşekkür ederim", True, "ignore"),
        ("Katip soracağım", False, "soft"),
        ("teslim tarihi neden değişti", False, "soft"),
        ("bu bütçeyi yarın konuşacağız", True, "ignore"),
    ],
)
def test_overlap(text, final, expected):
    assert (
        overlap_kind(
            text,
            spoken="Bu bütçeyi yarın konuşacağız.",
            is_final=final,
            wake=False,
            min_words=3,
        )
        == expected
    )


def test_presentation_requires_more_content():
    assert (
        overlap_kind(
            "teslim tarihi neden değişti",
            spoken="",
            is_final=False,
            wake=False,
            min_words=5,
        )
        == "ignore"
    )


def test_question_repeating_katips_words_is_not_echo():
    spoken = "Toplantı saat üçte başlayacak."
    assert is_echo("toplantı saat üçte başlayacak", spoken)
    # Soru Katip'in kelimelerini kullansa da yankı değildir; agent'a gitmeli.
    assert not is_echo("Katip, saat üçte mi başlayacak?", spoken)
    assert not is_echo("saat üçte mi başlayacak", spoken)


def test_objection_is_not_a_backchannel():
    kind = overlap_kind("yok onu demedim", spoken="", is_final=False, wake=False, min_words=3)
    assert kind == "soft"
