# Data sources, limits and terms

Written 2026-11-14 for v2.1 Part 4 #17: each source, its published limit, our rate,
and the terms that bear on how this project uses it, quoted. **This is a reading of the
terms, not legal advice.** Where the wording leaves a question open, it says so.

## What the service calls

Every call is an unauthenticated GET for public market data. No account, key, wallet
or order on any venue. The client keeps a token bucket per host and per endpoint at
about 80% of each published limit or less (`config.py`, `api.rate_limits`).

| Host | Used for | Published limit | Our ceiling | Typical use |
| --- | --- | --- | --- | --- |
| data-api.polymarket.com | trades, activity, positions, prices, user stats | 800 / 10 s overall | 600 / 10 s | well under |
| same, `/v2/trades` | the global trade feed | 300 / 10 s | 240 / 10 s | 10 / 10 s (one poll a second) |
| same, `/v2/activity`, `/v2/positions`, `/v2/prices-history` | wallet history, prices | 200 / 10 s each | 160 / 10 s each | bursts during the nightly review |
| gamma-api.polymarket.com | market details | 300 / 10 s for `/markets` | 240 / 10 s | a few a minute |
| clob.polymarket.com | order books | 1,500 / 10 s for `/book` | 400 / 10 s | two to four a signal, marks every 5 min |
| gateway.polymarket.us | row A: the US book, markets, settlement | 25 / s per IP | 5 / s | two or three a signal |
| api.elections.kalshi.com | backtests only | token based | 3 / s | only when `backtest --kalshi` runs |

Limits: <https://docs.polymarket.com/api-reference/rate-limits.md>,
<https://docs.polymarket.us/api-reference/rate-limits.md>,
<https://docs.kalshi.com/getting_started/rate_limits.md>.

One more host is used once, by hand: `scripts/check_block_times.py` asks a public
Polygon RPC (default `https://polygon.drpc.org`, listed at
<https://docs.polygon.technology/pos/reference/rpc-endpoints.md>) for transaction
receipts and block times, at 2 requests a second. On 2026-11-14 it showed the Data
API's trade time equal to the block time on 200 of 200 fills.

## Polymarket (polymarket.com)

Terms of Use, <https://polymarket.com/tos>, "Effective as of August 11, 2026", read
2026-11-14. Operator: Adventure One QSS, Inc.

What the terms cover: "(i) all content, informational functionality, and information
features (the “Content Features”) available on Polymarket.com (the “Site”) and any other
site to which the Terms are posted (each, as applicable, an “Interface”) and (ii)
software, including but not limited to the blockchain-based, smart contract protocol
... (the “Technology Features” ...)". Accepting them includes "connecting via an API".

The four passages that bear on this project:

1. **Automated collection.** Prohibited conduct includes: "Use any data mining tools,
   robots, crawlers, or similar data gathering and extraction tools to scrape or
   otherwise remove data from the Site, any other Interface, or Features".
2. **Who may not use the data.** "Access or use any data, content, or information
   contained on our Site, any Interface or Features directly or through an API ... (the
   “Data”) if you are (i) a non-retail, professional entity that engages in capital
   markets activities ... or (ii) a market data distributor, in each case unless
   otherwise agreed to in writing by us", and "Sell, resell, sublicense, redistribute, or
   otherwise commercially exploit the Data to any Capital Market Client or market data
   distributor".
3. **Restricted jurisdictions.** "If you are in a Restricted Jurisdiction (as defined
   below), you are only permitted to use the Content Features on the Site or any other
   Interface and may not use the Site or any other Interface for any other purpose and
   you may not access the Technology Features, including and in particular the
   Platform." The list is <https://docs.polymarket.com/api-reference/geoblock.md>. The
   United States (US) and its Minor Outlying Islands (UM) are under "Regulatory-Restricted
   Jurisdictions (Close-Only on Frontend and API)".
4. **Limits.** "The use of the Site or the Features may be subject to limitations,
   including transaction limits or rate limits ... The Company reserves the right ... to
   change any applicable Limits from time to time, with or without notice to you."

## Polymarket US (polymarket.us)

"Polymarket App — Terms & Conditions (ISV Front End)", "Effective Date: October 23,
2026", served by <https://polymarket.us/tos> from a published Google Doc, read
2026-11-14. Operator of the app: QC Tech Limited Liability Company; the exchange is QCX
LLC d/b/a Polymarket US. "App" is defined as "the Polymarket App, including related
websites, portals, and APIs".

The passages that bear on this project:

1. **Automation.** Under acceptable use, you agree not to "(c) use bots or automation
   except through authorized APIs".
2. **Market data.** "Market data, reference data, analytics, and documentation presented
   in the App are provided to you for personal, non-commercial use in connection with
   your trading activities through Polymarket US or Polymarket Clearing ... your rights
   to use such data, analytics or documentation can be revoked at any time.
   Redistribution, resale, scraping, bulk downloads, and commercialization of derived
   works are prohibited unless you have received an express license from the applicable
   rightsholder."
3. **Location controls.** You agree not to "(b) interfere with or bypass
   security/surveillance features, including geolocation controls (e.g. through a VPN)".

## What this means for the project

Plainly, with the open questions marked:

* **No trading anywhere, ever.** That is the project's own rule, and it keeps it clear of
  every passage about the Platform, orders and accounts.
* **Personal, non-commercial, not redistributed.** Nothing is sold or published. The
  dashboard is reachable only on your tailnet. Keep it that way: both sets of terms
  forbid redistributing or commercialising the data.
* **Open question 1, automated reading of polymarket.com data.** Passage 1 bans robots
  and scraping. Polymarket also publishes these APIs, with documentation and rate limits,
  for programmatic access to market data, and the project uses only those, far under
  their limits. The terms do not say whether using the documented public API is
  "scraping". I cannot settle that.
* **Open question 2, if you are in the United States.** The US is a Restricted
  Jurisdiction for polymarket.com. There you may use only the Content Features
  (information) and not the Platform. The project only reads information and never
  trades, which fits the first half. Whether an automated reader counts as using the
  Content Features is the same open question as above.
* **Open question 3, Polymarket US.** Its market data is for "personal, non-commercial
  use in connection with your trading activities" there. Paper-trading research is
  personal and non-commercial, but you do not trade there yet. Its automation clause
  allows "authorized APIs", and the project uses only the public gateway its docs
  describe.
* **The cache-busting parameter.** The detector adds a unique parameter (`_t`) to the
  trade-feed URL once a second so the CDN does not answer with a copy up to 300 s old.
  Row A does the same for the US book (30 s cache, `polymarket_us.bypass_cache`).
  Neither set of terms mentions caching. Every request still counts against, and stays
  under, the published rate limits. Turning the US one off costs row A up to 30 s of
  freshness. The trade-feed one has no switch: without it, detection would be minutes
  late and the bot model would not measure what it is meant to.

If any of this matters to you beyond research, the direct route is to ask each
company's support. Not read here: Kalshi's terms (Kalshi data is used only by the
optional backtest), and the venues' privacy policies.
