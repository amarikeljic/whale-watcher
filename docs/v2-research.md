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
