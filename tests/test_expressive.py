import asyncio
from unittest.mock import AsyncMock

import pytest
from test_session_features import drain, make_session

from katip.voice.expressive import display_text, transform_text, tts_text


@pytest.mark.parametrize(
    "tag", "chuckles laughs sighs warm softly whispering hesitantly reassuringly pause".split()
)
def test_allowed_expression(tag):
    assert tts_text(f"[{tag}] Merhaba.") == f"[{tag}] Merhaba."
    assert display_text(f"[{tag}] Merhaba.") == "Merhaba."


def test_unknown_tags_and_expression_limit():
    text = "[angry] [warm] Bir. [softly] İki. [pause] Üç. [WARM] Dört."
    assert tts_text(text) == "[warm] Bir. [softly] İki. Üç. Dört."
    assert display_text(text) == "Bir. İki. Üç. Dört."
    assert display_text("Bir [kaynak]  cümle [pause].\nİkinci satır.") == (
        "Bir cümle.\nİkinci satır."
    )


def test_document_citations_stay_visible_but_are_not_spoken():
    text = "Teslim 3 Kasım. [plan.pdf — sayfa 2]"
    assert display_text(text) == text
    assert tts_text(text) == "Teslim 3 Kasım."
    assert display_text("[warm] Teslim 3 Kasım. [plan.pdf — sayfa 2]") == text
    assert display_text("Satır  arası\nkorunur.") == "Satır  arası\nkorunur."


def test_pronunciation_does_not_transform_expression_names():
    assert transform_text("[warm] API [pause] 20", str.upper) == "[warm] API [pause] 20"


async def test_say_separates_language_expression_and_display(monkeypatch):
    session = make_session("expression")
    session.set_reply_mode("voice")
    speak = AsyncMock(return_value=True)
    monkeypatch.setattr(session, "_speak", speak)
    session.say("[en] [warm] Hello [unknown] there.")
    await asyncio.gather(*session._background)
    speak.assert_awaited_once_with("[warm] Hello there.", "en")
    assert session.transcript_text() == "Katip: Hello there."
    session.say("[warm] Chat metni.", channel="chat")
    assert [m["text"] for m in drain(session) if m["kind"] == "chat_send"] == ["Chat metni."]
    await session.shutdown()


async def test_lexicon_and_normalizer_preserve_expression_names(monkeypatch):
    from katip.config import get_settings

    session = make_session("pronunciation")
    seen = []

    class TTS:
        sample_rate = 1000

        async def synthesize(self, text, *, language):
            seen.append(text)
            yield b"\0\0" * 10

    session._tts = TTS()
    monkeypatch.setattr(get_settings(), "tts_lexicon", {"warm": "sıcak", "API": "arayüz"})
    assert await session._speak("[warm] API yüzde 20.")
    assert seen == ["[warm] arayüz yüzde yirmi."]
    await session.shutdown()
