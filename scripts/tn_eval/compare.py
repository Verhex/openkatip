"""Bağımsız, temkinli Türkçe karşılaştırma ve uyuşmazlık sınıflandırması."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

# Yalnız bu açık eşdeğerlikler uygulanır; sayısal değer değişimleri eşdeğer değildir.
EQUIVALENCES = (("türk lirası", "lira"), ("avro", "euro"))
CLASSES = (
    "numeric_value_error",
    "suffix_harmony",
    "style_difference",
    "unresolved_left",
    "dataset_suspect",
    "other",
)
ONES = {w: n for n, w in enumerate("sıfır bir iki üç dört beş altı yedi sekiz dokuz".split())}
TENS = dict(
    zip(
        "on yirmi otuz kırk elli altmış yetmiş seksen doksan".split(),
        range(10, 100, 10),
        strict=True,
    )
)
SCALES = {"bin": 10**3, "milyon": 10**6, "milyar": 10**9, "trilyon": 10**12}
NUMBER_TOKENS = set(ONES) | set(TENS) | set(SCALES) | {"yüz"}
UNRESOLVED = re.compile(r"\d|[%₺$€£]|(?<=\w)[@/](?=\w)")
SUFFIX = re.compile(
    r"(?:[dt][ae]n?|y?[aeıiuü]|n?[ıiuü]n|y?l[ae]|[ıiuü]yl[ae]|"
    r"l[ıiuü][kğ][ıiuü]?|[ıiuü]?nc[ıiuü]|s[ıiuü]|l[ae]r(?:[dt][ae]n?)?)$"
)
SUFFIX_STEMS = NUMBER_TOKENS | {
    "lira",
    "dolar",
    "euro",
    "kuruş",
    "sent",
    "metre",
    "kilometre",
    "gram",
    "kilogram",
    "santimetre",
    "litre",
    "saat",
    "dakika",
    "saniye",
    "ocak",
    "şubat",
    "mart",
    "nisan",
    "mayıs",
    "haziran",
    "temmuz",
    "ağustos",
    "eylül",
    "ekim",
    "kasım",
    "aralık",
}


def comparison_text(text: str, *, equivalent_styles: bool = False) -> str:
    text = unicodedata.normalize("NFC", text).translate(str.maketrans({"I": "ı", "İ": "i"}))
    text = text.lower()
    # Kesme işareti ekleri bölmez; diğer noktalama sözcük sınırına dönüşür.
    text = "".join(
        "" if c in "'’ʼ" else " " if unicodedata.category(c).startswith("P") else c for c in text
    )
    text = " ".join(text.split())
    if equivalent_styles:
        for original, replacement in EQUIVALENCES:
            text = re.sub(rf"(?<!\w){original}(?!\w)", replacement, text)
    return text


def _under_thousand(words: list[str]) -> int | None:
    if not words:
        return 0
    value, index = 0, 0
    if words[0] == "yüz":
        value, index = 100, 1
    elif len(words) > 1 and words[0] in ONES and ONES[words[0]] > 0 and words[1] == "yüz":
        value, index = ONES[words[0]] * 100, 2
    if index < len(words) and words[index] in TENS:
        value += TENS[words[index]]
        index += 1
    if index < len(words) and words[index] in ONES and ONES[words[index]] > 0:
        value += ONES[words[index]]
        index += 1
    return value if index == len(words) else None


def parse_integer_words(text: str) -> int | None:
    """Yalnız geçerli, azalan büyüklüklü tam sayı dizilerini çözümle."""
    words = comparison_text(text).split()
    sign = -1 if words and words[0] == "eksi" else 1
    if sign == -1:
        words = words[1:]
    if words == ["sıfır"]:
        return 0
    if not words or "sıfır" in words:
        return None
    total, previous_scale, group = 0, 10**15, []
    for word in words:
        if word in SCALES:
            scale = SCALES[word]
            coefficient = _under_thousand(group) if group else 1
            if scale >= previous_scale or coefficient is None or coefficient == 0:
                return None
            # "milyon" da örtük bir katsayısıyla okunabilir.
            total += coefficient * scale
            previous_scale, group = scale, []
        else:
            group.append(word)
    tail = _under_thousand(group)
    return sign * (total + tail) if tail is not None else None


def _fraction(words: list[str]) -> str | None:
    if not words:
        return None
    if all(word in ONES for word in words):
        return "".join(str(ONES[word]) for word in words)
    zeros = 0
    while zeros < len(words) and words[zeros] == "sıfır":
        zeros += 1
    value = parse_integer_words(" ".join(words[zeros:]))
    return "0" * zeros + str(value) if value is not None and value >= 0 else None


def split_suffix(token: str) -> tuple[str, str] | None:
    for stem in sorted(SUFFIX_STEMS, key=len, reverse=True):
        forms = [stem, "dörd"] if stem == "dört" else [stem]
        for form in forms:
            if token.startswith(form) and SUFFIX.fullmatch(token[len(form) :]):
                return stem, token[len(form) :]
    return None


def numeric_signature(text: str) -> tuple[list[Decimal], str]:
    """Çözümlenebilir sayı öbekleri ve hizalama için kalan bağlamı döndür."""
    tokens = comparison_text(text, equivalent_styles=True).split()
    values: list[Decimal] = []
    context: list[str] = []
    index = 0
    while index < len(tokens):
        end = index
        words: list[str] = []
        suffix = ""
        while end < len(tokens):
            token = tokens[end]
            if token in NUMBER_TOKENS or token in {"eksi", "virgül", "buçuk"}:
                words.append(token)
                end += 1
            else:
                attached = split_suffix(token)
                if attached and attached[0] in NUMBER_TOKENS:
                    words.append(attached[0])
                    suffix = attached[1]
                    end += 1
                break
        value = None
        if words and words[-1] == "buçuk":
            whole = parse_integer_words(" ".join(words[:-1]))
            if whole is not None:
                value = Decimal(whole) + (Decimal("-0.5") if words[0] == "eksi" else Decimal("0.5"))
        elif words.count("virgül") == 1:
            separator = words.index("virgül")
            whole = parse_integer_words(" ".join(words[:separator]))
            fraction = _fraction(words[separator + 1 :])
            if whole is not None and fraction is not None:
                sign = "-" if words[0] == "eksi" else ""
                value = Decimal(f"{sign}{abs(whole)}.{fraction}")
        elif words:
            integer = parse_integer_words(" ".join(words))
            if integer is not None:
                value = Decimal(integer)
        if value is not None:
            values.append(value)
            context.append("<sayı>" + suffix)
            index = end
        else:
            context.append(tokens[index])
            index += 1
    return values, " ".join(context)


def suffix_difference(left: str, right: str) -> bool:
    a, b = left.split(), right.split()
    if len(a) != len(b):
        return False
    changed = False
    for x, y in zip(a, b, strict=True):
        if x == y:
            continue
        sx, sy = split_suffix(x), split_suffix(y)
        if sx is None or sy is None or sx[0] != sy[0]:
            return False
        # Aynı ek ailesinde yalnız ünlü/kaynaştırma/sertleşme farkını işaretle.
        if suffix_family(sx[1]) != suffix_family(sy[1]):
            return False
        changed = True
    return changed


def suffix_family(suffix: str) -> str:
    return re.sub(r"[aeıioöuüy]", "", suffix).replace("t", "d")


def clock_style(source: str, left: str, right: str) -> bool:
    """Girdide tek saat varsa 24/12 saat ve buçuk/tam-saat biçemlerini eşleştir."""
    clocks = re.findall(r"(?<!\d)([0-2]?\d):([0-5]\d)(?!\d)", source)
    if len(clocks) != 1:
        return False
    hour, minute = map(int, clocks[0])
    if hour > 23:
        return False

    def small(n: int) -> str:
        if n < 10:
            return next(w for w, value in ONES.items() if value == n)
        tens, ones = divmod(n, 10)
        base = next(w for w, value in TENS.items() if value == tens * 10)
        return base + (" " + small(ones) if ones else "")

    phrases = set()
    for h in {hour, hour % 12 or 12}:
        phrases.add(f"{small(h)} {small(minute)}")
        if minute == 0:
            phrases.add(small(h))
        if minute == 30:
            phrases.add(small(h) + " buçuk")
        if minute < 10:
            phrases.add(f"{small(h)} sıfır {small(minute)}")
    expression = (
        r"(?<!\w)(?:saat\s+)?(?:"
        + "|".join(re.escape(phrase) for phrase in sorted(phrases, key=len, reverse=True))
        + r")(?!\w)"
    )
    a, na = re.subn(expression, "<saat>", left)
    b, nb = re.subn(expression, "<saat>", right)
    return na == nb == 1 and a == b


@dataclass(frozen=True)
class Comparison:
    exact: bool
    mismatch_class: str | None
    note: str
    output_numbers: tuple[str, ...] = ()
    reference_numbers: tuple[str, ...] = ()


def classify(source: str, output: str, reference: str, *, number: int | None = None) -> Comparison:
    left, right = comparison_text(output), comparison_text(reference)
    if left == right:
        return Comparison(True, None, "Türkçe harf/boşluk/noktalama karşılaştırması eşleşti.")
    lv, lc = numeric_signature(output)
    rv, rc = numeric_signature(reference)
    evidence = {"output_numbers": tuple(map(str, lv)), "reference_numbers": tuple(map(str, rv))}

    def result(kind: str, note: str) -> Comparison:
        return Comparison(False, kind, note, **evidence)

    raw_integer = re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+|-?\d+", source.strip())
    expected = (
        number if number is not None else (int(source.replace(".", "")) if raw_integer else None)
    )
    reference_integer = parse_integer_words(reference)
    if not right or (
        expected is not None and reference_integer is not None and reference_integer != expected
    ):
        return result("dataset_suspect", "Referans boş veya girdinin tam sayı değerinden farklı.")
    if UNRESOLVED.search(reference):
        return result(
            "dataset_suspect",
            "Referans okunuş yerine rakam/özel simge içeriyor; etiketi inceleyin.",
        )
    if expected is not None and reference_integer == expected:
        output_integer = parse_integer_words(output)
        if output_integer is not None and output_integer != expected:
            return result("numeric_value_error", f"Girdi={expected}; çıktı={output_integer}.")
    if lv and rv and len(lv) == len(rv) and lc == rc and lv != rv:
        return result("numeric_value_error", "Aynı bağlamdaki çözümlenmiş sayı değerleri farklı.")
    le = comparison_text(output, equivalent_styles=True)
    re_ = comparison_text(reference, equivalent_styles=True)
    if suffix_difference(le, re_):
        return result(
            "suffix_harmony", "Aynı gövde/ek ailesinde ünlü uyumu veya kaynaştırma farkı."
        )
    if UNRESOLVED.search(output):
        return result("unresolved_left", "Çıktıda rakam veya okunmamış özel simge kaldı.")
    if le == re_ or (lv and lv == rv and lc == rc) or clock_style(source, le, re_):
        return result(
            "style_difference", "Açık eşdeğerlik veya aynı bağlamdaki eşit sayısal değer."
        )
    return result("other", "Otomatik kurallar kesin neden bulamadı; Türkçe konuşur incelemeli.")
