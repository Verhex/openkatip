"""FastAPI uygulaması: bot ingest soketi, panel soketi ve REST uçları."""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from katip.api import auth
from katip.api.routes import (
    admin,
    approvals,
    documents,
    identities,
    integrations,
    internal,
    meetings,
    org,
    teams,
    usage,
    workers,
)
from katip.api.security import panel_identity, require_viewer
from katip.api.ws import router as ws_router
from katip.async_utils import wait_tasks
from katip.config import get_settings
from katip.logging import get_logger, setup_logging
from katip.pipeline.session import _report_tasks, registry
from katip.store import db, repo

log = get_logger(__name__)


def _find_panel_dir() -> Path | None:
    """Panel dosyalarını bulur: geliştirmede depo kökü, Docker'da çalışma dizini (/app).

    Paket `pip install .` ile site-packages'a kurulunca kaynak dosya konumu depo kökünü
    göstermez; bu yüzden yalnızca __file__'a güvenmiyoruz.
    """
    candidates = [Path(p) for p in [os.environ.get("KATIP_PANEL_DIR")] if p] + [
        Path.cwd() / "web" / "panel",
        Path(__file__).resolve().parents[3] / "web" / "panel",
    ]
    return next((c for c in candidates if (c / "index.html").exists()), None)


_PANEL_DIR = _find_panel_dir()
_ADMIN_DIR = _PANEL_DIR.parent / "admin" if _PANEL_DIR is not None else None
_HESAP_DIR = _PANEL_DIR.parent / "hesap" if _PANEL_DIR is not None else None
_FONTS_DIR = _PANEL_DIR.parent / "fonts" if _PANEL_DIR is not None else None


class FontStaticFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        # Windows MIME registry entries may otherwise produce application/octet-stream.
        if Path(full_path).suffix == ".woff2":
            response.headers["Content-Type"] = "font/woff2"
        return response


async def _catch_up_reports() -> None:
    """Açılışta: sunucu yeniden başladığı için raporu üretilmemiş toplantıları tamamla."""
    from katip.reports.service import generate_report

    settings = get_settings()
    if not (settings.auto_report and settings.anthropic_api_key):
        return
    await asyncio.sleep(5)  # açılışı geciktirmesin
    for meeting_id in await repo.meetings_missing_report():
        if registry.get(meeting_id) is not None:
            continue  # şu an süren toplantı
        try:
            await repo.close_interrupted(meeting_id)
            await generate_report(meeting_id, batch=settings.report_batch)
            log.info("eksik rapor tamamlandı", meeting_id=meeting_id)
        except Exception as exc:
            log.warning("eksik rapor üretilemedi", meeting_id=meeting_id, error=str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    db.configure()
    await db.init_db()
    from katip.store import crypto, migration

    if not crypto.available():
        log.warning("Veri anahtarı tanımlı değil; toplantı içeriği düz metin saklanıyor")
    from katip import runtime_settings

    await runtime_settings.load()  # panelden kaydedilmiş ayarlar
    from katip.workers import jobs as worker_jobs

    await worker_jobs.recover()
    from katip import retention
    from katip.integrations import calendar

    background: set[asyncio.Task] = set()
    for job in (
        migration.run_background(),
        _catch_up_reports(),
        retention.run_forever(),
        repo.retry_erasures(),
        calendar.run_forever(),
        worker_jobs.run_forever(),
    ):
        task = asyncio.create_task(job)
        background.add(task)
        task.add_done_callback(background.discard)
    try:
        yield
    finally:
        try:
            task = asyncio.create_task(_close_sessions())
            await wait_tasks({task}, budget_s=15, label="uygulama oturumları")
        finally:
            await wait_tasks(background, budget_s=2, label="uygulama döngüleri", cancel=True)
            # STT kullanım yazıları ve raporlar DB havuzu kapanmadan durdurulur.
            from katip.reports.service import shutdown_reports

            await shutdown_reports()
            await wait_tasks(_report_tasks.get(), budget_s=2, label="oturum raporları", cancel=True)
            task = asyncio.create_task(db.dispose())
            await wait_tasks({task}, budget_s=5, label="veritabanı")


async def _close_sessions() -> None:
    from katip.workers import jobs as worker_jobs

    # Kapanış (ör. Railway yeni sürüme geçerken SIGTERM): açık toplantıları kaydederek kapat.
    # Rapor üretimi uzun sürebileceği için burada beklenmez; açılışta tamamlanır.
    for session in registry.all():
        row = await worker_jobs.get(session.session_id)
        if not (row and row.backend == "remote" and row.status == "queued"):
            await worker_jobs.request_cancel(session.session_id, "api_shutdown")
        with contextlib.suppress(Exception):
            task = asyncio.create_task(registry.drop(session.session_id))
            await wait_tasks({task}, budget_s=8, label="oturum kaydı")
    from katip.bot.supervisor import supervisor

    for session_id in supervisor.running():
        with contextlib.suppress(Exception):
            await supervisor.stop(session_id)


app = FastAPI(title="Katip", version="0.1.0", lifespan=lifespan)
app.include_router(ws_router)
app.include_router(meetings.router)
app.include_router(org.router)
app.include_router(teams.router)
app.include_router(approvals.router)
app.include_router(documents.router)
app.include_router(workers.router)
app.include_router(internal.router)
app.include_router(usage.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(admin.panel_router)
app.include_router(identities.router)
app.include_router(integrations.router)
app.include_router(admin.public_router)

# Yönetim ve canlı panel: yalnız kendi dosyaları çalışır, başka siteye gömülemez.
_ADMIN_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
    "object-src 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    path = request.url.path
    if path in ("/admin", "/hesap") or path.startswith(("/admin/", "/hesap/", "/panel/")):
        h["Content-Security-Policy"] = _ADMIN_CSP
        h["Cache-Control"] = "no-store"
    if path.startswith(("/api/", "/admin")):
        h.setdefault("Cache-Control", "no-store")
    return response


@app.get("/gizlilik", response_class=HTMLResponse, include_in_schema=False)
async def privacy_notice_page() -> HTMLResponse:
    """Ayrıntılı KVKK aydınlatma metni (toplantı chat'indeki linkin açtığı sayfa)."""
    from katip.privacy import notice_page_html

    return HTMLResponse(notice_page_html())


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if _FONTS_DIR is not None and _FONTS_DIR.is_dir():
    app.mount("/fonts", FontStaticFiles(directory=_FONTS_DIR), name="fonts")


if _ADMIN_DIR is not None and (_ADMIN_DIR / "index.html").exists():
    app.mount("/admin/static", StaticFiles(directory=_ADMIN_DIR), name="admin-static")

    @app.get("/admin", include_in_schema=False)
    async def admin_page(request: Request) -> Response:
        access = await panel_identity(request)
        if access and access.role != "admin":
            return RedirectResponse("/hesap", status_code=303)
        return FileResponse(_ADMIN_DIR / "index.html")


if _PANEL_DIR is not None:
    app.mount("/panel/static", StaticFiles(directory=_PANEL_DIR), name="panel-static")

    @app.get("/panel/{session_id}", dependencies=[Depends(require_viewer)])
    async def panel(session_id: str) -> FileResponse:
        return FileResponse(_PANEL_DIR / "index.html")


if _HESAP_DIR is not None and (_HESAP_DIR / "index.html").exists():
    app.mount("/hesap/static", StaticFiles(directory=_HESAP_DIR), name="hesap-static")

    @app.get("/hesap", include_in_schema=False)
    async def account_page(request: Request) -> Response:
        access = await panel_identity(request)
        if access and access.role == "admin":
            return RedirectResponse("/admin", status_code=303)
        return FileResponse(_HESAP_DIR / "index.html")
