"""The rate-limiter's per-client key.

`client_key` decides whose bucket a login attempt counts against, so anything
it trusts is a value an attacker would like to control. Getting this wrong is
worse than having no limiter: a key the client picks hands out a fresh quota per
forged value, turning the limiter into an unlimited credential-stuffing and
argon2-CPU-burn channel while still locking out real users who share a bucket.
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

from app.config import settings
from app.ratelimit import client_key


def _request(headers: dict[str, str], peer: str = "127.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/login",
            "headers": [
                (k.lower().encode(), v.encode()) for k, v in headers.items()
            ],
            "client": (peer, 12345),
            "scheme": "http",
            "server": ("testserver", 80),
            "query_string": b"",
        }
    )


@pytest.fixture
def trusting(monkeypatch):
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)


def test_cf_connecting_ip_is_used_when_proxy_headers_are_trusted(trusting) -> None:
    """The whole point: real client addresses instead of one 127.0.0.1 bucket."""
    assert client_key(_request({"CF-Connecting-IP": "203.0.113.9"})) == "203.0.113.9"


def test_proxy_headers_are_ignored_when_not_trusted(monkeypatch) -> None:
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", False)
    req = _request({"CF-Connecting-IP": "203.0.113.9",
                    "X-Forwarded-For": "203.0.113.10"})
    assert client_key(req) == "127.0.0.1"


# --- X-Forwarded-For must never be honoured --------------------------------
# CF-Connecting-IP is safe to trust because Cloudflare OVERWRITES it at the
# edge. That argument does not carry over to XFF, which every well-behaved proxy
# APPENDS to — the left-most entry is simply what the client sent. Honouring it
# gave an attacker a fresh quota per forged value.

@pytest.mark.parametrize(
    "forged",
    ["1.2.3.4", "1.2.3.5", "10.0.0.1, 203.0.113.9", "  8.8.8.8  "],
)
def test_forged_x_forwarded_for_does_not_change_the_bucket(trusting, forged) -> None:
    assert client_key(_request({"X-Forwarded-For": forged})) == "127.0.0.1"


def test_varying_x_forwarded_for_shares_one_bucket(trusting) -> None:
    """The exploit, stated directly: N forged values must not mean N quotas."""
    keys = {
        client_key(_request({"X-Forwarded-For": f"1.2.3.{n}"}))
        for n in range(1, 25)
    }
    assert keys == {"127.0.0.1"}


def test_x_forwarded_for_cannot_override_cf_connecting_ip(trusting) -> None:
    """A client appending XFF must not be able to steal or dodge a CF bucket."""
    req = _request({"CF-Connecting-IP": "203.0.113.9",
                    "X-Forwarded-For": "1.2.3.4"})
    assert client_key(req) == "203.0.113.9"
