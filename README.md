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

Live paper trades take weeks to settle in numbers worth reading. To see what
the strategy would have made over the last six months now, run the backtest
once (about 30 minutes on an empty database). Its trades appear in `stats`
under SIMULATED, apart from the live ones:

```bash
docker compose exec whale-watcher python -m whalewatcher backtest --dataset data/optimize-folds.json
```

## Commands

```bash
python -m whalewatcher score --as-of 2026-11-01      # Phase 1 table + stored snapshot
python -m whalewatcher validate --as-of 2026-11-01   # select before the cutoff, measure after it
python -m whalewatcher optimize                      # walk-forward search for the requirements worth tracking
python -m whalewatcher backtest                      # simulated paper trades for the past 6 months, shown by `stats`
python -m whalewatcher monitor [--no-post] [--cycles N]
python -m whalewatcher settle
python -m whalewatcher stats [--post]
python -m whalewatcher export --csv exports/paper_trades.csv
python -m whalewatcher health                        # Docker healthcheck
```

In Docker: `docker compose exec whale-watcher python -m whalewatcher stats`.

Run time: a default `validate` (400 candidates) takes about 12 minutes on an
empty database and under a minute to re-run once the cache is warm. Both end
with a table of time and requests per stage. Most of a cold run is price
lookups for CLV; `scoring.clv_sample_positions` trades precision for speed.

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
cost, drawdown and the shadow comparison. The CI resamples wallets and events
together, because one wallet's repeated buys, or several wallets on one event,
are not independent results. Breakdowns by month, wallet, category, venue and
entry price. Under 30 settled live trades the output leads with a warning,
each strategy below 30 carries its own, and no profit number appears without
its n and CI.

**Backtest** (`backtest.py`). Replays past periods as simulated paper trades,
written as strategy `backtest:*`. `stats` shows them in their own section,
labelled SIMULATED, never mixed into the live numbers. See "Would copying have
made money?" below.

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
   realized return. The spec's `clv` is kept as the code default `rank_by`
   (the shipped `config.yaml` ranks by `roi`, see below), and
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
  | 2026-09-07 (400 candidates) | +0.11 (n=91) | -0.07 (n=91) | - |
* **Default `validate` on 2026-11-07 (cutoff 2026-09-07, 400 candidates, spec
  filters).** 3 wallets pass and lose 12.3% overall in the holdout. All 302
  scored candidates have a mean holdout ROI of **-4.8% per position, 95% CI
  [-7.4%, -2.0%]**. Large traders as a group lost money before any copying
  delay, and neither metric picked the exceptions.
* **Alert pipeline, 25 actively trading wallets (hand-picked to exercise the
  plumbing, not selected by the strategy).** Detection is about 28 s from the
  triggering fill to the alert, and the paper fill lands about 83 s after that
  fill. Duplicates: none (no fill in two alerts, one paper trade per alert).
  18% of paper trades were missed on slippage. Between the alert-time ask and
  the fill a minute later, the median move was 0. The 90th percentile was 7
  cents against the copier. 97% of alerts were live sports.

## Which wallets to track: the requirement search (2026-11-07)

`optimize` tried 216,000 combinations of the scoring requirements (plus win
rate and CLV floors), walk-forward. The data came from three back-to-back
two-month holdouts ending 2026-11-07, each with a 400-wallet leak-free pool.
The outcome is what copying would have earned: every alert-worthy buy group,
bought at the whale's price plus the taker fee and held to resolution. A
requirement set is chosen on earlier periods and judged on the next one.

ROI per copied alert, per holdout. Only the "chosen on earlier periods" rows of
`optimize`'s output are honest tests; this table also shows rules on the
periods they were chosen from.

| Rule | May-Jul | Jul-Sep | Sep-Nov |
|---|---|---|---|
| Every eligible wallet | -0.4% (213 wallets) | -7.3% (202) | -3.5% (117) |
| Spec thresholds | +4.6% (6) | -1.0% (1) | +5.9% (9) |
| **Max single-market share <= 0.2** | **+5.9% (22)** | **+6.1% (15)** | **+6.5% (14)** |
| 5+ profitable months and win rate >= 60% | +13.5% (23) | -2.6% (20) | +9.0% (12) |
| Win rate >= 60% | +7.1% (93) | -3.1% (67) | -0.6% (48) |
| Past ROI >= 5% | +0.6% (44) | -7.6% (71) | +4.9% (24) |
| CLV >= 0.02 | +2.8% (30) | -9.5% (23) | +0.3% (10) |

Honest tests, where the rule was chosen without seeing the test period:

* Choose on May-Jul, test Jul-Sep: "5+ profitable months and win rate >= 60%"
  gives -2.6%, 95% CI [-15.3%, +9.4%]. Random wallet sets did as well 14% of
  the time.
* Choose on May-Sep, test Sep-Nov: "max single-market share <= 0.2" gives
  +6.5%, 95% CI [-3.9%, +16.0%]. Random wallet sets did as well 0.9% of the
  time.

What this supports:

* Copying every large trader loses money, in all three periods.
* Filtering on concentration ranks wallets better than chance.
* No rule has yet shown, out of sample, that copying makes money: every
  interval includes zero.
* The automatic final pick flips with the objective (`--objective pooled`
  or `worst`). That is what a weak signal looks like.

The profile below is a judgment call, not a proven optimum. It is the only rule
positive in all three periods and it passed its one honest test. It kept 14-22
wallets per historical period, and 10 wallets on 2026-11-07's live pool (400 of
5,633 candidates). These ROIs are at the whale's own price. With this service's
copy delay the same rule lost money in its honest test (next section):

```yaml
scoring:
  min_settled_markets: 10
  min_profitable_months: 0
  max_single_market_share: 0.20   # no market supplies more than 20% of the profit
  min_avg_stake_usd: 0
  max_days_since_last_trade: 30
  max_two_sided_ratio: 1.0        # off
  min_roi: -1.0                   # off
  rank_by: roi
  top_k: 20
```

## Would copying have made money? The backtest (2026-11-07)

**No.** Replaying the last six months the way this service runs, copying lost
money. The whales' edge was real at their own price, but the market priced it
in within about a minute, before a 60-second poller can buy.

`python -m whalewatcher backtest` uses the same three periods as `optimize`. At
each period's start it picks wallets with earlier data only, ranks them like
scoring and keeps `top_k`. It then copies every alert-worthy buy group those
wallets placed during the period. Each copy fills 90 s after the fill that
triggered the alert (detection plus one poll in the live dry run), at the CLOB
minute price plus one cent, plus the market's taker fee, and is held to
resolution. As in the live engine, a fill more than 3 cents above the alert
price is MISSED. CIs resample wallets and events together.

| Rule | Period | Wallets | Settled copies | At the whale's price | Copy 90 s later, 95% CI |
|---|---|---|---|---|---|
| Walk-forward: 5+ profitable months, win rate >= 60% | Jul-Sep | 16 | 414 | -6.6% | -5.3% [-28.3%, +22.6%] |
| Walk-forward: max single-market share <= 0.2 (shipped profile) | Sep-Nov | 14 | 823 | +6.8% | -8.9% [-20.6%, +3.0%] |
| Spec thresholds | May-Jul | 6 | 1,227 | +3.4% | +2.3% [-11.7%, +13.6%] |
| Spec thresholds | Jul-Sep | 1 | 35 | -1.2% | -12.9% [-47.6%, +28.3%] |
| Spec thresholds | Sep-Nov | 9 | 1,016 | +6.2% | -5.8% [-17.7%, +5.3%] |
| **Walk-forward, all periods** | Jul-Nov | 30 | 1,237 | +3.4% | **-7.7% [-19.6%, +5.1%]** |
| **Spec, all periods** | May-Nov | 16 | 2,278 | +4.8% | **-1.6% [-10.9%, +7.1%]** |

"Walk-forward" is the honest test of this system: each period copies the
wallets that the requirements chosen on earlier periods select. "At the
whale's price" is the zero-latency shadow over every settled alert. It is the
return available to the whale, not to a copier.

Where the edge goes (walk-forward rule; the spec rule splits the same way):

| Copies | Alerts | Win rate | ROI at the whale's price |
|---|---|---|---|
| Filled: the price had not run away | 1,237 | 60% | -9.7% |
| Missed on slippage: the price ran 3+ cents first | 537 | 83% | +33.2% |

The whales' winning trades are the ones the market prices in within a minute or
two. A copier that waits gets the rest. 74-76% of the copies were sports, and
74-87% of those were bought after the game had started, where prices move on
each play.

How fast a copy would have to be (mean ROI per copy and 95% CI, by the time from
the triggering fill to the copy's fill; prices are per minute, so each row is a
range):

| Copy fill | Spec rule | Walk-forward rule |
|---|---|---|
| Within the first minute | +4.3% [-4.3%, +13.4%] | +0.8% [-14.6%, +16.2%] |
| 30-90 s | +0.5% [-7.9%, +9.1%] | -6.4% [-17.0%, +6.0%] |
| 90-150 s (this service) | -1.6% [-10.9%, +7.1%] | -7.7% [-19.6%, +5.1%] |
| About 5 minutes | -1.4% [-11.3%, +7.8%] | -6.8% [-18.7%, +7.5%] |
| 90-150 s, no slippage cap | -0.3% [-7.3%, +7.3%] | -4.7% [-14.2%, +5.7%] |

Even an instant copy shows no profit that clears its interval. Removing the
slippage cap does not help either: buying the winners after they have moved
earns about nothing. The live engine compares its fill with the ask at alert
time, not the whale's price, so it buys some of the moved trades. Its
expected result sits between the capped and uncapped rows.

What the replay cannot capture: real order books (a minute midpoint plus a
cent understates the cost of thin books, so the real result is likely worse),
nightly re-picking (here a period's wallets are fixed), and the true alert-time
ask. The live paper trades measure all three.

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
