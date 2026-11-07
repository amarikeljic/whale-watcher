"""Parsing the real response shapes (captured from the live APIs on 2026-11-07)."""

from whalewatcher.polymarket import Activity, Book, MarketInfo, assign_fill_keys, category_from_tags, parse_resolution
from whalewatcher.timeutil import parse_time

LIVE_ACTIVITY = {
    "proxy_wallet": "0x5a218c7ad04135830a45c41aaed7294df7809318", "timestamp": 1777593644,
    "condition_id": "0x957DDC82dc11959c5e6940b0239aecc050230c5b6ce8673bff21c14fe74bcb6e", "type": "TRADE",
    "size": 11.0, "usdc_size": 0.01057, "transaction_hash": "0x85d3", "price": 0.001,
    "token_id": "53108492762804322790881187287769990850666506477078895895702280937595932013486", "side": "SELL",
    "outcome_index": 0, "title": "Will Eric Swalwell win the California Governor Election in 2026?",
    "slug": "will-eric-swalwell", "event_slug": "ca-gov", "outcome": "Yes",
}

LIVE_GAMMA = {
    "question": "Will the fight end in a decision or draw?",
    "conditionId": "0xa4c2ce44c4fa91df06941d3ed06fc9e146809fbef3cad07cd9ce605ab03bcd1d",
    "slug": "ufc-naz-jnasci-2026-11-14-decision", "closed": True, "closedTime": "2026-11-06 03:30:03.883336+00",
    "umaResolutionStatus": "resolved", "outcomePrices": '["0.5", "0.5"]', "outcomes": '["Yes", "No"]',
    "clobTokenIds": '["111", "222"]', "endDate": "2026-11-15T04:59:59.999Z", "gameStartTime": "2026-11-14 22:00:00+00",
    "sportsMarketType": "ufc_go_the_distance", "feesEnabled": True,
    "feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.15},
    "events": [{"slug": "ufc-naz-jnasci-2026-11-14", "title": "UFC 334: Sadykhov vs. Nascimento"}],
    "tags": [{"label": "Sports", "slug": "sports"}, {"label": "UFC", "slug": "ufc"}],
}


def test_activity_row():
    a = Activity.from_api(LIVE_ACTIVITY)
    assert a.wallet == "0x5a218c7ad04135830a45c41aaed7294df7809318"
    assert a.condition_id == LIVE_ACTIVITY["condition_id"].lower()
    assert (a.side, a.size, a.usdc_size, a.outcome_index) == ("SELL", 11.0, 0.01057, 0)


def test_unlabelled_outcome_index_999_is_none():
    a = Activity.from_api({**LIVE_ACTIVITY, "type": "SPLIT", "outcome_index": 999, "token_id": "", "side": ""})
    assert a.outcome_index is None and a.type == "SPLIT"


def test_identical_fills_in_one_transaction_get_distinct_keys():
    rows = [Activity.from_api(LIVE_ACTIVITY), Activity.from_api(LIVE_ACTIVITY)]
    assign_fill_keys(rows)
    assert rows[0].fill_key != rows[1].fill_key
    again = assign_fill_keys([Activity.from_api(LIVE_ACTIVITY), Activity.from_api(LIVE_ACTIVITY)])
    assert [r.fill_key for r in again] == [r.fill_key for r in rows], "stable across fetches"


def test_gamma_market():
    m = MarketInfo.from_gamma(LIVE_GAMMA)
    assert m.category == "sports" and m.is_sports
    assert m.token_ids == ["111", "222"] and m.outcome_index_of("222") == 1
    assert m.closed_time == parse_time("2026-11-06T03:30:03Z")
    assert m.game_start_time == parse_time("2026-11-14T22:00:00Z")
    assert m.close_reference_ts() == m.game_start_time, "sports CLV closes at the start"
    assert (m.fees_enabled, m.fee_rate) == (True, 0.05)
    assert m.url == "https://polymarket.com/event/ufc-naz-jnasci-2026-11-14/ufc-naz-jnasci-2026-11-14-decision"
    assert m.gamma_final_payouts(LIVE_GAMMA) == [0.5, 0.5], "a cancelled fight pays 0.50, not a refund"


def test_fee_rate_is_zero_when_fees_are_disabled():
    m = MarketInfo.from_gamma({**LIVE_GAMMA, "feesEnabled": False})
    assert m.fee_rate == 0.0


def test_category_from_tags():
    assert category_from_tags(["world", "brazil", "politics", "elections"]) == "politics"
    assert category_from_tags(["crypto", "bitcoin"]) == "crypto"
    assert category_from_tags(["something"]) == "other"


def test_book_is_resorted_best_first():
    # Live /book order: bids ascending, asks descending (best LAST).
    raw = {"bids": [{"price": "0.01", "size": "10"}, {"price": "0.33", "size": "28"}],
           "asks": [{"price": "0.99", "size": "13"}, {"price": "0.35", "size": "952"}, {"price": "0.34", "size": "952"}],
           "timestamp": "1794015435330"}
    book = Book.from_api(raw, "t")
    assert book.best_bid == 0.33 and book.best_ask == 0.34
    assert [lv.price for lv in book.asks] == [0.34, 0.35, 0.99]
    assert book.ts == 1794015435


def test_resolution_rows():
    v2 = {"condition_id": "0x5e", "status": "resolved", "resolved_at": "2026-11-06T03:29:49Z",
          "payouts": [500000, 500000], "last_update_timestamp": "2026-11-06T03:29:49Z"}
    assert parse_resolution(v2) == (parse_time("2026-11-06T03:29:49Z"), [0.5, 0.5])
    uma = {"condition_id": "0x84", "status": "resolved", "price": "1000000000000000000",
           "last_update_timestamp": "1793005556"}
    assert parse_resolution(uma) == (1793005556, [1.0, 0.0])
    assert parse_resolution({"status": "posed", "price": "69", "last_update_timestamp": "1"}) is None
