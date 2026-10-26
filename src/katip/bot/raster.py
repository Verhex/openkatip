"""PDFium runs only in a disposable child of the meeting worker."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import math
import os
import struct
import sys
import zlib

MAX_BYTES = 8 * 1024 * 1024
MAX_PAGES = 60
PAGE_TIMEOUT = 5


def check_bytes(data: bytes) -> None:
    if not data.startswith(b"%PDF-") or len(data) > MAX_BYTES:
        raise ValueError("Sunum geçerli bir PDF olmalı ve 8 MB sınırını aşmamalı.")


def render(data: bytes, page: int) -> dict:
    import pypdfium2 as pdfium
    from pypdf import PdfReader

    check_bytes(data)
    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted:
        raise ValueError("Şifreli PDF sunumları desteklenmiyor.")
    with pdfium.PdfDocument(data) as doc:
        count = len(doc)
        if not 1 <= count <= MAX_PAGES:
            raise ValueError("Sunum 1–60 sayfa içermeli.")
        if type(page) is not int or not 1 <= page <= count:
            raise ValueError("Slayt numarası sunumun dışında.")
        slide = doc[page - 1]
        try:
            width, height = slide.get_size()
            if not all(math.isfinite(x) and x > 0 for x in (width, height)):
                raise ValueError("Geçersiz sayfa boyutu.")
            bmp = slide.render(scale=min(1280 / width, 720 / height), rev_byteorder=True)
            try:
                pixels = bmp.to_numpy()[:, :, :3].copy()
            finally:
                bmp.close()
        finally:
            slide.close()
    h, w, _ = pixels.shape

    def chunk(kind, payload):
        return (
            struct.pack("!I", len(payload))
            + kind
            + payload
            + struct.pack("!I", zlib.crc32(kind + payload))
        )

    raw = b"".join(b"\0" + row.tobytes() for row in pixels)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 3)) + chunk(b"IEND", b"")
    return {"pages": count, "image": "data:image/png;base64," + base64.b64encode(png).decode()}


async def render_page(data: bytes, page: int) -> dict:
    check_bytes(data)
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHONPATH"}
    }
    env.update({"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "katip.bot.raster",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    try:
        payload = json.dumps({"data": base64.b64encode(data).decode(), "page": page}).encode()
        output, _ = await asyncio.wait_for(proc.communicate(payload), PAGE_TIMEOUT)
        result = json.loads(output)
        if proc.returncode or "error" in result:
            raise ValueError(result.get("error", "PDF okunamadı."))
        return result
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise ValueError("PDF bozuk veya slayt işleme süresi aşıldı.") from exc
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


def main() -> None:
    try:
        if sys.platform != "win32":
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_CPU, (PAGE_TIMEOUT,) * 2)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        req = json.loads(sys.stdin.buffer.read(12 * 1024 * 1024))
        result = render(base64.b64decode(req["data"], validate=True), req["page"])
    except Exception:
        result = {"error": "PDF şifreli, bozuk veya 60 sayfa sınırını aşıyor."}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
