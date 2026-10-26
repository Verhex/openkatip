"""Yeni bir uç, açıkça seçilmiş kimlik denetimi olmadan yayınlanamaz."""

from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Mount

from katip.api import auth, security, ws
from katip.api.main import app
from katip.api.routes import internal, meetings, teams, workers

GUARDS = {
    security.require_admin,
    security.require_operator,
    security.require_panel,
    security.require_meeting_read,
    security.require_meeting_manage,
    security.require_meeting_admin,
    security.require_viewer,
    auth._panel_admin,
    auth.require_session,
    meetings.require_grant_manager,
    workers.manager_identity,
    internal.require_bot,
    ws.require_ingest_socket,
    ws.require_panel_socket,
    teams.authenticated_activity,
}

# Bu sayfalar veri içermez; oturum ve toplantı verileri ayrı korumalı API'den gelir.
PUBLIC = {
    ("GET", "/health"),
    ("GET", "/gizlilik"),
    ("GET", "/branding/logo"),
    ("GET", "/admin"),
    ("GET", "/hesap"),
    ("POST", "/api/auth/login"),  # Parola ve kaba kuvvet denetimi giriş gövdesindedir.
    ("POST", "/api/auth/logout"),  # Yalnız sunulan çerezi siler; veri döndürmez.
    ("GET", "/openapi.json"),  # API şeması; toplantı veya hesap verisi içermez.
    ("HEAD", "/openapi.json"),
    ("GET", "/docs"),
    ("HEAD", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("HEAD", "/docs/oauth2-redirect"),
    ("GET", "/redoc"),
    ("HEAD", "/redoc"),
}
# Yalnız kod, tasarım ve font dosyaları; veri dizini hiçbir zaman mount edilmez.
STATIC = {"/fonts", "/admin/static", "/hesap/static", "/panel/static"}


def dependencies(dependant):
    yield dependant.call
    for child in dependant.dependencies:
        yield from dependencies(child)


def iter_routes(routes):
    for route in routes:
        # FastAPI 0.141 router'ları geç bağlar; birleşmiş bağımlılıkları da incele.
        if hasattr(route, "effective_route_contexts"):
            for context in route.effective_route_contexts():
                yield context.original_route, context.starlette_route or context
        else:
            yield route, route


def assert_route_guard(route, context=None):
    context = context or route
    if isinstance(route, Mount):
        assert route.path in STATIC, f"İncelenmemiş dosya mount'u: {route.path}"
        return
    methods = route.methods if not isinstance(route, APIWebSocketRoute) else {"WS"}
    if all((method, context.path) in PUBLIC for method in methods):
        return
    assert isinstance(route, (APIRoute, APIWebSocketRoute)), route.path
    assert GUARDS.intersection(dependencies(context.dependant)), f"Korumasız uç: {context.path}"


def test_every_route_has_an_auth_guard_or_documented_public_purpose():
    routes = list(iter_routes(app.routes))
    assert len(routes) > 80
    for route, context in routes:
        assert_route_guard(route, context)


def test_new_unguarded_route_fails_the_inventory():
    import pytest

    route = APIRoute("/api/forgotten", lambda: {"secret": "private"})
    with pytest.raises(AssertionError, match="Korumasız"):
        assert_route_guard(route)
