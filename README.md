# Whale Watcher

Finds Polymarket wallets that look consistently profitable, alerts in Discord
when one of them opens or adds to a position, and **paper trades every alert at
the price available one poll later**, after fees, to answer one question: would
copying them make money after a realistic reaction delay?

Research tool. It **never places orders**, never asks for or stores wallet keys,
seed phrases or trading credentials, and publishes no ports. It covers whale
trades in every market category. Kalshi matching is deferred (see below).

## Quick start (home server)

```bash
cp .env.example .env              # add DISCORD_WEBHOOK_URL (and optionally DISCORD_STATS_WEBHOOK_URL)
docker compose up -d --build
docker compose logs -f
```

On first start it scores wallets (10 to 30 minutes), monitors the top 20, and
then polls every 60 seconds. Data lives in `./data/whalewatcher.sqlite3`.

## Commands

```bash
python -m whalewatcher score --as-of 2026-11-01      # Phase 1 table + stored snapshot
python -m whalewatcher validate --as-of 2026-11-01   # select before the cutoff, measure after it
python -m whalewatcher monitor [--no-post] [--cycles N]
python -m whalewatcher settle
python -m whalewatcher stats [--post]
python -m whalewatcher export --csv exports/paper_trades.csv
python -m whalewatcher health                        # Docker healthcheck
```

In Docker: `docker compose exec whale-watcher python -m whalewatcher stats`.

Tests (every network call mocked): `pip install -r requirements-dev.txt && pytest`.

## How it works

**Phase 1, scoring** (`scoring.py`). Candidates come from leaderboards (week,
month, all) plus large recent trades. For a past `as_of`, they come from large
trades placed before `as_of` instead, because today's leaderboard already
contains the period being evaluated. Each wallet's activity is rebuilt into
per-market ledgers (`ledger.py`). A market counts only if it resolved inside
`[as_of - lookback, as_of)`, and P&L is computed at the venue's actual payout.
Filters and ranking follow `config.yaml`. Every run is stored as a snapshot with
its `as_of`.

**Validation** (`validate.py`). Select at `as_of - holdout_months` with only
earlier data, then measure each candidate on positions it **opened** in the
holdout. Reports selected vs passed-but-unselected vs all candidates, with
bootstrap CIs and rank correlation.

**Phase 2, alerts** (`monitor.py`). Polls each monitored wallet's trades. Buys
of one outcome token within `dedupe_window_seconds` form one group. The alert
posts the moment the group crosses `min_whale_trade_usd`, and later fills edit
that same message. Skips (price outside 0.05 to 0.95, closed market, stale data)
are stored with the reason. Every alert stores whale trade time, detection time
and post time.

**Phase 3, paper trading** (`paper.py`). At alert time it records the best ask.
`reaction_polls` later it walks the live ask book for `stake_usd`, adds the
market's taker fee, and marks the trade MISSED if the market closed, the book
was too thin, or the fill was more than `max_slippage` above the alert ask.
Shadow fills at the whale's price and at the alert-time ask show what latency
costs.

**Phase 5, stats** (`stats.py`). Covers counts, P&L, ROI, win rate, mean ROI
per trade with a 95% bootstrap CI, CLV of the paper fills, delays, latency
cost, drawdown and the shadow comparison. Breakdowns by wallet, category,
venue and entry price. Under 30 settled trades the output leads with a warning,
and no profit number appears without its n and CI.

**Phase 6, walk-forward** (`service.py`). It rescores nightly with data up to
that day, rotates the monitored set, and links every alert to the snapshot
that was current when it fired.

## Where this deviates from the spec, and why

1. **Data API v1 is gone** (retired 2026-11-02). Everything uses v2. See
   `docs/API_NOTES.md` for every endpoint, the doc URL, and the gotchas found
   by testing.
2. **Polymarket charges taker fees now**, set per market:
   `fee = shares x rate x p x (1-p)`. Rates differ from the published
   category table, so the market's own `feeSchedule` is used.
3. **Voided markets do not refund.** Polymarket resolves cancelled or drawn
   sports markets "50-50", so each share pays $0.50. Settlement uses the
   venue's actual payout. A `REFUND` result exists for venues that refund.
4. **The spec's CLV only works for sports.** Outside sports, markets close
   after the outcome is known, so the "last price before close" is the outcome.
   On 38 resolved non-sports markets the median gap between that price and the
   final payout was 0.001; on 22 sports markets the pre-game price differed
   from the payout by 0.455. So for non-sports markets `clv` mostly repeats
   realized return. The spec's `clv` is kept as the default `rank_by`, and
   `clv_markout` (price 24h after the first buy, minus that buy's price) is
   computed alongside it. Use `validate --rank-by` to see which one predicts.
5. **Candidate pool leakage.** Using today's leaderboard to choose candidates
   for a past `as_of` is selection on the evaluated period. A past `as_of` uses a
   pool built only from trades before it (`--live-pool` overrides, with a warning).
6. **Bots and market makers.** About a quarter of large traders run tens of
   thousands of fills a month. Wallets estimated above `max_activity_rows` are
   skipped after one page. Neg-risk conversions cannot be attributed from the
   API rows, so markets that contain them are excluded, and so is any wallet
   with too many such markets (`max_unreliable_market_share`).
7. **Grouping without latency.** Alerts post when a group crosses the threshold
   instead of waiting out the window, and later fills edit the message.
8. **Skipped alerts are not paper traded.** "Every alert produces one paper
   trade" applies to alerts actually sent. With no webhook configured, alerts
   are stored as UNSENT and are still paper traded (useful for dry runs).
9. **Wallet P&L can be off by taker fees.** Activity rows do not say maker or
   taker. The error is at most about 1.75% of notional. Paper trades compute
   fees exactly.
10. **Kalshi is deferred.** The verified Kalshi notes, including real UFC
    name-matching edge cases, are in `docs/API_NOTES.md` for when it returns.

## First live results (2026-11-07)

These are single runs on live data, kept here so later runs have a baseline.
Each is one sample, not a conclusion.

* **Scoring, `--as-of 2026-11-01`, spec filters.** 1 of the 150 largest
  historical traders passes. 87 (58%) exceed the activity cap as bots or market
  makers. The rest fail mostly on `max_single_market_share` and
  `profitable_months`. Example: the #2 wallet on the monthly leaderboard made
  $5.3M on 22 markets, profitable in 1 month of 6.
* **Nightly rescore with today's leaderboards, spec filters.** 0 wallets pass,
  so the service monitors nothing until the filters change.
* **Validation, cutoff 2026-09-01, holdout to 2026-11-01.** With spec filters,
  no wallet passes at the cutoff. With relaxed filters (20 settled, 3
  profitable months, 0.5 max share, ROI >= 0), 16 wallets are selected. Their
  holdout ROI is -0.4% overall, with a mean per position of +5.1% and a 95% CI
  of [-0.3%, +56.5%]. The CI resamples wallets, and one wallet holds 69% of the
  positions. Over the 28 wallets with at least 5 holdout positions:
  * selection ROI vs holdout ROI: Spearman +0.04
  * spec CLV vs holdout ROI: +0.15
  * 24h markout vs holdout ROI: **+0.48 (n=27)**

  The markout looked best on that one holdout, after three metrics were tried.
* **Replication, cutoff 2026-08-01, holdout to 2026-10-01.** The markout result
  flips. The 8 selected wallets lost 15.5% overall in the holdout, and all
  scored candidates lost 4.9%. Across both holdouts, none of ROI, the spec's CLV
  or the 24h markout reliably picked the large traders who kept winning.

  Spearman correlation between the selection-period metric and holdout ROI:

  | cutoff | past ROI | spec CLV | 24h markout |
  |---|---|---|---|
  | 2026-09-01 | +0.04 (n=28) | +0.15 (n=28) | +0.48 (n=27) |
  | 2026-08-01 | +0.09 (n=34) | -0.25 (n=34) | -0.26 (n=30) |
* **Alert pipeline, 25 actively trading wallets (hand-picked to exercise the
  plumbing, not selected by the strategy).** Detection is about 28 s from the
  triggering fill to the alert, and the paper fill lands about 83 s after that
  fill. Duplicates: none (no fill in two alerts, one paper trade per alert).
  18% of paper trades were missed on slippage. Between the alert-time ask and
  the fill a minute later, the median move was 0. The 90th percentile was 7
  cents against the copier. 97% of alerts were live sports.

## Known limits

* Detection is polling (60 s) plus the Data API's indexing lag, which
  `/v2/status` reports and alerts flag when it exceeds 2 minutes.
* Positions opened more than `entry_lookback_days` before the lookback window
  and held to resolution without any later activity are invisible to scoring.
* Phase 3b (`mirror_exits`) is not implemented. SELL fills are already stored
  for it.
* Discord's webhook endpoints could not be re-read from the build sandbox. They
  follow the published API, and the tests mock them.

*Research and education only. Not financial advice.*
