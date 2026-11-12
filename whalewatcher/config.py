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
class WatchlistEntry:
    """What a wallet needs to JOIN the watchlist. Stricter than WatchlistExit on every
    shared rule (hysteresis), so a wallet near a line does not flip in and out nightly.

    Defaults come from Phase 0 and the walk-forward search: the concentration rule is
    the only requirement that held out of sample, 30 settled markets guards against the
    thin samples that fake most apparent edge, and a small positive ROI closes the gap
    that let a 1% ROI wallet in while ``scoring.min_roi`` was off."""
    min_roi: float = 0.02
    min_settled_markets: int = 30
    min_profitable_months: int = 0        # no out-of-sample value found; 0 = off
    max_single_market_share: float = 0.20
    max_two_sided_ratio: float = 1.0      # 1.0 = off; the market-maker screen below does this job
    max_days_since_last_trade: float = 30.0
    # Market-maker / bot screen from /v2/user-stats (lifetime figures).
    max_rebate_share: float = 0.30        # (rebates + rewards) / (rebates + rewards + position profit)
    max_fills_per_market: float = 100.0


@dataclass
class WatchlistExit:
    """What makes a wallet already on the list FAIL a nightly review. It is removed
    only after ``fail_reviews_in_a_row`` consecutive failed reviews."""
    min_roi: float = 0.0
    min_settled_markets: int = 15
    max_single_market_share: float = 0.30
    max_two_sided_ratio: float = 1.0
    max_days_since_last_trade: float = 45.0
    max_rebate_share: float = 0.50
    max_fills_per_market: float = 200.0
    fail_reviews_in_a_row: int = 3
    # Our own paper record of the wallet: once it has this many settled live events
    # with a closing price, a mean CLV below live_max_clv is a failed review.
    live_min_events: int = 15
    live_max_clv: float = 0.0


@dataclass
class WatchlistConfig:
    entry: WatchlistEntry = field(default_factory=WatchlistEntry)
    exit: WatchlistExit = field(default_factory=WatchlistExit)
    max_size: int = 40
    probation_days: int = 14              # new members are tracked, and flagged in alerts and stats


@dataclass
class StrategyConfig:
    """One paper account: its own bankroll and its own limits."""
    enabled: bool = True
    starting_bankroll: float = 1000.0
    stake_usd: float = 10.0
    max_stake_usd: float = 10.0
    max_market_exposure_usd: float = 30.0   # open cost in one market
    max_event_exposure_usd: float = 50.0    # ...and in one event: wallets pile into the same game
    daily_loss_limit_usd: float = 100.0     # realised loss in a UTC day that pauses new entries


def _scaled() -> StrategyConfig:
    return StrategyConfig(max_stake_usd=50.0, max_market_exposure_usd=100.0, max_event_exposure_usd=150.0,
                          daily_loss_limit_usd=200.0)


@dataclass
class BotConfig:
    """v2 Phase 3: a bot that acts the moment a whale fill is detected."""
    feed_poll_seconds: float = 1.0          # global trade feed: 1 req/s is ~3% of its 300 per 10 s limit
    runner_poll_seconds: float = 0.5        # how often a runner looks for newly published fills
    max_signal_age_seconds: float = 30.0    # older than this when we act: not "seconds after the whale"
    max_slippage: float = 0.03              # limit price = the whale's price + this (do not chase further)
    human_delay_seconds: int = 60           # the old rule, kept as a comparison column
    mirror_exit_fraction: float = 0.5       # flat_mirror sells when the whale has sold this share of its position
    scale_whale_usd: float = 500.0          # scaled_hold: stake = stake_usd x whale_usd / this, capped
    placebo_min_delay_seconds: int = 300    # placebo_late enters the same outcome at a random later time
    placebo_max_delay_seconds: int = 1800
    markout_seconds: int = 300              # price this long after our fill, for CLV on every fill
    flat_hold: StrategyConfig = field(default_factory=StrategyConfig)
    flat_mirror: StrategyConfig = field(default_factory=StrategyConfig)
    scaled_hold: StrategyConfig = field(default_factory=_scaled)
    placebo_late: StrategyConfig = field(default_factory=StrategyConfig)


@dataclass
class ConsensusPoolConfig:
    """Who counts as a "top trader" for the consensus strategy (v2 Phase 3b). Separate
    from the watchlist: this pool is broad and is judged as a crowd, not wallet by wallet."""
    min_profit_30d_usd: float = 100_000.0
    top_n: int = 0                      # > 0: instead keep the top N by 30-day profit
    min_trades_per_day: float = 5.0     # transactions a day, averaged over the 30 days
    max_trades_per_day: float = 200.0   # above this it is probably a market maker or a bot
    max_two_sided_ratio: float = 0.40   # share of its markets where it bought both sides
    # Two wallets are treated as one entity when either of these holds over the last 30 days.
    entity_min_jaccard: float = 0.60    # overlap of the sets of markets they bought (at least 5 each)
    entity_min_cotrades: int = 10       # ...or this many buys of the same outcome within 10 s of each other
    entity_min_cotrade_share: float = 0.30   # ...making up this share of the less active wallet's buys


@dataclass
class ConsensusConfig:
    """When the pool "agrees" (v2 Phase 3b)."""
    min_agreeing_entities: int = 3
    min_position_usd: float = 1000.0    # an entity's net position in the outcome, at cost
    max_opposing_entities: int = 0      # pool entities holding another outcome of the same market
    window_hours: float = 48.0          # agreeing entries older than this are not fresh agreement
    backtest_delay_seconds: int = 5     # the bot acts this long after the completing fill
    ask_premium: float = 0.01           # added to the historical minute price for the spread


def _fee_table() -> list[dict[str, Any]]:
    # https://docs.polymarket.com/trading/fees.md, table read 2026-11-10. `from` is the
    # day a table took effect; add a new entry when Polymarket changes its fees and
    # every trade is costed with the table in force at its own time.
    return [{"from": "2026-03-31", "rates": {
        "crypto": 0.07, "sports": 0.05, "politics": 0.04, "finance": 0.04, "economics": 0.05, "culture": 0.05,
        "weather": 0.05, "other": 0.05, "mentions": 0.04, "tech": 0.04, "geopolitics": 0.0, "combos": 0.05}}]


@dataclass
class FeesConfig:
    """Taker fee = shares x rate x price x (1 - price), on every buy and every sell.
    No rebate is ever counted.

    ``rate_source`` says where the rate comes from:
      market  the market's own published rate (feeSchedule.rate). What the venue charges.
      table   the category table below.
      higher  whichever of the two is higher (the pessimistic choice).
    The table is always the fallback for a fee-charging market that publishes no rate."""
    rate_source: str = "market"
    onramp_cost_pct: float = 1.5        # one-time cost of getting money in, used by the wallet simulation only
    schedules: list = field(default_factory=_fee_table)


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
    watchlist: WatchlistConfig = field(default_factory=WatchlistConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    consensus_pool: ConsensusPoolConfig = field(default_factory=ConsensusPoolConfig)
    consensus: ConsensusConfig = field(default_factory=ConsensusConfig)
    fees: FeesConfig = field(default_factory=FeesConfig)
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
    "watchlist": WatchlistConfig,
    "bot": BotConfig,
    "consensus_pool": ConsensusPoolConfig,
    "consensus": ConsensusConfig,
    "fees": FeesConfig,
    "api": ApiConfig,
}


def _build(cls: type, raw: dict[str, Any], where: str, base: Any = None) -> Any:
    """``base`` is the instance whose values apply to keys the file leaves out
    (a nested section can have defaults that differ from its class's)."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    base = cls() if base is None else base
    known = {f.name: f for f in dataclasses.fields(cls)}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")
    values = {}
    for name, value in raw.items():
        default = getattr(base, name)
        if dataclasses.is_dataclass(default):
            values[name] = _build(type(default), value or {}, f"{where}.{name}", default)
            continue
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
    return dataclasses.replace(base, **values)


BOT_STRATEGIES = ("flat_hold", "flat_mirror", "scaled_hold", "placebo_late")


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
    w = cfg.watchlist
    if w.max_size < 1:
        raise ConfigError("watchlist.max_size must be at least 1")
    if w.probation_days < 0:
        raise ConfigError("watchlist.probation_days cannot be negative")
    if w.exit.fail_reviews_in_a_row < 1:
        raise ConfigError("watchlist.exit.fail_reviews_in_a_row must be at least 1")
    # Hysteresis only works if entry is at least as strict as exit on every shared rule.
    for name, entry_stricter in (("min_roi", w.entry.min_roi >= w.exit.min_roi),
                                 ("min_settled_markets", w.entry.min_settled_markets >= w.exit.min_settled_markets),
                                 ("max_single_market_share",
                                  w.entry.max_single_market_share <= w.exit.max_single_market_share),
                                 ("max_two_sided_ratio", w.entry.max_two_sided_ratio <= w.exit.max_two_sided_ratio),
                                 ("max_days_since_last_trade",
                                  w.entry.max_days_since_last_trade <= w.exit.max_days_since_last_trade),
                                 ("max_rebate_share", w.entry.max_rebate_share <= w.exit.max_rebate_share),
                                 ("max_fills_per_market",
                                  w.entry.max_fills_per_market <= w.exit.max_fills_per_market)):
        if not entry_stricter:
            raise ConfigError(f"watchlist.entry.{name} must be at least as strict as watchlist.exit.{name}")
    fe = cfg.fees
    if fe.rate_source not in ("market", "table", "higher"):
        raise ConfigError("fees.rate_source must be market, table or higher")
    if not 0 <= fe.onramp_cost_pct < 50:
        raise ConfigError("fees.onramp_cost_pct must be a percentage between 0 and 50")
    if not isinstance(fe.schedules, list) or not fe.schedules:
        raise ConfigError("fees.schedules needs at least one table")
    for entry in fe.schedules:
        rates = entry.get("rates") if isinstance(entry, dict) else None
        if not isinstance(rates, dict) or "from" not in entry:
            raise ConfigError("each fees.schedules entry needs `from` (YYYY-MM-DD) and `rates`")
        for cat, rate in rates.items():
            if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not 0 <= rate <= 0.5:
                raise ConfigError(f"fees.schedules rate for {cat} must be a number between 0 and 0.5")
    c = cfg.consensus
    if c.min_agreeing_entities < 2:
        raise ConfigError("consensus.min_agreeing_entities must be at least 2 (one wallet is not a consensus)")
    if c.window_hours <= 0 or c.min_position_usd <= 0:
        raise ConfigError("consensus.window_hours and consensus.min_position_usd must be positive")
    if cfg.consensus_pool.min_trades_per_day > cfg.consensus_pool.max_trades_per_day:
        raise ConfigError("consensus_pool.min_trades_per_day cannot exceed max_trades_per_day")
    b = cfg.bot
    if b.feed_poll_seconds < 0.5:
        raise ConfigError("bot.feed_poll_seconds must be at least 0.5 (the feed's rate limit is shared)")
    if b.runner_poll_seconds <= 0:
        raise ConfigError("bot.runner_poll_seconds must be positive")
    if not 0 < b.mirror_exit_fraction <= 1:
        raise ConfigError("bot.mirror_exit_fraction must be within 0..1")
    if b.placebo_min_delay_seconds > b.placebo_max_delay_seconds:
        raise ConfigError("bot.placebo_min_delay_seconds cannot exceed bot.placebo_max_delay_seconds")
    for name in BOT_STRATEGIES:
        st = getattr(b, name)
        if st.stake_usd <= 0 or st.starting_bankroll <= 0:
            raise ConfigError(f"bot.{name}: stake_usd and starting_bankroll must be positive")
        if st.max_stake_usd < st.stake_usd:
            raise ConfigError(f"bot.{name}.max_stake_usd cannot be below stake_usd")
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
