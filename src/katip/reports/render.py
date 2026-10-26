"""Toplantı raporunu Word (DOCX) ve PDF olarak üretir.

Şema sabittir: her rapor aynı başlık bilgisini ve aynı numaralı bölümleri aynı sırayla içerir;
bir bölümde içerik yoksa "Kayıt yok." yazılır. Böylece raporlar yan yana okunabilir ve ekip
şemayı ileride değiştirmek istediğinde tek yer `SECTIONS` olur.

PDF, HTML + CSS'ten Chromium ile basılır: bot imajında Chromium zaten var, ek bir PDF
kütüphanesi ve font dosyası taşımak gerekmez, Türkçe karakterler sorunsuzdur.
"""

from __future__ import annotations

import asyncio
import base64
import html
import io
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor

from katip.reports.summarize import MeetingReport
from katip.store.crypto import DataKeyError, MeetingCipher, atomic_write, available, file_lock

EMPTY = "Kayıt yok."

# Bölüm sırası ve adları (numaralar otomatik). Ekip şemayı belirleyince burası değişir.
SECTIONS = (
    ("summary", "Yönetici özeti"),
    ("agenda", "Gündem ve konuşulan başlıklar"),
    ("decisions", "Kararlar"),
    ("actions", "Aksiyonlar"),
    ("questions", "Açık sorular"),
    ("notes", "Katip'e aldırılan notlar"),
    ("participants", "Katılımcı katkıları"),
    ("screenshots", "Ekran görüntüleri"),
)
_NOTE_KIND = {"note": "Not", "decision": "Karar", "action": "Aksiyon"}


@dataclass
class ReportContext:
    report: MeetingReport
    meeting_id: str
    cipher: MeetingCipher | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    platform: str | None = None
    company: str | None = None
    # (ad, unvan) ya da (ad, unvan, e-posta) — unvan ve e-posta şemadan gelir
    participants: list[tuple] = field(default_factory=list)
    segments: list[dict] = field(default_factory=list)
    include_transcript: bool = True
    # (sıra no, dosya yolu, Claude açıklaması, toplantı saniyesi)
    screenshots: list[tuple[int, str, str | None, float]] = field(default_factory=list)
    # Katip'e aldırılan kayıtlar: (tür, metin, kim, zaman)
    notes: list[tuple[str, str, str | None, datetime | None]] = field(default_factory=list)

    def _local(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        from zoneinfo import ZoneInfo

        from katip.config import get_settings

        if value.tzinfo is None:  # SQLite saat dilimsiz döndürebilir: değer UTC'dir
            value = value.replace(tzinfo=ZoneInfo("UTC"))
        return value.astimezone(ZoneInfo(get_settings().timezone))

    @property
    def date_text(self) -> str:
        local = self._local(self.started_at)
        return local.strftime("%d.%m.%Y %H:%M") if local else "-"

    @property
    def day_text(self) -> str:
        local = self._local(self.started_at)
        return local.strftime("%d.%m.%Y") if local else "-"

    @property
    def time_text(self) -> str:
        start = self._local(self.started_at)
        if not start:
            return "-"
        end = self._local(self.ended_at)
        return f"{start:%H:%M} – {end:%H:%M}" if end else f"{start:%H:%M} – devam ediyor"

    @property
    def duration_text(self) -> str:
        if not (self.started_at and self.ended_at):
            return "-"
        minutes = max(1, round((self.ended_at - self.started_at).total_seconds() / 60))
        hours, rest = divmod(minutes, 60)
        if not hours:
            return f"{minutes} dk"
        return f"{hours} sa {rest} dk" if rest else f"{hours} sa"

    @property
    def participants_text(self) -> str:
        return ", ".join(_participant_text(*p) for p in self.participants) or "-"

    def meta_rows(self) -> list[tuple[str, str]]:
        return [
            ("Konu", self.report.title),
            ("Tarih", self.day_text),
            ("Saat", self.time_text),
            ("Süre", self.duration_text),
            ("Platform", (self.platform or "-").title()),
            ("Katılımcılar", self.participants_text),
        ]

    def note_time(self, at: datetime | None) -> str:
        local = self._local(at)
        return local.strftime("%H:%M") if local else "-"


def _stamp(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def _participant_text(name: str, title: str | None = None, email: str | None = None) -> str:
    text = f"{name} ({title})" if title else name
    return f"{text} <{email}>" if email else text


def _headings() -> dict[str, str]:
    return {key: f"{i}. {title}" for i, (key, title) in enumerate(SECTIONS, start=1)}


# ---------------------------------------------------------------------- DOCX


def render_docx(ctx: ReportContext, path: Path) -> Path:
    _check_cipher(ctx)
    r = ctx.report
    h = _headings()
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)

    kicker = doc.add_paragraph(
        " · ".join(x for x in ((ctx.company or "").upper(), "TOPLANTI NOTU") if x)
    )
    kicker.runs[0].font.size = Pt(9)
    kicker.runs[0].font.color.rgb = RGBColor(0x6B, 0x72, 0x80)
    doc.add_heading(r.title, level=0)

    meta = doc.add_table(rows=0, cols=2)
    meta.style = "Light List Accent 1"
    for label, value in ctx.meta_rows():
        cells = meta.add_row().cells
        cells[0].text = label
        cells[1].text = value

    def empty() -> None:
        doc.add_paragraph(EMPTY).runs[0].italic = True

    doc.add_heading(h["summary"], level=1)
    doc.add_paragraph(r.summary or EMPTY)

    doc.add_heading(h["agenda"], level=1)
    for point in r.key_points:
        doc.add_paragraph(point, style="List Bullet")
    if not r.key_points:
        empty()

    doc.add_heading(h["decisions"], level=1)
    for d in r.decisions:
        doc.add_paragraph(f"{d.text} — {d.owner}" if d.owner else d.text, style="List Number")
    if not r.decisions:
        empty()

    doc.add_heading(h["actions"], level=1)
    if r.action_items:
        table = doc.add_table(rows=1, cols=4)
        table.style = "Light Grid Accent 1"
        for cell, head in zip(
            table.rows[0].cells, ("Sorumlu", "Görev", "Termin", "Öncelik"), strict=True
        ):
            cell.text = head
        for a in r.action_items:
            cells = table.add_row().cells
            cells[0].text = a.owner or "Atanmadı"
            cells[1].text = a.task
            cells[2].text = a.due or "-"
            cells[3].text = a.priority or "-"
    else:
        empty()

    doc.add_heading(h["questions"], level=1)
    for q in r.open_questions:
        doc.add_paragraph(q, style="List Bullet")
    if not r.open_questions:
        empty()

    doc.add_heading(h["notes"], level=1)
    if ctx.notes:
        table = doc.add_table(rows=1, cols=4)
        table.style = "Light Grid Accent 1"
        for cell, head in zip(table.rows[0].cells, ("Saat", "Tür", "Kayıt", "Kim"), strict=True):
            cell.text = head
        for kind, text, author, at in ctx.notes:
            cells = table.add_row().cells
            cells[0].text = ctx.note_time(at)
            cells[1].text = _NOTE_KIND.get(kind, "Not")
            cells[2].text = text
            cells[3].text = author or "-"
    else:
        empty()

    doc.add_heading(h["participants"], level=1)
    for p in r.participants:
        doc.add_paragraph().add_run(p.name).bold = True
        for item in p.highlights:
            doc.add_paragraph(item, style="List Bullet 2")
    if not r.participants:
        empty()

    doc.add_heading(h["screenshots"], level=1)
    shots = [s for s in ctx.screenshots if Path(s[1]).exists()]
    for number, shot_path, caption, at_s in shots:
        picture = io.BytesIO(ctx.cipher.read_file(Path(shot_path))) if ctx.cipher else shot_path
        doc.add_picture(picture, width=Inches(6.2))
        para = doc.add_paragraph()
        head = para.add_run(f"Görüntü {number} · {_stamp(at_s)}  ")
        head.bold = True
        head.font.size = Pt(9)
        if caption:
            body = para.add_run(caption)
            body.font.size = Pt(9)
    if not shots:
        empty()

    if ctx.include_transcript and ctx.segments:
        doc.add_page_break()
        doc.add_heading("Ek: Transkript", level=1)
        for seg in ctx.segments:
            para = doc.add_paragraph()
            para.paragraph_format.space_after = Pt(2)
            stamp = para.add_run(f"[{_stamp(seg['start_s'])}] ")
            stamp.font.color.rgb = RGBColor(0x9C, 0xA3, 0xAF)
            stamp.font.size = Pt(8.5)
            speaker = seg.get("speaker_name") or "Bilinmeyen"
            if seg.get("speaker_email"):
                speaker += f" <{seg['speaker_email']}>"
            who = para.add_run(f"{speaker}: ")
            who.bold = True
            who.font.size = Pt(9)
            body = para.add_run(seg["text"])
            body.font.size = Pt(9)

    footer = doc.sections[0].footer.paragraphs[0]
    footer.text = f"Katip tarafından oluşturuldu · Toplantı {ctx.meeting_id}"
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.runs[0].font.size = Pt(8)

    path.parent.mkdir(parents=True, exist_ok=True)
    if ctx.cipher:
        stream = io.BytesIO()
        doc.save(stream)
        ctx.cipher.write_file(path, stream.getvalue())
    else:
        with file_lock(ctx.meeting_id):
            doc.save(path)
    return path


# ----------------------------------------------------------------------- PDF

_CSS = """
@page { size: A4; margin: 18mm 16mm 20mm; }
* { box-sizing: border-box; }
body { font-family: "Segoe UI", "Noto Sans", "DejaVu Sans", Arial, sans-serif;
       color: #1f2430; font-size: 10.5pt; line-height: 1.5; margin: 0; }
.company { font-size: 8.5pt; letter-spacing: .14em; color: #6b7280; text-transform: uppercase; }
h1 { font-size: 21pt; margin: 4px 0 14px; color: #111827; line-height: 1.2; }
h2 { font-size: 12.5pt; margin: 22px 0 8px; padding-bottom: 4px; color: #1e3a8a;
     border-bottom: 1.5px solid #dbe3f4; break-after: avoid; }
.meta { display: grid; grid-template-columns: 110px 1fr; gap: 4px 14px; font-size: 9.5pt;
        background: #f5f7fb; border: 1px solid #e3e8f2; border-radius: 6px; padding: 10px 14px; }
.meta dt { color: #6b7280; } .meta dd { margin: 0; }
.summary { font-size: 11pt; }
.empty { color: #9ca3af; font-style: italic; }
ul, ol { margin: 4px 0; padding-left: 20px; } li { margin: 2px 0; }
table { width: 100%; border-collapse: collapse; font-size: 9.5pt; }
th { text-align: left; background: #1e3a8a; color: #fff; padding: 6px 8px; font-weight: 600; }
td { padding: 6px 8px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }
tr { break-inside: avoid; }
.muted { color: #9ca3af; }
.person { font-weight: 600; margin-top: 8px; }
.transcript { break-before: page; }
.shot { break-inside: avoid; margin: 10px 0 16px; }
.shot img { width: 100%; border: 1px solid #e5e7eb; border-radius: 4px; }
.shot .cap { font-size: 9pt; color: #4b5563; margin-top: 4px; }
.line { font-size: 9pt; margin: 0 0 3px; }
.line .t { color: #9ca3af; font-variant-numeric: tabular-nums; margin-right: 6px; }
.line .w { font-weight: 600; }
"""


def build_html(ctx: ReportContext) -> str:
    r = ctx.report
    h = _headings()
    e = html.escape
    empty = f'<p class="empty">{EMPTY}</p>'
    out = [f"<style>{_CSS}</style>"]
    kicker = " · ".join(x for x in (ctx.company or "", "Toplantı notu") if x)
    out.append(f'<div class="company">{e(kicker)}</div>')
    out.append(f"<h1>{e(r.title)}</h1>")
    out.append(
        '<dl class="meta">'
        + "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in ctx.meta_rows())
        + "</dl>"
    )

    out.append(f'<h2>{e(h["summary"])}</h2><p class="summary">{e(r.summary or EMPTY)}</p>')

    out.append(f"<h2>{e(h['agenda'])}</h2>")
    out.append(
        "<ul>" + "".join(f"<li>{e(p)}</li>" for p in r.key_points) + "</ul>"
        if r.key_points
        else empty
    )

    out.append(f"<h2>{e(h['decisions'])}</h2>")
    if r.decisions:
        out.append("<ol>")
        for d in r.decisions:
            owner = f' <span class="muted">— {e(d.owner)}</span>' if d.owner else ""
            out.append(f"<li>{e(d.text)}{owner}</li>")
        out.append("</ol>")
    else:
        out.append(empty)

    out.append(f"<h2>{e(h['actions'])}</h2>")
    if r.action_items:
        out.append("<table><tr><th>Sorumlu</th><th>Görev</th><th>Termin</th><th>Öncelik</th></tr>")
        for a in r.action_items:
            out.append(
                f"<tr><td>{e(a.owner or 'Atanmadı')}</td><td>{e(a.task)}</td>"
                f"<td>{e(a.due or '-')}</td><td>{e(a.priority or '-')}</td></tr>"
            )
        out.append("</table>")
    else:
        out.append(empty)

    out.append(f"<h2>{e(h['questions'])}</h2>")
    out.append(
        "<ul>" + "".join(f"<li>{e(q)}</li>" for q in r.open_questions) + "</ul>"
        if r.open_questions
        else empty
    )

    out.append(f"<h2>{e(h['notes'])}</h2>")
    if ctx.notes:
        out.append("<table><tr><th>Saat</th><th>Tür</th><th>Kayıt</th><th>Kim</th></tr>")
        for kind, text, author, at in ctx.notes:
            out.append(
                f"<tr><td>{e(ctx.note_time(at))}</td><td>{e(_NOTE_KIND.get(kind, 'Not'))}</td>"
                f"<td>{e(text)}</td><td>{e(author or '-')}</td></tr>"
            )
        out.append("</table>")
    else:
        out.append(empty)

    out.append(f"<h2>{e(h['participants'])}</h2>")
    if r.participants:
        for p in r.participants:
            out.append(f'<div class="person">{e(p.name)}</div><ul>')
            out += [f"<li>{e(item)}</li>" for item in p.highlights]
            out.append("</ul>")
    else:
        out.append(empty)

    out.append(f"<h2>{e(h['screenshots'])}</h2>")
    shots = [s for s in ctx.screenshots if Path(s[1]).exists()]
    for number, shot_path, caption, at_s in shots:
        raw = ctx.cipher.read_file(Path(shot_path)) if ctx.cipher else Path(shot_path).read_bytes()
        data = base64.b64encode(raw).decode()
        cap = f" — {e(caption)}" if caption else ""
        out.append(
            f'<div class="shot"><img src="data:image/jpeg;base64,{data}" alt="">'
            f'<div class="cap"><b>Görüntü {number} · {_stamp(at_s)}</b>{cap}</div></div>'
        )
    if not shots:
        out.append(empty)

    if ctx.include_transcript and ctx.segments:
        out.append('<div class="transcript"><h2>Ek: Transkript</h2>')
        for seg in ctx.segments:
            out.append(
                f'<p class="line"><span class="t">{_stamp(seg["start_s"])}</span>'
                f'<span class="w">{e(seg.get("speaker_name") or "Bilinmeyen")}:</span> '
                f"{e(seg['text'])}</p>"
            )
        out.append("</div>")
    return "\n".join(out)


_FOOTER = (
    '<div style="font-size:7.5pt;color:#9ca3af;width:100%;text-align:center;">'
    "Katip · Toplantı {mid} · "
    "<span class='pageNumber'></span>/<span class='totalPages'></span>"
    "</div>"
)


async def render_pdf(ctx: ReportContext, path: Path) -> Path:
    _check_cipher(ctx)
    from playwright.async_api import async_playwright

    path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(build_html(ctx), wait_until="load")
            data = await page.pdf(
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template="<div></div>",
                footer_template=_FOOTER.format(mid=html.escape(ctx.meeting_id)),
                margin={"top": "18mm", "bottom": "20mm", "left": "16mm", "right": "16mm"},
            )
            if ctx.cipher:
                await asyncio.to_thread(ctx.cipher.write_file, path, data)
            else:

                def write_plain():
                    with file_lock(ctx.meeting_id):
                        atomic_write(path, data)

                await asyncio.to_thread(write_plain)
        finally:
            await browser.close()
    return path


def _check_cipher(ctx: ReportContext) -> None:
    if available() and ctx.cipher is None:
        raise DataKeyError("Şifreli rapor için toplantı anahtarı gerekli")
    if ctx.cipher and ctx.cipher.meeting_id != ctx.meeting_id:
        raise DataKeyError("Rapor ve anahtar farklı toplantılara ait")
