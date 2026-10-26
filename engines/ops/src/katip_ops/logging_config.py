"""JSON metadata logs; third-party messages and exception bodies stay private."""

import json
import logging
import traceback


class MetadataFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        owned = record.name.startswith("katip_ops")
        result: dict[str, object] = {
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage() if owned else "dependency_log",
        }
        for field in ("job", "run_id", "approval_id", "kind", "digest", "error_type"):
            value = getattr(record, field, None)
            if isinstance(value, str):
                result[field] = value
        if record.exc_info and record.exc_info[1] is not None:
            # Exception messages may carry remote data, so only types and code locations are logged.
            chain: list[dict[str, object]] = []
            exc: BaseException | None = record.exc_info[1]
            while exc is not None and len(chain) < 5:
                frames = traceback.extract_tb(exc.__traceback__)[-6:]
                chain.append(
                    {
                        "type": type(exc).__name__,
                        "at": [
                            f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}:{f.name}" for f in frames
                        ],
                    }
                )
                exc = exc.__cause__ or exc.__context__
            result["exception"] = chain
        return json.dumps(result, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(MetadataFormatter())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
