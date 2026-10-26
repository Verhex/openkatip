"""Ses ifadesi etiketleri; görünen metin ve telaffuz birbirinden ayrılır."""

import re
from collections.abc import Callable

_TAG = re.compile(r"\[[^\]]*\]")
# İfade etiketi biçimi: yalnız Latin harfli 1–3 kelime ([sighs], [clears throat]). Belge kaynak
# gösterimleri ("[plan.pdf — sayfa 2]") buna uymaz; görünen metinde kalır, sesli okunmaz.
_EXPRESSIVE = re.compile(r"\[[A-Za-z]+(?: [A-Za-z]+){0,2}\]")
_ALLOWED = frozenset(
    "chuckles laughs sighs warm softly whispering hesitantly reassuringly pause".split()
)


def _spaces(text: str) -> str:
    text = re.sub(r"[^\S\n]+", " ", text)
    return re.sub(r" +([.,!?;:…])", r"\1", text).strip()


def tts_text(text: str) -> str:
    count = 0

    def keep(match: re.Match) -> str:
        nonlocal count
        if match[0][1:-1] in _ALLOWED and count < 2:
            count += 1
            return match[0]
        return " "

    return _spaces(_TAG.sub(keep, text))


def display_text(text: str) -> str:
    if not _EXPRESSIVE.search(text):
        return text
    return _spaces(_EXPRESSIVE.sub(" ", text))


def transform_text(text: str, transform: Callable[[str], str]) -> str:
    """Telaffuzu yalnızca etiketlerin arasındaki metne uygular."""
    pieces = []
    for part in re.split(r"(\[[^\]]*\])", text):
        if _TAG.fullmatch(part) or not part.strip():
            pieces.append(part)
        else:
            leading = part[: len(part) - len(part.lstrip())]
            trailing = part[len(part.rstrip()) :]
            pieces.append(leading + transform(part.strip()) + trailing)
    return "".join(pieces)
