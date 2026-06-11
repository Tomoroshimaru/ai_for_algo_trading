"""Typed configuration loader (Roadmap Step 1d/e).

Loads non-secret settings from ``configs/settings.toml`` and overlays
environment-specific / secret values from environment variables (optionally
populated from a local ``.env`` via python-dotenv).

Design rules enforced here:
* No secret is ever hard-coded or read from the TOML; secrets come only from
  the environment -> satisfies Step 1 acceptance: "secrets are not stored in
  the repository".
* Returns frozen dataclasses (explicit typed objects), never loose dicts,
  per the coding standard at cours.txt:1069-1071.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = REPO_ROOT / "configs" / "settings.toml"


@dataclass(frozen=True)
class IBKRConfig:
    host: str
    port: int
    client_id: int
    account: str | None
    market_data_type: int


@dataclass(frozen=True)
class EnvironmentConfig:
    name: str
    log_dir: Path
    data_dir: Path


@dataclass(frozen=True)
class UniverseConfig:
    source: str
    sp500_url: str


@dataclass(frozen=True)
class QCConfig:
    clock_skew_tolerance_sec: float


@dataclass(frozen=True)
class AppConfig:
    ibkr: IBKRConfig
    environment: EnvironmentConfig
    universe: UniverseConfig
    qc: QCConfig
    timezone: str


def _read_toml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Settings file not found: {path}")
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Env var {name!r} must be an integer, got {raw!r}") from exc


def load_config(
    settings_path: Path | None = None, *, load_env: bool = True
) -> AppConfig:
    """Build the typed AppConfig.

    Example
    -------
    >>> cfg = load_config()
    >>> cfg.ibkr.port
    4002
    """
    if load_env:
        load_dotenv(REPO_ROOT / ".env")

    raw = _read_toml(settings_path or DEFAULT_SETTINGS_PATH)
    ib = raw.get("ibkr", {})
    env = raw.get("environment", {})
    uni = raw.get("universe", {})
    qc = raw.get("qc", {})
    cal = raw.get("calendar", {})

    ibkr = IBKRConfig(
        host=os.getenv("IBKR_HOST", ib.get("host", "127.0.0.1")),
        port=_env_int("IBKR_PORT", int(ib.get("port", 4002))),
        client_id=_env_int("IBKR_CLIENT_ID", int(ib.get("client_id", 1))),
        account=os.getenv("IBKR_ACCOUNT") or (ib.get("account") or None),
        market_data_type=_env_int(
            "IBKR_MARKET_DATA_TYPE", int(ib.get("market_data_type", 3))
        ),
    )
    environment = EnvironmentConfig(
        name=os.getenv("APP_ENV", env.get("name", "dev")),
        log_dir=REPO_ROOT / env.get("log_dir", "artifacts/logs"),
        data_dir=REPO_ROOT / env.get("data_dir", "artifacts/data"),
    )
    universe = UniverseConfig(
        source=uni.get("source", "sp500_wikipedia"),
        sp500_url=uni.get("sp500_url", ""),
    )
    qc_cfg = QCConfig(
        clock_skew_tolerance_sec=float(qc.get("clock_skew_tolerance_sec", 2.0)),
    )
    return AppConfig(
        ibkr=ibkr,
        environment=environment,
        universe=universe,
        qc=qc_cfg,
        timezone=cal.get("timezone", "America/New_York"),
    )
