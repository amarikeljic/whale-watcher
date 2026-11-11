# Whale Watcher

Finds Polymarket wallets that look consistently profitable, alerts in Discord
when one of them opens or adds to a position, and **paper trades every alert at
the price available one poll later**, after fees, to answer one question: would
copying them make money after a realistic reaction delay?

Research tool. It **never places orders**, never asks for or stores wallet keys,
seed phrases or trading credentials, and publishes no ports. It covers whale
trades in every market category. Kalshi is used only to test copying the same
alerts there (`backtest --kalshi`), with public market data and no key.

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
python -m whalewatcher backtest --kalshi             # ...and the same alerts copied on Kalshi, where it lists the game
python -m whalewatcher fresh-wallets                # test copying large buys by brand-new wallets (non-sports)
python -m whalewatcher audit                        # hand-check settled paper trades and their shadow fills
python -m whalewatcher watchlist                    # who is being watched: metrics, days on the list, status
python -m whalewatcher watchlist --last             # what the latest nightly review added, kept, removed, and why
python -m whalewatcher watchlist --history          # every add and removal ever (never deleted)
python -m whalewatcher watchlist --review           # run the nightly review now
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
cost, drawdown and the shadow comparison. Latency cost is measured from the
whale's price when the paper trade opened (the price the zero-latency shadow
uses) in two parts: whale to alert-time ask over every alert, missed ones
included, and alert-time ask to fill over filled trades. CLV leaves out fills
made after a game started, because a sports market's closing price is its last
pre-game price, which is older than such a fill. The CI resamples wallets and events
together, because one wallet's repeated buys, or several wallets on one event,
are not independent results. Breakdowns by month, wallet, category, venue and
entry price. Under 30 settled live trades the output leads with a warning,
each strategy below 30 carries its own, and no profit number appears without
its n and CI.

**Backtest** (`backtest.py`). Replays past periods as simulated paper trades,
written as strategy `backtest:*`. `stats` shows them in their own section,
labelled SIMULATED, never mixed into the live numbers. See "Would copying have
made money?" below.

**The watchlist** (`watchlist.py`, v2). The monitored set is a list that
grows, not a nightly top 20. Every night the service scores candidates with
data up to that day, then:

1. **adds** wallets that meet `watchlist.entry`, best ranked first, while there
   is room (`max_size`). Nobody is pushed out to make room.
2. **re-scores every member** against the looser `watchlist.exit`. A wallet is
   **removed only after failing 3 reviews in a row**. One passing review resets
   the count. A wallet whose data could not be read that night is not judged.
3. records every decision with its reason (`watchlist_events`).

Entry is stricter than exit (hysteresis), so a wallet near a line does not flip
in and out. Two checks apply at the door as well as at review:

* **Market makers and bots** (`/v2/user-stats`): a wallet whose lifetime income
  is more than 30% maker rebates and liquidity rewards, or with more than 100
  fills per market, is turned away. A check that could not be read is never
  treated as a pass.
* **Our own record of the wallet**: once our paper trades on its alerts cover 15
  settled events, a negative mean closing-line value is a failed review, and
  keeps a removed wallet from walking straight back in.

Every stay on the list is one row of `watchlist_membership`. A wallet that
comes back gets a new row, and the database refuses to delete history. New
members are on **probation** for 14 days: alerts say so, and `stats` shows
probation, established and pre-watchlist trades as separate rows. Each alert
stores the membership row, the probation flag and the score snapshot that were
current when it fired, so nothing is judged with knowledge that came later.

The entry values in `config.yaml` come from the last real scoring run (400
candidates): 246 are dropped as too active to be anything but bots, 145 of the
rest fail the concentration rule, and raising the sample floor from 10 to 30
settled markets costs no wallet at all. The small ROI floor closes the gap that
let a 1% ROI wallet through while `scoring.min_roi` was off.

**Upgrading an existing database.** The first start on the new code takes a
full backup to `data/backups/<name>.<UTC time>.pre-v2` before changing anything
(SQLite's online backup, safe while the file is in use), then adds the new
tables and two columns to `alerts`. Existing rows are untouched. The wallets v1
was monitoring are carried over as members. If the disk has no room for the
backup, it stops and changes nothing.

**Protocol V2** (`protocol.py`). Sports markets move to Polymarket's Protocol
V2 on 2026-12-01, where outcome tokens are position ids. The decoder for those
ids is in place and tested against the layout in Polymarket's own SDK. It is
not yet checked against a live V2 market (none exists before 2026-11-23) and
nothing depends on it yet.

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
10. **Kalshi is a backtest venue only.** Live alerts and paper trades are
    Polymarket's. `backtest --kalshi` matches each copied game-winner alert to
    the same game on Kalshi and replays the copy there (see "Copying on Kalshi
    instead"). Only public market-data endpoints are called, so no Kalshi key
    is needed or stored. The verified notes are in `docs/API_NOTES.md`.

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

## Copying on Kalshi instead (2026-11-07)

**Also no.** If Kalshi's prices lagged Polymarket's, a copy placed on Kalshi
could keep the edge that Polymarket prices in within a minute. They do not lag
enough: even an instant copy at Kalshi's own ask barely clears Kalshi's fee.

`backtest --kalshi` takes every copy from the replay above whose market is a
game winner, finds the same game on Kalshi (league, date, both teams by code or
name), and replays the copy there under the same rule, fixed before the run:
- buy the matching Kalshi contract 90 s after the triggering fill, at Kalshi's
  real minute ask (Kalshi publishes bid and ask per minute, so no spread is
  assumed), plus Kalshi's taker fee, held to Kalshi's settlement;
- skip the copy if the Kalshi market was not trading at alert time or its ask
  was outside 0.05-0.95;
- MISSED if the ask rose more than 3 cents between alert time and the fill.

The matcher was checked against settlement before any price was read: of the
2,764 matched game-winner copies, Kalshi's result agreed with Polymarket's on
every one. Kalshi took 1,593 of the 3,007 spec-rule copies and 1,008 of the
1,813 walk-forward copies. Most of the rest are not game-winner markets.

Same alerts, both venues (mean ROI per settled copy, 95% CI, wallets and events
resampled together):

| Rule | Polymarket copy | Kalshi copy |
|---|---|---|
| Walk-forward | -4.7% [-21.1%, +9.7%], n=681 | -2.1% [-15.6%, +9.4%], n=897 |
| Spec thresholds | -2.1% [-14.9%, +11.1%], n=1,169 | -0.7% [-11.8%, +10.5%], n=1,416 |

Kalshi loses less mostly because it misses fewer copies: its alert-time ask has
already moved, so fewer fills look like slippage. That is buying the winners
after their price moved, the same trade as the uncapped Polymarket row above,
and it earns about nothing.

How much was there on Kalshi at all (mean ROI per copy after Kalshi's fee, 95% CI;
the first two rows are shadow fills over every settled copy, missed ones included):

| Kalshi fill | Spec rule | Walk-forward rule |
|---|---|---|
| Instant: Kalshi's ask in the minute of the whale's trade | +0.5% [-9.5%, +10.8%], n=1,593 | +1.3% [-10.7%, +12.2%], n=1,008 |
| At alert time (33 s) | -0.7% [-10.4%, +9.4%], n=1,593 | -0.1% [-11.5%, +10.2%], n=1,008 |
| 90-150 s (this service) | -0.7% [-11.7%, +10.9%], n=1,416 | -2.1% [-15.6%, +9.4%], n=897 |

Kalshi's taker fee, 0.07 x P x (1 - P) a contract (1.75 cents, 3.5% of the
price, at 50 cents), is 1.4 to 2.3 times Polymarket's on these markets (rates
0.05 and 0.03, 11% fee-free). What is left of the edge after an instant copy does
not cover it.

## Is the wallet selection too strict? (2026-11-07)

No. Copying every wallet in the candidate pool, with no requirements at all and
the same 90 s delay (a seeded sample of 1,500 alerts per period, wallets and
events resampled together in the CIs):

| Period | At the whale's price | Copied 90 s later |
|---|---|---|
| May-Jul | +1.9% [-9.2%, +13.9%], n=1,478 | +3.7% [-8.6%, +16.9%], n=1,352 |
| Jul-Sep | -5.6% [-15.4%, +5.2%], n=1,481 | -7.2% [-17.8%, +3.7%], n=1,323 |
| Sep-Nov | -2.4% [-10.8%, +6.0%], n=1,461 | -4.0% [-13.0%, +5.5%], n=1,258 |
| All | -2.0% [-8.0%, +4.2%], n=4,420 | -2.4% [-8.3%, +4.2%], n=3,933 |

Large traders as a group have no edge to copy. The selected wallets do have one
at their own price (+3.4% to +4.8%), but it is the kind that is gone within a
minute: the delay costs the selected wallets 6 to 11 points and the unselected
pool less than half a point. Selecting on past profit picks the fastest in-play
traders, and speed is the one thing a copier cannot copy.

Where the delay cost sits (every distinct copied alert from the backtest, by
time from the alert to the market's resolution):

| Resolves | Alerts | Sports | At the whale's price | Copied 90 s later | Slippage misses |
|---|---|---|---|---|---|
| Within 3 h | 1,834 | 96% | +4.3% [-6.3%, +15.5%] | -8.7% [-19.7%, +1.9%] | 36% |
| 3 to 24 h | 1,215 | 91% | +0.3% [-12.1%, +14.3%] | -1.1% [-14.4%, +13.5%] | 12% |
| 1 to 7 days | 518 | 22% | +16.0% [-2.7%, +32.9%] | +15.9% [-0.6%, +34.7%] | 6% |
| Over 7 days | 397 | 4% | -15.9% [-53.7%, +8.2%] | -17.6% [-53.1%, +5.9%] | 5% |

The returns per bucket are noise: the buckets were drawn after seeing the data,
and +16% sits next to -16%. The delay cost is not noise: all of it is in markets
that resolve within hours, nearly all in-play sports. Where a market resolves
days later, a copy gets about what the whale got.

## Fresh wallets: copying likely insiders (2026-11-07)

**Also no.** The tracker can never pick a brand-new wallet, and new wallets betting
big in non-sports markets are the pattern public insider trackers watch. Their
information could last hours or days, long enough to survive a delay. So
`python -m whalewatcher fresh-wallets` tests that pattern, under a rule fixed
before any result was seen (`whalewatcher/fresh.py`):
- markets: every non-sports market that resolved between 2026-05-07 and
  2026-11-07 among Gamma's 2,000 highest-volume closed markets per week of end
  date, plus closed markets with later end dates (they resolved early); chosen by
  volume and dates, never by outcome;
- signal: a buy group of $1,000+ within 5 minutes by a wallet whose first
  Polymarket trade was at most 14 days earlier;
- copy: 90 s later at the CLOB minute price plus a cent and the fee, held to
  resolution;
- verdict: counts only if the 95% CI is above zero.

| | Mean ROI per copy, 95% CI | n |
|---|---|---|
| Copied 90 s later (the test) | **-3.1% [-13.3%, +7.7%]** | 13,432 copies, 4,191 wallets, 2,835 events |
| At the fresh wallet's own price | +0.2% [-7.4%, +8.3%] | 17,182 |

It fails. 15,952 markets, 1.45 million fills of $500+, and 41,226 first-trade
lookups found 17,800 wallets that were fresh at a large buy and 38,702 signals
(17,182 inside the 0.05-0.95 price band). As a group, new wallets have no edge
even at their own price. Whatever insiders are among them are diluted by
ordinary new accounts and bots. No category's interval is above zero (politics
-3.5%, crypto -4.3%, geopolitics -5.0%, all with n > 3,000). The few positive
categories (tech, culture, finance, 55 to 264 copies each) have intervals that
span zero widely and are not evidence.

The replay prices a copy at the minute price plus a cent. Right after a $1,000+
buy in a thin market the real ask is often higher, so the true result is likely
worse.

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
