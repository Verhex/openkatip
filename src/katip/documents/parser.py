"""Untrusted document extraction in a disposable, bounded process. No file extraction."""

from __future__ import annotations

import base64
import io
import json
import re
import sys
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

MAX_TEXT = 200_000
MAX_PAGES = 400
MAX_EXPANDED = 32 * 1024 * 1024


def xml(data: bytes):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("XML varlıkları desteklenmiyor")
    return ET.fromstring(data)


def extract(data: bytes, kind: str) -> list[dict]:
    pages = []
    if kind == "txt":
        text = data.decode("utf-8-sig")
        pages = [
            {"location": f"bölüm {i // 4000 + 1}", "text": text[i : i + 4000]}
            for i in range(0, len(text), 4000)
        ]
    elif kind in {"pdf", "pdf-presentation"}:
        from pypdf import PdfReader

        if not data.startswith(b"%PDF-"):
            raise ValueError("Geçersiz PDF")
        reader = PdfReader(io.BytesIO(data), strict=True)
        limit = 60 if kind == "pdf-presentation" else MAX_PAGES
        if reader.is_encrypted or not 1 <= len(reader.pages) <= limit:
            raise ValueError("Şifreli veya çok uzun PDF desteklenmiyor")
        for n, page in enumerate(reader.pages, 1):
            pages.append({"location": f"sayfa {n}", "text": page.extract_text() or ""})
    elif kind in {"pptx", "docx"}:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 3000 or sum(i.file_size for i in infos) > MAX_EXPANDED:
                raise ValueError("Dosyanın açılmış boyutu sınırı aşıyor")
            for info in infos:
                path = PurePosixPath(info.filename)
                if path.is_absolute() or ".." in path.parts or info.flag_bits & 1:
                    raise ValueError("Geçersiz arşiv")
                if "vbaproject" in info.filename.lower():
                    raise ValueError("Makrolar desteklenmiyor")
                if info.file_size > 4 * 1024 * 1024:
                    # Large media can be ignored, but oversized XML cannot be parsed.
                    if info.filename.lower().endswith(".xml"):
                        raise ValueError("XML boyutu sınırı aşıldı")
                if info.filename.endswith(".rels"):
                    rels = xml(archive.read(info))
                    if any(r.get("TargetMode") == "External" for r in rels):
                        raise ValueError("Harici bağlantıları kaldırıp yeniden yükleyin")
            if kind == "pptx":
                # Presentation order is defined by relationships, not slide filenames.
                root = xml(archive.read("ppt/presentation.xml"))
                relationships = xml(archive.read("ppt/_rels/presentation.xml.rels"))
                targets = {r.get("Id"): r.get("Target", "") for r in relationships}
                ids = root.findall(
                    ".//{http://schemas.openxmlformats.org/presentationml/2006/main}sldId"
                )
                if len(ids) > MAX_PAGES:
                    raise ValueError("Çok fazla slayt")
                for n, slide_id in enumerate(ids, 1):
                    target = targets.get(
                        slide_id.get(
                            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
                        ),
                        "",
                    )
                    path = target.lstrip("/") if target.startswith("/") else "ppt/" + target
                    if not re.fullmatch(r"ppt/slides/slide\d+\.xml", path):
                        raise ValueError("Geçersiz slayt bağlantısı")
                    slide = xml(archive.read(path))
                    if slide.get("show") == "0":
                        continue
                    text = "\n".join(
                        t.text or ""
                        for t in slide.iter(
                            "{http://schemas.openxmlformats.org/drawingml/2006/main}t"
                        )
                    )
                    pages.append({"location": f"slayt {n}", "text": text})
                # Speaker notes are deliberately private: never import them for Q&A.
            else:
                root = xml(archive.read("word/document.xml"))
                paragraphs = root.iter(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
                )
                for n, paragraph in enumerate(paragraphs, 1):
                    text = " ".join(
                        t.text or ""
                        for t in paragraph.iter(
                            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
                        )
                    )
                    if text.strip():
                        pages.append({"location": f"paragraf {n}", "text": text})
    else:
        raise ValueError("Yalnız PDF, PPTX, DOCX ve UTF-8 TXT destekleniyor")
    if kind != "pdf-presentation":
        pages = [p for p in pages if p["text"].strip()]
    if not pages:
        raise ValueError("Okunabilir metin yok; taranmış dosyalar için metinli sürüm yükleyin")
    if len(pages) > MAX_PAGES or sum(len(p["text"]) for p in pages) > MAX_TEXT:
        raise ValueError("Belge metni sınırı aşıldı")
    return pages


def main() -> None:
    try:
        if sys.platform != "win32":
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
            resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        request = json.loads(sys.stdin.buffer.read(24 * 1024 * 1024))
        pages = extract(base64.b64decode(request["data"], validate=True), request["kind"])
        print(json.dumps({"pages": pages}, ensure_ascii=False))
    except Exception:
        print(json.dumps({"error": "Belge okunamadı veya güvenli işlem sınırı aşıldı"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
