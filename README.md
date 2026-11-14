# Whale Watcher

Finds Polymarket wallets that look consistently profitable, records an alert
when one of them opens or adds to a position, and **paper trades every alert the
way a bot would: against the live order book, seconds after the whale**, after
fees, to answer one question: would copying them make money?

Research tool. It **never places orders**, never asks for or stores wallet keys,
seed phrases or trading credentials, and publishes no ports. It **sends nothing
to anyone**: every request it makes is a GET to a public market-data API, it has
no secrets and needs no `.env`. Alerts and notices are rows in the database and
lines in the log, read on the dashboard. It covers whale
trades in every market category. Kalshi is used only to test copying the same
alerts there (`backtest --kalshi`), with public market data and no key.

## Quick start (home server)

```bash
GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
docker compose logs -f
```

`GIT_COMMIT` puts the commit on every row the service writes (see "Which code
made a row" below). Without it the rows say `unknown`, and nothing else changes.

That starts three containers from one image: `whale-watcher-detector` (watches
trades), `whale-watcher` (alerts, the paper bot, the nightly review) and
`whale-watcher-dashboard` (http://127.0.0.1:8787 on the server). On
first start it scores wallets (10 to 30 minutes) and fills the watchlist. Data
lives in `./data/whalewatcher.sqlite3`. `docker compose logs -f detector`
shows a line a minute with what the detector published.

Live paper trades take weeks to settle in numbers worth reading. To see what
the strategy would have made over the last six months now, run the backtest
once (about 30 minutes on an empty database). Its trades appear in `stats`
under SIMULATED, apart from the live ones:

```bash
docker compose exec whale-watcher python -m whalewatcher backtest --dataset data/optimize-folds.json
```

## Deploying the Part 4 batch (2026-11-14)

1. **Check free disk first.** Opening the database upgrades it to schema 11, and
   before that it copies the whole file to `data/backups/`. It needs free space of
   about the database's size, and refuses to upgrade (changing nothing) if there
   is not enough: `du -h data/whalewatcher.sqlite3` against `df -h .`
2. **Deploy:** `GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build`
3. **What the first start does.** On a 6 GB copy of the live data the upgrade took
   70 seconds, backup included, and kept every row. Then:
   * the gap while the service was down is recorded as an outage window, so
     signals from the restart are left out of results;
   * the headline starts again at model 1, from zero. Everything from before is
     under Version → "Before tagging" on the dashboard, or `stats --model before`;
   * the simulated wallet ends its run ("model version changed from before
     tagging to model 1") and starts a new one;
   * the follow-up is filled in for every past alert (seconds), settled trades get
     a resolution type (100 per settle), and old fills get their 1- and 30-minute
     prices a few dozen a minute.
4. The dashboard says "Not ready" until the runner has upgraded the database.
5. **Optional, once:** `python3 scripts/check_block_times.py --db data/whalewatcher.sqlite3`
   on the server (standard library only; it reads the database read-only and asks
   one public Polygon RPC, `polygon.drpc.org`, for receipts and blocks).
6. **Still yours to do:** delete the old `.env` if it is still on the server, and
   delete the two Discord webhooks so their addresses stop working.

`stats` (the full report) takes about a minute and a half on a database this size,
mostly for the breakdowns of the simulated replays. `stats --brief` is instant.

## Commands

```bash
python -m whalewatcher score --as-of 2026-11-01      # Phase 1 table + stored snapshot
python -m whalewatcher validate --as-of 2026-11-01   # select before the cutoff, measure after it
python -m whalewatcher optimize                      # walk-forward search for the requirements worth tracking
python -m whalewatcher backtest                      # simulated paper trades for the past 6 months, shown by `stats`
python -m whalewatcher backtest --kalshi             # ...and the same alerts copied on Kalshi, where it lists the game
python -m whalewatcher fresh-wallets                # test copying large buys by brand-new wallets (non-sports)
python -m whalewatcher audit --costs                # gross P&L, fee, slippage and net side by side; checks every stored fee
python -m whalewatcher audit                        # hand-check settled paper trades and their shadow fills
python -m whalewatcher watchlist                    # who is being watched: metrics, days on the list, status
python -m whalewatcher watchlist --last             # what the latest nightly review added, kept, removed, and why
python -m whalewatcher watchlist --history          # every add and removal ever (never deleted)
python -m whalewatcher watchlist --review           # run the nightly review now
python -m whalewatcher consensus                    # today's consensus pool: counts at $100k/$250k/$500k/top 50, entities
python -m whalewatcher consensus --backtest         # six months of the consensus strategy, no hindsight, with baselines
python -m whalewatcher us match [--days 14]         # how many recent alerts have the same bet on Polymarket US
python -m whalewatcher dashboard                    # the web dashboard on 127.0.0.1:8787
python -m whalewatcher wallet show                  # the simulated small wallet: amount, runs, replay
python -m whalewatcher wallet set 250               # change the amount (starts a new wallet run)
python -m whalewatcher detector                     # watch trades, publish tracked wallets' fills (own container)
python -m whalewatcher monitor [--cycles N] [--no-detector]   # the runner: alerts + the paper bot
python -m whalewatcher settle
python -m whalewatcher stats [--brief]              # the full report, or the daily summary (last 24 hours beside all-time)
python -m whalewatcher stats --model before         # ...for the rows made before model versions (also: all, or a number)
python -m whalewatcher stats --include-outages      # ...counting signals made inside outage windows
python -m whalewatcher version                      # the model version and commit new rows get, and what each version changed
python -m whalewatcher followup                     # what the whale did around each alert: other side, sold back, flags
python -m whalewatcher experiments                  # every variation tracked, its role, n, interval and forward test
python -m whalewatcher export --csv exports/paper_trades.csv
python -m whalewatcher health                        # Docker healthcheck
```

In Docker: `docker compose exec whale-watcher python -m whalewatcher stats`.

Run time: a default `validate` (400 candidates) takes about 12 minutes on an
empty database and under a minute to re-run once the cache is warm. Both end
with a table of time and requests per stage. Most of a cold run is price
lookups for CLV; `scoring.clv_sample_positions` trades precision for speed.

Tests (every network call mocked): `pip install -r requirements-dev.txt && pytest`.

Reference: `docs/data-sources.md` (every host, its limit, our rate, and the venues'
terms quoted), `docs/dependencies.md` (every pinned package and what it is for) and
`docs/worries.md` (the Part 4 report). One-off, by hand on the server:
`python3 scripts/check_block_times.py --db data/whalewatcher.sqlite3` compares the Data
API's trade times with Polygon block times (read-only; it asks one public Polygon RPC
for receipts and blocks, nothing else).

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

**The bot model** (`detector.py`, `bot.py`, v2 Phase 3). Paper only: no order
is ever placed, nothing is signed, no key exists.

*Detection.* One detector process publishes every fill of a tracked wallet
once, into the `fill_events` table, with the moment it was first seen. It has
two routes on one dedupe key:

* the **global trade feed** (`GET /v2/trades`, maker fills included), polled
  once a second. Measured over 600 polls: a fill shows up a median 3.4 s after
  its block. Each request carries a unique parameter, because an identical URL
  is answered from a 300-second cache.
* a **per-wallet backstop** once a minute, which cannot miss a fill and catches
  whatever the feed dropped or happened while the detector was down.

Runners read `fill_events` from their own cursor, so the APIs are asked once
and every runner sees identical timing. Detector and runner are **separate
containers**: one crashing or restarting never stops the other, the detector
keeps publishing while the runner is down, and the runner resumes where it
stopped. The runner logs it, and the Health page shows it, if the detector's
heartbeat goes stale.
The nightly review runs as a child process so the bot never freezes for it.

*The fill.* When a buy group crosses the alert threshold, the bot reads the
order book at once and walks the asks for its stake as a fill-or-kill limit
order at `whale price + max_slippage`. Fees are charged per level. Each signal
leaves one row per strategy in `bot_trades`, whatever happened:

| Status | Meaning |
|---|---|
| FILLED | the book absorbed the stake inside the limit |
| MISSED | the price had already run, the book was too thin, the market had closed, or we acted more than 30 s after the whale |
| SKIPPED | a risk limit refused it; the reason is stored |

*Strategies*, each its own paper account (bankroll, exposure, ledger, P&L):

| Strategy | Rule |
|---|---|
| `flat_hold` | $10, held to resolution. The headline |
| `flat_mirror` | $10, sold into the bids once the whale has sold half of what it held when we entered |
| `scaled_hold` | $10 to $50, growing with the size of the whale's buy |
| `placebo_late` | the control: the same outcome, $10, bought at a random time 5 to 30 minutes later |

The placebo is what tells timing apart from market choice. If copying a whale
within seconds carries information, `flat_hold` has to beat `placebo_late` on
the signals both filled; `stats` prints that paired difference with its
interval. Another study's copy edge more than halved against this kind of
control (docs/v2-research.md).

*Risk limits per strategy:* starting bankroll, stake cap, open cost per market,
open cost per **event** (wallets pile into the same game, and those results move
together), and a daily realised-loss limit that pauses new entries until
00:00 UTC.

*Comparison columns* on the hold strategies, same stake, never in the headline:
`zero_*` is the whale's own price (no latency at all) and `human_*` is the old
rule (a book walk 60 s later). Paper trades made before the bot model stay in
`paper_trades` under their old label and `stats` shows them as their own
section.

*A second chase limit* (`tight_hold`, the owner's question 8). The same signals,
stake and book read as the headline, but it gives up 1 cent above the whale's
price instead of 3. Most signals sit 1 to 2 cents above the whale (22 and about 10
of 57 on the test copy), so 1 cent changes about a third of them, where a looser
6 cents would change 4. `stats` compares the two on the same settled signals,
counting a miss as $0, with the interval of the difference. Its own setting is
`bot.tight_hold.max_slippage`. It is listed as an exploratory variation: it can
only replace the headline by passing the decision rule on later signals.

*CLV.* Pre-game fills are compared with the closing line as before. Every fill,
in-play included, also gets the price **1, 5 and 30 minutes** after it, which is
the only honest reference for a bet placed during a game. `stats` and the Edge
page show each one overall, pre-game, in-play and for the wallets with the most
fills. A price stamped before our fill (a quiet market's last trade) is not
counted. The price series moves in one-minute steps, so the 1-minute figure is
rough.

*Exchange precision* (v2.1 Part 4 #7, `precision.py`). Paper fills use the
amounts the venues accept. On Polymarket shares come in 0.01 steps at every tick
size and the dollar amount is rounded the way the order docs say (up to 4 more
decimals, then down to 3 to 6 depending on the tick). A buy never spends more
than its stake: the shares rounded off come from the worst level. On Polymarket
US contracts come in steps of the market's minimum (0.01, or whole contracts
where the minimum is 1; the docs allow decimals only below a minimum of 1). The
small wallet's stakes are whole cents, and its smallest order is rounded up to
the cent so it always buys the market's minimum. The effect is a cent or less a
trade: $10 at 0.44 buys 22.72 shares for $9.9968 instead of 22.727 for $10.

*Conservative equity* (v2.1 Part 4 #8). Every mark of an open position also keeps
the bids behind it. Beside its equity (each share at the best bid) the small
wallet shows a conservative equity: each open position sold, all of it, down the
recorded bids after the taker fee, with nothing for what the bids cannot absorb.
Polymarket US positions are marked from the US book (`us:<slug>:<side>`), a short
at 1 minus the offer. Before this they borrowed the international market's marks.

*The book a second later* (v2.1 Part 4 #6). After each decision the runner reads
the order book again (`bot.recheck_seconds`, 1 s) and records what the same
order would have done on it: the best ask, the fill price or a miss. `stats` and
the Edge page report how often the price moved and what it would have cost. It
is one extra GET per signal, in the background, and it never changes a
decision: the decision is always made on the first read. How long reading the
market and the book took is stored on each alert too.

**Costs** (`fees.py`, `costs.py`, v2.1 Part 1). Every P&L, ROI and interval in
`stats`, the daily summary and the dashboard is **net**. The paper model has
charged the taker fee from the start (`cost = price x shares + fee`), and a
mirror exit pays it again on the sell. No rebate is ever counted. What v2.1
adds is the breakdown and the audit trail:

* **Fee** = `shares x rate x price x (1 - price)`, charged per order-book level.
  `fees.rate_source` picks the rate: `higher` (the default, chosen by the
  owner: the worse of the next two), `market` (the market's own published rate,
  which is what the venue charges) or `table` (the dated category table in
  `config.yaml`). The two differ: on 2026-11-12, 311 open sports markets
  carried 0.03 beside 479 at 0.05, and 111 markets tagged geopolitics carried
  0.04 where the table says 0. Under `higher` a market the venue marks fee-free
  still pays its category's table rate. The rate each trade was charged and
  where it came from are stored on the fill.
* **Slippage** is what a fill paid beyond the best ask (and lost below the best
  bid on an exit), in dollars, stored per trade.
* **Breakdown:** gross (top of the book, no fees), fees, slippage, net. The
  spread is not a separate line because the bot buys at the ask, so it is
  already inside gross.
* **Existing trades** are recalculated into new columns (`fee_rate`,
  `fee_source`, `fees_recalc`, `slippage_cost`, `gross_pnl`, `net_pnl_recalc`).
  The original `fees`, `cost` and `pnl` (what the trade was charged when it was
  made) are never rewritten. Reports use the recalculated figures, so one fee
  policy applies to every trade however old, and changing `rate_source` re-costs
  them all. `audit --costs` shows both and checks every charged fee against the
  formula.
* **Migration backups** are pruned to the oldest (the data before any migration)
  and the newest two. A backup made by hand is never touched.
* `fees.onramp_cost_pct` (1.5%) is the one-time cost of getting money in. It is
  used by the wallet simulation only, not by per-trade P&L.

On the trades checked so far fees cost about 1.5% of every dollar staked. That
is the hurdle a copy strategy has to clear before anything else.

### The daily summary (v2 Phase 5)

Once a day (`stats.daily_summary_hour_utc`) the runner writes it to the log, and
`stats --brief` prints it any time. The same figures are on the dashboard's
Overview page, one card per runner plus a "Last nightly review" card, laid out to
fit a phone. Per runner it puts **the last 24 hours beside
all-time**: signals filled, missed and skipped, closed trades, net P&L, mean
return per trade with its 95% interval, win rate, and fees and slippage, so no
profit figure appears without its n and interval. Then the decision rule, what
the time-to-close cutoff skipped, the small wallet, and **what the latest nightly
review changed**: members now, wallets added and removed with the reason, reviews
failed and wallets turned away. The first column is the last 24 hours up to the
summary, not the calendar day, so every trade lands in exactly one daily summary
whatever hour it is written.

Phase 5's other items: outage windows are recorded automatically (below). The
healthchecks.io heartbeat and the nightly backups were dropped by the owner.

### Which code made a row (v2.1 Part 4)

Every new alert, bot trade, paper trade, wallet run and score run records three
things (`whalewatcher/version.py`):

* **Model version**, a number raised by hand whenever code that decides whether
  to copy, prices a fill, sizes a stake, or settles or marks a trade changes.
  The headline (stats and dashboard) counts **one model version: the current one**.
  A deploy that only changes documents, the dashboard or the reports keeps the
  number, so the sample keeps growing. Counting one commit instead would restart
  the sample at every deploy and never reach the 100 settled events the decision rule needs.
* **Code commit**, passed in when the image is built (`GIT_COMMIT`, above).
* **Settings fingerprint** of the loaded `config.yaml`. `stats` warns when one
  model version's rows were made under more than one.

Rows from before this existed have none of the three. They are labelled
**before tagging**, shown as their own group (`stats` prints them after the
headline, the dashboard has them under Version), and never deleted, relabelled
or added to a headline. On the first deploy of model 1 the headline therefore
starts from zero; everything before it is under "before tagging".

A new model version also ends the simulated wallet's run and starts a new one,
so a wallet never follows two models. The bot's own paper bankroll, which its
risk limits read, spans every version: it is operating state, not a result.

`tests/test_version.py` fails when decision code changes and nobody decided
whether that is a new model version: fixing it means either raising the number
or recording that the change cannot alter a decision. Comments and docstrings
do not count. It is a reminder, not a proof.

The dashboard and `stats` also leave out signals made inside recorded outage
windows unless asked (`Include outage windows`, `stats --include-outages`).

### The decision rule (v2 Phase 5)

Set before the results were in: **"Copying has an edge if mean CLV stays above 0
and the 95% CI for ROI excludes 0 after 100 settled events."** It is judged on
`single / flat_hold` only, in the current model version, outages left out. An
event is one game or question, so several fills in it count once. The Overview
page shows the rule, its three tests (settled events with a progress bar, mean
CLV, the interval) and a plain verdict: not decided yet, an edge, no edge shown,
or copying loses money. `stats` and the daily summary print the same line, and
`experiments` judges every forward test by the same rule. The 100 is
`stats.decision_min_events`. A hair of float noise does not count as above 0.

### Every variation, and what counts as proof (v2.1 Part 4)

Dozens of variations are tracked (strategies, the control, the US venue, the zero
and one-minute comparisons, the old rule, simulated replays, stake sizes, and
every breakdown slice). At 95% intervals, a few would look good by luck alone.
So only one result answers "does copying make money": `single / flat_hold`
against the decision rule above.

`experiments` (also in `stats` and on the Edge page) lists every variation with
its role (primary, control, comparison, exploratory, simulated), start date, n,
mean return with its interval, and status. When any other live variation first
looks good, the runner records the date in `experiment_flags`, a table that
refuses edits and deletes. From then on only its signals after that date count:
it is shown only if those later signals meet the decision rule on their own.
What it did before the flag is how it was found, so it cannot also be the proof.
Simulated replays are listed but never flagged: their data is fixed.

### What the whale did around each alert (v2.1 Part 4)

A whale's buy is not always a bet. In the first 60 live alerts the same wallet
also bought the other side of the market in 29 of them, always within the hour,
and in 5 the other side was at least as large: no real position at all
(`docs/worries.md`, 11). `followup.py` measures this on every alert, from the
fills the detector already stored (no extra requests):

* the other side bought in the **24 hours before** the alert (known when the
  bot acted) and in the **hour after** (known only later),
* buys in **other markets of the same event** (a draw, a spread),
* the share of the position **sold back within 30 minutes** (bait, worry 10).

Each alert is **clean**, **partial** or **hedged** (other side at least half of
the alert). `stats`, `followup` and the Wallets page show how our copies of each
class did, with n and the interval, and flag wallets that **usually buy both
sides** or **often sell back** (5 or more followed-up alerts). Three switches
can act on this, and **all three are off**: `followup.skip_hedged_alerts` (uses
only buys before the alert), `skip_both_sides_wallets` and
`skip_sold_back_wallets` (use only alerts already followed up). Turning one on
changes which signals are copied, so it should come with a new model version.

### Settlement, exits and fee changes (v2.1 Part 4)

* **Two times per settled trade.** `resolved_at` is when the market resolved,
  `paid_at` when this service recorded the payout. The wallet counts money as
  back at `paid_at`, never earlier. `stats` and the Health page show the gap.
* **How each market resolved** (`resolution_type`): `normal`, `delayed` (more
  than a day after its scheduled end), `fifty_fifty` (voided: every side paid
  $0.50) or `disputed` (the result was challenged on UMA). Disputes come from
  Gamma's `umaResolutionStatuses`, read fresh when a trade settles: the docs
  name the field but not its values, so they were read from live data
  (2026-11-14: 1 disputed market in 500 recently closed). Trades settled before
  this existed are typed later, 100 per settle.
* **Holding against mirroring** on only the signals where the whale sold before
  the market resolved, the one place the two strategies can differ (`stats`, Edge).
* **Fee changes.** When a market's published rate, or a Polymarket US market's
  coefficient, differs from the last one seen, a row goes into `fee_changes`, a
  warning into the log, and the Health page lists it. Each trade keeps the rate
  it paid.

### Time-to-close cutoff (v2.1)

A paper dollar in a market that settles in three months is gone for three
months. `filters.max_days_to_close` (14) makes every bot runner and both
backtests skip a signal whose market is scheduled to end later than that, and
`filters.skip_if_no_end_date` (true) skips a market with no schedule at all.

* **The schedule** is Gamma's `endDate`, or the game's start when that comes
  first: on 1 sports market in 10 `endDate` trails the game by more than a day.
  A market already past its scheduled end (a game in play) is 0 days away.
* **Skips** are rows like any other: status `SKIPPED`, `skip_code` `too_long` or
  `no_end_date`, and a reason such as `too_long: closes in 21.3 days (limit 14)`.
  The alert is still posted. The placebo control is skipped too. Exactly 14 days
  is allowed.
* **Schedule against reality.** Each trade stores the scheduled end and the days
  to it when we decided, and at settlement the market's real resolution time.
  `stats` and the dashboard's Breakdowns page report how many closed trades were
  held past the limit and how many resolved more than a day late, with P&L by
  holding time and by days to close (under 1 day, 1 to 7, 7 to 14, over 14).
  The dashboard has a "Days to close" filter.
* **Old trades stay.** A trade entered before the cutoff, in a market it would
  now refuse, is counted and named but never removed from a total.
* **What it would have done to the stored history** (sandbox copy, 2026-11-12):
  19% of 21,726 alerts are `too_long` and 1% have no end date. Politics loses
  29%, geopolitics 36%, sports under 1%. In the three simulated strategies the
  ROI with and without the cutoff differs by less than one point and every
  interval still includes zero. Among trades the cutoff allows, 0.6% to 5.2%
  were still held longer than 14 days because the market resolved late.
* **Not covered:** the old-rule comparison trader is left as it was. The wallet
  replay with and without the cutoff is on the Sim wallet page.

### The small wallet (v2.1)

The bot stakes a fixed $10 on every signal from a bankroll that never runs dry.
A person with $100 cannot. `bankroll.py` runs the same signals through a small
wallet: `python -m whalewatcher wallet show`, or the **Sim wallet** page.

* **Where to see it.** A wallet card for each runner on the Overview page
  (equity, return, drawdown, taken vs skipped, fees, and a chart of equity, cash
  and money tied up, with marks where a trade was skipped for lack of cash or a
  free slot), and everything else on the Sim wallet page.
* **Amount.** $100 by default. Set it on the dashboard or with
  `wallet set 250` (above $0, at most $100,000, two decimals). It lives in the
  `settings` table, the only thing the dashboard may write.
* **Stake size** (`bankroll.stake_mode`):
  * `auto` (the default) sizes the stake from the evidence. It takes the return
    per dollar of this source's signals that have settled, subtracts 1.96
    standard errors, and bets a quarter of the Kelly stake for that edge
    (`f* = cost x edge / (1 - cost)` for a share paying $1), never more than
    the fixed stake. The bar is the project's own decision rule: 100 settled
    signals and a 95% interval above zero. Until then no edge has been shown,
    the Kelly stake is zero and the signal is skipped as `no_edge`. The wallet
    does not need to bet to learn: the record grows from the fixed-stake ledger.
    `auto_no_edge_stake: smallest` places the venue's smallest order instead.
  * `fixed` is equity x `deploy_fraction` (0.8) / `max_open_positions` (10): 8%.
  * Either way the stake is all-in (shares plus taker fee, so cash never goes
    below zero), never more than the cash, and the on-ramp cost (1.5%) comes
    off the starting amount once. Only results known at the moment of a
    decision are used.
* **Equity** = cash + open positions at their last recorded best bid. The bot
  records each open position's best bid every 5 minutes. A position with no
  mark (everything before schema 8) is worth its shares at the price paid.
* **Skips:** `no_edge` (automatic stake, no edge shown), `no_slot` (all
  positions open), `no_cash`, `below_minimum`, `event_cap` (one event over 20%
  of equity), plus the cutoff's `too_long` and `no_end_date`. An order that would not have filled is a miss, not a skip.
* **Minimum order.** Polymarket's real minimum is per market and in shares
  (`min_order_size` on the book, 5 on every book sampled), not dollars.
  `min_order_usd` is our own floor on top of it. Both apply.
* **Fill prices.** From schema 8 each signal stores the ask levels it saw, so a
  stake of any size is walked through that book: a $100,000 wallet misses what
  an $8 order fills. Older signals carry only what a $10 order paid. A larger
  stake priced that way is flagged optimistic and counted.
* **Runs.** Each runner has its own wallet. A run ends when the amount or a
  `bankroll` setting changes, and a new one starts. An ended run's figures are
  frozen and a database trigger refuses any later change or deletion.
* **Replay and live are one engine.** It walks signals in the order they were
  decided and uses only what was known then. The replay covers a source's whole
  history, with and without the cutoff. The live wallet covers signals since
  its run began. The bot model, the old rule and each simulated backtest are
  replayed separately and never mixed.
* **Not a forecast.** A wallet is one path. With and without the cutoff the two
  wallets hold different trades from the first skip on, so most of the gap
  between them is luck. No confidence interval is given for a single path; the
  per-trade ROI interval is the evidence, and the page says when there are too
  few settled trades.

**Stake sizes compared** (sandbox copy, 2026-11-13, $100, with the cutoff).
`wallet show` and the Sim wallet page print this table for every source. It
describes the past: choosing the best row afterwards is hindsight.

| Stake | Bot, 55 live signals | Sim walk-forward | Sim spec | Sim fresh-wallet |
| --- | --- | --- | --- | --- |
| Automatic, no order without an edge | $98.50, 0 taken | $103.76, 35 taken | $97.97, 8 taken | $98.50, 0 taken |
| Automatic, smallest order without an edge | $97.05 | $89.32 | $11.68 | $1.15 |
| Fixed 1% | $98.50, 0 taken | $98.50, 0 taken | $98.50, 0 taken | $98.50, 0 taken |
| Fixed 2% | $96.47 | $49.85 | $47.54 | $43.96 |
| Fixed 4% | $98.42 | $24.72 | $23.88 | $15.48 |
| Fixed 8% | $85.55 | $0.00 | $11.91 | $5.66 |

What it shows:

* Smaller fixed stakes lose more slowly, and still lose. These signals have a
  negative return after fees, so every stake size ends below the start.
* A $100 wallet cannot stake 1%: $0.985 is under the $1 floor, and 5 shares
  cost more than that on almost every market.
* Even the smallest order loses most of the wallet over thousands of signals.
  As equity falls, 5 shares fit under the cap only on cheap long shots, which
  win least often.
* The automatic stake's answer today is not to bet. On the bot's live signals
  it takes nothing. In two simulated runs a hot streak briefly passed the bar
  (35 and 8 trades) and the wallet ended within a few dollars of the start.
* A first version used a lower bar (30 signals, one standard error). It sized
  up on lucky streaks and lost 88% to 96% in the simulated runs. That is why
  the bar is the pre-set decision rule and not something tuned to look good.

### Polymarket US, row A (v2.1)

Polymarket US is a separate, US-regulated exchange with its own order books and
fees. Nobody's trades are visible there, so there is nothing to copy on it
(`docs/polymarket-us.md`). What can be measured is what acting on the same
signals would have cost there. `polymarket_us.py` does that for every signal
the runner makes, in the background so the bot never waits for it:

1. **Match** the market by an exact rule on its name, or not at all
   (`not_on_us`): two-way game winners, tennis matches (the US slug is built
   from the players' names), three-way football markets, and esports map or
   game winners (the side is read from "Will X win Map 2 vs Y?"). Kickoff times
   must agree and each outcome must line up with exactly one side by name.
2. **Read** the US order book from the public gateway. No account, no key.
   Every request is a GET to `gateway.polymarket.us`. The trading host is never
   contacted.
3. **Fill** the same $10, fill-or-kill, under the same limit (the whale's price
   + `max_slippage`). There is one instrument per market: the other side is a
   short of it, filled against the bids at `1 - bid`.
4. **Charge** the US fee: `0.095 x contracts x p x (1 - p)`, to the cent. That
   is two to three times the international fee.
5. **Settle** at what the US venue itself paid (its settlement endpoint).

The rows live in the same ledger under the runner `us`, so `stats`, the
dashboard (strategy "Polymarket US", venue filter, the **US venue** page) and
the small wallet all show it, and none of them mix it with the international
numbers. The US venue page shows both venues side by side (missed rate, not-on-US
rate, win rate, net P&L, ROI with its interval, spread, slippage), the fill-price
gap with its trend over time, and both sites' resolution rules for the latest
matched markets. Each US row stores the US market's rules, end date and best
bid as published when it was priced. `python -m whalewatcher us match` prints the match rate on recent
alerts.

* Crypto up/down exists on both venues for the same window but settles on a
  different price index there, so it is skipped as `not_same_bet` unless
  `polymarket_us.include_different_index` is set.
* `polymarket_us.exclude_categories: [sports]` leaves a category out on the US
  venue only. Reports always carry a sports-excluded line.
* Spreads, totals, props and non-sports markets have no rule yet and count as
  `not_on_us`.
* A row A fill is decided a moment after the bot's, because it needs one or
  two more requests. Both times are stored.

Match rate on the sandbox copy, live alerts of the last 14 days (2026-11-13):
the same bet exists on Polymarket US for 32 of 60 (53%), all of them sports.
The other 28 are sports lines and props (20) and geopolitics, finance, politics
and tech (8).

**The dashboard** (`dashboard.py`, `dashview.py`, v2 Phase 4). A third
container that shows everything `stats` prints, as pages: Overview, Edge,
Wallets (with a page per wallet and the watchlist timeline), Trades (with CSV
export), Sim wallet (the small wallet and its amount form), US venue (row A
beside the international baseline), Breakdowns and Health. One filter row
scopes every number on a page: date range, day or week, strategy, venue,
wallet, days to close, model version (the current one unless picked) and
"Include outage windows" (off unless ticked). The US venue page uses only the
date range, the version and the outage switch. Sim wallet follows the version
and the outage switch and nothing else: a wallet is one path through all of its
signals.

**Outage windows** (`outages.py`, v2.1 Part 4 #15). The service records them
itself: when the runner or the detector starts again after a stop long enough
to make a signal late (longer than `bot.max_signal_age_seconds`), when the
runner sees the detector's heartbeat stop (until it moves again), and when
Polymarket's data API stops answering (from the first failed attempt to the
first answer). Signals made inside a window are left out of every result by
default; the Health page lists each window and who recorded it.

* **It reads, with one exception.** The database is opened read-only and
  query-only and every page is a GET. The one thing the site can change is the
  simulated wallet amount: a form that writes one row of the `settings` table
  through a connection SQLite restricts to that table. Tests prove that it
  cannot touch a trade, an alert or a wallet run, that a form on another
  website is refused, and that every other route refuses a write.
* **Tailnet only.** Compose publishes the port on the host's `127.0.0.1` and
  nowhere else, so nothing on the LAN or the internet can reach it. Run
  directly, the command refuses any address but loopback. To open it from your
  phone, expose it yourself: `tailscale serve --bg 8787`.
* **Self-contained and light.** No login, cookies, third-party scripts, fonts
  or outbound requests; the page's own stylesheet and one small script are
  served by the app. Charts are drawn in the browser so they fit a phone, and
  each has a table of the same numbers under it. Light and dark follow the
  device.
* **Honest about sample size.** Every figure carries its n, a banner appears
  under 30 closed trades in the selected range, the interval for mean ROI comes
  with a plain sentence saying what it does and does not show, and outage
  windows are shaded on every time chart.

The numbers come from the same code as `stats`, and the tests check each page
against a small fixture worked out by hand (`tests/dashfix.py`).

**Consensus** (`consensus.py`, v2 Phase 3b). A separate question from copying
one whale: when several *independent* top traders take the same side, is that
side more likely to win?

* *The pool* is broad and judged as a crowd: the 30-day profit leaderboard
  (polymarket.com, the international platform), minus wallets that trade fewer
  than 5 or more than 200 times a day or buy both sides in over 40% of their
  markets.
* *Entities.* Wallets that are probably one person are merged, and consensus
  counts entities, not wallets: the same set of markets (overlap of 60% or
  more), or repeatedly buying the same outcome within 10 seconds of each other.
  The brief's third test, "funded from the same address", is **not done**: it
  needs a chain node and this project uses free APIs only. Some twins will get
  through.
* *The signal.* Three or more entities each hold $1,000 or more of one outcome,
  their entries are within 48 hours of each other, and no pool entity holds
  another outcome of that market. It fires on the fill that completes it, and
  every signal is stored with who agreed, when, at what price, and how far the
  price had already moved.
* *The backtest comes first.* On each day of the replay the pool is rebuilt
  from that day's past only: profit is what a wallet realised on markets that
  resolved in the previous 30 days, and a wallet cannot be in the pool before
  the date a leak-free candidate search first found it. It is compared with two
  baselines over the same period: copying any single pool entity, and buying a
  random other market at a similar price at the same moment.

Two gaps between that backtest and the live pool, printed with every result:
history has no leaderboard, so the backtest ranks on realised profit while the
live board is marked to market; and the backtest only knows wallets our
candidate search found, a sample, not every trader.

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
* **Our own record of the wallet**: once our copies of its alerts cover 15
  settled events, a negative mean closing-line value is a failed review, and
  keeps a removed wallet from walking straight back in. "Our copies" are the
  bot's headline trades (`single / flat_hold`) plus the old rule's paper trades
  from before the bot model. Until 2026-11-14 it read only the second, so it
  had stopped being able to fire.

A review can be run for a past date (`nightly(as_of=...)`). It then uses only
what was known on that date: the leak-free candidate pool (markets that had
closed by the date, trades before it), our own record up to the date, and no
market-maker check, because `/v2/user-stats` gives lifetime figures as of
today. `tests/test_hindsight.py` builds the same history twice, once with
everything after the date deleted, and checks the pool, the scores and the
decisions come out the same.

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
7. **Grouping without latency.** An alert is made when a group crosses the
   threshold instead of waiting out the window, and later fills update its text.
8. **Skipped alerts are not paper traded.** "Every alert produces one paper
   trade" applies to alerts actually made (status `RECORDED`). Alerts are not
   sent anywhere. Rows marked `POSTED` or `UNSENT` date from when they were also
   posted to a chat webhook, which was removed on 2026-11-13.
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

## Does agreement among top traders predict anything? (2026-11-12)

**No, on this evidence.** `python -m whalewatcher consensus --backtest` replayed
2026-05-07 to 2026-11-12: 731 wallets found by leak-free candidate searches,
the pool rebuilt every day from that day's past, a signal when 3 or more
entities each held $1,000+ of one outcome within 48 hours with no pool entity
on the other side, a $10 fill 5 seconds later at the minute price plus a cent,
fees, held to resolution.

| Pool (30-day realised profit) | Settled signals (events) | ROI | Mean per trade, 95% CI | Win rate |
|---|---|---|---|---|
| $100k+ | 34 (29) | -35.2% | -35.1% [-62.9%, -6.4%] | 35% |
| $250k+ | 7 (6) | -33.0% | [-81.2%, +29.1%] | 43% |
| $500k+ | 1 (1) | n/a | one trade | n/a |
| top 50 that pass the rules | 183 (165) | -7.0% | -7.0% [-19.8%, +6.6%] | 59% |

Baselines over the same period, same fill rule:

| Baseline | Settled | Mean per trade, 95% CI |
|---|---|---|
| any single pool entity's entry (top-50 pool) | 1,378 | -5.2% [-10.0%, -0.2%] |
| any single pool entity's entry ($100k pool) | 1,363 | -5.3% [-10.9%, +0.6%] |
| consensus minus a random similar-priced market at the same moment (top-50 pool, 181 pairs) | | -16.8% [-40.9%, +3.4%] |

What the numbers say:

* No pool's interval is above zero, and the $100k pool's is wholly below it.
  That one is 34 signals bunched in May and July, so read it as "no support",
  not as "consensus is a reliable fade".
* Consensus did not beat copying a single pool wallet, and did not beat buying
  a random market at a similar price.
* The price had not run away: it moved only +0.5c to +3.4c between the first
  entity's entry and our fill, and agreement took about 9 to 10 hours to form.
  The idea fails on the outcomes, not on being late.
* CLV was negative too (-0.014 and -0.016 on pre-game fills): the market kept
  moving against these positions after the signal.
* Most apparent agreement is not agreement: 8,028 would-be signals in the
  top-50 pool were blocked because another pool entity held the other side.

Limits: the backtest pool is ranked on realised profit of resolved markets (no
historical leaderboard exists) and only knows the wallets our candidate search
found, so its $100k pool held a median of 5 wallets a day against 161 on
today's real board. The top-50 pool is the closest stand-in for a broad live
pool. Fills use minute prices, not real books. Entity merging has no funding
check.

Today's live pool, for comparison (`python -m whalewatcher consensus`):

| Definition | On the board | Pass the activity rules | Entities |
|---|---|---|---|
| $100k+ | 313 | 161 | 159 |
| $250k+ | 120 | 53 | 51 |
| $500k+ | 56 | 23 | 23 |
| top 50 by profit | 50 | 21 | 21 |

## Known limits

* Detection is the public trade feed polled once a second: a median of about
  3.5 s behind the whale, never sub-second. A websocket or mempool watcher
  would be faster (docs/v2-research.md); both were left out because the first
  is an unsupported legacy feed and the second needs a paid node.
* A paper fill assumes we could take the book we saw. A real order would
  arrive later still and move the price; treat results as an upper bound.
* Two tracked wallets filled identically in one transaction used to collide on
  one key and the second fill was dropped. The detector now keeps both; scoring
  history fetched before this fix may be missing a few such fills.
* Positions opened more than `entry_lookback_days` before the lookback window
  and held to resolution without any later activity are invisible to scoring.

*Research and education only. Not financial advice.*
