"""TTS'ten önce Türkçe metin normalizasyonu: sayıları, tutarları, tarihleri okunuşa çevirir.

Neden: Soniox'un 200 sesi çok dilli ve hiçbiri Türkçeye özel değil. Sayı biçimini her ses
kendi alışkanlığıyla yorumluyor; Türkçede binlik ayırıcı nokta, ondalık ayırıcı virgül
olduğu için bazı sesler "42.500,00 ₺"yi "kırk iki bin beş yüz virgül sıfır sıfır lira"
diye okuyor (canlı denemede görüldü). Metni TTS'e okunuşuyla verince sonuç sesten bağımsız
ve tutarlı olur.

Kapsam:
  42.500,00 ₺ / 42.500 TL / ₺1.250   → kırk iki bin beş yüz lira
  1.250,75 TL                         → bin iki yüz elli lira yetmiş beş kuruş
  $1.200 / 30 USD / €15,50            → dolar / avro (+ sent)
  %87,5 / 87,5% / % 12 / 12 %         → yüzde seksen yedi buçuk / yüzde on iki
  15.10.2026                          → on beş Ekim iki bin yirmi altı
  14:30'da                            → on dört otuzda
  3. çeyrek                           → üçüncü çeyrek
  1.250.000 / 3,5                     → bir milyon iki yüz elli bin / üç buçuk

Katip önce ondalık, sürüm, tarih, saat ve sıra sayısı biçemini uygular. Ardından tek bir
normalizer-tr örneği preserve ilkesiyle, 50 ms süre sınırıyla kalan metni işler. Çözülemeyen
yerler yerel kurallarla tamamlanır; tutarlar ve adresler Katip'in kısa okunuşunu korur.
Kütüphane yoksa veya hata verirse aynı yerel kurallar sesli cevap akışını sürdürür.
"""

from __future__ import annotations

import re
from datetime import date
from threading import Lock

from katip.config import get_settings
from katip.logging import get_logger

log = get_logger(__name__)
_warning_lock = Lock()
_warned = False
_DEADLINE_MS = 50


def _warn_once() -> None:
    """Metni ve istisna içeriğini kaydetmeden, süreç başına bir kez bildir."""
    global _warned
    with _warning_lock:
        if not _warned:
            _warned = True
            log.warning("tts_normalizer_katip_fallback")


try:
    from normalizer_tr import Normalizer

    _normalizer = Normalizer()  # Rust çekirdeği iş parçacıkları arasında paylaşılabilir.
except Exception:
    # Eksik paket, yüklenemeyen yerel modül veya kurucu hatası TTS'i engellemesin.
    _normalizer = None

_ONES = ["", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz"]
_TENS = ["", "on", "yirmi", "otuz", "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan"]
_SCALES = [(10**12, "trilyon"), (10**9, "milyar"), (10**6, "milyon"), (10**3, "bin")]
_MONTHS = [
    "",
    "Ocak",
    "Şubat",
    "Mart",
    "Nisan",
    "Mayıs",
    "Haziran",
    "Temmuz",
    "Ağustos",
    "Eylül",
    "Ekim",
    "Kasım",
    "Aralık",
]


def _below_thousand(n: int) -> list[str]:
    words: list[str] = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        # "yüz" (bir yüz değil), "iki yüz"
        words += ["yüz"] if hundreds == 1 else [_ONES[hundreds], "yüz"]
    tens, ones = divmod(rest, 10)
    if tens:
        words.append(_TENS[tens])
    if ones:
        words.append(_ONES[ones])
    return words


def number_words(n: int) -> str:
    """Tam sayıyı Türkçe okunuşuna çevirir (kelimeler arası boşluklu)."""
    if n == 0:
        return "sıfır"
    if n < 0:
        return "eksi " + number_words(-n)
    words: list[str] = []
    for value, name in _SCALES:
        count, n = divmod(n, value)
        if count:
            # "bin" (bir bin değil), ama "bir milyon"
            if name == "bin" and count == 1:
                words.append("bin")
            else:
                words += _below_thousand(count) if count < 1000 else number_words(count).split()
                words.append(name)
    words += _below_thousand(n)
    return " ".join(words)


_VOWELS = "aeıioöuü"
_BACK = "aıou"
_HARD = "pçtkfhsş"
_LETTERS = "a-zçğıöşü"
_APOSTROPHES = "'’‘´"
_SUFFIX = rf"(?:[{_APOSTROPHES}](?P<suffix>[{_LETTERS}]+))?"


def _attach_suffix(spoken: str, suffix: str, *, possessed: bool = False) -> str:
    """Ek ailesini koru; ünlü uyumunu ve kaynaştırmayı son okunan sözcükten seç."""
    if not suffix:
        return spoken
    last = spoken.lower().split()[-1]
    vowel = next((c for c in reversed(last) if c in _VOWELS), "e")
    a = "a" if vowel in _BACK else "e"
    i = {"a": "ı", "ı": "ı", "o": "u", "u": "u", "e": "i", "i": "i", "ö": "ü", "ü": "ü"}[vowel]
    d = "t" if last[-1] in _HARD else "d"
    ends_vowel = last[-1] in _VOWELS
    if re.fullmatch(r"l[ae]r.*", suffix):
        # 1990'larda, 2000'lerden: ikinci ek çoğul ekinin ünlüsüne uyar.
        return _attach_suffix(spoken + "l" + a + "r", suffix[3:])
    compound = re.fullmatch(r"s?[ıiuü](n(?:[ıiuü]n?|[ae]|[dt][ae](?:n|ki)?))", suffix)
    if compound:
        # İyelikten sonraki hâl eki de yeni gövdeye uyar: %20'sını → yüzde yirmisini.
        return _attach_suffix(_attach_suffix(spoken, "si"), compound[1], possessed=True)
    if re.fullmatch(r"n?[dt][ae](?:ki)?", suffix):
        ending = ("n" if possessed else "") + d + a + ("ki" if suffix.endswith("ki") else "")
    elif re.fullmatch(r"n?[dt][ae]n", suffix):
        ending = ("n" if possessed else "") + d + a + "n"
    elif re.fullmatch(r"[dt][ıiuü]r?", suffix):
        ending = d + i + ("r" if suffix.endswith("r") else "")
    elif re.fullmatch(r"[yn]?[ae]", suffix):
        ending = ("n" if possessed else "y" if ends_vowel else "") + a
    elif re.fullmatch(r"[yn]?[ıiuü]", suffix):
        ending = ("n" if possessed else "y" if ends_vowel else "") + i
    elif re.fullmatch(r"n?[ıiuü]n", suffix):
        ending = ("n" if ends_vowel else "") + i + "n"
    elif re.fullmatch(r"(?:y?l[ae]|[ıiuü]yl[ae])", suffix):
        ending = ("y" if ends_vowel else "") + "l" + a
    elif re.fullmatch(r"l[ıiuü][kğ](?:[ıiuü])?", suffix):
        ending = "l" + i + ("ğ" + i if len(suffix) == 4 else "k")
    elif re.fullmatch(r"[ıiuü]?nc[ıiuü]", suffix):
        ending = ("" if ends_vowel else i) + "nc" + i
    elif re.fullmatch(r"s[ıiuü]", suffix):
        ending = ("s" if ends_vowel else "") + i
    elif re.fullmatch(r"[cç][ae]", suffix):
        ending = ("ç" if last[-1] in _HARD else "c") + a
    elif re.fullmatch(r"[cç][ıiuü]", suffix):
        ending = ("ç" if last[-1] in _HARD else "c") + i
    else:
        # Tanınmayan ek için yeni bir dilbilgisi tahmini yapma.
        return spoken + "'" + suffix
    # Ünlüyle başlayan eklerde dört → dördü, buçuk → buçuğu yumuşaması uygulanır.
    if last in {"dört", "buçuk"} and ending[0] in _VOWELS:
        spoken = spoken[:-1] + ("d" if last == "dört" else "ğ")
    return spoken + ending


def ordinal_words(n: int) -> str:
    return _attach_suffix(number_words(n), "inci")


def _parse_number(raw: str) -> tuple[int, str | None]:
    """Türkçe binlik ve ondalık ayırıcıları ayrıştır."""
    whole, sep, frac = raw.partition(",")
    return int(whole.replace(".", "")), frac if sep else None


def _decimal_words(raw: str) -> str:
    whole, frac = _parse_number(raw)
    text = number_words(whole)
    # Yalnız pozitif tam kısmı olan tek basamaklı ,5 ondalığı buçuk okunur.
    if whole >= 1 and frac == "5":
        return text + " buçuk"
    if frac is not None:
        # Baştaki sıfırlar ayrı, kalan bölüm doğal Türkçe tam sayı olarak okunur.
        zeros = len(frac) - len(frac.lstrip("0"))
        rest = frac.lstrip("0")
        words = ["sıfır"] * zeros + ([number_words(int(rest))] if rest else [])
        text += " virgül " + " ".join(words)
    return text


_CURRENCIES = {
    "₺": ("lira", "kuruş"),
    "TL": ("lira", "kuruş"),
    "TRY": ("lira", "kuruş"),
    "$": ("dolar", "sent"),
    "USD": ("dolar", "sent"),
    "€": ("avro", "sent"),
    "EUR": ("avro", "sent"),
    "£": ("sterlin", "peni"),
    "GBP": ("sterlin", "peni"),
}
_UNITS = {
    "kg": "kilogram",
    "g": "gram",
    "mg": "miligram",
    "km": "kilometre",
    "m": "metre",
    "cm": "santimetre",
    "mm": "milimetre",
    "sn": "saniye",
    "dk": "dakika",
    "sa": "saat",
    "ml": "mililitre",
    "lt": "litre",
    "l": "litre",
    "km/sa": "kilometre",
}
_ABBREVIATIONS = {
    "Dr.": "doktor",
    "Prof.": "profesör",
    "Doç.": "doçent",
    "vb.": "ve benzeri",
    "vs.": "vesaire",
    "örn.": "örneğin",
    "KDV": "katma değer vergisi",
    "TL": "Türk lirası",
}
# Sınırlı kurum sözlüğü: harf adları aralıksız birleştirilir (B+D+D+K → bededeka).
# KDV'nin mevcut normalizer-tr açılımı yukarıda korunur; harf okunuşu kadeve olurdu.
_LETTER_NAMES = dict(
    zip(
        "ABCÇDEFGHIİJKLMNOÖPRSŞTUÜVYZ",
        "a be ce çe de e fe ge he ı i je ka le me ne o ö pe re se şe te u ü ve ye ze".split(),
        strict=True,
    )
)
_INITIALISMS = {
    code: "".join(_LETTER_NAMES[c] for c in code)
    for code in ("ATM", "EFT", "SGK", "PTT", "TC", "THY", "TCMB", "SPK", "BDDK", "TBMM")
}
# Yerleşmiş sözcük okunuşları, mekanik harf okunuşlarından ayrı tutulur.
_READ_AS_WORD = {
    "KOBİ": "kobi",
    "NATO": "nato",
    "ASELSAN": "aselsan",
    "TÜBİTAK": "tübitak",
    "İSKİ": "iski",
    "IBAN": "iban",
}
_MOBILE_GENERATIONS = {f"{n}G": number_words(n) + " ge" for n in range(2, 6)}
_ABBREVIATIONS.update(_INITIALISMS | _READ_AS_WORD | _MOBILE_GENERATIONS)


def _money_words(raw: str, code: str) -> str:
    # Katip'in kısa tutar okunuşu korunur: Türk lirası → lira; sıfat eki → liralık.
    unit, sub = _CURRENCIES[code]
    whole, frac = _parse_number(raw)
    text = f"{number_words(whole)} {unit}"
    if frac:
        cents = int((frac + "0")[:2])
        if cents:
            text += f" {number_words(cents)} {sub}"
    return text


_NUM = r"(?:\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?)"
_CUR = r"(?:₺|\$|€|£|TL|TRY|USD|EUR|GBP)"
_UNIT = "(?:" + "|".join(re.escape(u) for u in sorted(_UNITS, key=len, reverse=True)) + ")"
_ELECTRONIC = (
    r"(?<![\w@])(?:(?:https?://|www\.)[^\s<>]+"
    r"|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}"
    r"|(?:[a-zA-Z][\w-]*\.)+(?:com|org|net|io|com\.tr)(?:/[^\s<>]*)?)"
)
_IDENTIFIER = r"\b(?=[\w%./-]*[A-Za-zÇĞİÖŞÜçğıöşü])(?=[\w%./-]*\d)\w+(?:[-./%]\w+)*\b"
_INITIALISM = "(?:" + "|".join(_INITIALISMS | _READ_AS_WORD | _MOBILE_GENERATIONS) + ")"
_OPTIONAL_SUFFIX = rf"(?:[{_APOSTROPHES}][{_LETTERS}]+)?"
_PATTERNS = {
    "electronic": _ELECTRONIC,
    "iban": r"\b(?:IBAN\s+)?TR\d{2}(?:\s?\d{4}){5}\s?\d{2}\b",
    "initialism": rf"(?<!\w){_INITIALISM}(?!\w|[-./%]\w){_OPTIONAL_SUFFIX}",
    "identifier": _IDENTIFIER,
    "percent": rf"(?<![\w.,%])(?:%\s*{_NUM}|{_NUM}\s*%)(?!\w|[.,]\d){_OPTIONAL_SUFFIX}",
    "money": rf"(?<![\w.,])(?:{_NUM}\s*{_CUR}|[₺$€£]\s*{_NUM})(?!\w){_OPTIONAL_SUFFIX}",
    "quantity": rf"(?<![\w.,]){_NUM}\s*{_UNIT}(?!\w){_OPTIONAL_SUFFIX}",
    "date": rf"\b\d{{1,2}}[./]\d{{1,2}}[./]\d{{4}}\b{_OPTIONAL_SUFFIX}",
    "time": rf"\b(?:[01]?\d|2[0-3]):[0-5]\d\b{_OPTIONAL_SUFFIX}",
    # Noktalı saat yalnız yer ekiyle kesinleşir; 9.30 tek başına sürüm olabilir.
    "dotted_time": rf"\b(?:[01]?\d|2[0-3])\.[0-5]\d[{_APOSTROPHES}][dt][ae](?:n)?\b",
    "version": rf"(?<![\w.,])\d+(?:\.\d+)+\b(?![.,]\d){_OPTIONAL_SUFFIX}",
    "ordinal": r"\b\d{1,15}\.(?=\s+[a-zçğıöşü])",
    "roman": r"\b[IVXLCDM]+\.(?=\s+yüzyıl)",
    "range": r"(?<![\w.,])\d+[-–]\d+(?=\s+(?:kişi|gün|hafta|ay|yıl|adet|dakika|saat)\b)",
    "number": rf"(?<![\w.,]){_NUM}(?!\w|[.,]\d){_OPTIONAL_SUFFIX}",
}
_TOKEN = re.compile("|".join(f"(?P<{name}>{pattern})" for name, pattern in _PATTERNS.items()))
_MONEY = re.compile(
    rf"(?:(?P<num>{_NUM})\s*(?P<cur>{_CUR})|(?P<pre>[₺$€£])\s*(?P<pnum>{_NUM})){_SUFFIX}"
)
_QUANTITY = re.compile(rf"(?P<num>{_NUM})\s*(?P<unit>{_UNIT}){_SUFFIX}")
_ENDING = re.compile(rf"[{_APOSTROPHES}]([{_LETTERS}]+)$")
_ABBREVIATION = re.compile(
    r"(?<!\w)(?P<abbr>(?:" + "|".join(re.escape(a) for a in _ABBREVIATIONS) + rf"))(?!\w){_SUFFIX}"
)
_APOSTROPHE_SUFFIX = re.compile(rf"([{_LETTERS}])[{_APOSTROPHES}]([{_LETTERS}]+)")


def _time_words(h: int, m: int) -> str:
    if m == 0:
        return number_words(h)
    minute = number_words(m) if m >= 10 else f"sıfır {number_words(m)}"
    return f"{number_words(h)} {minute}"


def _electronic_words(raw: str) -> str:
    """Protokolü ve www bölümünü at; adresin anlamlı parçalarını seslendir."""
    raw = re.sub(r"^https?://(?:www\.)?|^www\.", "", raw)
    # Alan adı uzantısı Türkçe okunur: "example.com" → "example nokta kom".
    raw = re.sub(r"(?i)(?<=\.)com(?=$|[./:?#])", "kom", raw)
    replacements = {
        ".": " nokta ",
        "@": " et ",
        "/": " bölü ",
        "-": " tire ",
        "_": " alt çizgi ",
        "?": " soru işareti ",
        "=": " eşittir ",
        "&": " ve ",
        "+": " artı ",
        ":": " iki nokta ",
        "%": " yüzde ",
    }
    return "".join(replacements.get(c, c) for c in raw)


def _abbreviation_words(m: re.Match[str]) -> str:
    code, suffix = m["abbr"], m["suffix"] or ""
    # TL'lik bir sıfattır; Türk lirasılık yerine kısa liralık biçimini kullan.
    if code == "TL" and re.fullmatch(r"l[ıiuü]k", suffix):
        return _attach_suffix("lira", suffix)
    return _attach_suffix(_ABBREVIATIONS[code], suffix, possessed=code in {"TL", "KDV"})


def _rules(text: str, *, pre: bool = False) -> str:
    def token(m: re.Match[str]) -> str:
        kind, raw = m.lastgroup, m.group()
        suffix_match = _ENDING.search(raw)
        suffix = suffix_match[1] if suffix_match else ""
        base = raw[: suffix_match.start()] if suffix_match else raw
        if kind in {"identifier", "iban"}:
            return raw
        if kind == "electronic":
            # Cümle sonundaki nokta, virgül, soru işareti adresin parçası değildir.
            address = raw.rstrip(".,;:!?)")
            return raw if pre else _electronic_words(address) + raw[len(address) :]
        if kind == "initialism":
            return _attach_suffix(_ABBREVIATIONS[base], suffix)
        if kind == "money":
            if pre and not suffix:
                return raw
            match = _MONEY.fullmatch(raw)
            assert match is not None
            spoken = _money_words(match["num"] or match["pnum"], match["cur"] or match["pre"])
        elif kind == "quantity":
            if pre and "," not in base and not suffix:
                return raw
            match = _QUANTITY.fullmatch(raw)
            assert match is not None
            spoken = _decimal_words(match["num"]) + " " + _UNITS[match["unit"]]
            if match["unit"] == "km/sa":
                spoken = "saatte " + spoken
        elif kind == "date":
            day, month, year = map(int, re.split(r"[./]", base))
            try:
                date(year, month, day)
            except ValueError:
                return raw
            spoken = f"{number_words(day)} {_MONTHS[month]} {number_words(year)}"
        elif kind in {"time", "dotted_time"}:
            h, minute = map(int, re.split(r"[:.]", base))
            spoken = _time_words(h, minute)
        elif kind == "version":
            parts = base.split(".")
            # 1.234 ve 1.250.000 sürüm ipucu yoksa Türkçe binlik sayıdır.
            thousands = bool(re.fullmatch(r"\d{1,3}(?:\.\d{3})+", base))
            cue = re.match(r"\s+sürüm", text[m.end() :], re.IGNORECASE) or re.search(
                r"sürüm\s+$", text[: m.start()], re.IGNORECASE
            )
            if thousands and not cue:
                if pre and not re.match(r"\s+[A-Za-zÇĞİÖŞÜçğıöşü]", text[m.end() :]):
                    return raw
                spoken = number_words(int(base.replace(".", "")))
            else:
                if len(parts) > 3 and not cue:
                    # IP adresleri ve benzer noktalı tanımlayıcılar sürüm değildir.
                    return raw
                spoken = " nokta ".join(number_words(int(p)) for p in parts)
        elif kind == "percent":
            spoken = "yüzde " + _decimal_words(base.strip("% \t\r\n"))
        elif kind == "ordinal":
            return ordinal_words(int(base[:-1]))
        elif kind == "roman":
            values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
            letters = base[:-1]
            if not re.fullmatch(r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})", letters):
                return raw
            value = sum(
                -values[c]
                if j + 1 < len(letters) and values[c] < values[letters[j + 1]]
                else values[c]
                for j, c in enumerate(letters)
            )
            return ordinal_words(value)
        elif kind == "range":
            return " ila ".join(number_words(int(p)) for p in re.split("[-–]", base))
        else:
            if pre and "," not in base and not suffix:
                return raw
            zeros = len(base) - len(base.lstrip("0")) if len(base) > 1 and base[1].isdigit() else 0
            rest = base[zeros:]
            spoken = " ".join(["sıfır"] * zeros + ([_decimal_words(rest)] if rest else []))
        return _attach_suffix(spoken, suffix)

    # Kısaltmaları yalnız korunan belirteçler dışındaki boşluklarda işle.
    parts: list[str] = []
    end = 0

    def abbreviations(gap: str) -> str:
        return _ABBREVIATION.sub(
            lambda m: m.group() if pre and not m["suffix"] else _abbreviation_words(m), gap
        )

    for match in _TOKEN.finditer(text):
        gap = text[end : match.start()]
        parts.append(abbreviations(gap))
        try:
            parts.append(token(match))
        except (ValueError, OverflowError, RecursionError):
            # Çok uzun sayılar da sesli cevap akışını durdurmasın.
            parts.append(match.group())
        end = match.end()
    gap = text[end:]
    parts.append(abbreviations(gap))
    return "".join(parts)


def _finish(text: str) -> str:
    text = _APOSTROPHE_SUFFIX.sub(r"\1\2", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def normalize_tr(text: str) -> str:
    """Katip biçemi → normalizer-tr preserve → çözülemeyen yerel kurallar → ek temizliği."""
    prepared = _rules(text, pre=True)
    backend = get_settings().tts_normalizer
    if backend != "katip":
        if _normalizer is None:
            _warn_once()
        else:
            try:
                result = _normalizer.normalize(
                    prepared, ambiguity_policy="preserve", deadline_ms=_DEADLINE_MS
                )
                # Koordinatlar UTF-8 baytlarıdır; Türkçe karakterleri dilimlerken bozma.
                source = prepared.encode("utf-8")
                parts = []
                for segment in result.segments:
                    if segment.kind in {"unresolved", "verbatim", "money", "electronic"}:
                        original = source[segment.start_byte : segment.end_byte].decode("utf-8")
                        # Tutarlar ve adreslerde Katip'in mevcut kısa okunuş biçemini koru.
                        parts.append(_rules(original))
                    else:
                        parts.append(segment.text)
                return _finish("".join(parts))
            except Exception:
                # Süre aşımı da buraya gelir; kısmi kütüphane sonucunu kullanma.
                _warn_once()
    return _finish(_rules(prepared))
