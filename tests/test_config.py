"""Tests for the typed configuration loader (Step 1d/e)."""
import tomllib
from pathlib import Path

import pytest

from utils.config import load_config, REPO_ROOT

SECRET_ENV = [
    "IBKR_HOST", "IBKR_PORT", "IBKR_CLIENT_ID",
    "IBKR_ACCOUNT", "APP_ENV", "IBKR_MARKET_DATA_TYPE",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in SECRET_ENV:
        monkeypatch.delenv(key, raising=False)


def test_load_defaults():
    cfg = load_config(load_env=False)
    assert cfg.ibkr.host == "127.0.0.1"
    assert cfg.ibkr.port == 4002
    assert cfg.ibkr.client_id == 1
    assert cfg.ibkr.account is None        # no secret baked in
    assert cfg.environment.name == "dev"
    assert cfg.universe.sp500_url.startswith("https://")
    assert cfg.qc.clock_skew_tolerance_sec == 2.0


def test_env_override(monkeypatch):
    monkeypatch.setenv("IBKR_PORT", "7497")
    monkeypatch.setenv("IBKR_ACCOUNT", "DU1234567")
    monkeypatch.setenv("APP_ENV", "prod")
    cfg = load_config(load_env=False)
    assert cfg.ibkr.port == 7497
    assert cfg.ibkr.account == "DU1234567"
    assert cfg.environment.name == "prod"


def test_bad_int_env_raises(monkeypatch):
    monkeypatch.setenv("IBKR_PORT", "not-a-number")
    with pytest.raises(ValueError):
        load_config(load_env=False)


def test_no_secret_in_repo():
    """Acceptance: secrets are not stored in the repository."""
    raw = tomllib.load(open(REPO_ROOT / "configs" / "settings.toml", "rb"))
    assert raw["ibkr"].get("account", "") == ""


def test_dataclasses_are_frozen():
    cfg = load_config(load_env=False)
    with pytest.raises(Exception):
        cfg.ibkr.port = 1  # type: ignore[misc]
