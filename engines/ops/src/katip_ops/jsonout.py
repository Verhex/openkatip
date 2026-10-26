"""Tolerant parsing of model JSON answers (code fences or a short preamble around the object)."""

import re

from pydantic import BaseModel, ValidationError

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ModelOutputError(ValueError):
    """The model answer did not contain the requested JSON object."""


def parse_model_json[M: BaseModel](text: str, model: type[M]) -> M:
    candidates = [text.strip()]
    candidates += [match.strip() for match in _FENCE.findall(text)]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    last: ValidationError | None = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return model.model_validate_json(candidate)
        except ValidationError as exc:
            last = exc
    raise ModelOutputError("Model cevabı istenen JSON biçiminde değil.") from last
