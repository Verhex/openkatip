"""Sabitlenmiş veriyle iki Katip arka ucunu çevrimdışı değerlendir."""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

# Dosya olarak ve python -m scripts.tn_eval.run biçiminde çalıştırılabilir.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.tn_eval.compare import CLASSES, EQUIVALENCES, classify, comparison_text  # noqa: E402
from scripts.tn_eval.fetch import DATASETS, external_dir, sha256, verify_manifest  # noqa: E402

SEED = 20261008
ELIGIBLE = re.compile(
    r"\d|[%₺$€£]|\b(?:TL|USD|EUR|kg|mg|km|cm|mm|ml|litre|metre|gram|saat|dakika)\b",
    re.IGNORECASE,
)
PRIORITY = {
    kind: n
    for n, kind in enumerate(
        (
            "numeric_value_error",
            "suffix_harmony",
            "unresolved_left",
            "dataset_suspect",
            "other",
            "style_difference",
        )
    )
}


@dataclass(frozen=True)
class Case:
    dataset: str
    category: str
    input: str
    reference: str
    row_id: str
    variant: str = "original"
    number: int | None = None


def rows(root: Path, entry: dict):
    for file in entry["files"]:
        path = root / file["path"]
        if not file["source_file"].startswith("data/"):
            continue
        if path.suffix == ".csv":
            with path.open(encoding="utf-8-sig", newline="") as stream:
                for index, row in enumerate(csv.DictReader(stream)):
                    yield f"{file['source_file']}:{index}", row
        elif path.suffix == ".parquet":
            try:
                import pyarrow.parquet as pq
            except ImportError as exc:
                raise RuntimeError(
                    "Parquet okumak için pyarrow gerekir. README'deki repo dışı bağımlılık "
                    "kurulumunu kullanın; veri indirmek dışında run.py ağ kullanmaz."
                ) from exc
            parquet = pq.ParquetFile(path)
            wanted = {"number", "words", "text_raw", "text_model", "category", "id", "line_id"}
            columns = [name for name in parquet.schema_arrow.names if name in wanted]
            index = 0
            for batch in parquet.iter_batches(batch_size=8192, columns=columns):
                for row in batch.to_pylist():
                    yield f"{file['source_file']}:{index}", row
                    index += 1
        else:
            raise ValueError(f"Desteklenmeyen veri biçimi: {path}")


def magnitude(number: int) -> str:
    return "zero" if number == 0 else f"10^{len(str(abs(number))) - 1}"


def dotted(number: int) -> str:
    return format(number, ",").replace(",", ".")


def reservoir_add(pool: list, item, seen: int, limit: int, rng: random.Random) -> None:
    if len(pool) < limit:
        pool.append(item)
    else:
        slot = rng.randrange(seen)
        if slot < limit:
            pool[slot] = item


def balanced_quotas(counts: dict[str, int], limit: int) -> dict[str, int]:
    quotas = dict.fromkeys(sorted(counts), 0)
    remaining = min(limit, sum(counts.values()))
    while remaining:
        for key in quotas:
            if quotas[key] < counts[key] and remaining:
                quotas[key] += 1
                remaining -= 1
    return quotas


def select_cases(root: Path, manifest: dict, number_limit: int, alania_limit: int, seed: int):
    entries = {entry["id"]: entry for entry in manifest["datasets"]}
    cases: list[Case] = []
    population = {}
    dataset = DATASETS[0][0]
    counts = Counter()
    for row_id, row in rows(root, entries[dataset]):
        if not {"written", "spoken"} <= row.keys():
            raise ValueError(f"Beklenmeyen sütunlar: {dataset}")
        cases.append(
            Case(dataset, row.get("category") or "unknown", row["written"], row["spoken"], row_id)
        )
        counts[row.get("category") or "unknown"] += 1
    population[dataset] = {
        "total": sum(counts.values()),
        "categories": dict(counts),
        "selected_rows": sum(counts.values()),
    }
    if sum(counts.values()) != 17109:
        raise ValueError("Sabitlenmiş yagmurtuncer veri kümesinde 17.109 satır bekleniyor.")

    dataset = DATASETS[1][0]
    counts = Counter()
    pools = defaultdict(list)
    generators = {}
    for row_id, row in rows(root, entries[dataset]):
        number = int(row["number"])
        if not 0 <= number <= 999999:
            raise ValueError("number-words-1m beklenen aralık dışında.")
        bucket = magnitude(number)
        counts[bucket] += 1
        if bucket not in generators:
            generators[bucket] = random.Random(f"{seed}:{bucket}")
        rng = generators[bucket]
        case = Case(dataset, bucket, str(number), str(row["words"]), row_id, number=number)
        reservoir_add(pools[bucket], case, counts[bucket], number_limit, rng)
    quotas = balanced_quotas(counts, number_limit)
    for bucket, quota in quotas.items():
        # Rezervuarın ilk quota elemanı da düzgün dağılımlı bir alt örneklemdir.
        for case in sorted(pools[bucket][:quota], key=lambda c: c.row_id):
            cases.append(case)
            cases.append(
                Case(
                    dataset,
                    bucket,
                    dotted(case.number),
                    case.reference,
                    case.row_id,
                    "dotted",
                    case.number,
                )
            )
    population[dataset] = {
        "total": sum(counts.values()),
        "buckets": dict(counts),
        "selected_rows": sum(quotas.values()),
        "quotas": quotas,
        "evaluations_per_row": 2,
    }
    if sum(counts.values()) != 1000000:
        raise ValueError("Sabitlenmiş sayı veri kümesinde 1.000.000 satır bekleniyor.")

    dataset = DATASETS[2][0]
    pool, rng = [], random.Random(seed)
    seen = eligible = missing = 0
    eligible_categories = Counter()
    for row_id, row in rows(root, entries[dataset]):
        seen += 1
        if row.get("text_raw") is None or row.get("text_model") is None:
            missing += 1
            continue
        if not ELIGIBLE.search(row["text_raw"]):
            continue
        eligible += 1
        category = row.get("category") or "unknown"
        eligible_categories[category] += 1
        case = Case(dataset, category, row["text_raw"], row["text_model"], row_id)
        reservoir_add(pool, case, eligible, alania_limit, rng)
    cases.extend(sorted(pool, key=lambda c: c.row_id))
    population[dataset] = {
        "total": seen,
        "eligible": eligible,
        "missing_excluded": missing,
        "selected_rows": len(pool),
        "eligible_categories": dict(eligible_categories),
    }
    if seen != 1439639:
        raise ValueError("Sabitlenmiş Alania veri kümesinde 1.439.639 satır bekleniyor.")
    if len(pool) != alania_limit or sum(quotas.values()) != number_limit:
        raise ValueError("İstenen örneklem büyüklüğüne ulaşılamadı.")
    return cases, population


def pattern(record: dict) -> str:
    raw = record["input"]
    shapes = []
    checks = (
        ("date", r"\d{1,2}[./]\d{1,2}[./]\d{4}"),
        ("time", r"\d{1,2}:\d{2}"),
        ("decimal", r"\d+,\d+"),
        ("thousands", r"\d{1,3}(?:\.\d{3})+"),
        ("suffix", r"\d['’]\w+"),
        ("currency", r"[%₺$€£]|\b(?:TL|USD|EUR)\b"),
        ("unit", r"\d\s*(?:kg|mg|km|cm|mm|ml|m²)\b"),
    )
    for label, expression in checks:
        if re.search(expression, raw):
            shapes.append(label)
    return f"{record['class']} / {record['category']} / {','.join(shapes) or 'plain'}"


def review_queue(records: list[dict], limit: int = 300) -> list[dict]:
    ordered = sorted(
        records,
        key=lambda r: (
            PRIORITY[r["class"]],
            len(r["input"]),
            r["dataset"],
            r["row_id"],
            r["backend"],
        ),
    )
    groups = defaultdict(list)
    seen = set()
    for record in ordered:
        key = (record["dataset"], record["input"], record["katip_output"], record["reference"])
        if key in seen:
            continue
        seen.add(key)
        groups[
            (PRIORITY[record["class"]], record["dataset"], record["category"], pattern(record))
        ].append(record)
    selected = []
    for priority in sorted(set(key[0] for key in groups)):
        keys = sorted(key for key in groups if key[0] == priority)
        depth = 0
        while len(selected) < limit and any(depth < len(groups[key]) for key in keys):
            for key in keys:
                if depth < len(groups[key]) and len(selected) < limit:
                    selected.append(groups[key][depth])
            depth += 1
    return selected


def write_summary(target: Path, stats: dict, patterns: dict, metadata: dict) -> None:
    lines = [
        "# Türkçe metin normalizasyonu değerlendirmesi",
        "",
        "Exact: Türkçe küçük harf, boşluk ve noktalama temizliğinden sonra eşleşme. "
        "Biçem eşdeğerlikleri exact oranına dahil değildir.",
        "",
        "## Veri ve yöntem",
        "",
        f"Tohum: `{metadata['seed']}`. Manifest SHA-256: `{metadata['manifest_sha256']}`.",
        "",
        "Sayı örneklemi büyüklük kovalarına dengeli dağıtılır; az satırlı kovaların "
        "artık kotası diğerlerine dağıtılır. Her satır düz ve noktalı biçimde iki kez "
        "değerlendirilir (1000 altındaki iki biçim aynıdır). Bu oran nüfus ağırlıklı değildir.",
        "",
        "Alania uygunluk filtresinden sonra düzgün dağılımlı rezervuar örneklemi kullanır. "
        "Kategori oranları doğal dağılımı izler. Tüm split'ler kapsanır.",
        "",
        "Sınıflar tek etiketlidir: referans şüphesi → sayısal değer → ek uyumu → "
        "çözümlenmeyen çıktı → eşdeğer biçem → diğer. Sayısal tespit geçerli sayı dilbilgisi "
        "ve aynı bağlam gerektirir; eksik/fazla öbekler, telefonlar ve bazı tarih/ek "
        "okunuşları için sayı hatası yakalama oranı sınırlıdır. Sıfır sayı hatası kusursuzluk "
        "anlamına gelmez. Ek sınıfı da inceleme adayıdır, hangi tarafın doğru olduğunu kanıtlamaz.",
        "",
        "Uygulanan eşdeğerlikler:",
        "",
    ]
    lines.extend(f"- {a} ↔ {b} (tam sözcük öbeği)." for a, b in EQUIVALENCES)
    lines.extend(
        [
            "- Geçerli sayı öbeklerinin aynı bağlamda aynı tam sayı/ondalık değeri "
            "(bir yüz/yüz, bir bin/bin, virgül beş sıfır/virgül elli dahil).",
            "- Sayı + buçuk / aynı ondalık değer (iki buçuk / iki virgül beş).",
            "- Girdide tek geçerli HH:MM saati varsa 24/12 saat, sıfır dakika atlama, "
            "dakika önünde sıfır, saat sözcüğü ekleme/atlama ve :30/buçuk "
            "(aynı kalan bağlamda, birer saat öbeği).",
            "",
            f"Arka uç: {metadata['backend_info']}",
            "",
            "Normalizasyon çevrimdışı çalışır. auto içindeki çağrı bazlı kütüphane "
            "geri dönüşleri mevcut üretim davranışıdır; rapor auto'yu saf kütüphane "
            "performansı olarak yorumlamaz.",
            "",
            "### Popülasyon ve örneklem",
            "",
            "```json",
            json.dumps(metadata["population"], ensure_ascii=False, indent=2),
            "```",
            "",
            "## Veri kümesi / arka uç / kategori",
            "",
            "| Veri kümesi | Arka uç | Kategori | Toplam | Exact % | Sayı | Ek | "
            "Çözümlenmeyen | Biçem | Referans şüphesi | Diğer |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for (dataset, backend, category), count in sorted(stats.items()):
        values = [
            count[k]
            for k in (
                "numeric_value_error",
                "suffix_harmony",
                "unresolved_left",
                "style_difference",
                "dataset_suspect",
                "other",
            )
        ]
        lines.append(
            f"| {dataset} | {backend} | {category} | {count['total']} | "
            f"{100 * count['exact'] / count['total']:.2f} | " + " | ".join(map(str, values)) + " |"
        )
    lines.extend(["", "## En sık uyuşmazlık örüntüleri", ""])
    for (dataset, backend, category), counter in sorted(patterns.items()):
        lines.append(f"### {dataset} / {backend} / {category}")
        lines.append("")
        lines.extend(f"- {name}: {count}" for name, count in counter.most_common(10))
        lines.append("")
    lines.extend(
        [
            "## Etiket kalitesi ve sınırlar",
            "",
            "Yagmurtuncer ve number-words sentetik kural üretimidir; insan altın standardı "
            "değildir. Alania LLM metni ve otomatik normalizasyon etiketidir. Saat, telefon, "
            "kod, kısaltma, buçuk ve para biçemleri farklı olabilir. Referansın sayı değeri "
            "girdiyle çelişirse veya rakam/simge bırakırsa dataset_suspect kullanılır. "
            "Tam eşleşen fakat birlikte yanlış olan etiketler bu metrikte görünmez.",
            "",
            "review_queue.csv en çok 300 farklı anlaşmazlık içerir; sayı, ek ve kalan "
            "simge önceliklidir. Öncelik içinde veri kümesi/kategori/örüntü çeşitliliği "
            "korunur; aynı çıktı iki arka uçta da varsa tek satır gösterilir. "
            "Tüm anlaşmazlıklar mismatches.jsonl içindedir.",
            "",
            "## Lisans ve atıf",
            "",
            "Turkish Text Normalization (TN/ITN), Nur Yağmur Tuncer (2026), "
            "https://huggingface.co/datasets/yagmurtuncer/turkish-text-normalization, CC BY 4.0.",
            "",
            "Alania Turkish Domain Text, PatientDesk AI (2026), "
            "https://huggingface.co/datasets/cloud0day3/alania-domain-text-tr, CC BY 4.0.",
            "",
            "Turkish Number Words 1M, GoktugD, "
            "https://huggingface.co/datasets/GoktugD/turkish-number-words-1m, CC0-1.0.",
            "Lisanslar: https://creativecommons.org/licenses/by/4.0/deed.tr ve "
            "https://creativecommons.org/publicdomain/zero/1.0/deed.tr. "
            "Kaynak veriler değiştirilmedi; örneklem, karşılaştırmalar ve hata etiketleri "
            "Katip değerlendirme aracı tarafından üretildi.",
            "",
        ]
    )
    target.write_text("\n".join(lines), encoding="utf-8")


def evaluate(root: Path, number_limit: int, alania_limit: int, seed: int) -> Path:
    from katip.config import get_settings
    from katip.voice import tr_normalize

    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    verify_manifest(root, manifest)
    print("Veri hash'leri doğrulandı; örneklem seçiliyor.", flush=True)
    cases, population = select_cases(root, manifest, number_limit, alania_limit, seed)
    target = root / "results" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    target.mkdir(parents=True)
    metadata = {
        "seed": seed,
        "number_sample": number_limit,
        "alania_sample": alania_limit,
        "created_at": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "manifest_sha256": sha256(root / "manifest.json"),
        "datasets": [
            {key: entry[key] for key in ("id", "revision", "license")}
            for entry in manifest["datasets"]
        ],
        "population": population,
        "backend_info": {
            "auto_library_available": tr_normalize._normalizer is not None,
            "requested": ["auto", "katip"],
            "auto_initial_fallback": tr_normalize._normalizer is None,
        },
        "packages": {},
        "script_sha256": {},
    }
    for package in ("normalizer-tr", "pyarrow", "pydantic-settings"):
        try:
            metadata["packages"][package] = version(package)
        except PackageNotFoundError:
            metadata["packages"][package] = None
    for path in sorted(Path(__file__).parent.glob("*.py")):
        metadata["script_sha256"][path.name] = sha256(path)
    metadata["normalizer_sha256"] = sha256(Path(tr_normalize.__file__))
    with (target / "samples.jsonl").open("w", encoding="utf-8") as stream:
        for case in cases:
            stream.write(json.dumps(asdict(case), ensure_ascii=False) + "\n")
    stats, patterns = defaultdict(Counter), defaultdict(Counter)
    mismatches = []
    previous = os.environ.get("KATIP_TTS_NORMALIZER")
    try:
        with (target / "mismatches.jsonl").open("w", encoding="utf-8") as stream:
            for backend in ("auto", "katip"):
                os.environ["KATIP_TTS_NORMALIZER"] = backend
                get_settings.cache_clear()
                assert get_settings().tts_normalizer == backend
                print(f"Arka uç: {backend}; {len(cases)} değerlendirme.", flush=True)
                for index, case in enumerate(cases):
                    output = tr_normalize.normalize_tr(case.input)
                    comparison = classify(case.input, output, case.reference, number=case.number)
                    keys = [
                        (case.dataset, backend, "ALL"),
                        (case.dataset, backend, case.category),
                        (case.dataset, backend, f"variant:{case.variant}"),
                    ]
                    for key in keys:
                        stats[key]["total"] += 1
                        stats[key]["exact" if comparison.exact else comparison.mismatch_class] += 1
                    if not comparison.exact:
                        record = {
                            **asdict(case),
                            "backend": backend,
                            "katip_output": output,
                            "class": comparison.mismatch_class,
                            "suggested_note": f"[{backend}/{case.variant}] {comparison.note}",
                            "output_numbers": comparison.output_numbers,
                            "reference_numbers": comparison.reference_numbers,
                            "comparison_output": comparison_text(output),
                            "comparison_reference": comparison_text(case.reference),
                        }
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                        mismatches.append(record)
                        for key in keys:
                            patterns[key][pattern(record)] += 1
                    if (index + 1) % 10000 == 0:
                        print(f"{backend}: {index + 1}/{len(cases)}", flush=True)
    finally:
        if previous is None:
            os.environ.pop("KATIP_TTS_NORMALIZER", None)
        else:
            os.environ["KATIP_TTS_NORMALIZER"] = previous
        get_settings.cache_clear()
    fields = [
        "dataset",
        "category",
        "input",
        "katip_output",
        "reference",
        "class",
        "suggested_note",
    ]
    with (target / "review_queue.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(review_queue(mismatches))
    summary = [
        {
            "dataset": dataset,
            "backend": backend,
            "category": category,
            "total": count["total"],
            "exact": count["exact"],
            "exact_percent": 100 * count["exact"] / count["total"],
            **{kind: count[kind] for kind in CLASSES},
        }
        for (dataset, backend, category), count in sorted(stats.items())
    ]
    (target / "summary.json").write_text(
        json.dumps({"metadata": metadata, "statistics": summary}, ensure_ascii=False, indent=2)
        + "\n",
        "utf-8",
    )
    write_summary(target / "summary.md", stats, patterns, metadata)
    print(f"Sonuçlar: {target}", flush=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=external_dir)
    parser.add_argument("--number-sample", type=int, default=20000)
    parser.add_argument("--alania-sample", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    if args.number_sample <= 0 or args.alania_sample <= 0:
        parser.error("Örneklem büyüklükleri pozitif olmalıdır.")
    evaluate(args.data_dir, args.number_sample, args.alania_sample, args.seed)


if __name__ == "__main__":
    main()
