import hashlib

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from katip.api.auth import client_key
from katip.config import Settings, get_settings


def request(peer, headers):
    return Request({"type": "http", "client": (peer, 1234) if peer else None, "headers": headers})


def key(ip):
    return hashlib.sha256(f"katip-client:{ip}".encode()).hexdigest()[:16]


def test_railway_default_keeps_last_forwarded_hop():
    req = request("10.0.0.1", [(b"x-forwarded-for", b"1.2.3.4, 8.8.8.8")])
    assert client_key(req) == key("8.8.8.8")
    assert client_key(request("10.0.0.1", [])) == key("10.0.0.1")


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "192.0.2.1"])
def test_cf_accepts_only_configured_proxy(peer):
    settings = get_settings()
    settings.client_ip_header = "cf-connecting-ip"
    settings.client_ip_trusted_proxies = ["127.0.0.1", "::1", "192.0.2.1"]
    headers = [(b"cf-connecting-ip", b"2001:4860:4860:0:0:0:0:8888")]
    assert client_key(request(peer, headers)) == key("2001:4860:4860::8888")


@pytest.mark.parametrize("peer", ["8.8.4.4", "192.0.2.20", "10.0.0.1", None])
def test_cf_spoof_from_other_peers_is_ignored(peer):
    get_settings().client_ip_header = "cf-connecting-ip"
    headers = [(b"cf-connecting-ip", b"1.2.3.4"), (b"x-forwarded-for", b"8.8.8.8")]
    assert client_key(request(peer, headers)) == key(peer or "?")


@pytest.mark.parametrize(
    "values", [[], [b""], [b"bad"], [b"1.2.3.4, 8.8.8.8"], [b"1.2.3.4", b"8.8.8.8"]]
)
def test_cf_invalid_or_duplicate_header_falls_back_to_peer(values):
    get_settings().client_ip_header = "cf-connecting-ip"
    headers = [(b"cf-connecting-ip", value) for value in values]
    headers.append((b"x-forwarded-for", b"8.8.8.8"))
    assert client_key(request("127.0.0.1", headers)) == key("127.0.0.1")


def test_cf_setting_from_environment_and_invalid_proxy(monkeypatch):
    monkeypatch.setenv("KATIP_CLIENT_IP_HEADER", "cf-connecting-ip")
    monkeypatch.setenv("KATIP_CLIENT_IP_TRUSTED_PROXIES", '["192.0.2.1"]')
    assert Settings().client_ip_trusted_proxies == ["192.0.2.1"]
    monkeypatch.setenv("KATIP_CLIENT_IP_TRUSTED_PROXIES", '["0.0.0.0/0"]')
    with pytest.raises(ValidationError):
        Settings()
