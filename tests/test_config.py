import pytest

from whalewatcher.config import ConfigError, load


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_defaults_match_the_spec(tmp_path):
    cfg = load(tmp_path / "missing.yaml", env={})
    s = cfg.scoring
    assert (s.lookback_months, s.min_settled_markets, s.min_profitable_months) == (6, 50, 4)
    assert (s.max_single_market_share, s.min_avg_stake_usd, s.max_days_since_last_trade) == (0.30, 100, 30)
    assert (s.max_two_sided_ratio, s.min_roi, s.top_k, s.rank_by) == (0.40, 0.05, 20, "clv")
    a = cfg.alerts
    assert (a.poll_interval_seconds, a.min_whale_trade_usd, a.dedupe_window_seconds) == (60, 500, 300)
    assert (a.min_price, a.max_price) == (0.05, 0.95)
    p = cfg.paper
    assert (p.stake_usd, p.reaction_polls, p.max_slippage, p.strategy) == (10, 1, 0.03, "hold_to_resolution")


def test_typo_in_a_key_is_an_error_not_a_silent_default(tmp_path):
    with pytest.raises(ConfigError, match="min_settled_market"):
        load(write(tmp_path, "scoring:\n  min_settled_market: 5\n"), env={})


def test_wrong_type_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="integer"):
        load(write(tmp_path, "scoring:\n  top_k: lots\n"), env={})


def test_there_are_no_secrets_and_a_leftover_webhook_variable_is_ignored(tmp_path):
    cfg = load(write(tmp_path, "alerts:\n  min_price: 0.1\n"),
               env={"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/abc"})
    assert cfg.alerts.min_price == 0.1
    assert not hasattr(cfg, "discord_webhook_url") and not hasattr(cfg, "discord_stats_webhook_url")
    assert "abc" not in repr(cfg) and "abc" not in str(cfg.public_dict()), "an old .env cannot leak into anything"


def test_the_shipped_config_file_loads():
    cfg = load("config.yaml", env={})
    assert cfg.scoring.top_k == 20
