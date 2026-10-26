"""File-based secrets; exceptions expose only the path, never file contents."""

from pathlib import Path


class SecretUnavailable(OSError):
    pass


def read_secret(path: str | Path) -> str:
    location = Path(path)
    try:
        value = location.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError):
        raise SecretUnavailable(str(location)) from None
    if not value:
        raise SecretUnavailable(str(location))
    return value
