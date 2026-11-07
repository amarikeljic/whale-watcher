"""Configuration: one YAML file for settings, the environment (.env) for secrets.

Unknown keys are an error rather than silently ignored, because a typo in a
threshold ("min_settled_market: 5") would otherwise leave the default in place
and nobody would notice until the results looked odd.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

RANK_KEYS = ("clv", "clv_markout", "roi", "total_pnl")


@dataclass
class ScoringConfig:
    lookback_months: int = 6
    min_settled_markets: int = 50
    min_profitable_months: int = 4
    max_single_market_share: float = 0.30
    min_avg_stake_usd: float = 100.0
    max_days_since_last_trade: float = 30.0
    max_two_sided_ratio: float = 0.40
    min_roi: float = 0.05
    top_k: int = 20
    rank_by: str = "clv"
    entry_lookback_days: int = 180
    max_activity_rows: int = 20_000
    max_unreliable_market_share: float = 0.25
    markout_hours: float = 24.0
    min_clv_positions: int = 10
    # CLV needs one price lookup per position (two with the markout). A seeded
    # sample of this many positions per wallet estimates the mean closely at a
    # fraction of the requests; 0 means use every position.
    clv_sample_positions: int = 150


@dataclass
class CandidateConfig:
    leaderboard_periods: list[str] = field(default_factory=lambda: ["week", "month", "all"])
    leaderboard_categories: list[str] = field(default_factory=lambda: ["overall"])
    leaderboard_limit: int = 100
    recent_trades_min_usd: float = 1000.0
    recent_trades_pages: int = 5
    # Leak-free pool for a past as_of: large trades placed before it in the
    # top-volume markets, at most historical_per_event markets per event so one
    # mega-event (42 of the top 60 markets before 2026-07-07 were World Cup
    # winner markets) cannot fill the pool with one-off bettors.
    historical_markets: int = 150
    historical_per_event: int = 3
    historical_min_trade_usd: float = 1000.0
    max_candidates: int = 400


@dataclass
class AlertsConfig:
    poll_interval_seconds: int = 60
    min_whale_trade_usd: float = 500.0
    dedupe_window_seconds: int = 300
    min_price: float = 0.05
    max_price: float = 0.95
    # A fill older than this when first seen (API lag, or the backlog read on
    # start-up) is stored as SKIPPED "stale" instead of alerting.
    max_alert_age_seconds: int = 600


@dataclass
class PaperConfig:
    stake_usd: float = 10.0
    reaction_polls: int = 1
    max_slippage: float = 0.03
    strategy: str = "hold_to_resolution"
    settle_interval_seconds: int = 600


@dataclass
class StatsConfig:
    min_settled_for_conclusions: int = 30
    bootstrap_samples: int = 10_000
    daily_summary_hour_utc: int = 13


@dataclass
class RescoreConfig:
    enabled: bool = True
    hour_utc: int = 0
    minute_utc: int = 20


@dataclass
class ApiConfig:
    # https://docs.polymarket.com/api-reference/rate-limits.md (verified 2026-11-07)
    data_api: str = "https://data-api.polymarket.com"
    gamma_api: str = "https://gamma-api.polymarket.com"
    clob_api: str = "https://clob.polymarket.com"
    # Kalshi public market data (no key): https://docs.kalshi.com/openapi.yaml servers
    kalshi_api: str = "https://api.elections.kalshi.com/trade-api/v2"
    user_agent: str = "whale-watcher/0.1 (read-only research)"
    timeout_seconds: float = 20.0
    max_concurrency: int = 16
    # Requests per second we allow ourselves: about 80% of each published limit,
    # per host and per endpoint (a key with a path applies to that prefix only).
    # Published: Data API v2 800 req/10s overall, 200/10s on /v2/activity,
    # /v2/positions and /v2/prices-history, 300/10s on /v2/trades; Gamma /markets
    # 300/10s; CLOB /book 1500/10s.
    rate_limits: dict[str, float] = field(
        default_factory=lambda: {
            "data-api.polymarket.com": 60.0,
            "data-api.polymarket.com/v2/activity": 16.0,
            "data-api.polymarket.com/v2/positions": 16.0,
            "data-api.polymarket.com/v2/prices-history": 16.0,
            "data-api.polymarket.com/v2/trades": 24.0,
            "gamma-api.polymarket.com": 24.0,
            "clob.polymarket.com": 40.0,
            # Kalshi meters requests in tokens (Basic tier: 200/s at 10 per request,
            # https://docs.kalshi.com/getting_started/rate_limits.md). Unauthenticated
            # reads answered 429 on bursts in testing, so stay well below that.
            "api.elections.kalshi.com": 3.0,
        }
    )


@dataclass
class Config:
    database_path: str = "data/whalewatcher.sqlite3"
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    candidates: CandidateConfig = field(default_factory=CandidateConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    stats: StatsConfig = field(default_factory=StatsConfig)
    rescore: RescoreConfig = field(default_factory=RescoreConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    # Secrets, from the environment only.
    discord_webhook_url: str | None = field(default=None, repr=False)
    discord_stats_webhook_url: str | None = field(default=None, repr=False)

    def public_dict(self) -> dict[str, Any]:
        """Everything except secrets, for storing alongside a score run."""
        data = dataclasses.asdict(self)
        data.pop("discord_webhook_url", None)
        data.pop("discord_stats_webhook_url", None)
        return data


class ConfigError(ValueError):
    pass


_SECTIONS = {
    "scoring": ScoringConfig,
    "candidates": CandidateConfig,
    "alerts": AlertsConfig,
    "paper": PaperConfig,
    "stats": StatsConfig,
    "rescore": RescoreConfig,
    "api": ApiConfig,
}


def _build(cls: type, raw: dict[str, Any], where: str) -> Any:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    known = {f.name: f for f in dataclasses.fields(cls)}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")
    values = {}
    for name, value in raw.items():
        default = getattr(cls(), name)
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ConfigError(f"{where}.{name} must be true or false")
        elif isinstance(default, int) and not isinstance(default, bool):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ConfigError(f"{where}.{name} must be an integer")
        elif isinstance(default, float):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ConfigError(f"{where}.{name} must be a number")
            value = float(value)
        values[name] = value
    return cls(**values)


def validate(cfg: Config) -> None:
    s = cfg.scoring
    if s.rank_by not in RANK_KEYS:
        raise ConfigError(f"scoring.rank_by must be one of {', '.join(RANK_KEYS)}")
    if s.lookback_months < 1:
        raise ConfigError("scoring.lookback_months must be at least 1")
    if s.min_profitable_months > s.lookback_months + 1:
        raise ConfigError("scoring.min_profitable_months cannot exceed the months in the lookback")
    a = cfg.alerts
    if not 0 <= a.min_price < a.max_price <= 1:
        raise ConfigError("alerts.min_price must be below alerts.max_price, both within 0..1")
    if a.poll_interval_seconds < 5:
        raise ConfigError("alerts.poll_interval_seconds must be at least 5")
    p = cfg.paper
    if p.stake_usd <= 0:
        raise ConfigError("paper.stake_usd must be positive")
    if p.reaction_polls < 1:
        raise ConfigError("paper.reaction_polls must be at least 1")
    if p.strategy != "hold_to_resolution":
        raise ConfigError("paper.strategy: only hold_to_resolution is implemented (mirror_exits is Phase 3b)")


def load(path: str | os.PathLike | None = None, *, env: dict[str, str] | None = None) -> Config:
    """Read the YAML file (if any) and the secrets from the environment."""
    env = dict(os.environ) if env is None else env
    path = path or env.get("WHALEWATCHER_CONFIG") or "config.yaml"
    raw: dict[str, Any] = {}
    if Path(path).exists():
        loaded = yaml.safe_load(Path(path).read_text()) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} must contain a mapping at the top level")
        raw = loaded

    unknown = sorted(set(raw) - set(_SECTIONS) - {"database_path"})
    if unknown:
        raise ConfigError(f"unknown top-level key(s) in {path}: {', '.join(unknown)}")

    cfg = Config()
    for name, cls in _SECTIONS.items():
        if name in raw:
            setattr(cfg, name, _build(cls, raw[name] or {}, name))
    if "database_path" in raw:
        cfg.database_path = str(raw["database_path"])
    if env.get("WHALEWATCHER_DB"):
        cfg.database_path = env["WHALEWATCHER_DB"]

    cfg.discord_webhook_url = (env.get("DISCORD_WEBHOOK_URL") or "").strip() or None
    cfg.discord_stats_webhook_url = (env.get("DISCORD_STATS_WEBHOOK_URL") or "").strip() or None
    validate(cfg)
    return cfg
