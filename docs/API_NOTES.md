# API notes (verified 2026-11-07)

Every endpoint, field and limit the code uses, with where it was checked. The
spec warned its own endpoint names might be stale; several were.

## Polymarket

Index: https://docs.polymarket.com/llms.txt

### Data API v2 (`https://data-api.polymarket.com`)

Spec: https://data-api.polymarket.com/v2/openapi.json
Migration guide: https://docs.polymarket.com/migrate/data-api-v1-to-v2.md

**Data API v1 was retired on 2026-11-02.** `GET /trades` now answers
`410 {"error":"Data API v1 has been retired"}`. Everything here uses v2.

| Use | Endpoint | Notes |
|---|---|---|
| Candidate pool | `GET /v2/leaderboard?time_period=day\|week\|month\|all&category=overall&sort_by=PNL` | Finite windows are marked-to-market equity change; `all` is realized-only. Boards describe *now*. |
| Candidate pool | `GET /v2/trades?filter_type=CASH&filter_amount=N` | Bare feed covers the current and previous month only. `condition=` (<=20 ids) serves a 3-year window. `taker_only` defaults to true. |
| Wallet history | `GET /v2/activity?user=..&type=TRADE,SPLIT,MERGE,REDEEM,CONVERSION&start&end&sort_direction` | `start`/`end` inclusive epoch seconds; omitted `start` floors to 3 years back. `limit` <= 1000. |
| Resolutions | `GET /v2/resolutions?condition=..` (<=20 ids) | Condition rows: `resolved_at` (RFC3339), `payouts` (micro-USDC per share). UMA rows: `last_update_timestamp` (epoch string), `price` (1e18 = first outcome; `"69"` = unset). |
| CLV prices | `GET /v2/prices-history?token_id=..&as_of=..` | Latest observation at or before `as_of`. 1-minute data kept >= 7 days, 5-minute >= 60, 30-minute >= 90; 3h/12h permanent. Each point has `resolution_seconds`. |
| ADD vs NEW | `GET /v2/positions?user=..&condition=..` | `current_size` is shares held now. |
| Freshness | `GET /v2/status` | `serving.lag_seconds`; used to flag delayed data in alerts. |

Gotchas found by testing, not in the docs:

* **Cursors do not carry filters.** Page 2 of `/v2/activity?type=TRADE` with
  only `cursor` returns every activity type. Every page re-sends all params.
* **Python urllib's default User-Agent gets 403** from Cloudflare; aiohttp's
  default works. The client always sends `whale-watcher/0.1 (read-only research)`.
* SPLIT/MERGE/CONVERSION rows have an empty `token_id` and `outcome_index` 999.
  CONVERSION rows cannot be attributed to tokens, so those markets are excluded.
* Since 2026-08-10 REDEEM rows are per outcome (losing side has `usdc_size` 0).
* No per-fill id exists; fills are keyed by a hash of (tx, type, token, side,
  size, price, ts) plus an occurrence counter.

Rate limits: https://docs.polymarket.com/api-reference/rate-limits.md
v2 general 800 req/10s; `/v2/trades` 300/10s; `/v2/activity`, `/v2/positions`,
`/v2/prices-history` 200/10s; `/v2/status` 100/10s. Cloudflare throttles
(delays) rather than rejects. The client allows itself 12 req/s on this host.

### Gamma (`https://gamma-api.polymarket.com`)

Spec: https://docs.polymarket.com/api-spec/gamma-openapi.yaml

`GET /markets?condition_ids=a&condition_ids=b&closed=any&include_tag=true`

* **Array filters must repeat the key.** `condition_ids=a,b` silently returns `[]`.
* `closed=any` is required to include closed markets in an id lookup (<= 100 ids).
* `category` is empty on current markets; category comes from tags
  (`include_tag=true`).
* Fields used: `conditionId question slug events[0].slug outcomes clobTokenIds`
  (JSON strings), `endDate closed closedTime gameStartTime sportsMarketType`,
  `negRisk acceptingOrders description resolutionSource umaResolutionStatus
  outcomePrices feesEnabled feeSchedule{rate,exponent,takerOnly,rebateRate}`.
* Time formats seen: `2026-11-08T04:59:59.999Z`, `2026-09-29 23:00:00+00`,
  `2026-11-06 03:30:03.883336+00`, `...+00:00:00`.

Limits: general 4,000/10s, `/markets` 300/10s. Responses carry
`Cache-Control: max-age=300` and no ETag.

### CLOB (`https://clob.polymarket.com`), public market data only

Spec: https://docs.polymarket.com/api-spec/clob-openapi.yaml

`GET /book?token_id=..` returns `bids`, `asks` as `{price, size}` strings.
**The live API sorts bids ascending and asks descending (best price last).**
The OpenAPI schema text says the opposite; the guide
(https://docs.polymarket.com/market-data/prices-order-books.md) matches the
live API. The code sorts explicitly. 404 = no book (closed/resolved).
Limit: `/book` 1,500/10s.

`GET /prices-history?market=<token id>&startTs=..&endTs=..&fidelity=1` returns
`{"history": [{"t": unix seconds, "p": price}]}` at the requested fidelity in
minutes. Used only by `backtest` for the price a delayed copy would have paid.
Minute points were still served for markets five months old (checked
2026-11-07), where the Data API's `/v2/prices-history` had thinned out.
Limit: `/prices-history` 1,000/10s.

### Fees

https://docs.polymarket.com/trading/fees.md and
https://docs.polymarket.com/market-data/market-details.md#trading-fees

* `fee = C x feeRate x p x (1 - p)`, takers only, USDC, rounded to 5 dp,
  anything below 0.00001 rounds to 0.
* Use the market's own `feeSchedule.rate` (changelog 2026-03-31). The category
  table is not reliable per market: a live UFC market had rate 0.03 while the
  table says sports pay 0.05; another had 0.05; a Brazil politics market had 0.
* Fees fund maker rebates; makers pay nothing. Activity rows do not say whether
  a fill was maker or taker.

### Resolution

https://docs.polymarket.com/concepts/resolution.md

Sports markets that are drawn, ruled no contest, cancelled or postponed past a
deadline resolve **"50-50"**: each share pays $0.50 (`payouts [500000, 500000]`).
That is not a refund.

## Discord webhooks

discord.com was not reachable from the build sandbox; these follow Discord's
published webhook API (Execute Webhook with `?wait=true` to get the message id;
Edit Webhook Message via `PATCH {url}/messages/{id}`; 429s carry `retry_after`
in the body). Verify on first deployment.

## Kalshi (deferred)

Kalshi matching and paper trading are deferred on request. What was verified,
for when it comes back (https://docs.kalshi.com/llms.txt, OpenAPI
https://docs.kalshi.com/openapi.yaml, Trade API v3.36.0):

* Base URL: `https://external-api.kalshi.com/trade-api/v2` (recommended) or
  `https://api.elections.kalshi.com/trade-api/v2`. Market data needs no auth.
* `GET /events?series_ticker=KXUFCFIGHT&with_nested_markets=true`,
  `GET /markets/{ticker}`, `GET /markets/{ticker}/orderbook`.
* Order book: `orderbook_fp.yes_dollars` / `no_dollars`, `[price, count]`
  strings, ascending, **bids only**: a YES ask is `1 - best NO bid`.
* Settlement: `result` in `yes|no|scalar|""`, `settlement_value_dollars`;
  finalized markets have status `finalized`. UFC draws/no contests settle at
  $0.50; cancelled fights at "a fair price" (not a refund).
* Fees: series `fee_type` (`quadratic`, `quadratic_with_maker_fees`, ...) and
  `fee_multiplier` (1 on most series, 0.5 on some). The base coefficient (0.07)
  is in https://kalshi.com/docs/kalshi-fee-schedule.pdf, which sits behind a bot
  check and could not be read; fee rounding is documented at
  https://docs.kalshi.com/getting_started/fee_rounding.md.
* Live UFC edge cases: Kalshi dated a card Nov 6 that Polymarket dated Nov 7;
  "Jose Delano" vs "Jose Mauro Delano"; "Jose Henrique" vs "Jose Henrique
  Souza"; "Lorenço" vs "Lorenco"; fighter order swapped between venues.
