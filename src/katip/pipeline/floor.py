"""Söz sırası: cümle sınırları ve konuşma sırasında duyulan sözlerin anlamı."""

from __future__ import annotations

import re

from katip.agent.commands import _WAKE, detect_wake, normalize

_ABBREVIATIONS = {"dr", "prof", "av", "sn", "vb", "vs", "örn", "bkz", "no"}
BACKCHANNELS = frozenset(
    "hı hıhı hmm hm ı ıı ııı eee evet tamam tamamdır peki aynen anladım doğru olur "
    "ok okey oke iyi güzel süper harika teşekkürler teşekkür ederim "
    "sağol sağ ol he hee ya".split()
)
_FILLERS = BACKCHANNELS | {"ee", "eee", "ıııı", "şey", "yani"}
_STOP = re.compile(r"\b(dur|sus|yeter|bekle|kes|bir dakika|bir saniye)\b")
_STOP_PHRASE = re.compile(
    r"(?:(?:dur|sus|yeter|bekle|kes|tamam|artık|bir dakika|bir saniye)\s*)+\Z"
)


def split_sentences(text: str) -> list[str]:
    pieces = []
    start = 0
    for match in re.finditer(r"[.!?…]+|\n+", text):
        before = text[start : match.start()]
        if match.group().startswith("."):
            word = re.search(r"(\w+)$", before)
            if word and (word[1][-1].isdigit() or word[1].lower() in _ABBREVIATIONS):
                continue
        piece = text[start : match.end()].strip()
        if piece:
            pieces.append(piece)
        start = match.end()
    if tail := text[start:].strip():
        pieces.append(tail)
    merged = []
    for piece in pieces:
        if merged and (len(piece) < 15 or len(merged[-1]) < 15):
            merged[-1] += " " + piece
        else:
            merged.append(piece)
    result = []
    for piece in merged:
        while len(piece) > 180:
            cuts = [piece.rfind(sep, 0, 180) for sep in (", ", "; ", " ve ")]
            cut = max(cuts)
            if cut < 15:
                cut = piece.rfind(" ", 0, 180)
            if cut <= 0:
                break
            if piece[cut] in ",;":
                cut += 1
            result.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            result.append(piece)
    return result


def content_words(text: str) -> list[str]:
    return [w for w in _WAKE.sub("", normalize(text)).split() if w not in _FILLERS]


def is_backchannel(text: str) -> bool:
    words = normalize(text).split()
    return len(words) <= 3 and all(w in BACKCHANNELS for w in words)


def is_echo(text: str, spoken: str) -> bool:
    """Katip'in kendi sesi (açık hoparlörden geri gelen) mi? Katip'in sözünü tekrar eden bir
    soru ("Katip, saat üçte mi başlayacak?") yankı sayılmaz: seslenme yok, kelimelerin çoğu
    ve en az üç kelimelik bir dizi Katip'in söylediğinde birebir geçmeli."""
    if detect_wake(text) is not None:
        return False
    words = normalize(text).split()
    said = normalize(spoken).split()
    vocabulary = set(said)
    if len(words) < 3 or sum(w in vocabulary for w in words) / len(words) < 0.8:
        return False
    runs = {tuple(said[i : i + 3]) for i in range(len(said) - 2)}
    return any(tuple(words[i : i + 3]) in runs for i in range(len(words) - 2))


def is_stop(text: str) -> bool:
    rest = detect_wake(text)
    value = normalize(text) if rest is None else rest
    return bool(
        value
        and len(value.split()) <= 3
        and _STOP_PHRASE.fullmatch(value)
        and (_STOP.search(value) or value == "tamam tamam")
    )


def overlap_kind(text: str, *, spoken: str, is_final: bool, wake: bool, min_words: int) -> str:
    return overlap_decision(text, spoken=spoken, is_final=is_final, wake=wake, min_words=min_words)[
        0
    ]


def overlap_decision(
    text: str, *, spoken: str, is_final: bool, wake: bool, min_words: int
) -> tuple[str, str]:
    value = normalize(text)
    addressed = wake or detect_wake(text) is not None
    if (addressed and _STOP.search(value)) or (is_final and is_stop(text)):
        return "hard", "stop"
    if is_backchannel(text):
        return "ignore", "backchannel"
    if is_echo(text, spoken):
        return "ignore", "echo"
    if addressed:
        return "soft", "wake"
    return ("soft", "words") if len(content_words(text)) >= min_words else ("ignore", "short")
