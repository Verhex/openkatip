from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from katip.agent import brain
from katip.agent.language import explicit_language, needs_language_repair


def response(text, *uses):
    return NS(
        content=[NS(type="text", text=text), *uses],
        usage=NS(input_tokens=0, output_tokens=0),
        stop_reason="tool_use" if uses else "end_turn",
    )


@pytest.mark.parametrize(
    "request_text,expected",
    [
        ("Toplantıyı özetler misin?", True),
        ("Bu neden oldu?", True),
        ("Tekrar eder misin?", True),
        ("İngilizce söyle", False),
        ("Bunu Almancaya çevir", False),
        ("in English please", False),
        ("Can you explain the meeting?", False),
        ("What happened?", False),
        ("İngilizce dokümanı Türkçe özetle", False),
    ],
)
def test_language_guard_heuristic(request_text, expected):
    assert needs_language_repair("[en] An answer.", request_text, "tr") is expected
    assert not needs_language_repair("[tr] Bir cevap.", request_text, "tr")
    assert not needs_language_repair("[warm] Bir cevap.", request_text, "tr")


@pytest.mark.parametrize(
    "request_text",
    [
        "İngilizce cevap ver",
        "English please",
        "Translate this to German",
        "Fransızcaya tercüme et",
        "Cevabı İngilizce ver",
        "Lütfen İngilizce konuşur musun",
        "İngilizce olarak anlat",
        "İngilizce",
        "Katip, chat'e yazdığın fonksiyon listesini İspanyolca oku.",
        "Bunu Almanca seslendirir misin?",
    ],
)
def test_explicit_language_patterns(request_text):
    assert explicit_language(request_text)


def test_mentioning_foreign_document_is_not_a_language_request():
    text = "İngilizce dokümanı özetler misin?"
    assert not explicit_language(text)
    assert needs_language_repair("[en] The document.", text, "tr")


async def test_meeting_source_language_controls_repair():
    create = AsyncMock(side_effect=[response("[de] Antwort."), response("[en] Answer.")])
    result = await brain.run_turn(
        "Please explain the meeting",
        speaker=None,
        source="voice",
        context=brain.TurnContext(""),
        executor=AsyncMock(),
        source_lang="en",
        client=NS(messages=NS(create=create)),
    )
    assert result == "[en] Answer."
    assert create.call_args.kwargs["messages"][-1]["content"] == "Cevabı İngilizce ver."


@pytest.mark.parametrize(
    "request_text,second,count",
    [
        ("Toplantıyı özetler misin?", "Toplantıda bütçe konuşuldu.", 2),
        ("Bu neden oldu?", "[de] Noch eine Antwort.", 2),
        ("İngilizce söyle", "", 1),
        ("Can you explain the meeting?", "", 1),
    ],
)
async def test_brain_repairs_foreign_reply_at_most_once(request_text, second, count):
    first = "[en] The budget was discussed."
    create = AsyncMock(side_effect=[response(first), response(second)])
    result = await brain.run_turn(
        request_text,
        speaker="Ayşe",
        source="voice",
        context=brain.TurnContext(""),
        executor=AsyncMock(),
        client=NS(messages=NS(create=create)),
    )
    assert result == (second if count == 2 else first)
    assert create.await_count == count
    if count == 2:
        assert create.call_args.kwargs["messages"][-1]["content"] == "Cevabı Türkçe ver."


async def test_duplicate_continuation_is_executed_once():
    uses = [NS(type="tool_use", name="konusmaya_devam_et", input={}, id=f"u{i}") for i in range(2)]
    create = AsyncMock(return_value=response("Devam ediyorum.", *uses))
    execute = AsyncMock(return_value="Sistem okuyacak.")
    assert (
        await brain.run_turn(
            "Devam et",
            speaker="Ayşe",
            source="voice",
            context=brain.TurnContext(""),
            executor=execute,
            client=NS(messages=NS(create=create)),
            interruption={"spoken": "Bir.", "remainder": "İki.", "speaker": "Ayşe"},
        )
        == "Devam ediyorum."
    )
    execute.assert_awaited_once_with("konusmaya_devam_et", {})


async def test_language_retry_keeps_continuation_idempotent():
    uses = [NS(type="tool_use", name="konusmaya_devam_et", input={}, id=f"u{i}") for i in range(2)]
    create = AsyncMock(
        side_effect=[
            response("**[en] Continuing.**", uses[0]),
            response("[en] Continue.", uses[1]),
        ]
    )
    execute = AsyncMock(return_value="Sistem okuyacak.")
    result = await brain.run_turn(
        "Devam eder misin?",
        speaker="Ayşe",
        source="voice",
        context=brain.TurnContext(""),
        executor=execute,
        client=NS(messages=NS(create=create)),
        interruption={"spoken": "Bir.", "remainder": "İki.", "speaker": "Ayşe"},
    )
    assert result == "[en] Continue."
    assert create.await_count == 2
    execute.assert_awaited_once_with("konusmaya_devam_et", {})


def test_prompt_contains_interruption_and_voice_only_expression_rules():
    voice = brain._system(None, [], "voice")
    chat = brain._system(None, [], "chat")
    for phrase in (
        "tekrar eder misin",
        "konusmaya_devam_et çağırma",
        "1–2 cümleyle yeniden anlat",
        '"kesinti var"',
        "Onay verdiyse",
        "Susman istendiyse",
        "isteğin tamamı başka",
        "Toplantının dilinde (Türkçe)",
    ):
        assert phrase in voice
    assert "[chuckles]" in voice and "[chuckles]" not in chat
    assert "kimseye gülme" in voice and "kişisel veride etiket yok" in voice
    assert "Chat kanalında ifade etiketi yazma" in chat
    assert "Toplantının dilinde (Almanca)" in brain._system(None, [], "voice", "de")


@pytest.mark.parametrize(
    "request_text,count",
    [("Katip, neler yapabiliyorsun anlat.", 1), ("Katip, bunu not al.", 2)],
)
async def test_claim_guard_skips_ability_questions(request_text, count):
    first = "Toplantıyı dinleyip not alıyorum, sorulara cevap veriyorum."
    create = AsyncMock(side_effect=[response(first), response("Ne kaydedeyim?")])
    await brain.run_turn(
        request_text,
        speaker="Ayşe",
        source="voice",
        context=brain.TurnContext(""),
        executor=AsyncMock(),
        client=NS(messages=NS(create=create)),
    )
    assert create.await_count == count
    if count == 2:
        assert "bahsetme" in create.call_args.kwargs["messages"][-1]["content"]


def test_prompt_names_no_third_party_products_and_knows_presentation():
    voice = brain._system(None, [], "voice")
    assert "Monday" not in voice
    assert "Katip, sunumu başlat" in voice
    assert "Konuşur gibi yaz" in voice and "Konuşur gibi yaz" not in brain._system(None, [], "chat")
