"""PUBLIC_MODE boot posture: refuse forgeable secrets, fill public defaults.

The rest of the suite pins PUBLIC_MODE=false in conftest, so create_app at
import is a no-op against these checks. This file is the one that opts in.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-32b")

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.app.config import (  # noqa: E402
    DEV_CORS_ORIGIN_REGEX,
    Settings,
    apply_public_defaults,
    assert_public_posture,
    public_posture_errors,
    settings,
)
from server.app.main import create_app  # noqa: E402
from server.app.services import pipeline_bridge  # noqa: E402


def _public(**kw) -> Settings:
    """A Settings instance with PUBLIC_MODE on and safe secrets, plus overrides."""
    defaults = dict(
        PUBLIC_MODE=True,
        JWT_SECRET="a" * 32,
        APP_SECRET="b" * 32,
    )
    defaults.update(kw)
    return Settings(**defaults)


def test_local_mode_accepts_the_documented_jwt_default() -> None:
    s = Settings(PUBLIC_MODE=False, JWT_SECRET="dev-insecure-change-me", APP_SECRET="")
    assert public_posture_errors(s) == []
    assert_public_posture(s)  # must not raise


def test_public_mode_refuses_placeholder_jwt() -> None:
    errs = public_posture_errors(_public(JWT_SECRET="dev-insecure-change-me"))
    assert any("JWT_SECRET" in e for e in errs)


def test_public_mode_refuses_env_example_placeholder() -> None:
    errs = public_posture_errors(_public(JWT_SECRET="change-me-in-prod"))
    assert any("JWT_SECRET" in e for e in errs)


def test_public_mode_refuses_short_jwt() -> None:
    errs = public_posture_errors(_public(JWT_SECRET="short-secret"))
    assert any("JWT_SECRET" in e for e in errs)


def test_public_mode_refuses_empty_app_secret() -> None:
    errs = public_posture_errors(_public(APP_SECRET=""))
    assert any("APP_SECRET" in e for e in errs)


def test_public_mode_accepts_real_secrets() -> None:
    assert public_posture_errors(_public()) == []


def test_assert_public_posture_raises_runtime_error() -> None:
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        assert_public_posture(_public(JWT_SECRET="dev-insecure-change-me"))


def test_apply_public_defaults_fills_empty_values() -> None:
    s = _public(
        RATELIMIT_REGISTER="",
        RATELIMIT_LOGIN="",
        ACCESS_TTL_MIN=60 * 24 * 30,
        CORS_ORIGIN_REGEX=DEV_CORS_ORIGIN_REGEX,
    )
    apply_public_defaults(s)
    assert s.RATELIMIT_REGISTER == "10/hour"
    assert s.RATELIMIT_LOGIN == "10/minute;100/hour"
    assert s.ACCESS_TTL_MIN == 1440
    assert s.CORS_ORIGIN_REGEX == ""


def test_apply_public_defaults_keeps_explicit_values() -> None:
    s = _public(
        RATELIMIT_REGISTER="3/hour",
        RATELIMIT_LOGIN="1/minute",
        ACCESS_TTL_MIN=60,
        CORS_ORIGIN_REGEX=r"^https://studio\.example\.com$",
    )
    apply_public_defaults(s)
    assert s.RATELIMIT_REGISTER == "3/hour"
    assert s.RATELIMIT_LOGIN == "1/minute"
    assert s.ACCESS_TTL_MIN == 60
    assert s.CORS_ORIGIN_REGEX == r"^https://studio\.example\.com$"


def test_apply_public_defaults_is_noop_when_not_public() -> None:
    s = Settings(
        PUBLIC_MODE=False,
        RATELIMIT_REGISTER="",
        ACCESS_TTL_MIN=60 * 24 * 30,
        CORS_ORIGIN_REGEX=DEV_CORS_ORIGIN_REGEX,
    )
    apply_public_defaults(s)
    assert s.RATELIMIT_REGISTER == ""
    assert s.ACCESS_TTL_MIN == 60 * 24 * 30
    assert s.CORS_ORIGIN_REGEX == DEV_CORS_ORIGIN_REGEX


def test_create_app_refuses_insecure_public_mode(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PUBLIC_MODE", True)
    monkeypatch.setattr(settings, "JWT_SECRET", "dev-insecure-change-me")
    monkeypatch.setattr(settings, "APP_SECRET", "b" * 32)
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        create_app()


def test_create_app_public_ok_applies_defaults(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PUBLIC_MODE", True)
    monkeypatch.setattr(settings, "JWT_SECRET", "a" * 32)
    monkeypatch.setattr(settings, "APP_SECRET", "b" * 32)
    monkeypatch.setattr(settings, "RATELIMIT_REGISTER", "")
    monkeypatch.setattr(settings, "RATELIMIT_LOGIN", "")
    monkeypatch.setattr(settings, "ACCESS_TTL_MIN", 60 * 24 * 30)
    monkeypatch.setattr(settings, "CORS_ORIGIN_REGEX", DEV_CORS_ORIGIN_REGEX)
    create_app()
    assert settings.RATELIMIT_REGISTER == "10/hour"
    assert settings.ACCESS_TTL_MIN == 1440
    assert settings.CORS_ORIGIN_REGEX == ""


def test_fake_steps_refused_outside_pytest(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "PUBLIC_MODE", False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("BER_ALLOW_FAKE_STEPS", raising=False)
    monkeypatch.setenv("BER_ENGINE_FAKE_STEPS", str(tmp_path / "steps.json"))
    assert pipeline_bridge._synthetic_steps_from_env() is None


def test_fake_steps_refused_under_public_mode_even_with_allow(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(settings, "PUBLIC_MODE", True)
    monkeypatch.setenv("BER_ALLOW_FAKE_STEPS", "1")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_public_posture.py::x")
    monkeypatch.setenv("BER_ENGINE_FAKE_STEPS", str(tmp_path / "steps.json"))
    assert pipeline_bridge._synthetic_steps_from_env() is None


def test_fake_steps_honored_under_pytest(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "PUBLIC_MODE", False)
    fake = tmp_path / "steps.json"
    fake.write_text(
        json.dumps([{"id": "x", "num": 1, "script": "x.py", "args": [], "produces": []}]),
        encoding="utf-8",
    )
    monkeypatch.setenv("BER_ENGINE_FAKE_STEPS", str(fake))
    steps = pipeline_bridge._synthetic_steps_from_env()
    assert steps is not None
    assert steps[0].id == "x"
