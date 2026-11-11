# v2 research: existing whale trackers and copy-trading bots (2026-11-10)

Phase 1 of the v2 brief. Every repository below was cloned read-only and read,
never run. Endpoints were checked against Polymarket's current docs and specs on
2026-11-10. Licenses: code with no license file is all rights reserved, so only
its ideas are reused here, reimplemented in our own code.

## What changed under everyone's feet

* **Data API v1 is retired** (2026-11-02). Most hobby trackers still call
  `/trades`, `/activity`, `/positions` without `/v2`.
* **The exchange contracts moved on 2026-04-28** (CTF Exchange V2, pUSD
  collateral). Current addresses (https://docs.polymarket.com/resources/contracts.md):
  CTF Exchange `0xE111180000d2663C0091e4f400237545B87B996B`, Neg Risk CTF
  Exchange `0xe2222d279d744050d28e00520010520000310F59`. Tools watching the old
  `0x4bfb41…` / `0xc5d563…` addresses, or the old `OrderFilled` layout, see nothing.
  New event (github.com/Polymarket/ctf-exchange-v2, `ITrading.sol`):
  `OrderFilled(bytes32 indexed orderHash, address indexed maker, address indexed taker,
  uint8 side, uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled,
  uint256 fee, bytes32 builder, bytes32 metadata)`.
* **Polymarket Protocol V2 rolls out for new markets**
  (https://docs.polymarket.com/migrate/polymarket-v2/overview.md): Nov 23 (some
  crypto Up/Down, weather), Nov 24 (BTC/ETH, other crypto, finance, commodities,
  minor sports), **Dec 1 (soccer, NFL, college football, basketball, tennis,
  esports)**. V2 markets use position IDs instead of CTF token IDs, trade on
  `ExchangeV3` `0xe3333700cA9d93003F00f0F71f8515005F6c00Aa` (same `OrderFilled`
  layout, `PositionId` is a uint256), and settle through `PositionManager`.
  Existing CTF markets keep their contracts.
* **RTDS** (`wss://ws-live-data.polymarket.com`, topic `activity`, type
  `trades`): listed in the API overview as public "trade activity"
  (https://docs.polymarket.com/getting-started/api.md); the PolyBolt migration
  page says "Keep using RTDS for the activity topic. It has no PolyBolt channel."
  (https://docs.polymarket.com/migrate/rtds-to-polybolt.md). But it went down
  platform-wide for hours on 2026-08-31 (garnet README), and in September the
  docs did not list it (insider-tracker research note). Not measurable from the
  build sandbox (its proxy blocks websockets).
* **The global trade feed** (`GET /v2/trades`, spec
  https://data-api.polymarket.com/v2/openapi.json): newest first, `limit` ≤ 1000,
  `taker_only=false` adds maker rows, `start`/`end` are honored only with
  `user`. Responses carry `cache-control: public, max-age=300`, and an identical
  URL is served stale (checked 2026-11-10: `age` 0→4→8 s, newest row 1.7→10 s
  old). A distinct URL (for example a cycling `limit`) is fresh: newest row
  2-3 s old.

## The projects

| Project | Detection, latency | Wallet choice | Sizing, risk | Exits | Quality, license | Use |
|---|---|---|---|---|---|---|
| pselamy/polymarket-insider-tracker (209★, 2026-11-10, MIT) | Polls the global trades feed every 5 s (1 s allowed), all participants; probes measured newest rows within 1 s and 0.4-1.35 s requests (v1, Sep). Polygon RPC for wallet age and funding | Any wallet: fresh wallets, unusual size, niche markets, funding clusters, ML + heuristics | none (alerts only) | none | 83k lines, 190 test files, Postgres + Redis + Alembic; read-only (public CLOB client, no credentials) | Adapt its ingestion design and funding tracing (USDC only: add pUSD) |
| EricSpencer00/polymarket-whale-tracker (0★, archived 2026-08-31, MIT) | RTDS `activity/trades` websocket, filtered to the watchlist: seconds | Seed whales; fingerprint (sub-second clustering, both-sides hedging, buy share, size entropy, concentration) into `hf_scalper`/`market_maker`/`swing`/`degen`; PnL steadiness (slope × R² × (1 − drawdown)); conviction | none | none | 3.2k lines, 5 tests; calls retired v1 paths; weights calibrated on 4 wallets | Adapt the fingerprint features to exclude bots and market makers; validate weights before trusting them |
| lukkr1/polymarket-smartmoney (0★, 2026-10-15, no license) | Snapshot of current holdings (`/positions`) | Top 40 by 30-day profit from undocumented `lb-api.polymarket.com` | none | none | Static page + script, no tests | Ideas: live-position filter (not redeemable, 1-99 c, not below 15% of entry), per-outcome agreement, $500 floor, conviction + entry proximity score |
| sacha9214/polymarket-overlap (0★, 2026-09-17, MIT) | Snapshot of current holdings | User-supplied wallets; quality in [-1, 1] from leaderboard P&L and resolved win rate | none | none | One HTML page, no tests, v1 paths | Adapt: twin detection (position-set Jaccard ≥ 0.6 → one entity); disagreement shrinks the signal (2 × agreeing share − 1); cap moves at 4p(1 − p) |
| martkir/poly-trade-scan (86★, 2026-02-12, no license) | Polygon websocket RPC, decodes trades per block | wallet list | none | none | Watches the pre-April exchange addresses and old event: [Likely] blind since 2026-04-28 (unverified here) | Ignore |
| warproxxx/poly_data (2,349★, 2026-09-08, GPL-3.0) | Historical `OrderFilled` from CTF Exchange V2 via HyperSync, to CSV | n/a | n/a | n/a | Skips the Neg Risk exchange (misses multi-outcome events and three-way soccer) and ExchangeV3 | Run separately as a data source, never copy (GPL); fix its coverage first |
| enviodev/poly-whale-tracker (3★, 2026-10-28, no license) | HyperSync stream of `OrderFilled` on both current CTF exchanges (needs an Envio token) | large trades | none | none | 1.3k lines, no tests; no ExchangeV3 | Ideas: current addresses and event decoding, already verified against the contract source |
| Abomination81/copybot (111★, 2026-09-28, no license) | **Pending transactions** to the exchanges (Alchemy filtered subscription), decodes `matchOrders` calldata: before the block lands | Configured leaders ("lanes") | Caps per market, open total and daily; drawdown stop; kill switch | Mirror sells, exit ladder, merge/redeem | 40k lines Rust, 7 test files; trades live (presigned orders) | Ideas: risk caps, mirror-sell exits, latency accounting. The mempool route needs a paid RPC key; defer |
| AndreySchurko/garnet-polymarket (4★, 2026-10-02, BSL 1.1) | **Three circuits on one dedup key**: RTDS (fastest, ~10 ms feed from Amsterdam), `/activity` polling (safety net), Polygon logs (V2 ABI) | User-chosen wallets | yes | yes | Rust, trades live; source-available, not open source | Idea: redundant detection with one dedup key, per-source latency stats |
| cbaezp/polycopier (10★, 2026-05-29, MIT) | Polls each target's trades every 2 s (official Rust SDK), dedup by tx hash | Configured targets | Fixed, % of balance, or mirror the whale's dollars, all capped; limit price = price × (1 + slippage) | mirrors | 12k lines Rust, 13 tests; trades live | Idea: sizing modes |
| anthonyonazure/polymarket-copycat (0★, 2026-09-15, no license) | Polls v1 trades every 5 s | Auto top-5 by leaderboard | 10% of whale size, capped $50 | none | Paper fills at the whale's own price, no fees or book: optimistic by construction; live-mode config fields unused | Ignore |
| yntymacho/polymarket-honest-tools (0★, 2026-06-28, MIT) | n/a (validator) | Neutral universe from top markets | fixed stake, worse-than-trader fills | settlement | Small sample (70 wallets) | Confirms our result: cohort copy ROI −2.8% / −2.5% in two periods; latency was not the bottleneck for stale-quote sniping |
| 0xinsider/research (2★, 2026-11-04, CC-BY-4.0) | n/a (studies) | Wallet grades | n/a | n/a | SQL + clustered bootstraps | S-grade wallets' $10k+ sports/esports buys: +1.78 pts [+0.54, +3.02] at their own price (81,855 buys). Sports calibrated to 0.02 pts. Their September result was inflated by grades written after the trade, the hindsight trap our walk-forward avoids |

Not evaluated: the keyword-stuffed "most advanced open-source Polymarket trading
bot" family (Tando404, emmanuelwestra and copies), per the brief.

## Detection options for a bot that acts in seconds

| Route | Expected delay | Needs | Risk |
|---|---|---|---|
| Per-wallet `/v2/activity` polling (today, 60 s) | ~30-40 s | nothing | too slow |
| Global `/v2/trades` every 1 s, `taker_only=false`, cycling `limit` | ~3 s median (2-3 s freshness + 0.5 s average wait) | nothing (about 3% of the endpoint's 300/10 s limit) | one page holds ~10-20 s of fills; must not repeat a URL within 300 s |
| RTDS `activity` websocket | ~1 s (unmeasured) | nothing | went down for hours on 2026-08-31; documentation has flip-flopped |
| Polygon `OrderFilled` logs on 3 exchanges | ~2-4 s (block time) | Polygon RPC or HyperSync key (read-only data key) | must decode V2 position IDs from Dec 1 |
| Pending transactions (`matchOrders` calldata) | under a block | paid websocket RPC with pending-tx support | fragile; deferred |

## Round 2 (2026-11-10/11): measurements and more repos

Everything below was measured from the build sandbox or read from cloned repos.
Nothing from another repo was run. Sandbox requests go through a proxy, so the
server should see slightly lower request times.

### Global trade feed, measured for 10 minutes (2026-11-10 17:27-17:37 UTC)

`GET /v2/trades` (spec https://data-api.polymarket.com/v2/openapi.json), each
URL made unique with a `_t` parameter (Cloudflare answered `DYNAMIC`, not cached).

| Poller | Polls | Gaps | Newest row age, median / p90 | Page span, median (min) | Detection delay, median / p90 / p99 |
|---|---|---|---|---|---|
| `taker_only=false`, every 1 s | 600, all 200 | 0 | 3.4 s / 5.0 s | 23 s (4 s) | **3.4 s / 4.9 s / 6.4 s** (fills >= $500: 3.3 / 4.8 s, n=226) |
| `taker_only=true`, every 2 s | 300, all 200 | 0 | 3.2 s / 5.0 s | 60 s (25 s) | 4.0 s / 5.9 s |
| `filter_type=CASH&filter_amount=500`, every 5 s | 120, all 200 | 0 | 4.9 s / 14.9 s | 2,195 s | 4.0 s / 6.8 s, recall of the full feed's >= $500 rows: 100% |

* Detection delay = first time our poll saw the row minus its block timestamp
  (whole seconds). Request time median 0.23 s. Local clock vs the server's `Date`
  header: +0.55 s median, inside the header's 1 s resolution.
* Volume: 49.5 rows/s with maker rows (29,533 rows in 10 min, 4,123 wallets,
  1,168 markets). Busiest block-second 432 rows, busiest 10 s 1,610 rows, so a
  1,000-row page never covered less than 4 s. 62% of rows were crypto Up/Down.
* **66% of rows, and 103 of 226 fills >= $500 (46%), are maker rows.**
  `taker_only=true` (the default) misses almost half of what large wallets do.
* **No row id.** One transaction can carry two identical rows (same wallet,
  token, side, price and size: two maker orders), seen up to 55 times per page.
  Keep the existing occurrence counter, and only trust timestamp groups that are
  not the page's oldest (that group may be cut by the limit).
* The CASH filter is per fill, so it misses wallets that build through small
  fills. Poll the unfiltered feed and filter locally (1 req/s is about 3% of the
  300 per 10 s limit). Follow `next_cursor` whenever a page's oldest row is newer
  than the previous page's newest row.
* `/v2/status` answered 40 of 40: `serving.lag_seconds` 2 (worst
  `custody_balances`), 201 ingestion cursors, already including
  `auto_redeem_v3_*` and `binary_module_*` streams (Protocol V2). Good health
  signal for Phase 5.

**Verdict:** the Phase 3 target (median under 5 s) is reachable with polling
alone. RTDS stays optional (see below).

### RTDS, from other people's measurements (websockets are blocked here)

* structbuild/polymarket-rtds-vs-struct-benchmark (MIT, 2026-07): ~15,000 live
  trades. A commercial confirmed-trade socket beat RTDS by +0.32 s median;
  mempool sightings beat RTDS by +2.66 s median. So RTDS is about block time plus
  0.3 s, roughly 2-3 s ahead of our polling.
* Subscribe frame: `{"action":"subscribe","subscriptions":[{"topic":"activity","type":"trades"}]}`,
  optional `filters` as a JSON string `{"event_slug":..}` or `{"market_slug":..}`,
  text `PING` every 5 s, no ack. The payload matches the Data API row in
  camelCase (`proxyWallet`, `asset`, `conditionId`, `transactionHash`,
  `timestamp` in seconds, envelope timestamp in ms)
  (Polymarket/real-time-data-client README; kollikrishnarao/polymarket-endpoints-test).
* **Polymarket's official real-time client is deprecated**, and the new unified
  SDK (Polymarket/py-sdk, MIT, 2026-11-09) has **no activity/trades stream at
  all**: only comments, crypto/equity prices, the CLOB market channel (no
  wallets) and the authenticated user channel. RTDS `activity` is legacy that
  still works, not a supported product. Polling `/v2/trades` is the supported
  path.

### Protocol V2 (Dec 1 for sports), from the official SDK

`src/polymarket/_internal/protocol.py` in Polymarket/py-sdk (MIT):
* A V2 position id is a uint256 with bits 40-103 all zero (`is_v2_position_id`).
  Legacy CTF token ids are hashes, so they almost never pass that test.
* Decoding: as 64 hex chars, the first byte is the module id (1, 2 or 3), the
  first 31 bytes are the bytes31 condition id, and the last byte is the outcome
  index (0 or 1).
* The SDK changelog says Data API reads and CLOB asset ids accept V2 ids, so our
  string-keyed `token_id` handling should carry over. [Likely] Test it on a real
  V2 market on Nov 23.
* Order books (`GET /book`, `POST /books` for many): bids ascending, **asks
  descending (best ask is the last element)**, plus `tick_size`,
  `min_order_size`, `neg_risk` and `hash`.
* On-chain only: V2 split/merge/redeem go through `CtfCollateralAdapter` /
  `NegRiskCtfCollateralAdapter`, so the stakeholder is the adapter, not the
  wallet (PaulieB14/polymarket-v2-subgraphs). Official gen-2 adapters:
  `0xAdA100Db00Ca00073811820692005400218FcE1f`,
  `0xadA2005600Dec949baf300f4C6120000bDB6eAab`
  (https://docs.polymarket.com/resources/contracts.md).
  psyberck/polymarket-subgraph-v2 also lists a "third V2 exchange"
  `0xe2222d002000ba0053cef3375333610f64600036` that the docs do not: unverified.
* No public indexer reviewed covers ExchangeV3 trades yet.

### Consensus pool: leaderboard counts (2026-11-10)

`GET /v2/leaderboard?time_period=..&category=..&sort_by=PNL`. Finite windows are
marked-to-market and net of flows.

| Window / category | >= $100k | >= $250k | >= $500k | Rank 50 PnL |
|---|---|---|---|---|
| month / overall | **312** | **121** | **52** | **$513k** |
| month / sports | 116 | 44 | 20 | $215k |
| month / esports | 51 | 13 | 5 | $100k |
| month / politics | 97 | 31 | 13 | $181k |
| month / crypto | 13 | 4 | 2 | $42k |
| week / overall | 121 | 39 | 9 | $206k |
| all / overall | 1,991 | 855 | 417 | $3.1M |

Two problems with using the board as the pool:

1. **The board disagrees with Polymarket's own PnL engine.** For the top 120 of
   the month board, the 30-day change in `/v2/user-pnl` (`interval=1m`,
   `fidelity=1h`) was a median 0.74x the board figure. Spearman rank correlation
   was 0.27. 13 of 120 showed under 5% of their board profit. Example:
   `e46m3`, board +$3.86M, PnL series down from $662k to $510k. [Certain] that
   they disagree. [Guessing] why: position transfers or marks counted as equity.
   Use the board to find candidates, never as proof of skill.
2. **28% look like market makers or bots** (34 of 120) by two cheap
   `/v2/user-stats` checks. Rebates plus rewards over 30% of profit (the rule
   from drkxbt/sharps, MIT) flagged 21. Over 100 fills per market flagged 13.

`/v2/user-pnl?interval=max&fidelity=1d` returns a daily cumulative series from
the wallet's first trade (`source_fidelity` 1d). It could rebuild point-in-time
30-day PnL for the backtest without hindsight, but given (1) it must be
validated first. The universe must also come from data available at the time,
not from today's boards.

### Independent evidence on whether copying works

| Source | Setup | Result |
|---|---|---|
| maximusrafla/polymarket-sharps (2026-07/09, 4.7M trades, market-block bootstraps, FDR control) | Skill = outcome minus E[outcome given entry price]. Copy measured against same-market placebo entry times | 26 of 23,956 wallets certified (23 trade 5-min crypto). Thin samples made most of the apparent edge. Long-ledger cohort: copying at 30 s lost to a random-time entry by -1.3c [-2.1, -0.4]. Real-world certified cohort at a 2-min lag: pooled +2.0c/share [+1.2, +2.8] after fees, held-out +2.4c, only ~0.8c of it tied to the wallet's exact moment. **Picking which wallets was noise** (cluster-preserving permutation p=0.23). Forward test: vetted sports cohort -0.4c [-2.2, +1.6], unvetted pool +1.6c [+0.5, +2.6], all in-play. The pooled copy test was never read forward. README verdict: "Copying does not work." |
| BallesJr/polymarket-copy-trader (paper, 2026-07-03 to 11-02) | 2 screened leaders, $500 conviction, $100 flat, book-walk fills, mirror exits, in-play skip, ~6 min delay | README snapshot (Aug) showed +$171. Full state file: **612 trades, -$1,034, ROI -1.7% [-10.3%, +7.3%]**. After the in-play filter: +0.8% [-9.6%, +11.6%], n=436 |
| evan-kolberg/prediction-market-backtesting (account-ledger replay) | A public wallet's own 153 trades replayed as IOC limit orders at its own price against historical L2, zero delay | **Only 72 of 153 filled.** The original orders already consumed that liquidity |
| darrnhard/polymarket-smart-money (3 top sports wallets, 6.8M events) | Behavior study | All three are bots, buys in bursts of 5-7 fills over 60-90 s, 82-87% of markets entered in several sessions. Two wallets overlap in 78.5% of markets: a twin or a shared signal |

What this means for v2:
* The faint edge lives in **pools of wallets and in-play real-world markets**,
  not in named wallets. That favours Phase 3b consensus and broad watchlists
  over a few hand-picked whales, and it means per-wallet rankings should not
  drive sizing.
* Every paper strategy needs a **placebo arm**: same market and side, entered at
  a matched or random time, with the same costs. Then timing can be told apart
  from market choice.
* Book-walk fills must allow "not filled". Never assume the whale's price.

### Book-walk fills and fees

* agent-next/polymarket-paper-trader (MIT, 503 stars) walks real asks level by
  level, charges the fee per filled level, and supports FOK/FAK and a limit
  price. Our `fees.py` already matches the official formula
  (`fee = C x feeRate x p x (1 - p)` from `feeSchedule`, makers pay none,
  https://docs.polymarket.com/trading/fees.md).
* New since our fee check: a tiered **Taker Rebate Program** and Combos at 0.05.
  Paper trading should keep charging the full fee (conservative).
* Historical order books for the 6-month backtest: **PMXT** hourly L2 archive
  (`r2v2.pmxt.dev`, `r2.pmxt.dev`, files `polymarket_orderbook_YYYY-MM-DDTHH.parquet`,
  from 2026-02-21, book snapshots plus price changes), and **pmdata.dev**
  (`/v1/polymarket/{l2,trades}/{date}/{slug}.parquet`, free key). Both are
  blocked in the build sandbox, so coverage is unchecked. The server can reach
  them.

### Other data sources seen

* Goldsky: a Kafka topic `polymarket.pnl.user_positions` (per wallet and token:
  avg price, realized PnL, total bought), which they say backs Polymarket's
  leaderboards, plus a `matic.raw_logs` webhook pipeline already on the V2
  exchange addresses. Needs a Goldsky account (goldsky-io/copy-trader, no license).
* ForesightFlow/datasets (CC-BY 4.0): 8 documented insider cases mapped to 24
  markets (`ffic-inventory`). That's a small labelled test set for insider
  features.
* Read and skipped: wazzenrj-debug/polymarket-sports-consensus and
  Jeffrobro/consensus-polymarket. Both are top-N leaderboard position overlap
  dashboards with no validation, nothing beyond lukkr1.
