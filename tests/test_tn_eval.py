"""Veri indirmeden karşılaştırma, sayı çözümleme ve örnekleme kontrolleri."""

import random
from decimal import Decimal

import pytest

from scripts.tn_eval.compare import (
    classify,
    comparison_text,
    numeric_signature,
    parse_integer_words,
)
from scripts.tn_eval.fetch import external_dir, sha256, verify_manifest
from scripts.tn_eval.run import balanced_quotas, reservoir_add, review_queue


def test_turkish_case_punctuation_and_explicit_styles():
    assert comparison_text("IŞIK, İZMİR!  ığdır") == "ışık izmir ığdır"
    assert comparison_text("dört'ün") == "dörtün"
    assert comparison_text("AVRO, Türk Lirası", equivalent_styles=True) == "euro lira"
    assert classify("5 TL", "beş Türk lirası", "beş lira").mismatch_class == "style_difference"
    assert classify("5", "BEŞ!", "beş").exact
    # Rakamsal veya para simgesi farkları noktalama temizliğinde gizlenmemelidir.
    assert not classify("5 €", "beş €", "beş euro").exact


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("sıfır", 0),
        ("eksi kırk iki", -42),
        ("yüz bir", 101),
        ("bir bin iki yüz", 1200),
        ("bir milyon iki yüz bin üç", 1200003),
        ("iki milyar üç milyon dört bin beş", 2003004005),
        ("bir trilyon", 10**12),
        ("iki üç", None),
        ("yüz yüz", None),
        ("bir bin bir milyon", None),
        ("sıfır beş", None),
        ("beşte", None),
        ("", None),
    ],
)
def test_integer_parser(words, expected):
    assert parse_integer_words(words) == expected


def test_numeric_signature_retains_decimal_zeros_and_sign():
    assert numeric_signature("eksi sıfır virgül sıfır beş litre")[0] == [Decimal("-0.05")]
    assert numeric_signature("üç virgül beş sıfır")[0] == [Decimal("3.50")]
    assert numeric_signature("üç virgül sıfır elli")[0] == [Decimal("3.050")]


@pytest.mark.parametrize(
    ("source", "output", "reference", "kind"),
    [
        ("42", "kırk üç", "kırk iki", "numeric_value_error"),
        ("3,05 litre", "üç virgül elli litre", "üç virgül sıfır beş litre", "numeric_value_error"),
        ("5'te", "beşde", "beşte", "suffix_harmony"),
        ("4'e", "dörte", "dörde", "suffix_harmony"),
        ("20 TL'ye", "yirmi liraya", "yirmi liraye", "suffix_harmony"),
        ("5 kg", "beş kg", "beş kilogram", "other"),
        ("5 kg", "5 kilogram", "beş kilogram", "unresolved_left"),
        ("42", "kırk iki", "kırk üç", "dataset_suspect"),
        ("5 kg", "beş kilogram", "5 kg", "dataset_suspect"),
        ("1.000", "bin", "bir bin", "style_difference"),
        ("3,50", "üç virgül elli", "üç virgül beş sıfır", "style_difference"),
        ("2,5", "iki virgül beş", "iki buçuk", "style_difference"),
        ("Saat 14:30", "Saat on dört otuz", "Saat iki buçuk", "style_difference"),
        ("Saat 14:31", "Saat on dört otuz bir", "Saat iki buçuk", "other"),
        ("Aralık 2", "Aralık iki", "Ocak üç", "other"),
        ("5'te", "beşte", "beşten", "other"),
    ],
)
def test_classification(source, output, reference, kind):
    assert classify(source, output, reference).mismatch_class == kind


def test_numeric_error_requires_aligned_context():
    assert classify("2 ile 3", "iki ile dört", "iki ile üç").mismatch_class == "numeric_value_error"
    assert classify("iki üç", "iki üç", "beş").mismatch_class == "other"
    assert classify("5", "altı", "beş", number=5).mismatch_class == "numeric_value_error"


def test_clock_prefix_and_leading_zero_are_style():
    assert (
        classify("08:01", "sekiz sıfır bir", "saat sekiz bir").mismatch_class == "style_difference"
    )
    assert (
        classify("03:59", "üç elli dokuz", "saat üç elli dokuz").mismatch_class
        == "style_difference"
    )


def test_reservoir_and_balanced_quotas_are_repeatable():
    def sample():
        pool, rng = [], random.Random(12)
        for count in range(1, 1001):
            reservoir_add(pool, count, count, 20, rng)
        return pool

    assert sample() == sample()
    assert len(set(sample())) == 20
    assert balanced_quotas({"a": 1, "b": 9, "c": 90}, 20) == {"a": 1, "b": 9, "c": 10}


def test_review_priority_cap_and_backend_deduplication():
    records = []
    for index in range(350):
        for backend in ("auto", "katip"):
            records.append(
                {
                    "dataset": "demo",
                    "category": "cardinal",
                    "input": str(index),
                    "katip_output": "yanlış",
                    "reference": "doğru",
                    "row_id": str(index),
                    "backend": backend,
                    "class": "numeric_value_error" if index == 349 else "unresolved_left",
                }
            )
    queue = review_queue(records)
    assert len(queue) == 300
    assert queue[0]["class"] == "numeric_value_error"
    assert len({record["input"] for record in queue}) == 300


def test_repo_data_is_rejected():
    with pytest.raises(ValueError, match="repo dışında"):
        external_dir("scripts/tn_eval/data")


def test_manifest_rejects_corruption_without_network(tmp_path):
    from scripts.tn_eval.fetch import DATASETS

    manifest = {"schema_version": 1, "datasets": []}
    for index, (dataset, prefix, license_name, _) in enumerate(DATASETS):
        path = tmp_path / f"{index}.csv"
        path.write_text("örnek", encoding="utf-8")
        manifest["datasets"].append(
            {
                "id": dataset,
                "revision": prefix + "0" * 28,
                "license": license_name,
                "files": [
                    {"path": path.name, "sha256": sha256(path), "bytes": path.stat().st_size}
                ],
            }
        )
    verify_manifest(tmp_path, manifest)
    path.write_text("bozuk", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        verify_manifest(tmp_path, manifest)
