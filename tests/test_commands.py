import pytest

from katip.agent.commands import Intent, Level, classify, detect_wake, parse_slash, parse_voice


@pytest.mark.parametrize(
    ("text", "rest"),
    [
        ("Katip, bunu not al", "bunu not al"),
        ("katibim özetler misin", "özetler misin"),
        ("Katip'e bir soru: bütçe ne oldu", "bir soru bütçe ne oldu"),
        ("Bunu bir özetle Katip", "bunu bir özetle"),
        ("KATİP sesimi tanı", "sesimi tanı"),
    ],
)
def test_wake_word_variants(text: str, rest: str) -> None:
    assert detect_wake(text) == rest


def test_no_wake_word() -> None:
    assert detect_wake("Bütçe tablosunu cuma hazırlarım") is None
    assert parse_voice("Bütçe tablosunu cuma hazırlarım") is None


@pytest.mark.parametrize(
    ("utterance", "intent"),
    [
        ("Katip, sesimi tanı", Intent.ENROLL),
        ("Katip beni unut", Intent.FORGET),
        ("Katip, şu ana kadar ne konuştuk?", Intent.SUMMARY),
        ("Katip özetle", Intent.SUMMARY),
        ("Katip, not al: fiyatlar Kasım'da güncellenecek", Intent.NOTE),
        ("Katip aksiyon ekle Ayşe teklifi hazırlayacak", Intent.ACTION),
        ("Katip, çeviriyi İngilizce yap", Intent.LANGUAGE),
        ("Katip, talimat: raporları İngilizce hazırla", Intent.ADMIN_DIRECTIVE),
        ("Katip, geçen hafta bütçe için ne karar vermiştik?", Intent.ASK),
        ("Katip", Intent.LISTEN),
    ],
)
def test_voice_intents(utterance: str, intent: Intent) -> None:
    cmd = parse_voice(utterance)
    assert cmd is not None and cmd.intent is intent and cmd.source == "voice"


def test_note_body_and_language_arg() -> None:
    note = parse_voice("Katip, not al: fiyatlar Kasım'da güncellenecek")
    assert note.text == "fiyatlar kasım'da güncellenecek"
    lang = parse_voice("Katip çeviriyi Almanca yap")
    assert lang.arg == "de"
    directive = parse_voice("Katip, talimat: raporları İngilizce hazırla")
    assert directive.text == "raporları ingilizce hazırla"


@pytest.mark.parametrize(
    "utterance",
    [
        "Katip, çeviri dili İngilizce.",
        "Katip, çeviri dili İngilizce olarak aktif et.",
        "Katip, çeviri dili İngilizce ayarla.",
        "Katip, çeviri dili İngilizce etkinleştir.",
    ],
)
def test_live_translation_language_setting(utterance):
    command = parse_voice(utterance)
    assert command.intent is Intent.LANGUAGE and command.arg == "en"
    assert command.level is Level.PUBLIC


@pytest.mark.parametrize(
    "utterance", ["Katip, çeviri dili İngilizce mi?", "Katip, çeviri dili İngilizce olmasın."]
)
def test_language_questions_and_negation_do_not_change_settings(utterance):
    assert parse_voice(utterance).intent is Intent.ASK


def test_consent_and_confirm_without_wake_word() -> None:
    assert classify("Onaylıyorum", "voice", "Onaylıyorum").intent is Intent.CONSENT
    assert classify("Evet, unut", "voice", "Evet, unut").intent is Intent.CONFIRM_FORGET
    assert classify("vazgeç", "voice", "vazgeç").intent is Intent.CANCEL


def test_slash_commands() -> None:
    assert parse_slash("/özet").intent is Intent.SUMMARY
    note = parse_slash("/not Müşteri demo istedi")
    assert (
        note.intent is Intent.NOTE and note.text == "Müşteri demo istedi" and note.source == "chat"
    )
    assert parse_slash("/dil en").arg == "en"
    assert parse_slash("/bilinmeyen").intent is Intent.HELP
    assert parse_slash("merhaba") is None


def test_levels() -> None:
    assert parse_slash("/özet").level is Level.PUBLIC
    assert parse_voice("Katip beni unut").level is Level.SELF
    assert parse_slash("/admin herkese özet at").level is Level.ADMIN


@pytest.mark.parametrize(
    "utterance",
    [
        "Hoş geldin, Katip mi diyeyim, ne diyeyim?",  # canlı Teams testinde yanlış tetiklemişti
        "Toplantıyı Katip de not alıyor zaten",
        "Bizim bu Katip gibi bir araç lazım",
        "Dün akşam Katip hakkında konuştuk ama karar vermedik",
    ],
)
def test_mentions_of_katip_are_not_commands(utterance: str) -> None:
    assert parse_voice(utterance) is None


def test_real_commands_still_work_after_mention_filter() -> None:
    assert parse_voice("Katip, özetle").intent is Intent.SUMMARY
    assert parse_voice("Bunu bir özetle Katip").intent is Intent.SUMMARY
    assert parse_voice("Katip, bütçe için ne karar vermiştik?").intent is Intent.ASK
    assert parse_voice("Katip'e bir sorum var: bütçe ne durumda?").intent is Intent.ASK
    assert (
        parse_voice("Arkadaşlar bir dakika, Katip not al: fiyatlar değişti").intent is Intent.NOTE
    )


# Canlı Teams testinde (30 Eylül) söylenen gerçek cümleler ve olması gereken davranış.
@pytest.mark.parametrize(
    ("utterance", "intent", "body"),
    [
        ("Katip, bir not al, test notu yaz.", Intent.NOTE, "test notu yaz"),
        ("Katip, ben Deniz. Beni kaydet.", Intent.ENROLL, None),
        ("Katip, KVKK bilgilendirmesi yap.", Intent.PRIVACY_NOTICE, None),
        ("Katip.", Intent.LISTEN, None),
        ("Katip, sessiz moda geç.", Intent.REPLY_MODE, None),
        ("Katip, notları aldın mı?", Intent.ASK, None),
        ("Fiyatlar değişti Katip bunu not al", Intent.NOTE, "fiyatlar değişti"),
    ],
)
def test_live_meeting_commands(utterance, intent, body) -> None:
    cmd = parse_voice(utterance)
    assert cmd is not None and cmd.intent is intent
    if body is not None:
        assert cmd.text == body


@pytest.mark.parametrize(
    "utterance",
    [
        "Katip konuşuyor.",
        "Bence yakıştı katip.",
        "Adresine bağlayacağım. Böyle bot gibi gelmeyecek de. Katip.",
    ],
)
def test_live_meeting_chatter_is_ignored(utterance) -> None:
    assert parse_voice(utterance) is None


# İkinci canlı Teams testi (30 Eylül öğleden sonra).
@pytest.mark.parametrize(
    ("utterance", "intent"),
    [
        ("Katip. Ben Deniz. Beni kaydet.", Intent.ENROLL),
        ("Katip. Tüm komutlar nelerdir?", Intent.HELP),
        ("Katip. Bu toplantıyı dokümante et.", Intent.REPORT),
        ("Katip, raporu hazırla", Intent.REPORT),
        (
            "Katip. Bir sonraki toplantıda da beni tanıyacak mısın, yoksa sadece bu toplantıda mı?",
            Intent.ASK,
        ),
    ],
)
def test_second_live_meeting_commands(utterance, intent) -> None:
    assert parse_voice(utterance).intent is intent


def test_mentions_with_suffixes_do_not_wake_katip() -> None:
    for text in (
        "Katipimiz yazıyor mu? Evet yazıyor.",
        "Katip'i geçirmen lazım abi.",
        "Bu Katip'in yeni özelliği.",
        "Katipte bir sorun var galiba.",
    ):
        assert parse_voice(text, strict=False) is None, text
    for text in (
        "Katip, not al: demo cuma.",
        "Katibim, özetler misin?",
        "Katip'e bir sorum var: bütçe?",
    ):
        assert parse_voice(text, strict=False) is not None, text
