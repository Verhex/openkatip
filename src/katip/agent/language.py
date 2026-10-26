"""Yanlış dil etiketine karşı dar, bir kez kullanılan cevap koruması."""

import re

LANGUAGES = {"tr": "Türkçe", "en": "İngilizce", "de": "Almanca", "fr": "Fransızca"}
_NAMES = (
    r"(?:turkce|ingilizce|almanca|fransizca|arapca|ispanyolca|italyanca|rusca|"
    r"japonca|cince|turkish|english|german|french|arabic|spanish|italian|russian|japanese|chinese)"
)
_EXPLICIT = re.compile(
    rf"\bin\s+{_NAMES}\b|\b{_NAMES}(?:ye|ya|'ye|'ya)?\s+"
    r"(?:(?:olarak|dilinde|bir)\s+)?"
    r"(?:soyle\w*|yaz\w*|anlat\w*|cevap\w*|konus\w*|ozetle\w*|acikla\w*|ver\w*|"
    r"oku\w*|seslendir\w*|tekrarla\w*|"
    r"lutfen|please|reply|respond|answer)\b"
)
_WORDS = {
    "tr": set(
        "bu su bir ne neden nasil neyi nedir misin tekrar bana bize bugun toplantiyi "
        "toplanti raporu anlat ozetle soyle lutfen eder anlatir teslim tarihi".split()
    ),
    "en": set("the what why how please can could would meeting explain tell is are".split()),
    "de": set("der die das bitte wie warum ist sind erklaren".split()),
    "fr": set("le la les pourquoi comment explique reunion est sont".split()),
}


def _plain(text: str) -> str:
    return text.lower().translate(str.maketrans("çğıöşü", "cgiosu")).replace("i̇", "i")


def explicit_language(request: str) -> bool:
    value = _plain(request)
    return bool(
        _EXPLICIT.search(value)
        or re.fullmatch(rf"\s*{_NAMES}[.!?]?\s*", value)
        or (
            re.search(r"\b(?:cevir\w*|tercume\w*|translate\w*|translation)\b", value)
            and re.search(_NAMES, value)
        )
    )


def needs_language_repair(reply: str, request: str, source_lang: str) -> bool:
    tag = re.match(r"^\s*\[([a-z]{2})\]", reply)
    if not tag or tag[1] == source_lang or explicit_language(request):
        return False
    words = set(re.findall(r"\w+", _plain(request)))
    return bool(
        words & _WORDS.get(source_lang, set())
        or (source_lang == "tr" and re.search(r"[çğıöşüÇĞİÖŞÜ]", request))
    )
