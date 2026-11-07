from whalewatcher.timeutil import add_months, iso, months_in_window, parse_as_of, parse_time


def test_parses_every_shape_polymarket_uses():
    expected = parse_time("2026-11-06T03:30:03Z")
    assert parse_time("2026-11-06 03:30:03.883336+00") == expected           # closedTime
    assert parse_time("2026-11-06 03:30:03.883336+00:00:00") == expected      # umaEndDate
    assert parse_time("2026-11-06T03:30:03.999Z") == expected
    assert parse_time("1793935803") == expected                               # epoch string
    assert parse_time(1793935803000) == expected                              # milliseconds
    assert parse_time("2026-09-29 23:00:00+00") == parse_time("2026-09-29T23:00:00Z")  # gameStartTime
    assert parse_time("2026-12-10") == parse_time("2026-12-10T00:00:00Z")
    assert parse_time("") is None and parse_time(None) is None and parse_time("garbage") is None


def test_as_of_dates_are_utc_midnight():
    assert iso(parse_as_of("2026-11-01")) == "2026-11-01 00:00:00Z"


def test_add_months_clamps_the_day():
    assert iso(add_months(parse_time("2026-03-31T00:00:00Z"), -1)) == "2026-02-28 00:00:00Z"
    assert iso(add_months(parse_time("2026-11-01T00:00:00Z"), -6)) == "2026-05-01 00:00:00Z"
    assert iso(add_months(parse_time("2026-01-15T00:00:00Z"), -2)) == "2025-11-15 00:00:00Z"


def test_months_in_window():
    start, end = parse_time("2026-05-01T00:00:00Z"), parse_time("2026-11-01T00:00:00Z")
    assert months_in_window(start, end) == ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    mid = parse_time("2026-05-07T00:00:00Z"), parse_time("2026-11-07T00:00:00Z")
    assert len(months_in_window(*mid)) == 7
