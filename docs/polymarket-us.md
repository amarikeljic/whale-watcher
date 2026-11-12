# Polymarket US: what the public data exposes

Checked 2026-11-12 against the official docs and the live public gateway, with
read-only GET requests and no account or key. Nothing here is built yet. This is
the research report v2.1 Part 3b asks for before source B is designed.

Sources: <https://docs.polymarket.us/llms.txt> (page index),
<https://docs.polymarket.us/api-reference/introduction.md>,
<https://docs.polymarket.us/fees.md>,
<https://docs.polymarket.us/api-reference/rate-limits.md>,
<https://docs.polymarket.us/api-reference/websocket/markets.md>, and the official
SDK `Polymarket/polymarket-us-python` (MIT, read, not installed).

## Two hosts, and only one is allowed here

| Host | Needs | What it is |
| --- | --- | --- |
| `https://gateway.polymarket.us` | nothing | Public market data. The only US host this project may call. |
| `https://api.polymarket.us` | account, identity check, Ed25519 API key | Orders, portfolio, balances, and both WebSockets. Never called: the key that reads the trade stream is the same key that places orders. |

## Whale tracking on Polymarket US is not possible

No endpoint, public or authenticated, names who traded. There is no leaderboard,
no per-account activity, no holders list. The trade stream
(`SUBSCRIPTION_TYPE_TRADE` on `wss://api.polymarket.us/v1/ws/markets`) carries
market slug, price, quantity, time and each side's `side` and `intent`. It has no
account identifier, and it needs an API key.

The gateway returns 404 for every trade route tried (`/v1/trades`,
`/v1/markets/{slug}/trades`).

## What the public gateway does give

| Route | Verified | Notes |
| --- | --- | --- |
| `GET /v1/markets` | yes | Filters `active`, `closed`, `slug` (repeatable), `limit`, `offset`. More than 60,500 open markets. |
| `GET /v1/market/slug/{slug}`, `/v1/market/id/{id}` | yes | Works for resolved markets too. |
| `GET /v1/events`, `/v1/events/slug/{slug}` | yes | Event slug has no prefix (`nfl-jax-bal-2026-11-05`). Its markets do (`aec-nfl-jax-bal-2026-11-05`). |
| `GET /v1/markets/{slug}/book` | yes | Full depth, `bids` and `offers`, each `{px: {value}, qty}`. Also `state` and `stats`. |
| `GET /v1/markets/{slug}/bbo` | yes | Best bid and ask, depth counts, last trade price, shares traded, open interest. |
| `GET /v1/price-history` | yes | `symbol`, `fixedInterval` (`INTERVAL_ALL` ...), `fidelity` in minutes (required). One-minute display prices, not trades. |
| `GET /v1/markets/{slug}/settlement` | yes | `{"slug", "settlement": 1}` once resolved, 404 before. |
| `GET /v1/search`, `/v1/series`, `/v1/sports`, `/v1/tags` | yes | Reference data. |

Fields on a market that matter to us: `slug`, `category`, `marketType`,
`gameStartTime`, `endDate`, `status`, `outcomes`, `outcomePrices`,
`orderPriceMinTickSize`, `minimumTradeQty`, `feeCoefficient`, `marketSides`
(team, provider ids), and on an event `sportradarGameId`.

`stats` on the book carries `sharesTraded`, `notionalTraded`, `lastTradePx`,
`lastTradeQty`, `openInterest`, each with the time it was last set. That is the
closest thing to a trade feed without a key.

## Limits and freshness

* 25 requests a second for the whole retail API, counted per source IP.
* Book, BBO and price history are served `cache-control: public, max-age=30`.
  A plain request was 29 s old when checked. A request with a unique query
  parameter was served fresh. Whether to send one is a terms-of-use decision for
  the owner, not a technical one.
* With 60,500 markets and 25 requests a second, only a subset can be watched.

## Costs, measured against international

Taker fee: `fee = Θ × contracts × p × (1 − p)`, the same shape as international.
Rounded to the cent with banker's rounding, per fill.

| Trailing 30-day taker volume | Θ |
| --- | --- |
| below $10,000 | 0.095 |
| $10,000 to $50,000 | 0.085 |
| $50,000 to $250,000 | 0.075 |
| $250,000 or more | 0.0695 |

A new account pays 0.0695 for its first seven days. Every one of the 60,500 open
markets reports `feeCoefficient: 0.095`. There is no category table: sports,
crypto and politics all pay the same.

As a share of the stake the fee is `Θ × (1 − p)`: 4.75% at 50 cents and 1.9% at
80 cents on entry, and the same again on an early exit. International sports
markets charge 0.03 or 0.05, so a US fill costs about two to three times as much
in fees. Combos use a different, higher curve.

| Setting | Value | Source |
| --- | --- | --- |
| Tick size | 0.01 on 54,520 markets, 0.005 on 4,793, 0.001 on 1,159, 0.0025 on 28 | `orderPriceMinTickSize` |
| Minimum order | 0.01 contracts on 60,294 markets, 1 contract on 206 | `minimumTradeQty` |
| Deposits | debit card, ACH, wire, PayPal, Venmo, Apple Pay. No fee is stated in the docs | deposit overview |

## Matching an international market to its US market

| Market type | Rule | Checked |
| --- | --- | --- |
| Two-way game winner | `aec-<league>-<a>-<b>-<date>`. The US date is the Eastern date of the game, international uses the UTC date, so try the date and the day before | 82 of 142 recent markets. Misses are team-code differences (esports, tennis, some NBA) |
| Crypto up/down | `btc-updown-5m-<unix start>` becomes `cpc-btc-updown-5m-YYYY-MM-DD-HHMMz` | 162 of 162 |
| State election winner | event `usse-<st>-2026-11-03` or `usgub-<st>-2026-11-03` | 3 of 3 states sampled |
| Spreads, totals, props, three-way soccer | US slugs exist (`asc-`, `tsc-`, `atc-`) but lines must be matched by number | not yet |

Two traps:

* **Crypto up/down is not the same bet.** The US market settles on the CME CF
  index `BRTI`, the international one on a Chainlink stream. On a close window
  the two can resolve differently.
* **`endDate` on a US sports market is the game plus about 14 days.** Use
  `gameStartTime`, never `endDate`, for a days-to-close filter.

On the sandbox copy of the database, 1,926 alerts in the last 14 days: 25% match
by the first two rules, about 65% if the election rule holds for every state.
The real figure has to come from the server's database.
