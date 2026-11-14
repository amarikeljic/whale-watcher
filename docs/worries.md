# Part 4: the 19 worries, checked against the code

Written 2026-11-14 for the v2.1 brief, Part 4: "For each one: tell me whether the
current code already handles it, and if not, propose a fix and wait for my
go-ahead. Some may need only a measurement, not a fix."

**Update, the same day:** every proposed fix was approved ("yes for all three") and
has been built. The "Built" column below says where. Item 7's description of
Polymarket's rounding was wrong and is corrected under 7. The text of each item is
otherwise as first written, so the reasoning stays readable.

Measurements come from the sandbox copy of the database: 60 live alerts from 18
watched wallets, 30 bot fills, 22 of them settled, 2,050 detected fills. That is
a small sample, so read each figure as "is this a problem at all", not as a rate.

## The short version

| # | Worry | Today | What it needs | Built |
| --- | --- | --- | --- | --- |
| 1 | Mixed code versions | Not handled | Tag every row with the commit and a model version | `version.py`; headlines count model 1; Version picker |
| 2 | Too many experiments | Partly | A list of every variation, and a forward test before believing one | `experiments.py`, `experiment_flags` |
| 3 | Hindsight in wallet selection | Partly, plus a bug | An end-to-end test, two small leaks closed, a frozen rule fixed | `tests/test_hindsight.py`; both leaks fail it on the old code |
| 4 | Correlated bets | Handled | Nothing | nothing needed |
| 5 | In-play quality | Partly | Prices 1 and 30 minutes after each fill, by wallet | 1, 5 and 30 min, in-play, pre-game, by wallet |
| 6 | Stale order books | Not measured | Record the fetch time and re-read the book 1 second later | second read in the background; fetch time per alert |
| 7 | Price rules | Partly | Round amounts and shares the way the exchanges do | `precision.py`, both venues and the wallet |
| 8 | Marking open positions | Partly | A conservative value from walking the bids | conservative equity; US positions marked from the US book |
| 9 | Settlement timing | Mostly handled | Name the two times separately | `paid_at` beside `resolved_at`, gap reported |
| 10 | Bait trades | Measured: none seen | Keep measuring per wallet and flag repeat offenders | `followup.py`, switch off |
| 11 | Hedges | Measured: real | Mark possible hedges and report them apart | `followup.py`, switches off |
| 12 | Following buys, not sells | Mostly handled | One comparison line | `stats` and Edge page |
| 13 | Disputed, delayed, voided markets | Partly | A resolution type per trade, P&L by type | `resolution_type`; disputes from Gamma's UMA history |
| 14 | Fee changes | Partly | Log when a published fee changes | `fee_changes`, Health page |
| 15 | Outages | Partly | Record them automatically, exclude by default | `outages.py`; excluded unless asked |
| 16 | Block time vs API time | Unverified, nothing odd seen | A one-off check against the chain (needs your OK) | `scripts/check_block_times.py`: equal on 200 of 200 |
| 17 | Rate limits and terms | Limits yes, terms no | A sources list, and reading the terms (needs your OK) | `docs/data-sources.md`: read, quoted, 3 open questions |
| 18 | Dependencies | Not handled | Pin exact versions with a purpose list | pinned, base image by digest, `docs/dependencies.md` |
| 19 | Secrets | Handled | Delete the old `.env` on the server, revoke the webhooks | rechecked: none in the history; the server's `.env` is yours to delete |

Found along the way (not on your list):

* **A frozen safety rule.** The nightly review can drop a wallet when our own
  copies of its trades do badly ("our own record"). It reads only the old-rule
  paper trades, which stopped growing when the bot model started. So that rule
  has not seen a new trade since. Details under 3.
* **Owed work from Parts 2 and 3b**, already approved, now fixed: the $100
  wallet chart is on the Overview page for each runner, its markers show only
  trades skipped for lack of cash or a free slot, and the Polymarket US page has
  both sites' resolution rules side by side, the fill-price gap with its trend,
  and win rate, missed rate, spread and slippage in the side-by-side table.

---

## Is the result real?

### 1. Mixed code versions: not handled

**Today.** The old rule, the bot model and each simulated replay are separate
sources and are never added together in one headline. But nothing records which
code produced a row, and the bot model itself has changed many times since it
started (fees, the cutoff, the stake, the US row). The Docker image does not
contain the git history (`.dockerignore` leaves `.git` out), so the program
cannot look its own commit up.

**I disagree with one part.** I disagree because a headline that never combines
two commits starts again from zero at every deploy, including deploys that only
change a document, so it would never reach the 100 settled trades your decision
rule needs. Instead, tag every row with the commit and also with a model version
number that I change on purpose whenever code that decides, prices or settles a
trade changes. Headlines combine only rows with the same model version. The risk
in your approach is an experiment that can never finish.

**Proposed fix.**

* Pass the commit into the image at build time and stamp it on every alert,
  fill, paper trade, wallet run and score run, with the model version beside it.
* Rows from before the change get the label "before tagging" and are shown as
  their own group, never deleted and never mixed into the new headline.
* A version filter on the dashboard, and n per version in `stats`.

### 2. Too many experiments: partly handled

**Today.** One test is fixed in advance: `single / flat_hold` against the
decision rule in the README, with `placebo_late` as its control. The wallet
selection rules were picked by a walk-forward search, so each choice was checked
on later data it had not seen. What is missing is a list, and a rule for what
happens when something else looks good.

**Measured: what is being tracked.**

* Live: 4 bot strategies on the international runner (one of them the control),
  1 on Polymarket US, and the closed old rule. Two comparison columns on the hold
  strategies (zero delay, human delay).
* Replays: 5 stored backtests (spec and walk-forward on Polymarket and Kalshi,
  fresh wallets), 12 consensus groups (4 pool sizes, 3 groups each).
* The small wallet: 6 stake sizes for each of 6 sources, each source also with
  and without the cutoff, plus the live runs. About 45 curves.
* Settings that change results: the fee source, the cutoff, the slippage limit,
  the stake, the watchlist thresholds.
* The wallet selection search tried **216,000 rule combinations** once.
* Every breakdown slice (category, price, hour, wallet, holding time) is another
  chance for a lucky-looking group.

With dozens of variations shown at 95% intervals, two or three will look
"significant" by chance alone.

**Proposed fix.**

* `python -m whalewatcher experiments` and a dashboard section listing every
  variation with its role (primary, control, comparison, exploratory, simulated),
  start date, n and result.
* A forward test: when any variation other than the primary looks good, I record
  the date. From then on only signals after that date count for it, until it
  meets the decision rule on its own.
* A plain note beside the list: how many variations there are, and how many would
  look good by luck.

### 3. Hindsight in wallet selection: partly handled, plus a bug

**Today.**

* Per-wallet scoring ignores trades and results after the date. A test proves it
  (`test_future_rows_and_future_resolutions_change_nothing`), and another proves
  markouts never look past the date.
* The replays (`optimize`, `backtest`, `validate`, the consensus backtest) build
  their wallet pool from large trades made before the date, not from today's
  leaderboard.

**Not handled.**

* **No end-to-end test** of the kind you described: the whole list for a past
  date, then delete everything after that date, then the same list again.
* **A small leak in the replay pool.** It looks for big traders in the busiest
  markets that ended between 90 days before and 14 days after the date. "Busiest"
  is each market's total volume as reported today. For markets that ended before
  the date that is fine (nothing trades after the end). For the ones ending in the
  14 days after it, total volume includes trading after the date.
* **The nightly review run for a past date** (`watchlist --review --as-of`) uses
  today's leaderboard (it warns), the bot screen's lifetime statistics, and our
  own record without a date limit. Run for today it is fine, and it only ever runs
  for today on its own.
* **The bug.** "Our own record" reads `paper_trades` only. Since the bot model
  started, new copies go to `bot_trades`, so that exit rule sees nothing new.

**Proposed fix.**

* The end-to-end test, on the replay pool and on the review.
* The replay pool takes only markets that had ended by the date.
* A past-date review uses the replay pool, skips the lifetime screen (saying so)
  and limits our own record to trades settled before the date.
* Our own record reads the bot's trades.

### 4. Correlated bets: handled

**Today.** Each strategy caps how much can sit in one event, and the small wallet
caps one event at 20% of equity. Every confidence interval resamples by event and
by wallet together, so several fills in one game count as one independent bet.

**Measured.** 68% of alerts shared an event with another alert, 55% with another
wallet's alert. The bot's 30 fills fell in 20 events, at most 4 in one. That is
exactly why the event resampling matters, and it is in place.

**Proposed fix.** None.

### 5. In-play quality: partly handled

**Today.** The price 5 minutes after each bot fill is recorded (29 of 30 fills)
and shown on the Edge page. The closing-line value covers pre-game fills only.

**Measured.** 16 of 30 bot fills (53%) came during a live event in this sample,
fewer than the 87% in your note. Either way, half or more of the fills have no
closing line.

**Proposed fix.** Record the price 1, 5 and 30 minutes after every fill, show each
against the fill price, overall and by wallet, split into in-play and pre-game.

## Are the paper fills realistic?

### 6. Stale order books: not measured yet

**Today.** The order book is fetched right after a whale fill is detected and the
decision is made at once. I checked that Polymarket does not serve the book from a
cache (`cf-cache-status: DYNAMIC`), so each copy is live when fetched. The book's
own timestamp is stored with each alert, but it marks the book's last change, not
how old our copy was (median 0 s, at most 22 s before detection). Nothing measures
how the book moves between our read and a real order.

**Proposed fix.** Store the fetch time on every fill, read the book a second time
1 second later, and record whether the best ask moved and what the fill would
have cost on the second book. One extra request per signal, far inside the limit.
Report how often the price moved and the average cost of the move.

### 7. Price rules: partly handled

**Verified in the docs** (<https://docs.polymarket.com/trading/place-orders.md>,
the precision table, read 2026-11-14):

* **Minimum order size.** Enforced on both venues: 5 shares on every Polymarket
  book sampled, 0.01 contracts on Polymarket US.
* **Tick size.** Fills only happen at real book levels, which are always on the
  tick. The limit price (the whale's price plus 3 cents) is not rounded to the
  tick, but that never changes which levels qualify.
* **Rounding.** Not done. Paper fills keep every decimal, and the small wallet's
  stakes are not rounded to the cent.

  **Correction (same day, re-read while building it).** I first wrote that
  Polymarket rounds the shares to 4 decimals at a 0.01 tick. The page says otherwise:
  shares are rounded down to **2** decimals at every tick size (the "size
  decimals"), and it is the dollar amount that has 3 to 6 decimals depending on the
  tick ("round it up first to Amount decimals + 4, then down to Amount decimals").
  Polymarket US allows decimal quantities only on markets whose minimum is below
  one contract (0.01 on almost all of them). `precision.py` follows the corrected
  reading.

**Proposed fix.** Apply the precision table to every paper fill on both venues and
round wallet stakes down to the cent. The effect is pennies a trade.

### 8. Marking open positions: partly handled

**Today.** The small wallet values each open position at its last recorded best
bid (recorded every 5 minutes). In a thin market the best bid may hold only a few
shares, so this can overstate what the whole position would fetch.

**Proposed fix.** The same book read already has the bids. Walk them for the full
position size and store that value with each mark. Show "conservative equity"
beside equity on the Sim wallet page and in `stats`.

### 9. Settlement timing: mostly handled

**Today.** The $100 wallet does not count money as back at the resolution time.
It counts it when our own service records the payout, which is never earlier than
the money can really be claimed.

**Measured.** Across 33,675 markets where the watched whales claimed payouts,
the first claim came a median of 12 seconds after the resolution time we record,
and never more than an hour before it. So on Polymarket, winnings can be claimed
as soon as the market resolves. In 30% of markets the first claim came over a day
later, which is whales choosing to claim late, not money being held back. For a
quarter of markets (8,678) the recorded resolution time is the trading close,
which can be slightly early.
The sandbox's own delay in noticing (17 hours median) is not meaningful, because
the sandbox only ran part-time. On your server it will be the settle interval.

**Proposed fix.** Small: store the two times under clear names (resolved at, paid
at), show the gap between them, and keep using the paid time in the wallet.

## Can the whales hurt the copiers?

### 10. Bait trades: measured, none seen

**Today.** Nothing measures it on an ongoing basis.

**Measured.** In none of the 60 alerts did the whale sell half or more of the
position back within 5 minutes, or within 30. No wallet would be flagged.

**Proposed fix.** Each night, compute for each wallet the share of its alerts it
sold back within 30 minutes. Show it on the Wallets page and flag a wallet with at
least 5 alerts that does it on 20% or more, with the option to stop copying it.

### 11. Hedges, not bets: measured, real

**Today.** Not handled. The two-sided check in wallet selection is switched off
(`max_two_sided_ratio: 1.0`, the setting the search recommended), and it measured
whole markets over months, not one hour.

**Measured.** In 29 of 60 alerts the same whale also bought the other side of the
same market, every time within the same hour. Usually small (median 20% of the
copied buy), but in 7 alerts at least half, and in 5 at least as large: there the
whale had no real bet at all. Three wallets do it on most of their alerts (10 of
13, 11 of 12, 6 of 7). 32 of 60 alerts also had a buy in another market of the
same event, which may or may not offset.

**Proposed fix.**

* At alert time, add up the whale's buys of both sides of the market over the past
  24 hours. Mark the alert "possible hedge" when the other side is at least half
  of this one, and note other markets of the same event.
* Report results for hedged and clean alerts separately, and per wallet.
* Then decide, with numbers, whether to skip hedged alerts or the wallets that
  mostly trade both sides.

### 12. Following buys but not sells: mostly handled

**Today.** `flat_mirror` records every whale sell after our entry and sells with
it. `flat_hold` keeps the position. Both run on every signal.

**Measured.** In none of the 30 copied buys did the whale sell before the market
resolved, so there is nothing to compare yet.

**Proposed fix.** One line in `stats` and on the Edge page: hold against mirror on
only the trades where the whale exited.

## Resolution risk

### 13. Disputed, delayed or voided markets: partly handled

**Today.** 50/50 resolutions pay $0.50 a share (tested). Late resolutions are
counted by the time-to-close report. Nothing stores a resolution type per trade.

**Measured.** 22 settled fills, none 50/50, none more than a day late.

**Proposed fix.** Store a type per settled trade: normal, 50/50, delayed (over a
day after the scheduled end), disputed (from the market's resolution status,
after I verify the field in the docs). Report P&L from anything not normal.

### 14. Fee changes: partly handled

**Today.** The category table in `config.yaml` is dated and the table in force at
each trade's time is applied. Every fill records the rate it paid and where the
rate came from. Polymarket US fills record its coefficient. But when a market's
own published rate changes, the new value simply replaces the old.

**Proposed fix.** Keep a history row whenever a market's rate or the US
coefficient changes, write a log line, and list recent changes on the Health page.

## Operations and security

### 15. Outages: partly handled

**Today.** An outage table exists, the dashboard can leave its windows out, and
one window (Nov 9) was entered by hand. Nothing records new windows, and nothing
is excluded unless you tick the box.

**Proposed fix** (this is the first Phase 5 item):

* Record a window automatically when the runner restarts after a gap, when the
  detector's heartbeat goes stale, or when a data host stops answering.
* Exclude windows from `stats` and the dashboard by default, with a switch to
  include them.

### 16. Block time vs API time: unverified, nothing odd seen

**Today.** The whale's fill time is the Data API's trade timestamp. Checking it
against the chain needs a Polygon RPC, which is blocked here, and you do not want
paid data.

**Measured.** Across 2,050 detected fills, the API's time was never later than our
own detection (the shortest delay was 0.55 s). That fits the block time, but does
not prove it.

**Proposed fix.** A one-off script you run on the server against a free public
Polygon RPC: compare 200 fills' API times with their block times. It needs your
OK, because it contacts a new outside host.

### 17. Rate limits and terms: limits yes, terms not read

**Today.** Every published limit is in `config.yaml` with its documentation link,
and the client stays under each:

| Source | Published limit | Our ceiling | Used for |
| --- | --- | --- | --- |
| data-api.polymarket.com | 800 / 10 s overall | 600 / 10 s | trades, activity, positions, prices |
| same, `/v2/trades` | 300 / 10 s | 240 / 10 s (the detector uses 10) | the global trade feed |
| same, `/v2/activity`, `/positions`, `/prices-history` | 200 / 10 s each | 160 / 10 s each | wallet history, prices |
| gamma-api.polymarket.com `/markets` | 300 / 10 s | 240 / 10 s | market details |
| clob.polymarket.com `/book` | 1,500 / 10 s | 400 / 10 s | order books |
| gateway.polymarket.us | 25 / s | 5 / s | row A |
| api.elections.kalshi.com | token based | 3 / s | backtests only |

**Not done.** The terms of service of Polymarket and Polymarket US have not been
read. Two things to check against them: the unique query parameter that skips the
CDN cache (the trade feed once a second, and the US book), and automated research
use in general.

**Proposed fix.** Read both sets of terms and write `docs/data-sources.md` with
each source, its limit, our rate and the relevant terms, quoted. Neither terms
page is reachable from this sandbox, so it needs your OK to allow the two hosts.

### 18. Dependencies: not handled

**Today.** `requirements.txt` allows ranges (`aiohttp>=3.9,<4`, `PyYAML>=6.0`),
the Docker base image is `python:3.12-slim` without a fixed digest, and there is
no list of what each package is for. No package has been added since the brief,
and `python-dotenv` was removed with Discord.

**Installed now:** aiohttp 3.14.5 (with aiohappyeyeballs 2.7.1, aiosignal 1.4.0,
attrs 26.1.0, frozenlist 1.8.0, idna 3.20, multidict 7.2.1, propcache 0.5.4,
typing-extensions 4.16.0, yarl 1.25.1) and PyYAML 6.0.3. For tests only: pytest
9.2.0 and pytest-asyncio 1.4.1.post1, with iniconfig, packaging, pluggy, pygments.

**Proposed fix.** Pin every package, sub-dependencies included, to the exact
version, pin the base image by digest, and add `docs/dependencies.md` saying what
each one is for. No new packages.

### 19. Secrets: handled

**Today.** There are no secrets left. Discord is gone, and none of the APIs in use
needs a key. Every request is a public, read-only GET.

**Checked.** All 41 commits in the git history: no webhook address, token or key
of any kind. The old webhook code never logged the address (it redacted the
token), and the dashboard shows no configuration.

**What is left is on your server.** The old `.env` still holds the two real
webhook addresses. Delete it, and delete the two webhooks in Discord so the
addresses stop working.

## Found in the review of 2026-11-14

Checked on the 6 GB live copy and against live Gamma data.

* **Fills on new-format markets would never settle: fixed.** Polymarket starts
  Protocol V2 on 2026-11-23 and moves most sports to it on 2026-12-01. Its markets
  trade under `positionIds`, chosen by the market's `version`. The code read
  `clobTokenIds` only, so a V2 fill's token matched no outcome and settlement skipped
  it every time: open forever, a wallet slot held, never counted toward the decision
  rule. Now chosen by version, both id lists kept, and V2's `resolutionStatus` read.
  An end-to-end test fails on the old code and passes now. The Health page card "Can
  every trade settle?" counts V2 markets, stuck fills and signals missed for lack of
  market data. Not checkable before a real V2 market exists: whether the Data API
  names V2 trades with the same condition id as Gamma.
* **The position id decoder described the layout wrongly: corrected.** On 74 live
  ids the outcome index was right every time, but the key inside is not the market's
  `conditionId`. Nothing depended on it.
* **A 1 GB index nobody reads: dropped** (schema 12). `wallet_trades (wallet,
  condition_id)`: every query goes by fill key or by wallet and time (checked with
  SQLite's query planner). It also slowed every insert.
* **Every dashboard page loaded 24,800 replay rows: fixed.** Only Edge and the
  replay views show them. Overview 1.28 s to 0.09 s, peak memory 185 MB to 45 MB;
  the wallet page 2.45 s and 210 MB to 0.07 s and 45 MB (only the open tab is
  worked out now).
* **A stuck process stayed stuck: fixed.** Docker's healthcheck only marks a
  container. The runner and the detector now exit after 15 minutes without a
  finished step, and Docker restarts them.
* **Not fixed: the database keeps growing.** Whale trade histories are 5.1 GB of
  6 GB (7.5 million rows, 2,195 wallets). After the first load they grew 180,000 to
  290,000 rows a day, about 120 to 200 MB, with one 1.8 million row day when the
  review took in many new wallets. Rough guess: 50 to 70 GB a year. Shrinking it
  means rewriting the table or deleting old history, which needs your OK.
* **Not fixed: the Edge page and a replay's wallet tab still peak near 190 MB**
  of the dashboard's 256 MB. Fine today. A second large replay would push them over,
  and the dashboard would restart instead of showing the page.

## Parked for later

Ideas the owner chose to keep for later. Nothing here is built.

* **Faster detection** (question 9, parked 2026-11-14). A whale's fill shows up in
  the public trade feed a median 3.4 s after its block (README, "Detection").
  Reading new blocks straight from the chain, through a free key from a blockchain
  data provider such as Alchemy or QuickNode, might save a second or two (not
  measured). It needs a free account and key from the owner, a new outside host
  approved by the owner, and that provider's terms read first. It is a read-only
  data key, never a trading key. The detector would publish the same rows as now,
  so nothing after it changes.
