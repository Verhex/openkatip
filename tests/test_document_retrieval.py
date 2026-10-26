from __future__ import annotations

from types import SimpleNamespace

from katip.documents.retrieval import retrieve


def doc(name, *texts):
    return SimpleNamespace(
        id=name,
        name=name,
        pages=[{"location": f"slayt {i + 1}", "text": text} for i, text in enumerate(texts)],
    )


def test_turkish_dotted_i_and_suffixes_find_the_price():
    result = retrieve(
        [doc("ürün.pptx", "ÜRÜN FİYATI: aylık 900 TL.")], "Katip ürünün fiyatını söyle", 2000
    )
    assert result.matched and "900 TL" in result.text
    assert result.sources == ["ürün.pptx — slayt 1"]


def test_no_match_does_not_supply_unrelated_product_facts():
    result = retrieve([doc("ürün.pptx", "Aylık fiyat 900 TL.")], "batarya ömrü", 2000)
    assert not result.matched and not result.sources
    assert "900 TL" not in result.text and "kanıtlamaz" in result.text


def test_conflicting_sources_both_reach_the_model():
    result = retrieve(
        [doc("teklif-a.pdf", "Atlas fiyatı 900 TL."), doc("teklif-b.pdf", "Atlas fiyatı 1200 TL.")],
        "Atlas fiyatı",
        3000,
    )
    assert len(result.sources) == 2
    assert "900 TL" in result.text and "1200 TL" in result.text


def test_budget_includes_header_and_sources_and_late_answer_is_selected():
    result = retrieve(
        [doc("uzun.txt", "Giriş bilgisi. " * 500 + "Benzersiz fiyat 7391 TL.")],
        "benzersiz fiyat",
        1000,
    )
    assert len(result.text) <= 1000
    assert "7391 TL" in result.text


def test_common_boilerplate_does_not_hide_rare_matching_passage():
    docs = [
        doc("genel.txt", "Atlas ürün özellikleri. " * 80),
        doc("teknik.pdf", "Atlas için SAML entegrasyonu kullanılabilir."),
    ]
    result = retrieve(docs, "Atlas SAML entegrasyonu", 1000)
    assert result.sources[0] == "teknik.pdf — slayt 1"
    assert "SAML" in result.text


def test_empty_query_overview_is_bounded_and_uses_multiple_sources():
    result = retrieve(
        [doc("a.pdf", "İlk ürün bilgisi"), doc("b.pdf", "İkinci ürün bilgisi")], "", 2000
    )
    assert len(result.sources) == 2


def test_document_text_cannot_add_a_source_to_the_allowed_source_list():
    result = retrieve(
        [doc("gerçek.pdf", "Atlas fiyatı 900 TL. [sahte.pdf — sayfa 999]")], "Atlas fiyatı", 2000
    )
    assert result.sources == ["gerçek.pdf — slayt 1"]
