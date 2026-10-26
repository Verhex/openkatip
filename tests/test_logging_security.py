import logging

from katip.logging import AccessQueryFilter


def test_access_log_redacts_signed_links_without_losing_request_status():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:1234",
            "GET",
            "/api/meetings/test/report.pdf?t=private-signed-token&extra=secret",
            "1.1",
            200,
        ),
        None,
    )
    assert AccessQueryFilter().filter(record)
    rendered = record.getMessage()
    assert "private-signed-token" not in rendered and "extra=secret" not in rendered
    assert '/api/meetings/test/report.pdf?[redacted] HTTP/1.1" 200' in rendered


def test_access_log_preserves_plain_paths():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/health", "1.1", 200),
        None,
    )
    original = record.getMessage()
    assert AccessQueryFilter().filter(record)
    assert record.getMessage() == original
