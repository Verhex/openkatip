import html

import pytest
from pydantic import ValidationError

from katip import privacy, runtime_settings
from katip.config import get_settings


def test_server_location_default_and_notice_rules():
    sentence = "Katip şu an için yurt dışındaki sunucularda çalışmaktadır"
    assert get_settings().privacy_server_location == sentence
    for text in (privacy.privacy_notice_chat(), privacy.privacy_notice_spoken()):
        assert sentence in text
        assert "geliştirme aşamasındadır" in text
        assert "toplantıdan çık" in text
        assert "aktarılıyor" in text
        assert not any(
            name in text for name in ("Railway", "Example sunucu", "Anthropic", "Soniox")
        )
        assert "onaylıyorum" not in text
    assert "/ayrıl" in privacy.privacy_notice_chat()
    assert privacy.privacy_notice_chat().endswith(privacy.notice_url() + " adresinde.")
    assert privacy.late_joiner_notice().endswith(privacy.notice_url() + " adresinde.")


def test_environment_location_changes_all_notices_and_versions(monkeypatch):
    old_chat = privacy.version(privacy.privacy_notice_chat())
    old_spoken = privacy.version(privacy.privacy_notice_spoken())
    sentence = "Katip Türkiye'deki sunucularda çalışmaktadır"
    monkeypatch.setenv("KATIP_PRIVACY_SERVER_LOCATION", sentence)
    get_settings.cache_clear()
    assert sentence in privacy.privacy_notice_chat()
    assert sentence in privacy.privacy_notice_spoken()
    assert html.escape(sentence) in privacy.notice_page_html()
    assert "veriler yurt dışına aktarılıyor" in privacy.privacy_notice_chat()
    assert "veriler yurt dışına aktarılıyor" in privacy.privacy_notice_spoken()
    assert "yurt dışında bulunan" in privacy.notice_page_html()
    assert privacy.version(privacy.privacy_notice_chat()) != old_chat
    assert privacy.version(privacy.privacy_notice_spoken()) != old_spoken


async def test_panel_location_persists_and_html_is_escaped(database):
    sentence = "Katip Türkiye'deki <sunucularda> çalışmaktadır"
    await runtime_settings.update(
        runtime_settings.EditableSettings(privacy_server_location=sentence)
    )
    get_settings().privacy_server_location = "geçici"
    await runtime_settings.load()
    assert runtime_settings.current()["privacy_server_location"] == sentence
    assert "&lt;sunucularda&gt;" in privacy.notice_page_html()
    assert "<sunucularda>" not in privacy.notice_page_html()


@pytest.mark.parametrize("sentence", ["", "   ", "konum\nkomut", "x" * 301])
def test_panel_rejects_empty_control_and_oversized_location(sentence):
    with pytest.raises(ValidationError):
        runtime_settings.EditableSettings(privacy_server_location=sentence)
