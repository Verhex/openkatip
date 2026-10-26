"""Komut algılama: sesli ("Katip, …") ve chat ("/özet"), ve yetki politikası.

Çekirdek komutlar kural tabanlıdır (hızlı, ücretsiz, deterministik). Kurala uymayan
serbest istekler ("Katip, geçen hafta bütçe için ne demiştik?") agent'a (Claude) gider.

Yetki seviyeleri:
- PUBLIC : herkes (özet, not, soru, yardım)
- SELF   : yalnızca komutu veren kişinin kendisiyle ilgili (sesimi tanı, beni unut)
- ADMIN  : yönetici — ses izi eşleşmesi + toplantıdaki hesap adı eşleşmesi gerekir;
           kritik olanlar ayrıca panel/e-posta onayı ister.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


class Level(StrEnum):
    PUBLIC = "public"
    SELF = "self"
    ADMIN = "admin"


class Intent(StrEnum):
    ENROLL = "enroll"  # sesimi tanı
    CONSENT = "consent"  # onaylıyorum
    FORGET = "forget"  # beni unut
    CONFIRM_FORGET = "confirm_forget"  # evet unut
    CANCEL = "cancel"  # vazgeç
    SUMMARY = "summary"  # özetle
    NOTE = "note"  # not al: …
    ACTION = "action"  # aksiyon/görev ekle: …
    LANGUAGE = "language"  # dili İngilizce yap
    HELP = "help"
    ASK = "ask"  # serbest soru → agent
    ADMIN_DIRECTIVE = "admin_directive"  # yönetici talimatı (ayar, dışarıya mail, …)
    REPLY_MODE = "reply_mode"  # sesli / yazılı cevap moduna geç (arg: "voice" | "chat")
    SCREENSHOT = "screenshot"  # ekran görüntüsü al
    PAUSE = "pause"  # kayıt dışı: not almayı durdur
    RESUME = "resume"  # kayda devam
    DECISION = "decision"  # karar olarak kaydet: …
    LEAVE = "leave"  # toplantıdan ayrıl
    PRIVACY_NOTICE = "privacy_notice"  # KVKK / aydınlatma bilgilendirmesi
    LISTEN = "listen"  # yalnızca "Katip" dendi: "Buyurun" de, sonraki cümleyi dinle
    REPORT = "report"  # şu ana kadarki toplantının raporunu şimdi hazırla
    PRESENTATION = "presentation"
    NARRATE = "narrate"
    READ_SLIDE = "read_slide"


LEVELS: dict[Intent, Level] = {
    Intent.ENROLL: Level.SELF,
    Intent.CONSENT: Level.SELF,
    Intent.FORGET: Level.SELF,
    Intent.CONFIRM_FORGET: Level.SELF,
    Intent.CANCEL: Level.PUBLIC,
    Intent.SUMMARY: Level.PUBLIC,
    Intent.NOTE: Level.PUBLIC,
    Intent.ACTION: Level.PUBLIC,
    Intent.LANGUAGE: Level.PUBLIC,
    Intent.HELP: Level.PUBLIC,
    Intent.ASK: Level.PUBLIC,
    Intent.ADMIN_DIRECTIVE: Level.ADMIN,
    Intent.REPLY_MODE: Level.PUBLIC,
    Intent.SCREENSHOT: Level.PUBLIC,
    Intent.PAUSE: Level.PUBLIC,
    Intent.RESUME: Level.PUBLIC,
    Intent.DECISION: Level.PUBLIC,
    Intent.LEAVE: Level.PUBLIC,
    Intent.PRIVACY_NOTICE: Level.PUBLIC,
    Intent.LISTEN: Level.PUBLIC,
    Intent.REPORT: Level.PUBLIC,
    Intent.PRESENTATION: Level.ADMIN,
    Intent.NARRATE: Level.ADMIN,
    Intent.READ_SLIDE: Level.ADMIN,
}

# Kritik yönetici işleri ses + hesap eşleşse bile ayrıca onay ister.
REQUIRES_APPROVAL = frozenset({Intent.ADMIN_DIRECTIVE})


@dataclass(frozen=True)
class Command:
    intent: Intent
    text: str  # komutun gövdesi (ör. not metni, soru)
    source: str  # "voice" | "chat"
    raw: str
    arg: str | None = None  # ör. dil kodu

    @property
    def level(self) -> Level:
        return LEVELS[self.intent]


def normalize(text: str) -> str:
    """Türkçe duyarlı küçük harf + noktalama sadeleştirme (eşleştirme için)."""
    text = text.replace("İ", "i").replace("I", "ı").lower()
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[^\w\s/'-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# "Katip", "Katip'e", "Katibim", "katibe" — ünsüz yumuşaması (p→b) dahil.
_WAKE = re.compile(r"(?:^|\s)kati[pb]\w*(?:'\w+)?[\s,]*", re.UNICODE)


def detect_wake(text: str) -> str | None:
    """Metinde bota seslenme varsa, seslenmeden sonraki kısmı döner; yoksa None."""
    norm = normalize(text)
    m = _WAKE.search(norm)
    if not m:
        return None
    rest = norm[m.end() :].strip(" ,'")
    # Seslenme cümlenin sonundaysa ("... değil mi Katip?") önceki kısım komuttur.
    return rest or norm[: m.start()].strip(" ,'")


_LANGS = {
    "ingilizce": "en",
    "türkçe": "tr",
    "almanca": "de",
    "fransızca": "fr",
    "ispanyolca": "es",
    "italyanca": "it",
    "arapça": "ar",
    "rusça": "ru",
}

_RULES: list[tuple[Intent, re.Pattern[str]]] = [
    (Intent.CONFIRM_FORGET, re.compile(r"\bevet\b.*\bunut\b|\bunut(abilirsin)?\s+evet\b")),
    (Intent.FORGET, re.compile(r"\bbeni unut\b|\bsesimi sil\b|\bverilerimi sil\b|\bunut beni\b")),
    (Intent.CONSENT, re.compile(r"\bonaylıyorum\b|\bkabul ediyorum\b|\bonay veriyorum\b")),
    (
        Intent.ENROLL,
        # Yalnızca emir kipi: "beni tanıyacak mısın?" gibi sorular kayıt komutu değil.
        re.compile(r"\bsesimi (tanı|kaydet|öğren)\b|\bbeni (tanı|kaydet)\b|\bkendimi kaydet\b"),
    ),
    (Intent.CANCEL, re.compile(r"^(vazgeç|iptal|boşver)\b")),
    (Intent.PRIVACY_NOTICE, re.compile(r"\b(kvkk|aydınlatma|gizlilik)\b")),
    (Intent.ADMIN_DIRECTIVE, re.compile(r"^(yönetici talimatı|talimat|direktif)\b")),
    (
        Intent.REPLY_MODE,
        re.compile(
            r"\b(sesli|konuşarak|yazılı|yazarak)\b.*\b(cevap|yanıt|mod|konuş)"
            r"|\bchat'?e yaz\b|\bsessiz (ol|moda)\b|\bsesli konuş\b"
        ),
    ),
    (
        Intent.SCREENSHOT,
        re.compile(
            r"\bekran (görüntüsü|resmi|fotoğrafı)\b|\bekranı (kaydet|çek|yakala)\b"
            r"|\bscreenshot\b|\bss al\b"
        ),
    ),
    (
        Intent.PAUSE,
        re.compile(
            r"\bkayıt dışı\b|\bkaydı (durdur|kapat|beklet)\b"
            r"|\b(not almayı|dinlemeyi) (bırak|durdur)\b"
        ),
    ),
    (
        Intent.RESUME,
        re.compile(
            r"\bkayda (devam|geri dön|başla)\b|\b(not almaya|dinlemeye) devam\b"
            r"|\bkaydı (başlat|aç|sürdür)\b"
        ),
    ),
    (
        Intent.LEAVE,
        re.compile(r"\btoplantıdan (ayrıl|çık)\w*|\b(ayrılabilirsin|çıkabilirsin)\b"),
    ),
    (Intent.DECISION, re.compile(r"^karar\b|\bkarar olarak (yaz|kaydet|not al|işaretle)\w*")),
    (Intent.SUMMARY, re.compile(r"\bözet(le|ler|ini|i)?\b|\bne konuştuk\b")),
    # "Katip, bir not al: …", "şunu not et", "not alır mısın" — dolgu kelimeleri serbest.
    (Intent.NOTE, re.compile(r"\bnot (al|et|düş)\w*")),
    (Intent.ACTION, re.compile(r"\b(aksiyon|görev)\w* (ekle|oluştur|aç|yaz)\w*")),
    (
        Intent.LANGUAGE,
        re.compile(
            r"\b(" + "|".join(_LANGS) + r")\b.*\b(çevir|yap|geç)\b"
            r"|^çeviri dili (?:" + "|".join(_LANGS) + r")"
            r"(?: (?:olarak )?(?:ayarla|aktif et|etkinleştir))?$"
        ),
    ),
    (
        Intent.REPORT,
        re.compile(
            r"\bdokümante et\w*|\brapor\w* (hazırla|çıkar|oluştur|ver|üret)\w*"
            r"|\btoplantı notlarını (çıkar|hazırla)\w*"
        ),
    ),
    (
        Intent.HELP,
        re.compile(
            r"\b(yardım|komutlar\w*|neler yapabilirsin|ne yapabiliyorsun|neler yapabiliyorsun)\b"
        ),
    ),
]

_BODY_INTENTS = frozenset({Intent.NOTE, Intent.ACTION, Intent.DECISION, Intent.ADMIN_DIRECTIVE})
_TRAILING_FILLER = re.compile(r"(\s*\b(bunu|şunu|bir|lütfen)\b)+\s*$")


def _body(norm: str, match: re.Match[str]) -> str:
    after = norm[match.end() :].strip(" :,-")
    if after:
        return after
    before = _TRAILING_FILLER.sub("", norm[: match.start()])
    return before.strip(" :,-")


_VOICE_WORDS = ("sesli", "konuşarak", "sesli konuş")


def classify(text: str, source: str, raw: str) -> Command:
    norm = normalize(text)
    actions = {
        "sunumu başlat": "start",
        "sunumu bitir": "stop",
        "sunumu durdur": "stop",
        "sonraki slayt": "next",
        "ileri": "next",
        "önceki slayt": "prev",
        "geri": "prev",
        "sunumu yap": "auto",
        "sunumu baştan sona yap": "auto",
        "sunumu anlatarak yap": "auto",
        "dur": "pause",
        "burada dur": "pause",
        "bekle": "pause",
        "bir dakika": "pause",
        "duraklat": "pause",
        "devam et": "resume",
        "sunuma devam": "resume",
        "kaldığın yerden devam et": "resume",
    }
    if norm in actions:
        return Command(Intent.PRESENTATION, norm, source, raw, actions[norm])
    if re.fullmatch(r"(?:bu )?slaytı anlat", norm):
        return Command(Intent.NARRATE, norm, source, raw)
    if re.fullmatch(r"(?:bu )?slaytı oku", norm):
        return Command(Intent.READ_SLIDE, norm, source, raw)
    ordinals = {
        "birinci": 1,
        "ikinci": 2,
        "üçüncü": 3,
        "dördüncü": 4,
        "beşinci": 5,
        "altıncı": 6,
        "yedinci": 7,
        "sekizinci": 8,
        "dokuzuncu": 9,
        "onuncu": 10,
    }
    if match := re.fullmatch(r"(\d+|" + "|".join(ordinals) + r") slayt(?:a git)?", norm):
        number = match[1]
        page = str(ordinals[number]) if number in ordinals else number
        return Command(Intent.PRESENTATION, norm, source, raw, "goto:" + page)
    for intent, pattern in _RULES:
        if match := pattern.search(norm):
            arg = None
            if intent is Intent.LANGUAGE:
                arg = next(code for name, code in _LANGS.items() if name in norm)
            elif intent is Intent.REPLY_MODE:
                arg = "voice" if any(w in norm for w in _VOICE_WORDS) else "chat"
            body = _body(norm, match) if intent in _BODY_INTENTS else norm
            return Command(intent, body, source, raw, arg)
    return Command(Intent.ASK, norm, source, raw)


_SLASH: dict[str, Intent] = {
    "özet": Intent.SUMMARY,
    "ozet": Intent.SUMMARY,
    "not": Intent.NOTE,
    "aksiyon": Intent.ACTION,
    "görev": Intent.ACTION,
    "gorev": Intent.ACTION,
    "sesimi-tanı": Intent.ENROLL,
    "sesimi-tani": Intent.ENROLL,
    "kayıt": Intent.ENROLL,
    "onay": Intent.CONSENT,
    "unut": Intent.FORGET,
    "beni-unut": Intent.FORGET,
    "dil": Intent.LANGUAGE,
    "sor": Intent.ASK,
    "yardım": Intent.HELP,
    "yardim": Intent.HELP,
    "admin": Intent.ADMIN_DIRECTIVE,
    "sesli": Intent.REPLY_MODE,
    "yazılı": Intent.REPLY_MODE,
    "yazili": Intent.REPLY_MODE,
    "mod": Intent.REPLY_MODE,
    "ekran": Intent.SCREENSHOT,
    "ss": Intent.SCREENSHOT,
    "dur": Intent.PAUSE,
    "kayıt-dışı": Intent.PAUSE,
    "kayit-disi": Intent.PAUSE,
    "devam": Intent.RESUME,
    "karar": Intent.DECISION,
    "ayrıl": Intent.LEAVE,
    "ayril": Intent.LEAVE,
    "kvkk": Intent.PRIVACY_NOTICE,
    "rapor": Intent.REPORT,
}


def parse_slash(text: str) -> Command | None:
    raw = text.strip()
    if not raw.startswith("/"):
        return None
    head, _, body = raw[1:].partition(" ")
    if normalize(head) == "sunum":
        short = normalize(body)
        if short in {"yap", "dur", "devam"}:
            action = {"yap": "auto", "dur": "pause", "devam": "resume"}[short]
            return Command(Intent.PRESENTATION, short, "chat", raw, action)
        if short in {"oku", "anlat"}:
            intent = Intent.READ_SLIDE if short == "oku" else Intent.NARRATE
            return Command(intent, short, "chat", raw)
        return classify(body, "chat", raw)
    intent = _SLASH.get(normalize(head))
    if intent is None:
        return Command(Intent.HELP, "", "chat", raw)
    body = body.strip()
    arg = None
    if intent is Intent.LANGUAGE and body:
        arg = normalize(body).split()[0]
    elif intent is Intent.REPLY_MODE:
        # /sesli, /yazılı ya da /mod sesli|yazılı
        word = normalize(body).split()[0] if normalize(head) == "mod" and body else normalize(head)
        arg = "voice" if word == "sesli" else "chat"
    return Command(intent, body, "chat", raw, arg)


# Seslenme değil, Katip'ten *bahsetme*: "Katip mi diyeyim", "Katip de not alıyor", "Katip gibi".
_MENTION_AFTER = re.compile(
    r"^(mi|mı|mu|mü|da|de|gibi|diye|diyeyim|diyelim|dediğimiz|denen|olarak|ile|ne)\b"
)


# Seslenme biçimleri: "Katip", "Katibim", "Katipciğim" ve yönelme ("Katip'e bir sorum var").
# Diğer ekli biçimler ("Katipimiz yazıyor mu", "Katip'i geçir", "Katip'te", "Katibin") Katip'ten
# bahsetmektir: modeli çağırmaya değmez (10. test: 56 çağrının 19'u böyle boşa gitti).
_ADDRESS_FORMS = frozenset(
    {"katip", "katibim", "katipciğim", "katipcim", "katipçiğim", "katip'e", "katibe", "katipe"}
)
_WAKE_TOKEN = re.compile(r"kati[pb]\w*(?:'\w+)?")


def _is_address(norm: str, match: re.Match[str]) -> bool:
    token = _WAKE_TOKEN.search(norm, match.start())
    return bool(token) and token.group(0) in _ADDRESS_FORMS


def parse_voice(transcript: str, *, strict: bool = True) -> Command | None:
    """Seslenmeyi ve komutu bulur.

    strict=True (kural modu): serbest soru yalnızca "Katip" cümle başındaysa ve istek en az 3
    kelimeyse ya da soru işareti varsa. strict=False (agent modu): "Katip" cümlenin herhangi bir
    yerinde geçebilir; Katip'e mi söylendiğine model karar verir (yalnızca bahsediliyorsa susar).
    """
    rest = detect_wake(transcript)
    if rest is None:
        return None
    norm = normalize(transcript)
    m = _WAKE.search(norm)
    after = norm[m.end() :].strip(" ,'") if m else ""
    if after and _MENTION_AFTER.match(after):
        return None  # Katip'ten bahsediliyor, Katip'e seslenilmiyor
    if m and not _is_address(norm, m):
        return None  # ekli biçim ("Katipimiz", "Katip'i"): bahsetme
    if not rest:
        return Command(Intent.LISTEN, "", "voice", transcript)
    command = classify(rest, "voice", transcript)
    if command.intent in _BODY_INTENTS and not command.text and m:
        # "Fiyatlar değişti, Katip bunu not al" → not metni seslenmeden önce söylendi.
        before = norm[: m.start()].strip(" ,'")
        if before:
            command = Command(command.intent, before, command.source, command.raw, command.arg)
    if command.intent is Intent.ASK and strict:
        words_before = len(norm[: m.start()].split()) if m else 0
        is_question = "?" in transcript
        # Serbest soru yalnızca Katip'e gerçekten yöneltildiyse: soru işareti var ya da
        # seslenme cümle başında ve istek en az 3 kelime. "Katip konuşuyor", "Bence
        # yakıştı Katip" gibi sohbet cümleleri cevaplanmaz (canlı testte görüldü).
        if not is_question and (words_before > 0 or len(rest.split()) < 3):
            return None
    return command
